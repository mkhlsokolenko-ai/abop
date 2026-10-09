# -*- coding: utf-8 -*-
"""Админ-панель: люди и роли из Keycloak, штат из Redmine, аудит ИБ, настройки среды, лимиты.

Пятый домен, вынесенный из монолита (ADR-052, docs/RASKLADKA_SERVERA.md). Домен связный и
замкнутый: ручки читают Keycloak/Redmine и четыре стора, и ни одна другая часть сервиса в них не
заглядывает. Единственное наружу — `nlu_config` (пороги подбора) осталась в монолите: это
настройка ПОДБОРА, а не админки, и её зовут планировщик и NLU.

Главное правило домена сохранено дословно: матрица прав **выводится** из гейтов кода
(`rbac_map.собрать()`), а не задаётся константой. Словесное описание уровня (`POLICY_УРОВНЕЙ`) —
подпись к матрице, не сама матрица: раньше экран отвечал на вопрос «что мы написали про права»
вместо «как они работают».

Зависимости наружу: `server/deps` (кто пользователь и что ему можно) и сторы. `web_api` отсюда
не импортируется.
"""
from __future__ import annotations

import os

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from .. import admin_store, audit_store, cachebus, clients, dlq_store, hitl_store
from .. import observability as obs
from .. import pipeline_store, rbac_map, run_bus, run_queue, run_store, runner
from ..deps import _LEVELS, _role_level, require_level, user

from cli import ape
from .. import agent_store, assembly, audit_store, contract_store, ingress
from ..deps import can_see_family, user
from .agents import _resolve_role
from .agents import _skills_reading_data

router = APIRouter()


# Каталог демо-пакетов считается от КОРНЯ репозитория. При расщеплении монолита этот код
# уехал на уровень глубже (server/ → server/routes/), и `parents[1]` стал указывать в
# server/demo: «Собрать и запустить» молча отвечало «нет демо-пакета». Путь от корня
# пакета, а не от файла, — и тест, который это сторожит.
_DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"




@router.get("/api/admin/llm")
async def admin_llm_get(u: dict = Depends(user)) -> dict:
    """Текущий self-host LLM-эндпоинт (для UI-настройки «эндпоинт бокса»)."""
    return {"llm": clients.llm_active()}


@router.post("/api/admin/llm")
async def admin_llm_set(body: dict, u: dict = Depends(user)) -> dict:
    """1-клик переключение self-host LLM-эндпоинта БЕЗ правки .env/рестарта. Тело:
    {base_url, model?, api_key?}. Сохраняется в admin_config (PG, переживает рестарт) + инжект в clients.
    Пустой base_url → сброс на env. manager+."""
    require_level(u, "manager")
    base_url = str((body or {}).get("base_url", "")).strip()
    cfg = {}
    if base_url:
        cfg = {"base_url": base_url,
               "model": str((body or {}).get("model", "")).strip() or None,
               "api_key": str((body or {}).get("api_key", "")).strip() or None}
        cfg = {k: v for k, v in cfg.items() if v}
    clients.set_llm_override(cfg)
    await admin_store.save("llmOverride", cfg, editor=u.get("name") or u.get("sub") or "dev")
    await cachebus.notify("llm")   # применить на других репликах
    await audit_store.record(u.get("name") or "dev", "admin.llm", "llmOverride",
                             {"base_url": base_url or "(reset to env)"}, severity="info")
    return {"ok": True, "llm": clients.llm_active()}


# ═══════════════ АДМИН-ПАНЕЛЬ: реальные пользователи (Keycloak) и штат (Redmine) ═══════════════

def _kc_base() -> str:
    """Базовый URL Keycloak (без /realms/...), из KEYCLOAK_ADMIN_BASE или ISSUER."""
    base = os.getenv("KEYCLOAK_ADMIN_BASE")
    if base:
        return base.rstrip("/")
    iss = os.getenv("KEYCLOAK_ISSUER") or "http://127.0.0.1:8811/realms/abop"
    return iss.split("/realms/")[0].rstrip("/")


_KC_НЕ_НАСТРОЕН = ("админ-API Keycloak не настроено: не задан KC_ADMIN_PASS. "
                   "Пользователи, роли и штат ведутся в Keycloak — без доступа к нему читать нечего.")


