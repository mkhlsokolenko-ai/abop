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

from . import access, admin_store, agent_store, arbiter, assembly, audit_store, blackboard, planner, cachebus, charts, clients, compute, contract_store, cost_estimate, dataplane_store, delivery as delivery_mod, dlq_store, families_store, lexicon, nlu, finding_store, findings, hitl_store, identity_store, ingress, langfuse_trace, layout_store, observability as obs, bpmn_import, dataplane_records, mcp_server, pipeline_store, process_deploy, process_store, rbac_map, reglament_store, chat_store, orchestrator, report_compose, report_store, choice_store, doclink, run_cache_store, run_parts_store, run_store, runner, safety, pipeline_graph, schema_store, skill_contract, skill_store, skill_templates, skill_tools, slava, systems_store, trigger_store, triggers, run_bus, run_queue, userdata_store  # noqa: E402
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


# Свободный ответ в чате — это РАЗГОВОР, а не работа. Без этой рамки модель берётся отчитываться за
# систему: 04.10 в чате появились «цепочка выполнена», «отчёт собран в PDF», имя файла и путь
# «docs/…» — ничего из этого не существовало. Это худший вид отказа: не ошибка, а правдоподобный
# отчёт о несделанном, под которым стоит подпись модели и ноль рублей.
# Рамка добавляется НА СЕРВЕРЕ, а не в клиенте: правило про границы рантайма обязано действовать в
# любом канале, а не только в той версии десктопа, которую успели обновить.
CHAT_GUARD = (
    "Ты отвечаешь ТЕКСТОМ и не выполняешь действий: не запускаешь агентов, не собираешь отчёты, "
    "не создаёшь файлы, не отправляешь письма и не заводишь задачи. Никогда не утверждай, что "
    "что-то выполнено, собрано, сохранено, прикреплено или отправлено, и не выдумывай имена файлов, "
    "пути, ссылки и идентификаторы. Если просят сделать работу — дай текстом то, что можешь дать "
    "сам (разбор, план, вопросы по существу), а про запуск исполнителя скажи ОДНОЙ короткой "
    "строкой в конце: «запустить исполнителя — команда /work». НИКОГДА не выводи команду с "
    "текстом задачи внутри и не цитируй задачу человека обратно вместо ответа: человек уже "
    "написал её, переписывать её ему не нужно. Команды набирает человек, не ты. "
    "За готовым отчётом прогона отправляй к команде «/report» — она соберёт настоящий файл. "
    "О том, что уже сделано, человек узнаёт из карточек прогона, а не из твоего пересказа. "
    # Qwen на длинной системной рамке иногда вставляет иероглифы в середину русской фразы —
    # проверено живым ответом. Язык объявляем явно, это дешевле, чем чистить вывод.
    "Отвечай по-русски."
)


@app.post("/api/chat")
async def chat_freeform(body: dict, u: dict = Depends(user)) -> dict:
    """Свободный LLM-ответ под JWT пользователя — единый рантайм-канал для desktop-чата,
    цепочек графа и распознавания (тот же self-host каскад, что и у агентов). Отвязка от
    курсового шлюза: desktop больше НЕ ходит в MCP/portal курса. Rate-limit по пользователю."""
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(422, "нужен prompt")
    actor = u.get("name") or u.get("sub") or "dev"
    if not _rate_check(actor):
        raise HTTPException(429, "слишком часто — попробуйте чуть позже")
    system = str((body or {}).get("system") or "").strip()
    context = str((body or {}).get("context") or "").strip()
    profile = str((body or {}).get("profile") or "standard").strip() or "standard"
    max_tokens = min(int((body or {}).get("max_tokens") or 1200), 4096)
    messages: list[dict] = [{"role": "system", "content": CHAT_GUARD}]
    if system:
        messages.append({"role": "system", "content": system})
    user_content = (f"Контекст:\n{context}\n\n" if context else "") + prompt
    messages.append({"role": "user", "content": user_content})
    try:
        resp = await clients.chat(messages=messages, profile=profile, max_tokens=max_tokens)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"LLM недоступен: {ex}")
    return {"text": resp.get("text") or "", "model": resp.get("model"),
            "input_tokens": resp.get("input_tokens"), "output_tokens": resp.get("output_tokens")}


