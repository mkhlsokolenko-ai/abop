"""ABOP Web API — тонкий REST/SSE-слой поверх ядра `ape` для веб-среды.

Принцип PRD v1.1 «единое ядро, много клиентов»: CLI и веб дёргают ОДНИ функции ядра.
Этот модуль НЕ дублирует логику — только выставляет функции `ape` (семьи/навыки/Data Plane/
план/прогон) как HTTP-эндпоинты за Keycloak JWT (тот же realm, что MCP-шлюз).

Запуск (dev): uvicorn server.web_api:app --reload --port 8091
Прод: за Caddy (TLS), рядом с FastMCP-шлюзом. Контракт — docs/ABOP_API.md.

Статус эндпоинтов: заглушек не осталось. READ, RUN, HITL и STREAM реализованы — прогон идёт
заданием очереди (202 + job_id), подтверждения человека возвращают задание с чекпоинта, живой ход
прогона отдаётся по SSE `/api/runs/{id}/stream`. Шапка держала обратное до 08.10, хотя RUN и HITL
работали давно: описание пережило реализацию на три недели.
"""
from __future__ import annotations

import asyncio as _asyncio
import os
import sys
import time
from pathlib import Path

import re as _re
from fastapi import Query, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# ── ядро: импорт функций `ape` без запуска REPL (верхний уровень чист) ──
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
import ape  # noqa: E402

from . import access, admin_store, agent_store, arbiter, assembly, audit_store, blackboard, planner, cachebus, charts, clients, compute, contract_store, cost_estimate, dataplane_store, delivery as delivery_mod, dlq_store, families_store, lexicon, nlu, finding_store, findings, hitl_store, identity_store, ingress, langfuse_trace, layout_store, observability as obs, bpmn_import, dataplane_records, mcp_server, pipeline_store, process_deploy, process_store, rbac_map, reglament_store, chat_store, orchestrator, report_compose, report_store, choice_store, doclink, rules_store, run_cache_store, run_parts_store, run_store, runner, safety, pipeline_graph, schema_store, skill_contract, skill_store, skill_templates, skill_tools, slava, systems_store, trigger_store, triggers, run_bus, run_queue, userdata_store  # noqa: E402
from .config import settings  # noqa: E402
from .vectors import cosine as _cosine  # noqa: E402
from .nlu import nlu_config  # noqa: E402  (пороги подбора переехали к разбору фразы)

# Контекст шаблона отчёта уехал в server/report_context.py: его зовут и карточка прогона, и
# предпросмотр шаблона. Имена оставлены в этом пространстве имён — на них ссылаются ручки отчётов.
# Исполнение прогона уехало в server/run_exec.py: это не домен маршрутов, а то, что домены зовут
# (прогоны, агенты, триггеры, цепочки, повтор из DLQ, пробный шаг). Имена оставлены в этом
# пространстве имён — на web_api.execute_agent_run ссылаются и очередь, и тесты.
from .run_exec import (  # noqa: E402,F401  (часть имён — реэкспорт для очереди и тестов)
    _build_report_html, _data_fingerprint, _deliver_out_nodes, _deliver_templates,
    _gate_agent_data, _html_to_text, _input_received, _INPUT_KEEP, _out_nodes,
    _refresh_source_data, _run_cache_key, _send_channel, _structured_html, _template_for_run,
    execute_agent_run, run_summary)

from .report_context import (  # noqa: E402
    _audit_cards_html, _labels_ctx, _report_context, _report_footer_html, _requisites_html,
    _service_html, _sources_html)



# Аутентификация, уровни доступа и ABAC живут в server/deps.py — их переиспользуют и маршруты,
# вынесенные в отдельные модули, и MCP-сервер. Имена оставлены в этом пространстве имён: на
# `web_api.user` ссылаются и тесты, и mcp_server, и ломать это ради чистоты импорта незачем.
import hashlib as _hashlib  # noqa: E402

from .deps import (  # noqa: E402
    _client, _extra_issuers, _identity, _jwks_url, _jwt_cache_get, _jwt_cache_put,
    _LEVELS, _role_level, _sub, _uid_of, can_see_family, require_level, user)