def _kc_configured() -> bool:
    """Настроено ли админ-API Keycloak. READ-ручки при `false` отвечают 200 с пустым телом и
    причиной: 501 «не реализовано» было неправдой — ручка реализована, просто не настроена, и
    браузер писал ошибку в консоль на каждой загрузке."""
    return bool(os.getenv("KC_ADMIN_PASS", ""))


async def _kc_admin_token(client) -> str:
    """Токен админа Keycloak (realm master, client admin-cli) по KC_ADMIN_USER/KC_ADMIN_PASS."""
    usr = os.getenv("KC_ADMIN_USER", "admin")
    pw = os.getenv("KC_ADMIN_PASS", "")
    if not pw:
        raise HTTPException(501, "KC_ADMIN_PASS не задан на сервере (нужен для админ-панели)")
    r = await client.post(_kc_base() + "/realms/master/protocol/openid-connect/token",
                          data={"client_id": "admin-cli", "grant_type": "password",
                                "username": usr, "password": pw})
    if r.status_code != 200:
        raise HTTPException(502, "Keycloak admin: не удалось получить токен")
    return r.json()["access_token"]


@router.get("/api/admin/users")
async def admin_users(u: dict = Depends(user)) -> dict:
    """Реальные пользователи Keycloak realm abop: роли (уровень) + отдел (ABAC). Только admin/support."""
    require_level(u, "support")
    if not _kc_configured():
        return {"users": [], "configured": False, "reason": _KC_НЕ_НАСТРОЕН}
    import httpx
    realm = (os.getenv("KEYCLOAK_ISSUER") or "/realms/abop").split("/realms/")[-1].strip("/") or "abop"
    async with httpx.AsyncClient(timeout=20) as c:
        at = await _kc_admin_token(c)
        h = {"Authorization": "Bearer " + at}
        ru = await c.get(_kc_base() + f"/admin/realms/{realm}/users?max=200", headers=h)
        if ru.status_code != 200:
            raise HTTPException(502, "Keycloak: не удалось получить пользователей")
        out = []
        for usr in ru.json():
            uid = usr.get("id")
            roles = []
            try:
                rr = await c.get(_kc_base() + f"/admin/realms/{realm}/users/{uid}/role-mappings/realm", headers=h)
                roles = [x.get("name") for x in (rr.json() if rr.status_code == 200 else []) if x.get("name")]
            except Exception:  # noqa: BLE001
                pass
            attrs = usr.get("attributes") or {}
            dept = (attrs.get("department") or [None])[0]
            level = _role_level(roles)
            if level in ("admin", "support"):
                dept = "*"
            name = (usr.get("firstName", "") + " " + usr.get("lastName", "")).strip() or usr.get("username")
            out.append({"id": uid, "username": usr.get("username"), "email": usr.get("email"),
                        "name": name, "enabled": usr.get("enabled", True),
                        "roles": roles, "level": level, "department": dept or "—"})
    out.sort(key=lambda x: (_LEVELS.index(x["level"]) * -1, x["username"] or ""))
    return {"users": out, "realm": realm}