@app.post("/api/chat/stream")
async def chat_freeform_stream(body: dict, u: dict = Depends(user)):
    """Стриминг свободного ответа (SSE): токены-дельты по мере генерации — настоящий стрим для
    desktop-чата (не псевдо-нарезка на сайдкаре). Формат строк: `data: {"delta": "..."}` и финал
    `data: {"done": true, "model": ...}`. Тот же self-host каскад, что и /api/chat."""
    import json as _json
    from fastapi.responses import StreamingResponse
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(422, "нужен prompt")
    actor = u.get("name") or u.get("sub") or "dev"
    if not _rate_check(actor):
        raise HTTPException(429, "слишком часто — попробуйте чуть позже")
    system = str((body or {}).get("system") or "").strip()
    context = str((body or {}).get("context") or "").strip()
    profile = str((body or {}).get("profile") or "standard").strip() or "standard"
    max_tokens = min(int((body or {}).get("max_tokens") or 1500), 4096)
    messages: list[dict] = [{"role": "system", "content": CHAT_GUARD}]
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": (f"Контекст:\n{context}\n\n" if context else "") + prompt})

    async def gen():
        try:
            async for ev in clients.chat_stream(messages=messages, profile=profile, max_tokens=max_tokens):
                yield "data: " + _json.dumps(ev, ensure_ascii=False) + "\n\n"
        except Exception as ex:  # noqa: BLE001
            yield "data: " + _json.dumps({"error": f"LLM: {ex}"}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/auth/login")
async def auth_login(body: dict) -> dict:
    """Прокси-логин к Keycloak (realm abop) — Keycloak не публичный, поэтому вход идёт через ABOP.
    Тело: {username, password}. Возвращает {access_token, refresh_token, expires_in, user}: access у Keycloak живёт
    ~5 минут, поэтому фронт обязан обновлять сессию через /api/auth/refresh, иначе всё отваливается по 401
    и выглядит как «сервер недоступен»."""
    import httpx
    import jwt
    un = str((body or {}).get("username", "")).strip()
    pw = str((body or {}).get("password", ""))
    if not un or not pw:
        raise HTTPException(422, "нужны username и password")
    iss = os.getenv("KEYCLOAK_ISSUER") or "http://localhost:8811/realms/abop"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(iss + "/protocol/openid-connect/token",
                             data={"client_id": "abop-web", "username": un, "password": pw,
                                   "grant_type": "password"})
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"Keycloak недоступен: {ex}")
    if r.status_code != 200:
        raise HTTPException(401, "неверный логин или пароль")
    tokens = r.json()
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    return {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token"),
            "expires_in": int(tokens.get("expires_in") or 0),
            "refresh_expires_in": int(tokens.get("refresh_expires_in") or 0),
            "user": _identity(claims)}


@app.post("/api/auth/refresh")
async def auth_refresh(body: dict) -> dict:
    """Обновление сессии по refresh_token (Keycloak realm abop). Без этого веб жил 5 минут до первого 401."""
    import httpx
    import jwt
    rt = str((body or {}).get("refresh_token", "")).strip()
    if not rt:
        raise HTTPException(422, "нужен refresh_token")
    iss = os.getenv("KEYCLOAK_ISSUER") or "http://localhost:8811/realms/abop"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(iss + "/protocol/openid-connect/token",
                             data={"client_id": "abop-web", "grant_type": "refresh_token", "refresh_token": rt})
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"Keycloak недоступен: {ex}")
    if r.status_code != 200:
        raise HTTPException(401, "сессия истекла — войдите заново")
    tokens = r.json()
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    return {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or rt,
            "expires_in": int(tokens.get("expires_in") or 0), "user": _identity(claims)}






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

for _r in (_routes_skills, _routes_pipelines, _routes_plan):
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

@app.get("/api/triggers")
async def triggers_list(u: dict = Depends(user)) -> dict:
    """Стартовые события агентов (триггеры из графов) + последний фаер. Планировщик фаерит только
    enabled schedule/event-триггеры. §триггер-узел (persistence-localstorage-hole)."""
    return {"triggers": await triggers.list_triggers(), "fires": await trigger_store.list_fires(limit=30)}