app = FastAPI(title="ABOP Web API", version="0.1.0",
              description="Тонкий REST/SSE поверх ядра ape (среда разработки агентов)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1024)   # UX-аудит: 1 МБ index.html, 14 с загрузки снаружи


def _route_label(request) -> str:
    """Лейбл роута для метрик: ШАБЛОН пути, а не сам путь.

    Сырой `request.url.path` даёт отдельную серию на каждый идентификатор — `/api/runs/run-x-ab12`,
    `/api/runs/run-x-cd34` и так далее. Счётчики живут в обычном словаре без ограничения размера
    (`observability.py`), поэтому это не просто мусор в Prometheus, а медленная утечка памяти.
    Starlette кладёт сопоставленный роут в scope — берём его шаблон; для несопоставленных
    (404, статика) хватает одной общей метки.
    """
    маршрут = request.scope.get("route")
    шаблон = getattr(маршрут, "path", None)
    if шаблон:
        return str(шаблон)
    путь = request.url.path
    return путь if путь.count("/") <= 2 else "/".join(путь.split("/")[:3]) + "/*"


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """index.html фронта не кэшировать в браузере (иначе деплой не виден без hard-refresh).
    Браузер ревалидирует по etag/last-modified. Шрифты/бинарники StaticFiles кэшируются как есть."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.middleware("http")
async def _observability(request, call_next):
    """Observability spine (Фаза 0): сквозной trace_id (из заголовка X-Trace-Id или новый),
    структурный лог запроса, метрики латентности/статусов. Пробрасывается в прогон и доставку."""
    tid = request.headers.get("x-trace-id") or obs.new_trace_id()
    token = obs.trace_id_var.set(tid)
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
        dt = time.perf_counter() - t0
        path = request.url.path
        if path.startswith("/api/"):
            метка = _route_label(request)
            obs.inc("abop_http_requests_total", route=метка, status=resp.status_code)
            obs.observe("abop_http_request_seconds", dt, route=метка)
            obs.log_event("info", "http.request", route=path, method=request.method,
                          status=resp.status_code, ms=round(dt * 1000, 1))
        resp.headers["X-Trace-Id"] = tid
        return resp
    except Exception as ex:  # noqa: BLE001 — фиксируем ошибку в метрики/лог и пробрасываем
        obs.inc("abop_http_requests_total", route=_route_label(request), status="500")
        obs.log_event("error", "http.error", route=request.url.path,
                      error=f"{type(ex).__name__}: {ex}")
        raise
    finally:
        obs.trace_id_var.reset(token)


# ═══════════════ READ: реальные вызовы ядра ═══════════════

# Руководства поставки отдаём статикой: документ, лежащий только в репозитории, до пользователя не
# доходит. Каталога может не быть (например, в урезанной сборке) — тогда просто не монтируем.
_GUIDE_DIR = Path(__file__).resolve().parents[1] / "docs" / "guide"
if _GUIDE_DIR.is_dir():
    app.mount("/guide", StaticFiles(directory=str(_GUIDE_DIR), html=True), name="guide")


@app.get("/api/guides")
def guides() -> dict:
    """Какие руководства доступны в этой установке. Пустой список — каталог не вошёл в сборку."""
    if not _GUIDE_DIR.is_dir():
        return {"guides": [], "note": "руководства не включены в сборку"}
    out = []
    for name, title, about in (
            ("RUKOVODSTVO_POLZOVATELYA", "Руководство пользователя",
             "Как поставить задачу и получить результат: десктоп, веб, карточки, подтверждения, бюджеты"),
            ("RUKOVODSTVO_ADMINISTRATORA", "Руководство администратора",
             "Установка, окружение, доступы, модель, источники данных, навыки, лимиты, диагностика")):
        html, pdf = _GUIDE_DIR / f"{name}.html", _GUIDE_DIR / f"{name}.pdf"
        if not html.exists():
            continue
        out.append({"id": name, "title": title, "about": about,
                    "html": f"/guide/{name}.html",
                    "pdf": (f"/guide/{name}.pdf" if pdf.exists() else ""),
                    "size_kb": round(html.stat().st_size / 1024)})
    return {"guides": out}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "families": len(ape.AGENT_FAMILIES), "skills": len(ape.SKILLS),
            "adapters": sorted(ape.SOURCE_ADAPTERS)}


_GPU_CACHE: dict = {}


async def _gpu_credit() -> dict | None:
    """Остаток на арендованном GPU (Vast), если задан VAST_API_KEY в окружении сервера. Кэш 10 мин,
    любая ошибка → None (блок в UI просто не показывается). Ключ наружу не отдаём."""
    key = os.getenv("VAST_API_KEY") or ""
    if not key:
        return None
    import time as _t
    if _GPU_CACHE.get("at") and _t.time() - _GPU_CACHE["at"] < 600:
        return _GPU_CACHE.get("data")
    data = None
    try:
        import httpx as _hx
        async with _hx.AsyncClient(timeout=8) as cli:
            r = await cli.get("https://console.vast.ai/api/v0/users/current/", headers={"Authorization": "Bearer " + key})
            if r.status_code == 200:
                u = r.json() or {}
                data = {"credit": round(float(u.get("credit") or 0), 2), "provider": "vast"}
        if data is not None:
            async with _hx.AsyncClient(timeout=8) as cli:
                r = await cli.get("https://console.vast.ai/api/v0/instances/", headers={"Authorization": "Bearer " + key})
                if r.status_code == 200:
                    ins = [i for i in ((r.json() or {}).get("instances") or []) if str(i.get("label") or "").startswith("ape-") or str(i.get("label") or "").startswith("abop-")]
                    dph = round(sum(float(i.get("dph_total") or 0) for i in ins if (i.get("actual_status") == "running")), 3)
                    data.update({"instances": [{"id": i.get("id"), "label": i.get("label"), "status": i.get("actual_status"), "dph": round(float(i.get("dph_total") or 0), 3)} for i in ins],
                                 "dph_running": dph, "hours_left": (round(data["credit"] / dph, 1) if dph else None)})
    except Exception:  # noqa: BLE001 — внешний сервис не должен ломать статус
        data = None
    _GPU_CACHE.update({"at": _t.time(), "data": data})
    return data


@app.get("/api/llm/status")
async def llm_status(u: dict = Depends(user)) -> dict:
    """Состояние модели для баннера: активный профиль и модель, свой бокс (доступен/нет, override или .env),
    откаты каскада на облако (сколько и последний), тариф прогона, остаток на GPU (если ключ задан)."""
    act = clients.llm_active()
    casc = settings.cascade_for("standard")
    active = casc[0] if casc else None
    health = await clients.local_health()
    fb_total = obs.counter_total("abop_llm_fallback_total") if hasattr(obs, "counter_total") else 0.0
    from .pricing import cost_rub as _cost
    rub_1k = _cost(active or "", 1000, 1000) if active else None
    return {"active": active, "cascade": casc, "self_hosted": bool(active and str(active).startswith("local/")),
            "local": {**act, "health": health},
            "fallback": {"total": int(fb_total), "last": clients.last_fallback()},
            "cost_per_1k_rub": rub_1k, "free": rub_1k == 0,
            "gpu": await _gpu_credit()}


@app.get("/api/models")
def models() -> dict:
    """РЕАЛЬНЫЕ модели по профилям (из cascade в .env) — чтобы UI показывал фактическую модель узла,
    а не хардкод. `active` профиля = первая в каскаде (её и берёт прогон). local — self-host инстанс."""
    profiles = {}
    for p in ("standard", "research", "code"):
        casc = settings.cascade_for(p)
        profiles[p] = {"cascade": casc, "active": (casc[0] if casc else None)}
    act = clients.llm_active()   # override поверх env (1-клик из UI)
    return {"profiles": profiles,
            "default_profile": "standard",
            "local": {"model": act.get("model") or None,
                      "base_url": act.get("base_url") or None,
                      "override": act.get("override"),
                      "configured": bool(act.get("base_url"))}}


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    """Метрики в формате Prometheus (scrape). Открыт для внутреннего мониторинга."""
    st = clients.inflight_stats()  # загрузка admission-семафора LLM (multi-user)
    extra = (f"# TYPE abop_llm_inflight gauge\nabop_llm_inflight {st['busy']}\n"
             f"# TYPE abop_llm_inflight_max gauge\nabop_llm_inflight_max {st['max']}\n")
    return PlainTextResponse(obs.render_prometheus() + extra, media_type="text/plain; version=0.0.4")


@app.get("/api/observability")
def observability_snapshot(u: dict = Depends(user)) -> dict:
    """JSON-срез метрик для внутренних дашбордов ABOP (кто/что/сколько прогонов, латентность, стоимость)."""
    return obs.snapshot()
















# Карта процессов переехала в server/routes/process.py (п.23): семнадцать ручек, один стор, свои
# правила прав. Внешняя поверхность не изменилась — сторожит tests/test_routes_inventory.py.
from .routes import process as _routes_process  # noqa: E402

app.include_router(_routes_process.router)

# Админ-панель переехала в server/routes/admin.py: люди и роли из Keycloak, штат, аудит ИБ,
# настройки среды, лимиты прогона. Пороги подбора (`nlu_config`) остались здесь — это настройка
# ПОДБОРА, а не админки, и её зовут планировщик и NLU.
from .routes import admin as _routes_admin  # noqa: E402

app.include_router(_routes_admin.router)

# Ключи админ-конфига и описание уровней нужны и вне домена: первое — валидации в других ручках,
# второе — экранам. Ссылаемся ПО ИМЕНИ у роутера, а не копией: копия со временем разойдётся.
_ADMIN_CONFIG_KEYS = _routes_admin._ADMIN_CONFIG_KEYS
POLICY_УРОВНЕЙ = _routes_admin.POLICY_УРОВНЕЙ
_kc_configured = _routes_admin._kc_configured
run_limits_get = _routes_admin.run_limits_get

# Четыре домена, вынесенные тем же порядком: сквозной ID (кто я и мои аккаунты), реестр систем и
# манифест области, регламент с ключом НСИ, семьи ролей. Выбраны по связности, а не по размеру:
# в каждый не заглядывает никто, кроме него самого.
from .routes import families as _routes_families  # noqa: E402
from .routes import identity as _routes_identity  # noqa: E402
from .routes import reglament as _routes_reglament  # noqa: E402
from .routes import registry as _routes_registry  # noqa: E402

from .routes import templates as _routes_templates  # noqa: E402

from .routes import agents as _routes_agents  # noqa: E402
from .routes import runs as _routes_runs  # noqa: E402

for _r in (_routes_identity, _routes_registry, _routes_reglament, _routes_families,
           _routes_templates, _routes_agents, _routes_runs):
    app.include_router(_r.router)

# Проверка доступа нужна и прогонам (решить, можно ли агенту в систему), и подтверждениям.
# Ссылка ПО ИМЕНИ у роутера, а не копия: копия со временем разойдётся с оригиналом.
access_check = _routes_registry.access_check
# Домен агентов зовут и те части, что ещё не переехали: старт обновляет лексикон, очередь
# спрашивает слоты, подбор — роль и чтение данных навыком. Ссылки по имени, не копии.
_refresh_lexicon = _routes_agents._refresh_lexicon
_resolve_role = _routes_agents._resolve_role
_skills_reading_data = _routes_agents._skills_reading_data
_graph_titles = _routes_agents._graph_titles
_edit_scope = _routes_agents._edit_scope
agent_slots = _routes_agents.agent_slots
# Подбор и сборка из плана зовут те же ручки, что и человек: одно решение по задаче живёт в
# оркестраторе, а исполняют его эти две (ADR-041).
agents_match = _routes_agents.agents_match
agent_author = _routes_agents.agent_author
# Разбор канвы и объяснение отказа конверта — правила, на которые ссылаются проверки: держим имя
# в этом пространстве, пока домен не переехал целиком со своими тестами.
_graph_from_canvas = _routes_agents._graph_from_canvas
_envelope_reason = _routes_agents._envelope_reason
_order_by_contract = _routes_agents._order_by_contract
# Из домена прогонов остающемуся коду нужны: ограничитель частоты (его зовёт и чат), видимость
# прогона (доска), разрешение предмета (подбор) и дедуп-замок (очередь).
_rate_check = _routes_runs._rate_check
_run_visible = _routes_runs._run_visible
_board_scope = _routes_runs._board_scope
_run_lock = _routes_runs._run_lock
resolve_entity = _routes_runs.resolve_entity
run_start = _routes_runs.run_start
шаги_с_источниками = _routes_runs.шаги_с_источниками
from .routes import pipelines as _routes_pipelines  # noqa: E402
from .routes import plan as _routes_plan  # noqa: E402
from .routes import skills as _routes_skills  # noqa: E402

from .routes import auth as _routes_auth  # noqa: E402
from .routes import board as _routes_board  # noqa: E402
from .routes import canvas as _routes_canvas  # noqa: E402
from .routes import hitl as _routes_hitl  # noqa: E402
from .routes import triggers as _routes_triggers  # noqa: E402

for _r in (_routes_skills, _routes_pipelines, _routes_plan, _routes_hitl, _routes_triggers,
           _routes_board, _routes_auth, _routes_canvas):
    app.include_router(_r.router)

# Старт инжектит оверрайды навыков в ядро: имя живёт в своём домене, приложение берёт его оттуда.
_refresh_skill_ds_cache = _routes_skills._refresh_skill_ds_cache
# Сборка агентов из плана: на неё ссылаются проверки «не плодить дубли при повторной сборке».
plan_auto_build = _routes_plan.plan_auto_build
_agent_for_skills = _routes_plan._agent_for_skills
# Деление семей на бизнесовые и инженерные — классификация самих семей, живёт у них.
BIZ_FAMILIES = _routes_families.BIZ_FAMILIES

# Операции участка по агенту: нужны прогонам, чтобы записать op_ids и подтвердить карту (ADR-044).
_seat_ops = _routes_process._seat_ops



























# Домен «data» переехал в server/routes/data.py (п.23): 18 ручек. Внешняя
# поверхность не изменилась — сторожит tests/test_routes_inventory.py.
from .routes import data as _routes_data  # noqa: E402
# Наборы правил — свой домен: правило правится в интерфейсе и живёт в сторе, а не в файле
# пакета вертикали (шаг 3 плана «правила — данные»).
from .routes import rules as _routes_rules  # noqa: E402
# Вертикаль — единица поставки (шаг 4): манифест + правила + рецепты + контракт + агент,
# ставится одной операцией и отчитывается, что именно изменила в системе.
from .routes import verticals as _routes_verticals  # noqa: E402

app.include_router(_routes_data.router)
app.include_router(_routes_rules.router)
app.include_router(_routes_verticals.router)

# Инъекция рецептов и коннекторов в ядро `ape` — забота СТАРТА, а не маршрута, но живёт она
# рядом с доменом данных. Берём оттуда по имени: дублировать функцию ради чистоты импорта значило
# бы завести второй путь инициализации Data Plane.
_backfill_dataplane_from_files = _routes_data._backfill_dataplane_from_files
_refresh_dataplane_cache = _routes_data._refresh_dataplane_cache
_attach_record_store = _routes_data._attach_record_store
_backfill_records_from_files = _routes_data._backfill_records_from_files
# Перечень сущностей нужен и оркестратору: он решает, исполним ли план на фактических данных.
data_entities = _routes_data.data_entities





# ── История чатов пользователя: в Postgres, а не в локальной базе машины ─────────────────────────
# Переписка жила у сайдкара на диске: на каждой машине своя, чистка кэша или новый ноутбук означали
# «истории не было». В чате принимаются решения и запускаются агенты, в карточках лежат ссылки на
# прогоны и подтверждения внешних действий — это рабочий журнал, он обязан быть у человека, а не у
# машины. Владение — по `sub` из JWT; чужую переписку не отдаём, проверка стоит в хранилище.

# ── Блекборд: общая память человека и отдела, живущая дольше прогона ────────────────────────────
# Доска была только внутри запроса: выводы умирали вместе с прогоном, и соседний разговор о них не
# знал. Долгая доска добавляет к ней два правила, без которых она превратилась бы в свалку:
#   · на доску попадает ОБЪЯВЛЕННОЕ — факт кладёт либо прогон, либо человек нажатием, но не «всё
#     подряд из всех чатов»: именно дисциплина (ключ, автор, срок) делает доску полезной;
#   · у каждого факта автор, время, срок жизни и ссылка на прогон — утверждение без происхождения
#     проверить нечем, а месячный факт, выданный за текущий, хуже отсутствия факта.
# Доступ: личная доска — только владельцу; доска отдела — тем, кому открыта эта семья (ABAC). Это не
# формальность: доска не должна стать каналом в обход прав на данные.











# Домен «chat» переехал в server/routes/chat.py (п.23): 7 ручек. Внешняя поверхность
# не изменилась — сторожит tests/test_routes_inventory.py.
from .routes import chat as _routes_chat  # noqa: E402

app.include_router(_routes_chat.router)

# Рамка свободного чата — правило про границы рантайма; на него ссылаются проверки.
CHAT_GUARD = _routes_chat.CHAT_GUARD





















# ═══════════════ CONTRACT INGRESS: приём бандла из LUDA (ADR-029, SDD §4) ═══════════════

@app.on_event("startup")
async def _startup() -> None:
    """Создать таблицы contract_sets/agent_versions/runs, если есть Postgres (иначе память)."""
    await contract_store.init()
    await agent_store.init()
    await run_store.init()
    await layout_store.init()
    await audit_store.init()
    await skill_store.init()
    await admin_store.init()
    await userdata_store.init()
    await systems_store.init()
    await systems_store.seed_if_empty()  # одноразовый сид реестра из server-inventory (демо-стенд)
    await identity_store.init()
    await identity_store.seed_if_empty()  # сквозной ID: связка мастер-UID → аккаунты в системах (демо)
    await families_store.init()
    # Посев догоняет состав из кода: навык, добавленный в семью позже, иначе не попадёт в
    # конструктор агентов никогда — его просто не предложат выбрать.
    _fam_sync = await families_store.seed_from_code(ape.AGENT_FAMILIES, BIZ_FAMILIES)
    if _fam_sync.get("создано") or _fam_sync.get("дополнено"):
        obs.log_event("info", "families.sync", created=_fam_sync.get("создано"),
                      added={k: len(v) for k, v in (_fam_sync.get("дополнено") or {}).items()})
    await report_store.init()
    await report_store.seed_if_empty()   # шаблоны отчётов в БД (вид меняется без передеплоя)
    await schema_store.init()
    await schema_store.seed_if_empty()   # шаблоны извлечения (JSON Schema) — структура данных из БД
    try:                                  # подробные шаблоны навыков из репо (skills/<sid>/template.json) → builtin
        _n = await skill_templates.seed(schema_store)
        if _n:
            obs.log_event("info", "skill_templates.seeded", count=_n)
    except Exception as _ex:  # noqa: BLE001
        obs.log_event("warning", "skill_templates.seed_failed", error=str(_ex)[:200])
    await pipeline_store.init()          # цепочки агентов (линейный конвейер, выход→контекст)
    await process_store.init()           # карта процессов: редакции, посадки, журнал правок
    await trigger_store.init()
    await reglament_store.init()
    await rules_store.init()             # check_rules: правка набора правил поверх пакета вертикали
    # Прогон должен считать по ТОЙ же версии набора, что видна в интерфейсе. Поэтому снимок правок
    # поднимаем на старте и инжектим в ядро — иначе после рестарта правила снова стали бы файловыми.
    await rules_store.refresh()
    ape.set_rule_store(rules_store.ЭФФЕКТИВНЫЕ)
    await run_cache_store.init()
    await hitl_store.init()
    await finding_store.init()
    await blackboard.init()               # доска прогона: общая память ветвей с авторством
    import asyncio as _asyncio
    _asyncio.create_task(triggers.scheduler_loop(execute_agent_run))  # фоновый планировщик (leader-election)
    # ── очередь прогонов (гейт масштабирования, Фаза 1): воркеры вместо исполнения в HTTP-запросе ──
    await run_queue.init()
    await run_parts_store.init()
    await choice_store.init()
    try:
        _st = await run_queue.requeue_stale()
        if _st:
            obs.log_event("warn", "run_queue.requeued_stale", count=_st)
    except Exception:  # noqa: BLE001
        pass
    await run_bus.start()
    try:   # Блок 3: пара топиков на каждую систему реестра + слушатель событий → триггеры
        _sys_ids = [x["id"] for x in await systems_store.all()]
        await run_bus.ensure_system_topics(_sys_ids)
        _b = run_bus.bus()
        if getattr(_b, "active", False) and hasattr(_b, "events_loop"):
            _asyncio.create_task(_b.events_loop(_bus_event_handler))
    except Exception as _ex:  # noqa: BLE001
        obs.log_event("warning", "bus.systems.init_failed", error=str(_ex)[:200])
    for _wi in range(run_queue.WORKERS):
        _asyncio.create_task(run_bus.worker_loop(f"{os.getenv('HOSTNAME', 'api')}-w{_wi}", _handle_job))
    _asyncio.create_task(run_queue.gauges_loop())   # глубина очереди/RSS/ожидание → Prometheus/Grafana
    obs.log_event("info", "run_queue.started", workers=run_queue.WORKERS, bus=run_bus.describe().get("bus"))
    await _refresh_skill_ds_cache()  # инжект data-need оверрайдов из PG в ape
    await dataplane_store.init()
    await chat_store.init()   # история чатов пользователя: в БД, а не на машине
    try:
        await dlq_store.init()
    except Exception as _ex:  # noqa: BLE001
        obs.log_event("warning", "dlq_store.init_failed", error=str(_ex)[:200])
    try:
        await lexicon.init()
        agent_store.on_save(_refresh_lexicon)      # каждый новый агент приносит свои лексемы
        _asyncio.create_task(_reseed_lexicons())   # словарь подстановки живёт от заведённых агентов
    except Exception as _ex:  # noqa: BLE001
        obs.log_event("warning", "lexicon.init_failed", error=str(_ex)[:200])
    await _backfill_dataplane_from_files()  # одноразовый перенос ~/.ape → PG (сохранить демо-рецепты)
    await _refresh_dataplane_cache()  # инжект рецептов/коннекторов из PG в ape
    _attach_record_store()                  # канонические записи — из PG, а не из файлов тома
    await _backfill_records_from_files()    # одноразовый перенос ~/.ape/data/*.jsonl в PG
    # LLM-override (эндпоинт бокса из UI) — восстановить из PG при старте, инжектить в clients
    await _refresh_llm_override()
    # Cache-bus: инвалидация in-process кэшей между репликами (LISTEN/NOTIFY). Разблокирует 2+ реплики.
    cachebus.register("skills", _refresh_skill_ds_cache)
    cachebus.register("dataplane", _refresh_dataplane_cache)
    cachebus.register("llm", _refresh_llm_override)
    cachebus.register("rules", rules_store.refresh)   # правка набора на другой реплике → перечитать снимок
    cachebus.register("process", lambda: process_store.poke())   # правка карты с другой реплики → толкнуть своих SSE-подписчиков
    _asyncio.create_task(cachebus.listen_loop())


async def _refresh_llm_override() -> None:
    """Подтянуть self-host LLM-override из admin_config (PG) → инжект в clients (переживает рестарт)."""
    try:
        cfg = (await admin_store.all()).get("llmOverride") or {}
        cfg = cfg if isinstance(cfg, dict) else {}
        base = str(cfg.get("base_url") or "").rstrip("/")
        if base and base != (settings.local_llm_base_url or "").rstrip("/"):
            # override указывает не на env-бокс: проверим, жив ли он, иначе молчаливый откат всего каскада в RouteAI
            import httpx as _hx
            try:
                async with _hx.AsyncClient(timeout=6) as _c:
                    _ok = (await _c.get(base + "/models")).status_code == 200
            except Exception:  # noqa: BLE001
                _ok = False
            if not _ok:
                obs.log_event("warning", "llm.override_unreachable", base_url=base, fallback=settings.local_llm_base_url)
                cfg = {}
        clients.set_llm_override(cfg)
    except Exception:  # noqa: BLE001
        pass








# ═══════════════ AGENTS: сборка агента на канве → AgentVersion (ADR-013/014, SDD §4.4) ═══════════════



















































async def _reseed_lexicons() -> None:
    """Засевка словарей по всем агентам (старт сервера и после массовых правок навыков)."""
    n = 0
    try:
        for b in await agent_store.list_for(None):
            full = await agent_store.get(b["id"])
            if full and await _refresh_lexicon(full):
                n += 1
    except Exception as _ex:  # noqa: BLE001
        obs.log_event("warning", "lexicon.reseed_failed", error=str(_ex)[:160])
        return
    obs.log_event("info", "lexicon.reseeded", agents=n)




























# ═════════════════════════════════ RUN / STREAM / HITL ═════════════════════════════════════════
# Осиротевший _NOT_IMPL убран 08.10 вместе с последним 501: прогон идёт заданием очереди, HITL
# возвращает задание с чекпоинта, ход прогона отдаётся по SSE.















































# ─── Multi-user: per-user rate-limit + идемпотентность (дедуп двойных сабмитов) ───
import asyncio as _aio
import time as _time
























async def _handle_job(job: dict) -> dict:
    """Воркер очереди: kind=run → execute_agent_run; kind=pipeline → шаги цепочки с чекпоинтом."""
    if (job.get("kind") or "run") == "pipeline":
        # ветвление, условия и слияние умеет только граф-исполнитель; плоские цепочки оставляем на
        # прежнем пути, чтобы не менять поведение того, что уже работает на демо
        _p = await pipeline_store.get(((job.get("payload") or {}).get("pid")) or "")
        _st = (_p or {}).get("steps") or []
        _branched = any(isinstance(x, dict) and (x.get("after") or x.get("join") or x.get("when")) for x in _st)
        return await (_pipeline_graph_job(job) if _branched else _pipeline_job(job))
    p = job.get("payload") or {}
    agent = await agent_store.get(job["agent_id"])
    if not agent:
        raise RuntimeError("агент не найден")
    contract = await _contract_for_agent(agent, p.get("contract_audit_id") or "")
    out = await execute_agent_run(agent, contract, job["actor"],
                                  started_by_sub=str(p.get("actor_sub") or job.get("actor_sub") or ""),
                                  use_cache=bool(p.get("use_cache", False)),
                                  user_context=p.get("user_context") or "", deliver_filter=p.get("deliver_filter") or "",
                                  data_scope=p.get("data_scope") or None,   # предмет работы едет с заданием
                                  budget=p.get("budget") or None, limits=p.get("limits") or None,
                                  # общая память ветвей одного запроса и снимок данных на всю группу
                                  board_scope=p.get("board_scope") or job.get("group_id") or "",
                                  data_snapshot=p.get("data_snapshot") or None,
                                  parent_trace_id=p.get("parent_trace_id") or "",
                                  trigger=p.get("trigger"), job_id=job["id"])
    return {"run_id": out["saved"]["id"]}


async def _pipeline_graph_job(job: dict) -> dict:
    """Цепочка-граф: волны шагов, параллельные ветви, условия запуска и узлы слияния.

    Отличия от линейного исполнителя: между шагами едет СТРУКТУРА (результаты нужных шагов помеченным
    блоком данных, а не проза с обрезкой), ветви внутри волны идут параллельно, ошибка ветви не рушит
    цепочку, а попадает в слияние как «нет результата». Плоские цепочки без ветвления по-прежнему идут
    старым путём: у них нет ни условий, ни слияния, и незачем менять их поведение.
    """
    p = job.get("payload") or {}
    cp = dict(job.get("checkpoint") or {})
    pipe = await pipeline_store.get(p.get("pid") or "")
    if not pipe:
        raise RuntimeError("цепочка не найдена")
    steps = pipeline_graph.normalize(pipe.get("steps") or [])
    errs = pipeline_graph.validate(steps)
    if errs:
        raise RuntimeError("граф цепочки: " + "; ".join(errs[:3]))
    base_ctx = str(p.get("context") or "")[:20000]
    actor = job["actor"]
    results: dict = dict(cp.get("results") or {})     # step_id → {data, agent_name, run_id, ...}
    done_steps: list = list(cp.get("steps") or [])
    started = {d.get("id") for d in done_steps if d.get("id")}

    for wave in pipeline_graph.waves(steps):
        todo = [st for st in wave if st["id"] not in started]
        if not todo:
            continue
        if run_queue.cancel_requested(job["id"]):
            raise RuntimeError("отменено оператором")

        async def _one(st: dict) -> dict:
            sid = st["id"]
            # узел слияния: агента не запускает, сводит результаты ветвей по политике
            if st.get("join"):
                m = pipeline_graph.merge(st["join"], results)
                return {"id": sid, "join": True, "policy": m.get("policy"), "note": m.get("note"),
                        "missing": m.get("missing") or [], "data": m.get("data") or {}}
            ok, why = pipeline_graph.should_run(st, results)
            if not ok:
                return {"id": sid, "agent_id": st.get("agent_id"), "skipped": True, "reason": why}
            agent = await agent_store.get(st.get("agent_id"))
            if not agent:
                return {"id": sid, "agent_id": st.get("agent_id"), "error": "агент не найден", "skipped": True}
            contract = await _contract_for_agent(agent)
            up = pipeline_graph.step_input(st, results)
            parts = [x for x in (base_ctx, safety.data_block("ВХОД ОТ ПРЕДЫДУЩИХ ШАГОВ ЦЕПОЧКИ", up) if up else "") if x]
            deliver = st.get("deliver") or "chat"
            try:
                res = await execute_agent_run(agent, contract, actor, use_cache=False,
                                              user_context="\n\n".join(parts), deliver_filter=deliver,
                                              data_scope=st.get("scope") or None,
                                              budget=st.get("budget") or (p.get("budget") or None),
                                              # параллельные ветви цепочки пишут на ОДНУ доску: иначе
                                              # шаг слияния видит два вывода и не знает, какой верен
                                              board_scope="job-" + str(job["id"]),
                                              data_snapshot=cp.get("data_snapshot") or None,
                                              job_id=job["id"])
                result = res["result"]
                data = {o.get("skill"): o.get("structured") for o in (result.get("skill_outputs") or [])
                        if isinstance(o.get("structured"), dict)}
                flat: dict = {}
                for st_out in data.values():
                    for k, v in (st_out or {}).items():
                        flat.setdefault(k, v)
                _sc = ((res.get("saved") or {}).get("run_metrics") or {}).get("cost") or {}
                return {"id": sid, "agent_id": agent["id"], "agent_name": agent.get("name"),
                        "run_id": res["saved"]["id"], "deliver": deliver, "data": flat,
                        "tokens": int(_sc.get("input_tokens") or 0) + int(_sc.get("output_tokens") or 0),
                        "findings_total": (result.get("findings_summary") or {}).get("total") or len(result.get("findings") or []),
                        "delivery": result.get("delivery") or [], "verdict": result.get("verdict") or {},
                        "run_metrics": {"data_snapshot": (result.get("run_metrics") or {}).get("data_snapshot"),
                                        "arbitration": (result.get("run_metrics") or {}).get("arbitration")},
                        "trace_id": result.get("trace_id") or ""}
            except Exception as ex:  # noqa: BLE001 — сбой ветви не рушит цепочку
                return {"id": sid, "agent_id": agent["id"], "agent_name": agent.get("name"),
                        "error": str(ex)[:300]}

        got = await _asyncio.gather(*[_one(st) for st in todo])
        for r in got:
            done_steps.append(r)
            started.add(r["id"])
            if r.get("data"):
                results[r["id"]] = {"data": r["data"], "agent_name": r.get("agent_name"),
                                    "run_id": r.get("run_id")}
        if not cp.get("data_snapshot"):
            # Картину задаёт первая ветвь, у которой она есть: дальше все шаги сверяются с ней, и
            # разные цифры в ветвях перестают быть загадкой — видно, что состав данных изменился.
            for r in got:
                _sn = ((r.get("run_metrics") or {}).get("data_snapshot")) if isinstance(r, dict) else None
                if _sn:
                    cp["data_snapshot"] = _sn
                    break
        cp.update({"results": results, "steps": done_steps, "steps_total": len(steps),
                   "steps_done": len(started)})
        await run_queue.set_checkpoint(job["id"], cp)

    last = next((d for d in reversed(done_steps) if d.get("run_id")), None)
    return {"run_id": (last or {}).get("run_id") or "", "pipeline": pipe.get("id"), "steps": done_steps}


def _pipeline_checkpoint(step: int, total: int, done: list, prev_ctx, prev_src) -> dict:
    """Чекпойнт шага цепочки: что уже сделано и с чем идём дальше.

    Ограничение по длине относится к КОНТЕКСТУ — он текст, и в следующий шаг его нужно отдать
    урезанным. `prev_src` — объект «от кого пришёл вход» ({шаг, агент, прогон}); срез словаря роняет
    всё задание (`TypeError: unhashable type: 'slice'`), и именно это случилось: ограничение стояло
    на соседнем ключе, поэтому падала ЛЮБАЯ цепочка из очереди — на первом же шаге, уже после того
    как агент отработал. Отдельная функция ради одной строки нужна затем, что эту строку можно
    проверить тестом, а не только живым прогоном.
    """
    return {"step": step, "steps_total": total, "steps": done,
            "prev_ctx": str(prev_ctx or "")[:6000], "prev_src": prev_src}






async def _pipeline_job(job: dict) -> dict:
    """Цепочка агентов через очередь: каждый шаг — обычный прогон; выход шага → контекст следующего.
    Если у шага есть доставка «ждёт подтверждения» и шаг не последний — задание уходит в awaiting_hitl
    с чекпоинтом (индекс, контекст, hitl_ids); решение оператора возвращает его в очередь, и цепочка
    продолжается со следующего шага (отклонение — тоже продолжает, с пометкой в контексте)."""
    p = job.get("payload") or {}
    cp = dict(job.get("checkpoint") or {})
    pipe = await pipeline_store.get(p.get("pid") or "")
    if not pipe:
        raise RuntimeError("цепочка не найдена")
    steps = pipe.get("steps") or []
    base_ctx = str(p.get("context") or "")[:20000]
    i = int(cp.get("step") or 0)
    prev_ctx = cp.get("prev_ctx") or ""
    prev_src = cp.get("prev_src")   # от кого пришёл вход: переживает паузу на подтверждении
    done_steps = list(cp.get("steps") or [])
    # решение по HITL прошлого шага — в контекст следующего
    if cp.get("hitl_ids") and cp.get("hitl_decisions"):
        decs = cp["hitl_decisions"]
        note = "; ".join(f"{k}: {'подтверждено' if v == 'approve' else 'отклонено'}" for k, v in decs.items())
        prev_ctx = (prev_ctx + f"\n\n=== РЕШЕНИЕ ОПЕРАТОРА ПО ПРЕДЫДУЩЕМУ ШАГУ ===\n{note}").strip()
        cp["hitl_ids"] = []
    actor = job["actor"]
    while i < len(steps):
        if run_queue.cancel_requested(job["id"]):
            raise RuntimeError("отменено оператором")
        st = steps[i]
        agent = await agent_store.get(st.get("agent_id"))
        if not agent:
            done_steps.append({"agent_id": st.get("agent_id"), "error": "агент не найден", "skipped": True})
            i += 1
            continue
        contract = await _contract_for_agent(agent)
        last = i == len(steps) - 1
        ctx_parts = [x for x in (base_ctx, (safety.data_block("РЕЗУЛЬТАТ ПРЕДЫДУЩЕГО АГЕНТА ЦЕПОЧКИ (вход для тебя)", prev_ctx) if prev_ctx else "")) if x]
        step_ctx = "\n\n".join(ctx_parts)
        deliver = st.get("deliver") or ("" if last else "chat")
        try:
            res = await execute_agent_run(agent, contract, actor, use_cache=False, user_context=step_ctx,
                                          deliver_filter=deliver, job_id=job["id"], input_from=prev_src)
            result = res["result"]
            prev_ctx = _result_to_context(agent, result)
            prev_src = {"шаг": st.get("id") or ("#" + str(i + 1)), "агент": agent.get("name") or agent.get("id"),
                        "прогон": res["saved"]["id"]}
            _sc = ((res.get("saved") or {}).get("run_metrics") or {}).get("cost") or {}
            done_steps.append({"agent_id": agent["id"], "agent_name": agent.get("name"), "run_id": res["saved"]["id"],
                               "deliver": deliver, "tokens": int(_sc.get("input_tokens") or 0) + int(_sc.get("output_tokens") or 0),
                               # Что шаг сказал — строками. Без этого карточка цепочки показывала
                               # только счётчики, и результат приходилось искать в прогоне.
                               "summary": run_summary(result),
                               "findings": result.get("findings") or [],
                               "findings_total": (result.get("findings_summary") or {}).get("total") or len(result.get("findings") or []),
                               "investigations_total": len(result.get("investigations") or []),
                               "delivery": result.get("delivery") or [], "verdict": result.get("verdict") or {},
                               "trace_id": result.get("trace_id") or ""})
            waits = [d.get("hitl_id") for d in (result.get("delivery") or []) if d.get("mode") == "awaiting_hitl" and d.get("hitl_id")]
        except Exception as ex:  # noqa: BLE001 — сбой шага не рушит цепочку
            done_steps.append({"agent_id": agent["id"], "agent_name": agent.get("name"), "error": str(ex)[:300]})
            waits = []
        i += 1
        cp.update(_pipeline_checkpoint(i, len(steps), done_steps, prev_ctx, prev_src))
        await run_queue.set_checkpoint(job["id"], cp)
        if waits and i < len(steps):   # HITL-пауза: следующий шаг только после решения оператора
            cp["hitl_ids"] = waits
            return {"await_hitl": cp}
    await audit_store.record(actor, "pipeline.run", p.get("pid") or "", {"steps": len(done_steps), "job_id": job["id"]})
    return {"checkpoint": cp}


async def _contract_for_agent(agent: dict, audit_id: str = "") -> dict:
    """Контракт агента (или минимальный конверт из его autonomy_max) — общий хелпер API и воркеров."""
    contract = await contract_store.get(agent.get("contract_audit_id") or audit_id)
    if not contract:
        contract = {"intake": {"autonomy_ceiling": agent.get("autonomy_max") or "A2"}, "bundle": {}}
    return contract






















































# Домен «bus» переехал в server/routes/bus.py (п.23): 6 ручек. Внешняя
# поверхность не изменилась — сторожит tests/test_routes_inventory.py.
from .routes import bus as _routes_bus  # noqa: E402

app.include_router(_routes_bus.router)

# Обработчик событий шины и сборка контекста цепочки живут рядом с шиной, но зовутся из
# планировщика триггеров и из прогонов. Берём по имени — это та же функция, а не копия.
# Перенос результата шага в контекст следующего нужен и воркеру цепочек в приложении.
_result_to_context = _routes_bus._result_to_context
_bus_event_handler = _routes_bus._bus_event_handler
_on_command_result = _routes_bus._on_command_result



























# ═══════════════ Предмет работы: слоты навыка и разрешение по данным ═══════════════
# Подбор агента отвечает на вопрос «что делать». Этого мало: «сравни дорожную карту проекта с фактом»
# без конкретного проекта даёт мета-ответ. Слот описывает недостающий предмет, а разрешение ищет его
# кандидатов в данных этого человека. Подробности: docs/PODBOR_AGENTA_I_PREDMETA.md.





















































# ═══════════════ Статика: buildless-React фронт ABOP (webapp/) ═══════════════
# Монтируется ПОСЛЕ всех /api-роутов, чтобы они имели приоритет. html=True → SPA-fallback.
# UI ДЕСКТОПА (desktop/ui) раздаём по /desktop-ui/ — Electron грузит его по сети (APE_UI_URL),
# правки чат-панели прилетают через git-deploy БЕЗ пересборки .exe (см. ADR смычки, путь А).
_DESKTOP_UI = Path(__file__).resolve().parents[1] / "desktop" / "ui"


@app.get("/desktop-ui-bundle")
def desktop_ui_bundle() -> dict:
    """Весь UI десктопа одним ответом {version, files:{relpath:text}} — сайдкар тянет его при старте
    (server-side, без Chromium PNA), кэширует и отдаёт с 127.0.0.1. Так UI обновляется git-деплоем
    БЕЗ пересборки .exe (правильный путь А: UI и сайдкар-API в одном origin)."""
    import hashlib as _hl
    if not _DESKTOP_UI.is_dir():
        return {"version": "", "files": {}}
    import base64 as _b64
    files: dict = {}
    binary: dict = {}
    for p in sorted(_DESKTOP_UI.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(_DESKTOP_UI).as_posix()
        ext = p.suffix.lower()
        try:
            if ext in (".html", ".js", ".css", ".json", ".svg"):
                files[rel] = p.read_text(encoding="utf-8")
            elif ext in (".woff2", ".woff", ".ttf", ".png", ".ico"):
                # шрифты/иконки — base64 (UX-аудит 25.09 D-C6: без них десктоп рендерился в Segoe UI)
                binary[rel] = _b64.b64encode(p.read_bytes()).decode("ascii")
        except Exception:  # noqa: BLE001 — нечитаемое пропускаем
            continue
    ver = _hl.md5("".join(f"{k}:{len(v)}" for k, v in sorted({**files, **binary}.items())).encode()).hexdigest()[:12]
    return {"version": ver, "files": files, "binary": binary}


if _DESKTOP_UI.is_dir():
    app.mount("/desktop-ui", StaticFiles(directory=str(_DESKTOP_UI), html=True), name="desktop-ui")

# MCP-сервер: чужой агент работает с процессами и данными под теми же правами (ADR-028).
# Выключен по умолчанию, включается ABOP_MCP=1 — см. server/mcp_server.py.
if mcp_server.подключить(app):
    obs.log_event("info", "mcp.enabled", path="/mcp")
_WEBAPP = Path(__file__).resolve().parents[1] / "webapp"
if _WEBAPP.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEBAPP), html=True), name="webapp")