@router.get("/api/admin/staff")
async def admin_staff(u: dict = Depends(user)) -> dict:
    """Штатное расписание из Redmine (users API) — источник для ABAC-областей. Только admin/support."""
    require_level(u, "support")
    import httpx
    base = (os.getenv("REDMINE_BASE") or "http://127.0.0.1:3000").rstrip("/")
    key = os.getenv("REDMINE_API_KEY", "")
    if not key:
        return {"staff": [], "note": "REDMINE_API_KEY не задан на сервере"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(base + "/users.json?limit=100&status=1",
                        headers={"X-Redmine-API-Key": key})
        if r.status_code != 200:
            raise HTTPException(502, f"Redmine: HTTP {r.status_code}")
        staff = []
        for x in (r.json().get("users") or []):
            staff.append({"id": x.get("id"), "login": x.get("login"),
                          "name": (x.get("firstname", "") + " " + x.get("lastname", "")).strip(),
                          "mail": x.get("mail"), "created_on": x.get("created_on"),
                          "last_login_on": x.get("last_login_on")})
    return {"staff": staff, "source": "redmine"}


@router.post("/api/admin/cleanup")
async def admin_cleanup(body: dict, u: dict = Depends(user)) -> dict:
    """Убрать следы проверочных прогонов перед показом (admin).

    Стенд, на котором тестировались, и стенд, который показывают заказчику, — разные вещи: журнал с
    сотней проверочных прогонов, очередь чужих превью и тестовые цепочки выглядят как настоящая
    работа и смазывают демо.

    Тело: {what: [runs|jobs|hitl|dlq|pipelines], confirm: "зачистить", dry_run: bool, before?: ISO}.
    По умолчанию **сухой прогон**: возвращает, сколько чего будет удалено, и ничего не трогает.
    Удаление требует `confirm: "зачистить"` — случайный вызов не должен стирать историю.

    Что НЕ трогает ни при каких параметрах: агентов, навыки и шаблоны, данные Data Plane,
    засеянные демо-материалы, журнал аудита ИБ. Чистятся только следы исполнения.
    """
    require_level(u, "admin")
    b = body or {}
    known = ("runs", "jobs", "hitl", "dlq", "pipelines")
    what = [w for w in (b.get("what") or known) if w in known]
    if not what:
        raise HTTPException(422, "нечего чистить: what пуст или содержит неизвестное")
    dry = b.get("dry_run") is not False
    before = str(b.get("before") or "").strip() or None
    if not dry and str(b.get("confirm") or "") != "зачистить":
        raise HTTPException(422, 'удаление требует confirm: "зачистить"')

    plan: dict[str, int] = {}
    if "runs" in what:
        plan["runs"] = len(await run_store.list_runs(limit=100000))
    if "jobs" in what:
        st = await run_queue.stats()
        by = st.get("by_status") or {}
        plan["jobs"] = sum(int(v or 0) for k, v in by.items() if k in ("done", "failed", "cancelled"))
    if "hitl" in what:
        plan["hitl"] = len(await hitl_store.list_pending())
    if "dlq" in what:
        # Сообщение Kafka удалить нельзя — состояние разбора живёт отдельно, по ключу partition:offset.
        # Поэтому считаем и помечаем сами сообщения хвоста DLQ, а не уже существующие отметки.
        _acks = await dlq_store.all()
        _tail = await run_bus.bus().tail(run_bus.TOPIC_DLQ, 200)
        plan["dlq"] = len([m for m in _tail
                           if (_acks.get(dlq_store.key_of(m.get("partition") or 0, m.get("offset") or 0)) or {}).get("state") != "acked"])
    if "pipelines" in what:
        plan["pipelines"] = len(await pipeline_store.all())

    if dry:
        return {"dry_run": True, "что_будет_удалено": plan,
                "подсказка": 'повторите с {"confirm": "зачистить", "dry_run": false}',
                "не_трогаем": ["агентов", "навыки и шаблоны", "данные Data Plane",
                               "засеянные демо-материалы", "журнал аудита"]}

    done: dict[str, int] = {}
    if "runs" in what:
        done["runs"] = await run_store.purge(before=before)
    if "jobs" in what:
        done["jobs"] = await run_queue.purge()
    if "hitl" in what:
        done["hitl"] = await hitl_store.purge(pending_only=True)
    if "dlq" in what:
        acks = await dlq_store.all()
        n = 0
        for m in await run_bus.bus().tail(run_bus.TOPIC_DLQ, 200):
            k = dlq_store.key_of(m.get("partition") or 0, m.get("offset") or 0)
            if (acks.get(k) or {}).get("state") != "acked":
                await dlq_store.mark(k, "acked", u.get("name") or "admin", "зачистка перед показом")
                n += 1
        done["dlq"] = n
    if "pipelines" in what:
        n = 0
        for p in await pipeline_store.all():
            if await pipeline_store.delete(p["id"]):
                n += 1
        done["pipelines"] = n

    await audit_store.record(u.get("name") or u.get("sub") or "dev", "admin.cleanup", ",".join(what), done)
    obs.log_event("warn", "admin.cleanup", **{k: int(v) for k, v in done.items()})
    return {"dry_run": False, "удалено": done,
            "note": "следы прогонов убраны; агенты, навыки, шаблоны и данные на месте"}


@router.get("/api/admin/audit")
async def admin_audit(limit: int = 100, u: dict = Depends(user)) -> dict:
    """Аудит ИБ: неизменяемый лог governance-событий среды (не хардкод). Только admin/support."""
    require_level(u, "support")
    return {"events": await audit_store.list_events(limit=limit), "source": "audit_log"}


# Словесное описание уровня — ПОДПИСЬ к матрице, а не сама матрица: фактические разрешения
# выводятся разбором гейтов require_level в исходнике (server/rbac_map.py). Раньше здесь была
# только эта проза, и экран отвечал на вопрос «что мы написали про права», а не «как они работают».
POLICY_УРОВНЕЙ = {
    "admin":   {"allow": "всё: RBAC, арендаторы, модели, все отделы, деплой в прод",
                "deny": "—"},
    "support": {"allow": "чтение всех отделов, аудит ИБ, штат, эскалации",
                "deny": "правка RBAC, деплой в прод"},
    "manager": {"allow": "сборка/прогон/деплой агентов своего отдела, HITL-подтверждения",
                "deny": "чужие отделы, правка политик безопасности"},
    "analyst": {"allow": "чтение агентов/прогонов/данных своего отдела",
                "deny": "любые мутации (read-only), действия наружу"},
}


@router.get("/api/admin/rbac")
async def admin_rbac(u: dict = Depends(user)) -> dict:
    """RBAC-матрица из РЕАЛЬНЫХ ролей Keycloak: роль → люди + разрешения/запреты (не хардкод-константа)."""
    require_level(u, "support")
    карта0 = rbac_map.собрать()
    гейты = {"levels": list(rbac_map.УРОВНИ), "routes_total": карта0["total"],
             "routes_gated": карта0["total"] - карта0["open"],
             "routes_open": карта0["open"], "derived_from": карта0["source"]}
    if not _kc_configured():
        # Людей без Keycloak не назвать, но ГЕЙТЫ выводятся из кода и доступны всегда —
        # «кто есть в системе» и «что уровню разрешено» это два разных вопроса. Отдаём строки
        # уровней без людей: экран показывает права, а не выдуманный список сотрудников.
        пусто = [{"role": lv, "count": 0, "people": "—", "departments": [],
                  "allow": POLICY_УРОВНЕЙ[lv]["allow"], "deny": POLICY_УРОВНЕЙ[lv]["deny"],
                  "enforced": rbac_map.сводка(lv)} for lv in rbac_map.УРОВНИ]
        return {"rows": пусто, "configured": False, "reason": _KC_НЕ_НАСТРОЕН,
                "editable": False, "enforcement": гейты}
    try:
        data = await admin_users(u)  # переиспользуем живой список Keycloak
    except HTTPException:
        raise
    users = data.get("users", [])
    POLICY = POLICY_УРОВНЕЙ
    rows = []
    for lvl in ("admin", "support", "manager", "analyst"):
        people = [usr["name"] or usr["username"] for usr in users if usr.get("level") == lvl]
        depts = sorted({usr.get("department") for usr in users if usr.get("level") == lvl and usr.get("department") not in (None, "—", "*")})
        pol = POLICY.get(lvl, {"allow": "—", "deny": "—"})
        факт = rbac_map.сводка(lvl)
        rows.append({"role": lvl, "count": len(people), "people": ", ".join(people[:6]) or "нет пользователей",
                     "departments": depts, "allow": pol["allow"], "deny": pol["deny"],
                     "enforced": факт})
    карта = rbac_map.собрать()
    return {"rows": rows, "realm": data.get("realm"), "source": "keycloak",
            # Матрица не редактируется из интерфейса: права живут в ролях Keycloak и в гейтах кода.
            # Кнопка «применить», которая показывала успех и ничего не писала, убрана — см. §7.1
            # docs/ISSLEDOVANIE_VEB_DESKTOP_2026-10-08.md.
            "editable": False,
            "editable_note": "матрица выводится из ролей Keycloak и гейтов кода и здесь не правится: "
                             "роли меняются в Keycloak, гейты — в исходнике",
            "enforcement": {"levels": list(rbac_map.УРОВНИ), "routes_total": карта["total"],
                            "routes_gated": карта["total"] - карта["open"],
                            "routes_open": карта["open"], "derived_from": карта["source"]}}


# ── Админ-конфиг среды (модели/арендаторы/квоты/пороги ИБ) + карта процессов (области) в Postgres — §7.2/БД-фаза ──
# Раньше эти настройки правились в UI и оседали в localStorage браузера (per-браузер, терялись
# при перенакате). Теперь общие для всех операторов и переживают рестарт. См.
# persistence-localstorage-hole. Отсутствующие ключи ⇒ клиент берёт свой дефолт (сид в state).
# tree/assignments — карта процессов (области ответственности агентов), авторится в RBAC-редакторе.
# `tree` и `assignments` отсюда убраны 08.10: карта процессов и посадки живут в собственных
# таблицах (ADR-042/043), и эти ключи больше никто не читает. Оставлять их в белом списке записи
# значило бы держать настройку, которую можно поменять и не получить ничего, — худший вид мёртвого
# интерфейса. Чистка уже записанных значений — `scripts/cleanup_legacy_config.py`.
_ADMIN_CONFIG_KEYS = {"modelCfg", "defaultProfile", "tenantMode", "quotaLimit", "quotaPolicy",
                      "escThresholds", "tokenQuota", "nluConfig", "runLimits"}

@router.get("/api/admin/config")
async def admin_config(u: dict = Depends(user)) -> dict:
    """Сохранённые в Postgres админ-настройки среды (только правки; дефолты — на клиенте)."""
    return {"config": await admin_store.all()}


@router.post("/api/admin/config")
async def admin_config_set(body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить админ-настройку в Postgres (общая для всех операторов, переживает перенакат —
    не localStorage). Тело: {key, value}. key ∈ modelCfg/defaultProfile/tenantMode/quotaLimit/
    quotaPolicy/escThresholds. Правка настроек среды — уровень manager+ (RBAC-гейт)."""
    require_level(u, "manager")
    key = (body or {}).get("key")
    if key not in _ADMIN_CONFIG_KEYS:
        raise HTTPException(422, "неизвестный ключ конфига")
    value = (body or {}).get("value")
    editor = u.get("name") or u.get("sub") or "dev"
    await admin_store.save(key, value, editor=editor)
    await audit_store.record(editor, "admin.config", key, {"key": key})
    return {"key": key, "value": value}


@router.get("/api/admin/run-limits")
async def run_limits_get(u: dict = Depends(user)) -> dict:
    """Действующие лимиты прогона: что задано кодом, что изменено настройкой и в каких границах.

    Раньше это жило только в переменных окружения сервера: человек видел лимит ответа навыка в
    карточке, но изменить его не мог и не знал, откуда взялось число."""
    saved = (await admin_store.all()).get("runLimits") or {}
    saved = saved if isinstance(saved, dict) else {}
    defaults = runner.default_limits()
    return {"limits": runner.effective_limits(saved), "defaults": defaults, "saved": saved,
            "fields": [{"key": k, "label": v[0], "min": v[1], "max": v[2],
                        "default": defaults.get(k), "changed": k in saved}
                       for k, v in runner.LIMIT_FIELDS.items()]}


@router.post("/api/admin/run-limits")
async def run_limits_set(body: dict, u: dict = Depends(user)) -> dict:
    """Изменить лимиты прогона (manager+). Пустое значение поля возвращает его к умолчанию.

    Значение вне границ не отвергается молча, а подрезается: опечатка в поле иначе ломала бы прогоны
    до тех пор, пока кто-нибудь не догадается заглянуть в настройки."""
    require_level(u, "manager")
    cur = (await admin_store.all()).get("runLimits") or {}
    cur = dict(cur) if isinstance(cur, dict) else {}
    patch = (body or {}).get("limits")
    if not isinstance(patch, dict):
        raise HTTPException(422, "нужен объект limits")
    for k, v in patch.items():
        if k not in runner.LIMIT_FIELDS:
            raise HTTPException(422, f"неизвестный лимит «{k}»")
        if v in (None, ""):
            cur.pop(k, None)
            continue
        cur[k] = v
    clean = {k: v for k, v in runner.effective_limits(cur).items() if k in cur}
    editor = u.get("name") or u.get("sub") or "dev"
    await admin_store.save("runLimits", clean, editor=editor)
    await audit_store.record(editor, "admin.run_limits", "runLimits", {"keys": sorted(clean)})
    obs.log_event("info", "run_limits.changed", editor=editor, keys=sorted(clean))
    return await run_limits_get(u)


@router.post("/api/demo/prepare")
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