@app.post("/api/triggers/{agent_id}/{trigger_id}/fire")
async def trigger_fire(agent_id: str, trigger_id: str, u: dict = Depends(user)) -> dict:
    """Ручной запуск триггера (кнопка/тест): чтит политику spawn (автономия ≤ контракт, HITL-на-создание),
    но игнорирует enabled/min-interval. Уровень manager+ (запуск = мутация среды)."""
    require_level(u, "manager")
    res = await triggers.fire_manual(agent_id, trigger_id, execute_agent_run)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "trigger.fire", trigger_id,
                             {"agent_id": agent_id, "status": res.get("status")})
    return res
























# Домен «data» переехал в server/routes/data.py (п.23): 18 ручек. Внешняя
# поверхность не изменилась — сторожит tests/test_routes_inventory.py.
from .routes import data as _routes_data  # noqa: E402

app.include_router(_routes_data.router)

# Инъекция рецептов и коннекторов в ядро `ape` — забота СТАРТА, а не маршрута, но живёт она
# рядом с доменом данных. Берём оттуда по имени: дублировать функцию ради чистоты импорта значило
# бы завести второй путь инициализации Data Plane.
_backfill_dataplane_from_files = _routes_data._backfill_dataplane_from_files
_refresh_dataplane_cache = _routes_data._refresh_dataplane_cache
_attach_record_store = _routes_data._attach_record_store
_backfill_records_from_files = _routes_data._backfill_records_from_files
# Перечень сущностей нужен и оркестратору: он решает, исполним ли план на фактических данных.
data_entities = _routes_data.data_entities

@app.get("/api/canvas/layout/{key}")
async def canvas_layout_get(key: str, u: dict = Depends(user)) -> dict:
    """Прочитать сохранённую раскладку канвы (узлы с x/y + рёбра) по ключу (контракт/сценарий)."""
    row = await layout_store.get(key)
    return row or {"key": key, "nodes": None, "edges": None}


@app.post("/api/canvas/layout/{key}")
async def canvas_layout_save(key: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить раскладку канвы в Postgres. Тело: {nodes:[{id,kind,x,y,...}], edges:[...]}."""
    require_level(u, "manager")  # правка раскладки — мутация (analyst read-only)
    nodes = (body or {}).get("nodes") or []
    edges = (body or {}).get("edges") or []
    saved = await layout_store.save(key, nodes, edges)
    return {"key": key, "saved": True, "updated_at": saved.get("updated_at")}


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



@app.get("/api/board")
async def board_read(kind: str = "user", family: str = "", stale: bool = False,
                     u: dict = Depends(user)) -> dict:
    """Факты доски: личной или отдела. Просроченные не отдаём, пока не попросят явно."""
    scope = _board_scope(kind, u, family)
    rows = await blackboard.load(scope, include_stale=bool(stale))
    return {"scope": scope, "facts": rows, "count": len(rows)}


@app.post("/api/board/facts")
async def board_put(body: dict, u: dict = Depends(user)) -> dict:
    """Положить факт на доску руками: «вынести на общую доску».

    Автором ставим человека, а не агента: это его утверждение, даже если взято из прогона.
    """
    key = str((body or {}).get("key") or "").strip()
    if not key:
        raise HTTPException(422, "нужен ключ факта")
    scope = _board_scope(str((body or {}).get("kind") or "user"), u, str((body or {}).get("family") or ""))
    row = await blackboard.put_fact(
        scope, key, (body or {}).get("value"),
        author=u.get("name") or _sub(u), note=str((body or {}).get("note") or ""),
        ttl_sec=int((body or {}).get("ttl_sec") or blackboard.DEFAULT_TTL_SEC),
        run_id=str((body or {}).get("run_id") or ""))
    await audit_store.record(u.get("name") or _sub(u), "board.put", scope,
                             {"key": key, "run_id": row.get("run_id"), "ttl_sec": row.get("ttl_sec")})
    return {"ok": True, "scope": scope, "fact": row}


@app.delete("/api/board/facts/{key}")
async def board_drop(key: str, kind: str = "user", family: str = "", author: str = "",
                     u: dict = Depends(user)) -> dict:
    scope = _board_scope(kind, u, family)
    n = await blackboard.drop_fact(scope, key, author)
    await audit_store.record(u.get("name") or _sub(u), "board.drop", scope, {"key": key})
    return {"ok": True, "removed": n}




# Домен «chat» переехал в server/routes/chat.py (п.23): 7 ручек. Внешняя поверхность
# не изменилась — сторожит tests/test_routes_inventory.py.
from .routes import chat as _routes_chat  # noqa: E402

app.include_router(_routes_chat.router)





















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



_DEMO_DIR = Path(__file__).resolve().parents[1] / "demo"


@app.post("/api/demo/prepare")
async def demo_prepare(body: dict, u: dict = Depends(user)) -> dict:
    """Один клик «Собрать и запустить»: идемпотентно готовит демо-сценарий к прогону —
    ингест контракта + сохранение AgentVersion из demo/<key>/ (contract*.json + agent_body.json).
    Возвращает {audit_id, agent_id, contract} для привязки на канве. Сценарий без демо-пакета → 404.

    Закрывает разрыв UX: вкладка сценария на канве не привязывала контракт, поэтому «Запустить
    прогон» молча ничего не делал. Теперь запуск из UI доступен в один клик (см. persistence-hole)."""
    import json as _json
    import glob as _glob
    key = str((body or {}).get("scenario") or "").strip()
    ddir = _DEMO_DIR / key
    if not key or not ddir.is_dir():
        raise HTTPException(404, f"нет демо-пакета для сценария «{key}»")
    cfiles = sorted(_glob.glob(str(ddir / "contract*.json")))
    bfile = ddir / "agent_body.json"
    if not cfiles or not bfile.is_file():
        raise HTTPException(404, f"демо-пакет «{key}» неполон (нужны contract*.json + agent_body.json)")
    with open(cfiles[0], encoding="utf-8") as f:
        contract = _json.load(f)
    audit_id = ((contract.get("capability_request") or {}).get("audit_id") or "").strip()
    if not audit_id:
        raise HTTPException(422, "в контракте нет capability_request.audit_id")
    actor = u.get("name") or u.get("sub") or "demo"
    # 1) контракт — ингест, если ещё не принят (идемпотентно)
    if not await contract_store.get(audit_id):
        res = ingress.validate(contract)
        if not res.accepted:
            raise HTTPException(422, {"errors": res.errors})
        await contract_store.save(contract, res.intake, ingested_by=actor)
    cs = await contract_store.get(audit_id)
    intake = cs.get("intake") or {}
    # ABAC: пользователь должен иметь доступ к семье контракта (иначе чужой процесс не запустить)
    if not can_see_family(u, intake.get("family")):
        raise HTTPException(403, f"нет доступа к семье «{intake.get('family')}»")
    # 2) агент — upsert draft из agent_body.json (идемпотентно: правки файла, вкл. OUT-узел,
    #    подхватываются на следующем «Собрать и запустить», версии не плодятся — ADR-024).
    with open(bfile, encoding="utf-8") as f:
        body_data = _json.load(f)
    graph = body_data.get("graph") or {}
    check = assembly.check_graph(graph, intake, ape.skill_safety, reads_data=await _skills_reading_data())
    if check["errors"]:
        raise HTTPException(422, {"errors": check["errors"]})
    fam = intake.get("family") or ""
    saved = await agent_store.save_draft(
        name=body_data.get("name") or key, audit_id=audit_id, graph=graph,
        autonomy_max=check["autonomy_max"], created_by=actor,
        family=fam, role=_resolve_role(fam, intake))
    agent_id = saved["id"]
    await audit_store.record(actor, "agent.save", agent_id,
                             {"family": fam, "via": "demo.prepare", "scenario": key})
    return {"audit_id": audit_id, "agent_id": agent_id,
            "contract": {"audit_id": audit_id, "autonomy": intake.get("autonomy_ceiling"),
                         "family": intake.get("family"), "skills": intake.get("skills") or []}}














































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






















@app.get("/api/triggers/mine")
async def triggers_mine(u: dict = Depends(user)) -> dict:
    """Расписания, заведённые ТЕКУЩИМ пользователем (created_by агента = я, или trig.owner = я): агент,
    cron, вкл/выкл, куда доставка (чат/система), последний фаер + краткий итог последнего прогона.
    Для раздела «Мои расписания» и уведомлений о завершении."""
    me = _uid_of(u)
    admin = u.get("department") in ("*", None)
    out = []
    for t in await triggers.list_triggers():
        if (t.get("source") or "") != "chat.recurring":
            continue
        agent = await agent_store.get(t["agent_id"])
        owner = ((_node_trig(agent, t.get("trigger_id")) or {}).get("owner")) or (agent or {}).get("created_by")
        if not admin and owner != me:
            continue
        last_run = None
        fires = [f for f in await trigger_store.list_fires(limit=200)
                 if f.get("agent_id") == t["agent_id"] and f.get("trigger_id") == t.get("trigger_id")]
        if fires:
            rid = fires[0].get("run_id")
            at = fires[0].get("fired_at")
            if rid:
                r = await run_store.get(rid)
                if r:
                    last_run = {"run_id": rid, "at": at,
                                "findings": (r.get("findings_summary") or {}).get("total"),
                                "status": fires[0].get("status")}
                else:
                    last_run = {"run_id": rid, "at": at, "status": fires[0].get("status")}
            else:
                last_run = {"at": at, "status": fires[0].get("status"), "note": fires[0].get("note")}
        node = _node_trig(agent, t.get("trigger_id")) or {}
        out.append({"agent_id": t["agent_id"], "agent": t.get("agent"), "trigger_id": t.get("trigger_id"),
                    "title": t.get("title"), "cron": t.get("cron"), "enabled": t.get("enabled"),
                    "deliver": node.get("deliver") or "chat", "last_fire": t.get("last_fire"),
                    "last_run": last_run})
    return {"schedules": out, "count": len(out)}


def _node_trig(agent: dict, trigger_id: str) -> dict | None:
    for n in ((agent or {}).get("graph") or {}).get("nodes") or []:
        if n.get("kind") == "trigger" and n.get("id") == trigger_id:
            return n.get("trig") or {}
    return None




# ═════════════════════════════════ RUN / STREAM / HITL ═════════════════════════════════════════
# Осиротевший _NOT_IMPL убран 08.10 вместе с последним 501: прогон идёт заданием очереди, HITL
# возвращает задание с чекпоинта, ход прогона отдаётся по SSE.





































# Каталог каналов OUT-узла: объявлен РЯДОМ с кодом, который их исполняет (`_send_channel`), и
# отдаётся интерфейсу. Раньше список жил в вёрстке канвы и разошёлся с рантаймом: предлагался
# YouGile, которого нет на стенде, и отсутствовал Redmine, в который уходит аудит 1С.
# `to` описывает, что именно спрашивать у человека для адреса: у вики это номер книги, у почты
# адрес, у файла имя. Эти подписи тоже были тремя тернарниками в вёрстке.
OUT_CHANNELS = [
    {"id": "redmine",   "label": "Redmine — задача",        "system": "redmine",
     "to": {"label": "проект трекера", "placeholder": "demo", "kind": "text"}},
    {"id": "bookstack", "label": "BookStack — страница вики", "system": "bookstack",
     "to": {"label": "книга BookStack (book_id)", "placeholder": "1", "kind": "int", "field": "book_id"}},
    {"id": "email",     "label": "Почта стенда (Mailpit) — наружу не уходит", "system": "mailpit",
     "to": {"label": "адрес получателя", "placeholder": "glavbuh@demo.local", "kind": "email"}},
    {"id": "yandex",    "label": "Яндекс.Почта — реальная отправка", "system": "mailpit",
     "to": {"label": "адрес получателя", "placeholder": "user@yandex.ru", "kind": "email"}},
    {"id": "yougile",   "label": "YouGile — задача",        "system": "yougile",
     "to": {"label": "колонка YouGile (column_id)", "placeholder": "ID колонки", "kind": "text"}},
    {"id": "pdf",       "label": "PDF-файл",                "system": None,
     "to": {"label": "имя файла", "placeholder": "report", "kind": "text"}},
    {"id": "file",      "label": "Файл",                    "system": None,
     "to": {"label": "имя файла", "placeholder": "report", "kind": "text"}},
]


@app.get("/api/out-channels")
async def out_channels(family: str = "", u: dict = Depends(user)) -> dict:
    """Куда узел вывода может доставить результат — по факту, а не по списку в вёрстке.

    Канал показывается всегда (чтобы было видно, что платформа умеет), но помечается: есть ли его
    система в реестре и открыт ли к ней доступ отделу. Собирать узел на систему, которой нет, —
    та же ошибка, что показывать демо-данные как настоящие.
    """
    known = {s.get("id"): s for s in (await systems_store.all() or [])}
    fam = str(family or "").strip()
    out = []
    for c in OUT_CHANNELS:
        sysid = c.get("system")
        entry = {**c, "registered": True, "allowed": True, "note": ""}
        if sysid:
            sysrec = known.get(sysid)
            entry["registered"] = bool(sysrec)
            if not sysrec:
                entry["allowed"] = False
                entry["note"] = f"системы «{sysid}» нет в реестре — узел соберётся, но доставки не будет"
            elif fam:
                try:
                    chk = await access_check({"family": fam, "system_id": sysid}, u)
                    entry["allowed"] = bool(chk.get("allowed"))
                    if not entry["allowed"]:
                        entry["note"] = str(chk.get("reason") or "отделу закрыт доступ к этой системе")
                except Exception:  # noqa: BLE001 — проверка доступа усиление, а не условие показа
                    pass
        else:
            entry["note"] = "локальный файл, наружу не уходит"
        out.append(entry)
    return {"channels": out, "family": fam}








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



















































@app.get("/api/audit1c/norms/{name}")
async def audit1c_norm(name: str, u: dict = Depends(user)):
    """Текст нормы из экспертного справочника demo/audit1c/norms (markdown) — источник «Чем грозит / Что проверить»."""
    from fastapi.responses import PlainTextResponse
    import re as _re
    if not _re.match(r"^[\w\-]+\.md$", name):
        raise HTTPException(404, "нет такой нормы")
    p = findings.NORMS_DIR / name
    if not p.exists():
        raise HTTPException(404, "нет такой нормы")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")



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





































def _parse_rub(v, default: float) -> float:
    """Число рублей из значения квоты ('4 000 ₽' → 4000). Пусто/мусор → default."""
    if isinstance(v, (int, float)):
        return float(v)
    import re as _re
    digits = _re.sub(r"[^\d]", "", str(v or ""))
    return float(digits) if digits else default


@app.get("/api/billing")
async def billing(u: dict = Depends(user)) -> dict:
    """Реальный биллинг (7.1, БД-фаза): расход ₽/токенов из RunMetrics.cost всех прогонов (реальные
    токены RouteAI × тариф pricing — не хардкод 1240₽/0.42₽/0.14₽) + недельная квота из admin_config.
    ABAC: не-admin видит только прогоны агентов своего отдела."""
    items = await run_store.list_runs(limit=500)
    total_rub = 0.0
    tin = tout = calls = 0
    by_model: dict = {}
    by_run: list = []
    _fam_cache: dict = {}
    for it in items:
        aid = it.get("agent_id")
        if aid not in _fam_cache:
            ag = await agent_store.get(aid) if aid else None
            _fam_cache[aid] = ag or {}
        ag = _fam_cache[aid]
        if not can_see_family(u, ag.get("family")):
            continue
        c = it.get("cost") or {}
        rub = float(c.get("rub") or 0)
        total_rub += rub
        tin += int(c.get("input_tokens") or 0)
        tout += int(c.get("output_tokens") or 0)
        calls += int(c.get("calls") or 0)
        for m, bm in (c.get("by_model") or {}).items():
            agg = by_model.setdefault(m, {"input_tokens": 0, "output_tokens": 0, "rub": 0.0, "calls": 0})
            agg["input_tokens"] += int(bm.get("input_tokens") or 0)
            agg["output_tokens"] += int(bm.get("output_tokens") or 0)
            agg["rub"] = round(agg["rub"] + float(bm.get("rub") or 0), 4)
            agg["calls"] += int(bm.get("calls") or 0)
        if rub > 0 or c.get("calls"):
            by_run.append({"id": it["id"], "agent_name": ag.get("name") or aid,
                           "rub": round(rub, 4), "input_tokens": int(c.get("input_tokens") or 0),
                           "output_tokens": int(c.get("output_tokens") or 0),
                           "when": (it.get("created_at") or "").replace("T", " ")[:16],
                           "by": it.get("started_by")})
    cfg = await admin_store.all()
    quota = _parse_rub(cfg.get("quotaLimit"), 4000.0)
    total_rub = round(total_rub, 4)
    # Токен-квота: на self-host стоимость≈0, но токены списываем с квоты — чтобы пользователь видел
    # ОСТАТОК (сколько ещё может отработать) и это был повод для оптимизации. Лимит из
    # admin_config.tokenQuota (по умолчанию 30M — прежние 5M быстро выжигались, #3).
    tok_quota = int(_parse_rub(cfg.get("tokenQuota"), 30_000_000.0))
    tokens = tin + tout
    return {"spent_rub": total_rub, "quota_limit_rub": quota,
            "quota_pct": min(100, round(total_rub / quota * 100)) if quota else 0,
            "input_tokens": tin, "output_tokens": tout, "tokens": tokens,
            "token_quota": tok_quota, "tokens_remaining": max(0, tok_quota - tokens),
            "tokens_pct": min(100, round(tokens / tok_quota * 100)) if tok_quota else 0,
            "calls": calls, "avg_call_rub": round(total_rub / calls, 4) if calls else 0.0,
            "by_model": by_model, "by_run": by_run[:12], "source": "run_metrics"}






@app.get("/api/hitl/queue")
async def hitl_queue(u: dict = Depends(user)) -> dict:
    """Очередь HITL-подтверждений: pending-заявки на доставку наружу (ABAC по семье агента)."""
    items = await hitl_store.list_pending()
    return {"queue": [i for i in items if can_see_family(u, i.get("family"))]}


@app.get("/api/hitl/{item_id}")
async def hitl_item(item_id: str, u: dict = Depends(user)) -> dict:
    """Одна HITL-заявка с содержимым доставки (html/cfg) — превью перед подтверждением
    (UX-аудит 25.09 D-C3: подтверждали письмо, не видя его текста)."""
    item = await hitl_store.get(item_id)
    if not item:
        raise HTTPException(404, "нет такой HITL-заявки")
    if not can_see_family(u, item.get("family")):
        raise HTTPException(403, "нет доступа к семье заявки")
    payload = item.get("payload") or {}
    cfg = payload.get("cfg") or {}
    cp = payload.get("payload") if payload.get("kind") == "command" and isinstance(payload.get("payload"), dict) else {}
    # Команда в систему (задача трекера, страница вики) шла на подтверждение без текста: html пуст,
    # и человек видел «Содержимое не приложено». Отдаём тело и остальные поля команды — подтверждают
    # ровно то, что уйдёт.
    _body, _fields = "", []
    if cp:
        for _k in ("description", "body", "text", "content", "comment", "message", "html"):
            _v = cp.get(_k)
            if isinstance(_v, str) and _v.strip():
                _body = _v[:20000]
                break
        for _k, _v in cp.items():
            if _k in ("description", "body", "text", "content", "comment", "message", "html"):
                continue
            if isinstance(_v, (str, int, float, bool)) and str(_v).strip():
                _fields.append([_k, str(_v)[:300]])
        _fields = _fields[:12]
    return {"body": _body, "command_fields": _fields,
            "id": item.get("id"), "state": item.get("state"), "agent_id": item.get("agent_id"),
            "agent_name": payload.get("agent_name"), "title": item.get("title"), "channel": item.get("channel"),
            "to": item.get("to_addr"), "format": cfg.get("format") or ("command" if cp else None),
            "subject": cfg.get("subject") or cfg.get("title") or cp.get("subject") or cp.get("title"),
            "html": (payload.get("html") or "")[:20000], "created_at": item.get("created_at"),
            "requested_by": item.get("requested_by"),
            "kind": payload.get("kind") or "report", "system": payload.get("system"), "type": payload.get("type"),
            "command_id": payload.get("command_id"), "result_state": payload.get("result_state"),
            "result": payload.get("result"), "source": payload.get("source")}


@app.post("/api/hitl/{item_id}/approve")
async def hitl_approve(item_id: str, body: dict = None, u: dict = Depends(user)) -> JSONResponse:
    """Одобрить/отклонить HITL-заявку. Тело: {decision: approve|reject, reason?}. При approve —
    выполняется РЕАЛЬНАЯ доставка отчёта в канал OUT-узла. manager+ (analyst — только чтение)."""
    require_level(u, "manager")
    item = await hitl_store.get(item_id)
    if not item:
        raise HTTPException(404, "нет такой HITL-заявки")
    if not can_see_family(u, item.get("family")):
        raise HTTPException(403, "нет доступа к семье заявки")
    if item.get("state") != "pending":
        raise HTTPException(409, f"заявка уже обработана: {item.get('state')}")
    decision = str((body or {}).get("decision", "approve")).lower()
    reason = str((body or {}).get("reason", ""))
    actor = u.get("name") or u.get("sub") or "operator"
    payload = item.get("payload") or {}
    if decision == "reject":
        await hitl_store.decide(item_id, "rejected", actor, reason)
        await audit_store.record(actor, "hitl.reject", item_id,
                                 {"agent_id": item.get("agent_id"), "channel": item.get("channel")}, severity="warn")
        obs.inc("abop_hitl_total", decision="reject")
        resumed = await _resume_job_after_hitl(item_id, payload, "reject")
        return JSONResponse({"id": item_id, "state": "rejected", "resumed_job": resumed})
    # approve: заявка на ЗАПУСК (триггер с HITL-на-создание) → прогон в очередь заданий
    if payload.get("kind") == "spawn":
        ag = await agent_store.get(item.get("agent_id") or "")
        if not ag:
            raise HTTPException(404, "агент заявки не найден")
        job = await run_queue.enqueue(agent_id=ag["id"], actor=actor, kind="run",
                                      payload={"contract_audit_id": ag.get("contract_audit_id"), "use_cache": False,
                                               "user_context": "", "deliver_filter": "",
                                               "trigger": payload.get("trigger_node"), "trace_id": obs.current_trace_id()})
        await run_bus.bus().publish_request(job, obs.current_trace_id())
        await hitl_store.decide(item_id, "approved", actor, reason)
        await audit_store.record(actor, "hitl.approve", item_id, {"agent_id": ag["id"], "channel": "spawn", "job_id": job["id"]})
        obs.inc("abop_hitl_total", decision="approve")
        return JSONResponse({"id": item_id, "state": "approved", "job_id": job["id"], "delivery": "прогон поставлен в очередь"})
    # approve: команда навыка (канал command) → в шину, исполнит коннектор-воркер
    if payload.get("kind") == "command":
        if not getattr(run_bus.bus(), "active", False):
            raise HTTPException(503, "шина не подключена — команду некуда публиковать")
        res = await run_bus.publish_command(str(payload.get("system") or ""), str(payload.get("type") or "command"),
                                            payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                                            actor=actor, trace_id=str(payload.get("trace_id") or obs.current_trace_id()))
        await hitl_store.decide(item_id, "approved", actor, reason)
        await hitl_store.update_payload(item_id, {"command_id": (res.get("command") or {}).get("id"), "topic": res.get("topic")})
        await audit_store.record(actor, "hitl.approve", item_id, {"agent_id": item.get("agent_id"), "channel": "command",
                                                                  "topic": res.get("topic"), "command_id": (res.get("command") or {}).get("id")})
        obs.inc("abop_hitl_total", decision="approve")
        resumed = await _resume_job_after_hitl(item_id, payload, "approve")
        return JSONResponse({"id": item_id, "state": "approved", "delivery": "команда опубликована в " + str(res.get("topic")),
                             "command_id": (res.get("command") or {}).get("id"), "resumed_job": resumed})
    # approve → реальная отправка в канал
    cfg = payload.get("cfg") or {}
    out = await _send_channel(cfg, payload.get("agent_name"), payload.get("html") or "", real=True)
    await hitl_store.decide(item_id, "approved", actor, reason)
    await audit_store.record(actor, "hitl.approve", item_id,
                             {"agent_id": item.get("agent_id"), "channel": item.get("channel"),
                              "result": str(out)[:200]}, severity="info")
    obs.inc("abop_hitl_total", decision="approve")
    obs.log_event("info", "hitl.approved", item_id=item_id, channel=item.get("channel"))
    resumed = await _resume_job_after_hitl(item_id, payload, "approve")
    return JSONResponse({"id": item_id, "state": "approved", "delivery": str(out)[:400], "resumed_job": resumed})


async def _resume_job_after_hitl(item_id: str, payload: dict, decision: str) -> str | None:
    """Если заявка принадлежит заданию очереди, которое ждёт решения (цепочка на HITL-шаге) —
    вернуть его в очередь; воркер продолжит со следующего шага с учётом решения."""
    job = None
    jid = payload.get("job_id")
    if jid:
        job = await run_queue.get(jid)
    if not job or job.get("status") != "awaiting_hitl":
        job = await run_queue.find_awaiting_by_hitl(item_id)
    if not job:
        return None
    await run_queue.resume(job["id"], decision, item_id)
    await run_bus.bus().publish_request(job, obs.current_trace_id())
    obs.log_event("info", "run_queue.resumed", job_id=job["id"], hitl_id=item_id, decision=decision)
    return job["id"]


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
