"""ABOP Web API — тонкий REST/SSE-слой поверх ядра `ape` для веб-среды.

Принцип PRD v1.1 «единое ядро, много клиентов»: CLI и веб дёргают ОДНИ функции ядра.
Этот модуль НЕ дублирует логику — только выставляет функции `ape` (семьи/навыки/Data Plane/
план/прогон) как HTTP-эндпоинты за Keycloak JWT (тот же realm, что MCP-шлюз).

Запуск (dev): uvicorn server.web_api:app --reload --port 8091
Прод: за Caddy (TLS), рядом с FastMCP-шлюзом. Контракт — docs/ABOP_API.md.

Статус эндпоинтов:
- READ (families/skills/dataplane/plan) — реальные вызовы ядра, готовы.
- RUN/STREAM/HITL — контракт зафиксирован; исполнение прогона через API требует выноса
  cmd_agents в фон + SSE (следующий инкремент), сейчас отдают 501 с формой ответа.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ── ядро: импорт функций `ape` без запуска REPL (верхний уровень чист) ──
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
import ape  # noqa: E402

from . import access, admin_store, agent_store, assembly, audit_store, clients, contract_store, dataplane_store, ingress, layout_store, reglament_store, run_store, runner, skill_store, slava, systems_store, trigger_store, triggers, userdata_store  # noqa: E402

BIZ_FAMILIES = {"analytics", "finance", "credit", "architecture", "management"}


# ── Auth: Keycloak JWT (тот же realm, что MCP). Dev-режим без JWKS. ──
def _jwks_url() -> str:
    return os.getenv("KEYCLOAK_JWKS_URI") or os.getenv("KEYCLOAK_JWKS_INTERNAL") or ""


_jwk_client = None


def _client():
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient
        _jwk_client = PyJWKClient(_jwks_url())
    return _jwk_client


_LEVELS = ["analyst", "manager", "support", "admin"]  # по возрастанию прав (support/admin — сквозной доступ)


def _role_level(roles: list) -> str:
    """Уровень доступа (RBAC) из realm-ролей. admin>support>manager>analyst."""
    for lv in ("admin", "support", "manager", "analyst"):
        if lv in roles:
            return lv
    return "analyst"


def _identity(claims: dict, dev: bool = False) -> dict:
    roles = (claims.get("realm_access") or {}).get("roles") or []
    level = _role_level(roles)
    dept = claims.get("department")
    if isinstance(dept, list):
        dept = dept[0] if dept else None
    # admin/support видят всё (ABAC-область = *), даже если department не задан
    if level in ("admin", "support"):
        dept = "*"
    return {"sub": claims.get("sub", "dev"), "name": claims.get("preferred_username") or claims.get("name") or "dev",
            "roles": roles, "level": level, "department": dept or "*", "dev": dev}


def user(request: Request) -> dict:
    """Текущий пользователь из Bearer-JWT (roles + department → level + ABAC-область).
    Bearer есть → декодируем (JWKS-верификация если настроена, иначе decode для демо-переключателя).
    Bearer нет: без JWKS — dev-admin; с JWKS — 401."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        import jwt
        try:
            if _jwks_url():  # строгая верификация подписи по JWKS
                key = _client().get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=["RS256"],
                                    audience=os.getenv("KEYCLOAK_AUDIENCE") or None,
                                    issuer=os.getenv("KEYCLOAK_ISSUER") or None,
                                    options={"verify_aud": bool(os.getenv("KEYCLOAK_AUDIENCE"))})
            else:  # демо/переключатель: decode без проверки подписи (токен от нашего Keycloak)
                claims = jwt.decode(token, options={"verify_signature": False})
        except Exception:  # noqa: BLE001
            raise HTTPException(401, "невалидный токен")
        return _identity(claims, dev=False)
    if _jwks_url():
        raise HTTPException(401, "нужен Bearer-JWT")
    return _identity({"sub": "dev", "preferred_username": "dev (admin)",
                      "realm_access": {"roles": ["admin"]}, "department": "*"}, dev=True)


def can_see_family(u: dict, family: str) -> bool:
    """ABAC: admin/support (область *) видят все семьи; иначе только свою (department == family)."""
    return u.get("department") in ("*", None) or u.get("department") == family


def require_level(u: dict, minimum: str) -> None:
    """RBAC-гейт мутаций: уровень пользователя ≥ minimum (иначе 403)."""
    if _LEVELS.index(u.get("level", "analyst")) < _LEVELS.index(minimum):
        raise HTTPException(403, f"нужен уровень доступа ≥ {minimum} (у вас {u.get('level')})")


app = FastAPI(title="ABOP Web API", version="0.1.0",
              description="Тонкий REST/SSE поверх ядра ape (среда разработки агентов)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """index.html фронта не кэшировать в браузере (иначе деплой не виден без hard-refresh).
    Браузер ревалидирует по etag/last-modified. Шрифты/бинарники StaticFiles кэшируются как есть."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ═══════════════ READ: реальные вызовы ядра ═══════════════

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "families": len(ape.AGENT_FAMILIES), "skills": len(ape.SKILLS),
            "adapters": sorted(ape.SOURCE_ADAPTERS)}


@app.get("/api/me")
def me(u: dict = Depends(user)) -> dict:
    return {"user": u}


@app.post("/api/auth/login")
async def auth_login(body: dict) -> dict:
    """Прокси-логин к Keycloak (realm abop) — Keycloak не публичный, поэтому вход идёт через ABOP.
    Тело: {username, password}. Возвращает {access_token, user}. Фронт хранит токен и шлёт Bearer."""
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
    return {"access_token": tokens["access_token"], "user": _identity(claims)}


# ═══════════════ АДМИН-ПАНЕЛЬ: реальные пользователи (Keycloak) и штат (Redmine) ═══════════════

def _kc_base() -> str:
    """Базовый URL Keycloak (без /realms/...), из KEYCLOAK_ADMIN_BASE или ISSUER."""
    base = os.getenv("KEYCLOAK_ADMIN_BASE")
    if base:
        return base.rstrip("/")
    iss = os.getenv("KEYCLOAK_ISSUER") or "http://127.0.0.1:8811/realms/abop"
    return iss.split("/realms/")[0].rstrip("/")


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


@app.get("/api/admin/users")
async def admin_users(u: dict = Depends(user)) -> dict:
    """Реальные пользователи Keycloak realm abop: роли (уровень) + отдел (ABAC). Только admin/support."""
    require_level(u, "support")
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


@app.get("/api/admin/staff")
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


@app.get("/api/admin/audit")
async def admin_audit(limit: int = 100, u: dict = Depends(user)) -> dict:
    """Аудит ИБ: неизменяемый лог governance-событий среды (не хардкод). Только admin/support."""
    require_level(u, "support")
    return {"events": await audit_store.list_events(limit=limit), "source": "audit_log"}


@app.get("/api/admin/rbac")
async def admin_rbac(u: dict = Depends(user)) -> dict:
    """RBAC-матрица из РЕАЛЬНЫХ ролей Keycloak: роль → люди + разрешения/запреты (не хардкод-константа)."""
    require_level(u, "support")
    try:
        data = await admin_users(u)  # переиспользуем живой список Keycloak
    except HTTPException:
        raise
    users = data.get("users", [])
    # разрешения по уровню (ADR-013/014): что уровень может в среде
    POLICY = {
        "admin":   {"allow": "всё: RBAC, арендаторы, модели, все отделы, деплой в прод",
                    "deny": "—"},
        "support": {"allow": "чтение всех отделов, аудит ИБ, штат, эскалации",
                    "deny": "правка RBAC, деплой в прод"},
        "manager": {"allow": "сборка/прогон/деплой агентов своего отдела, HITL-подтверждения",
                    "deny": "чужие отделы, правка политик безопасности"},
        "analyst": {"allow": "чтение агентов/прогонов/данных своего отдела",
                    "deny": "любые мутации (read-only), действия наружу"},
    }
    rows = []
    for lvl in ("admin", "support", "manager", "analyst"):
        people = [usr["name"] or usr["username"] for usr in users if usr.get("level") == lvl]
        depts = sorted({usr.get("department") for usr in users if usr.get("level") == lvl and usr.get("department") not in (None, "—", "*")})
        pol = POLICY.get(lvl, {"allow": "—", "deny": "—"})
        rows.append({"role": lvl, "count": len(people), "people": ", ".join(people[:6]) or "нет пользователей",
                     "departments": depts, "allow": pol["allow"], "deny": pol["deny"]})
    return {"rows": rows, "realm": data.get("realm"), "source": "keycloak"}


# ── Админ-конфиг среды (модели/арендаторы/квоты/пороги ИБ) + карта процессов (области) в Postgres — §7.2/БД-фаза ──
# Раньше эти настройки правились в UI и оседали в localStorage браузера (per-браузер, терялись
# при перенакате). Теперь общие для всех операторов и переживают рестарт. См.
# persistence-localstorage-hole. Отсутствующие ключи ⇒ клиент берёт свой дефолт (сид в state).
# tree/assignments — карта процессов (области ответственности агентов), авторится в RBAC-редакторе.
_ADMIN_CONFIG_KEYS = {"modelCfg", "defaultProfile", "tenantMode", "quotaLimit", "quotaPolicy",
                      "escThresholds", "tree", "assignments"}


@app.get("/api/admin/config")
async def admin_config(u: dict = Depends(user)) -> dict:
    """Сохранённые в Postgres админ-настройки среды (только правки; дефолты — на клиенте)."""
    return {"config": await admin_store.all()}


@app.post("/api/admin/config")
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


@app.get("/api/me/scenarios")
async def my_scenarios(u: dict = Depends(user)) -> dict:
    """Черновики агентов ТЕКУЩЕГО пользователя (per-user, ключ = JWT sub) из Postgres — не localStorage,
    не видны другим. §БД-фаза (persistence-localstorage-hole)."""
    return {"scenarios": await userdata_store.get_scenarios(u.get("sub") or "")}


@app.post("/api/me/scenarios")
async def my_scenarios_save(body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить черновики агентов текущего пользователя в Postgres. Тело: {scenarios:{key:scenario}}."""
    data = (body or {}).get("scenarios")
    if not isinstance(data, dict):
        raise HTTPException(422, "нужен объект scenarios")
    saved = await userdata_store.save_scenarios(u.get("sub") or "", data)
    return {"scenarios": saved}


@app.get("/api/systems")
async def systems_list(u: dict = Depends(user)) -> dict:
    """Реестр систем/подключений (эндпоинты REST/БД/вектор + Kafka-топики) из Postgres. Единый каталог,
    на который ссылаются коннекторы/рецепты/триггеры (system_id + путь/топик), а не хардкод URL.
    Секреты НЕ отдаём — только auth_ref (имя переменной). Каждой системе проставляем `allowed` —
    доступна ли текущему отделу (ABAC-scope, изоляция агентов на MCP-шлюзе). §БД-фаза."""
    dept = u.get("department")
    out = []
    for s in await systems_store.all():
        s = dict(s)
        s["allowed"] = systems_store.allowed_for(s, dept)
        out.append(s)
    return {"systems": out, "department": dept}


@app.get("/api/systems/{sid}")
async def system_get(sid: str, u: dict = Depends(user)) -> dict:
    s = await systems_store.get(sid)
    if not s:
        raise HTTPException(404, "нет такой системы")
    return s


@app.post("/api/systems/{sid}")
async def system_save(sid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить систему в реестр (эндпоинт/топики/auth_ref/egress). Тело: {kind, base_url, brokers[],
    topics[], auth_ref, tenant, egress, note}. Правка интеграций — уровень manager+ (RBAC-гейт)."""
    require_level(u, "manager")
    saved = await systems_store.save(sid, body or {}, editor=u.get("name") or u.get("sub") or "dev")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "system.save", sid,
                             {"kind": saved.get("kind"), "egress": saved.get("egress")})
    return saved


@app.delete("/api/systems/{sid}")
async def system_delete(sid: str, u: dict = Depends(user)) -> dict:
    require_level(u, "manager")
    await systems_store.delete(sid)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "system.delete", sid, {})
    return {"id": sid, "deleted": True}


def _cosine(a: list, b: list) -> float:
    import math
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _parse_json_array(text: str) -> list:
    import json as _json
    import re as _re
    t = (text or "").strip()
    m = _re.search(r"\[.*\]", t, _re.S)
    if m:
        t = m.group(0)
    try:
        return _json.loads(t)
    except Exception:  # noqa: BLE001
        return []


@app.post("/api/reglament/ingest")
async def reglament_ingest(body: dict, u: dict = Depends(user)) -> dict:
    """Загрузить регламент и РАЗМЕТИТЬ чанки ЛЛМ (RouteAI DeepSeek v4): каждому — ключ НСИ [P][SS][OOO]
    + метки процесс/подпроцесс/операция; посчитать эмбеддинг (BGE-M3) и сохранить в Postgres.
    Тело: {text, tenant?, replace?}. §регламент-конформанс (reglament-conformance-slava)."""
    require_level(u, "manager")
    text = str((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(422, "нужен text регламента")
    tenant = str((body or {}).get("tenant") or "default").strip() or "default"
    import re as _re
    raw = [c.strip() for c in _re.split(r"\n\s*\n", text) if len(c.strip()) >= 15]
    if not raw:
        raw = [c.strip() for c in text.split("\n") if len(c.strip()) >= 15]
    raw = raw[:40]
    if not raw:
        raise HTTPException(422, "не удалось выделить фрагменты")
    prompt = (
        "Ты размечаешь регламент бизнес-процесса ключами НСИ. Для КАЖДОГО фрагмента присвой "
        "иерархический ключ вида [P][SS][OOO] (P=номер процесса 1..9, SS=подпроцесс 01..99, "
        "OOO=операция 001..999; связанные фрагменты — общий процесс/подпроцесс) и краткие метки. "
        "Верни СТРОГО JSON-массив без пояснений: "
        "[{\"i\":0,\"nsi_key\":\"[1][01][001]\",\"process\":\"...\",\"subprocess\":\"...\",\"op\":\"...\"}].\n\n"
        + "\n".join(f"[{i}] {c[:300]}" for i, c in enumerate(raw)))
    try:
        # deepseek-v4-pro — reasoning-модель: даём запас max_tokens, иначе «мышление» съедает бюджет → пустой content
        resp = await clients.chat(messages=[{"role": "user", "content": prompt}],
                                  model="deepseek/deepseek-v4-pro", max_tokens=4000)
        marks = _parse_json_array(resp.get("text") or "")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"LLM-разметка недоступна: {ex}")
    by_i = {int(m["i"]): m for m in marks if isinstance(m, dict) and m.get("i") is not None}
    embs = await clients.embed(raw)
    if (body or {}).get("replace"):
        await reglament_store.clear(tenant)
    editor = u.get("name") or u.get("sub") or "dev"
    saved = []
    for i, (chunk, emb) in enumerate(zip(raw, embs)):
        m = by_i.get(i, {})
        key = str(m.get("nsi_key") or f"[9][99][{i + 1:03d}]")
        await reglament_store.save_chunk(tenant, key, m.get("process") or "—", m.get("subprocess") or "—",
                                         m.get("op") or chunk[:60], chunk, emb, editor=editor)
        saved.append({"nsi_key": key, "op": m.get("op") or chunk[:60], "process": m.get("process") or "—"})
    await audit_store.record(editor, "reglament.ingest", tenant, {"chunks": len(saved), "model": resp.get("model")})
    return {"tenant": tenant, "chunks": saved, "count": len(saved), "markup_model": resp.get("model")}


@app.get("/api/family/collections")
async def family_collections(u: dict = Depends(user)) -> dict:
    """Коллекции sLAVA (в т.ч. slava_fam_<family>) + доступность текущему отделу (ABAC)."""
    try:
        cols = await slava.collections()
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"sLAVA недоступна: {ex}")
    return {"collections": cols, "department": u.get("department")}


@app.post("/api/family/ingest")
async def family_ingest(body: dict, u: dict = Depends(user)) -> dict:
    """Загрузить знание/регламент в корпус СЕМЬИ (slava_fam_<family>) через sLAVA. ABAC: отдел = семья
    (или admin/support). Тело: {family, text, filename?, replace?}. §graph-RAG по семьям."""
    require_level(u, "manager")
    family = str((body or {}).get("family") or "").strip()
    text = str((body or {}).get("text") or "").strip()
    if not family or not text:
        raise HTTPException(422, "нужны family и text")
    if not access.can_reach_family(u.get("department"), family):
        await access.audit_denial(u.get("name") or u.get("sub") or "dev", u.get("department"),
                                  slava.fam_collection(family), "family.ingest", "отдел ≠ семья")
        raise HTTPException(403, f"нет доступа к корпусу семьи «{family}» (изоляция знания)")
    col = slava.fam_collection(family)
    try:
        res = await slava.ingest(col, str((body or {}).get("filename") or f"{family}.txt"), text,
                                 tenant="abop", replace=bool((body or {}).get("replace")))
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"sLAVA ingest не удался: {ex}")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.ingest", col, {"family": family})
    return {"family": family, "collection": col, "result": res}


@app.post("/api/family/query")
async def family_query(body: dict, u: dict = Depends(user)) -> dict:
    """RAG-поиск по корпусу СЕМЬИ (slava_fam_<family>) через sLAVA. ABAC-изоляция: отдел = семья
    (аналитик не читает корпус архитектуры). Тело: {family, query, top_k?}."""
    family = str((body or {}).get("family") or "").strip()
    q = str((body or {}).get("query") or "").strip()
    if not family or not q:
        raise HTTPException(422, "нужны family и query")
    if not access.can_reach_family(u.get("department"), family):
        await access.audit_denial(u.get("name") or u.get("sub") or "dev", u.get("department"),
                                  slava.fam_collection(family), "family.query", "отдел ≠ семья")
        raise HTTPException(403, f"нет доступа к корпусу семьи «{family}» (изоляция знания)")
    col = slava.fam_collection(family)
    try:
        res = await slava.query(col, q, top_k=int((body or {}).get("top_k") or 5), tenant="abop")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"sLAVA query не удался: {ex}")
    return {"family": family, "collection": col, "result": res}


def _nsi_parts(key: str) -> list:
    import re as _re
    return _re.findall(r"\[([^\]]+)\]", key or "")


@app.post("/api/reglament/graph/build")
async def reglament_graph_build(body: dict, u: dict = Depends(user)) -> dict:
    """Построить граф-слой регламента: (1) рёбра ИЕРАРХИИ НСИ (contains: процесс→подпроцесс→операция,
    детерминированно из ключа), (2) КРОСС-ЦИТИРОВАНИЕ норм через RouteAI DeepSeek v4 (нарушение одной
    нормы влечёт проверку связанной / обязательное действие — уточнённые декларации). §граф-слой."""
    require_level(u, "manager")
    tenant = str((body or {}).get("tenant") or "default").strip() or "default"
    chunks = await reglament_store.all_for(tenant)
    if not chunks:
        raise HTTPException(422, f"нет регламента для tenant «{tenant}» — сначала /api/reglament/ingest")
    await reglament_store.clear_edges(tenant)
    # (1) иерархия НСИ: в группе по [P][SS] родитель = чанк с минимальным [OOO], остальные — contains
    groups: dict = {}
    for c in chunks:
        p = _nsi_parts(c["nsi_key"])
        pref = "".join(f"[{x}]" for x in p[:2]) if len(p) >= 2 else c["nsi_key"]
        groups.setdefault(pref, []).append(c["nsi_key"])
    hier = 0
    for pref, keys in groups.items():
        keys = sorted(keys)
        parent = keys[0]
        for k in keys[1:]:
            await reglament_store.save_edge(tenant, parent, k, "contains", "иерархия НСИ")
            hier += 1
    # (2) кросс-цитирование норм (DeepSeek v4)
    listing = "\n".join(f"{c['nsi_key']} — {c['op']}: {c['text'][:160]}" for c in chunks)
    prompt = (
        "Ниже операции/нормы регламента (ключ НСИ — текст). Определи КРОСС-ССЫЛКИ: нарушение или "
        "выполнение одной нормы ВЛЕЧЁТ проверку связанной ИЛИ обязательное действие (напр. уточнённая "
        "декларация по НДС/прибыли, проверка договора). Верни СТРОГО JSON-массив без пояснений: "
        "[{\"from\":\"[1][01][002]\",\"to\":\"[1][01][005]\",\"relation\":\"entails\",\"note\":\"почему\"}]. "
        "relation: entails (связанная норма, to=ключ) | action (обязательное действие, to=\"\", note=действие).\n\n"
        + listing)
    xcite = 0
    model = ""
    try:
        resp = await clients.chat(messages=[{"role": "user", "content": prompt}],
                                  model="deepseek/deepseek-v4-pro", max_tokens=8000)  # reasoning + граф-вывод
        model = resp.get("model", "")
        for e in _parse_json_array(resp.get("text") or ""):
            if isinstance(e, dict) and e.get("from"):
                await reglament_store.save_edge(tenant, str(e["from"]), str(e.get("to") or ""),
                                                str(e.get("relation") or "entails"), str(e.get("note") or "")[:200])
                xcite += 1
    except Exception as ex:  # noqa: BLE001 — кросс-цитирование опционально
        model = f"(LLM недоступен: {ex})"
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "reglament.graph", tenant,
                             {"hierarchy": hier, "xcite": xcite})
    return {"tenant": tenant, "nodes": len(chunks), "hierarchy_edges": hier, "xcite_edges": xcite, "markup_model": model}


@app.get("/api/process/graph")
async def process_graph(tenant: str = "default", u: dict = Depends(user)) -> dict:
    """Граф-слой регламента: узлы (операции/нормы с ключом НСИ) + рёбра (contains-иерархия +
    entails/action-кросс-цитирование норм). Для карты и трассировки последствий. §граф-слой."""
    chunks = await reglament_store.all_for(tenant)
    edges = await reglament_store.edges_for(tenant)
    nodes = [{"nsi_key": c["nsi_key"], "op": c["op"], "process": c["process"], "subprocess": c["subprocess"]}
             for c in chunks]
    return {"tenant": tenant, "nodes": nodes, "edges": edges,
            "summary": {"nodes": len(nodes), "contains": sum(1 for e in edges if e["relation"] == "contains"),
                        "entails": sum(1 for e in edges if e["relation"] == "entails"),
                        "action": sum(1 for e in edges if e["relation"] == "action")}}


@app.get("/api/reglament")
async def reglament_list(tenant: str = "default", u: dict = Depends(user)) -> dict:
    """Размеченный регламент (чанки + ключи НСИ, без эмбеддингов)."""
    reg = await reglament_store.all_for(tenant)
    return {"tenant": tenant, "chunks": [{"nsi_key": r["nsi_key"], "process": r["process"],
            "subprocess": r["subprocess"], "op": r["op"], "text": r["text"]} for r in reg]}


@app.post("/api/process/conformance")
async def process_conformance(body: dict, u: dict = Depends(user)) -> dict:
    """Сверка собранного процесса ABOP с регламентом по ключу НСИ: структурный drift (нет в регламенте /
    не собрано) + смысловой (cosine эмбеддинга операции ABOP vs чанк регламента, порог). §регламент-
    конформанс. Тело: {ops:[{nsi_key,label}], tenant?}."""
    ops = [o for o in ((body or {}).get("ops") or []) if o.get("nsi_key")]
    tenant = str((body or {}).get("tenant") or "default").strip() or "default"
    # sLAVA-режим: сверка против РЕАЛЬНОГО регламент-корпуса (slava_reglament_ru/audit1c_norms) —
    # для каждой операции ABOP vector-поиск в sLAVA → лучший чанк+score → статус. Прямое направление
    # (ABOP-операция vs регламент); «не собрано» тут не считаем (корпус 22k чанков не перечислить).
    if str((body or {}).get("source") or "pg").strip() == "slava":
        collection = str((body or {}).get("collection") or "slava_reglament_ru").strip()
        report = []
        for o in ops:
            label = o.get("label") or ""
            try:
                srcs = (await slava.query(collection, label, top_k=1, tenant="abop")).get("sources") or []
            except Exception:  # noqa: BLE001
                srcs = []
            if not srcs:
                report.append({"nsi_key": o.get("nsi_key"), "label": label, "status": "нет в регламенте", "sim": None})
            else:
                sc = float(srcs[0].get("score") or 0)
                status = "соответствует" if sc >= 0.7 else ("расходится по сути" if sc >= 0.5 else "сильно расходится")
                report.append({"nsi_key": o.get("nsi_key"), "label": label, "status": status,
                               "sim": round(sc, 3), "reglament_op": (srcs[0].get("text") or "")[:90]})
        summary = {"total": len(report), "ok": sum(1 for x in report if x["status"] == "соответствует"),
                   "drift": sum(1 for x in report if x["status"] != "соответствует")}
        return {"collection": collection, "source": "slava", "report": report, "summary": summary}
    reg = await reglament_store.all_for(tenant)
    reg_by_key: dict = {}
    for r in reg:
        reg_by_key.setdefault(r["nsi_key"], r)
    op_embs = await clients.embed([o.get("label") or "" for o in ops]) if ops else []
    report, seen = [], set()
    for o, emb in zip(ops, op_embs):
        k = o.get("nsi_key")
        seen.add(k)
        r = reg_by_key.get(k)
        if not r:
            report.append({"nsi_key": k, "label": o.get("label"), "status": "нет в регламенте", "sim": None})
        else:
            sim = _cosine(emb, r.get("embedding") or [])
            status = "соответствует" if sim >= 0.75 else ("расходится по сути" if sim >= 0.5 else "сильно расходится")
            report.append({"nsi_key": k, "label": o.get("label"), "reglament_op": r.get("op"),
                           "status": status, "sim": round(sim, 3)})
    for k, r in reg_by_key.items():
        if k not in seen:
            report.append({"nsi_key": k, "label": None, "reglament_op": r.get("op"),
                           "status": "не собрано (есть в регламенте)", "sim": None})
    summary = {"total": len(report), "ok": sum(1 for x in report if x["status"] == "соответствует"),
               "drift": sum(1 for x in report if x["status"] != "соответствует")}
    return {"tenant": tenant, "report": report, "summary": summary}


@app.get("/api/access/manifest")
async def access_manifest(department: str = "", family: str = "", u: dict = Depends(user)) -> dict:
    """Least-privilege манифест области (отдел/семья): доступные/закрытые системы реестра + Qdrant-тенант.
    Основа гейта агентов на MCP-шлюзе (agent-rbac-mcp-gateway). Без параметров — область текущего юзера."""
    key = access.scope_key(department=department or None, family=family or None) if (department or family) \
        else access.scope_key(department=u.get("department"))
    return await access.manifest(key)


@app.post("/api/access/check")
async def access_check(body: dict, u: dict = Depends(user)) -> dict:
    """Проверка права доступа: {department|family, system_id} → {allowed, reason}. Отказ пишется в аудит."""
    b = body or {}
    key = access.scope_key(department=b.get("department"), family=b.get("family"))
    system = await systems_store.get(str(b.get("system_id") or ""))
    ok, reason = access.can_reach_system(key, system or {})
    if not ok:
        await access.audit_denial(u.get("name") or u.get("sub") or "dev", key,
                                  str(b.get("system_id") or "?"), "check", reason)
    return {"scope": key, "system_id": b.get("system_id"), "allowed": ok, "reason": reason,
            "qdrant_tenant": access.qdrant_tenant(key)}


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


@app.get("/api/memory/{scope}")
async def memory_get(scope: str, u: dict = Depends(user)) -> dict:
    """Память агента/процесса (per-АГЕНТ, ОБЩАЯ для всех — не пер-юзер, не localStorage). scope =
    сценарий/процесс. None ⇒ клиент берёт свой сид. §БД-фаза (persistence-localstorage-hole)."""
    mem = await userdata_store.get_memory(scope)
    return {"scope": scope, "memory": mem, "seeded": mem is not None}


@app.post("/api/memory/{scope}")
async def memory_save(scope: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить память агента/процесса в Postgres (общая для всех операторов). Тело: {memory:[...]}."""
    require_level(u, "manager")
    data = (body or {}).get("memory")
    if not isinstance(data, list):
        raise HTTPException(422, "нужен массив memory")
    saved = await userdata_store.save_memory(scope, data)
    return {"scope": scope, "memory": saved}


@app.get("/api/families")
def families(u: dict = Depends(user)) -> dict:
    """Ростер Семья→Роль→Навык (для палитры канвы и каталога). §4/§6 ABOP_SCREENS."""
    out = []
    for fid, fam in ape.AGENT_FAMILIES.items():
        out.append({
            "id": fid, "title": fam["title"], "profile": fam["profile"],
            "mission": fam["mission"], "kind": "business" if fid in BIZ_FAMILIES else "engineering",
            "members": [{"key": mk, "title": mt, "skills": sk} for mk, (mt, sk) in fam["members"].items()],
        })
    return {"families": out}


@app.get("/api/agents/spec")
def agent_spec(family: str, member: str = "", u: dict = Depends(user)) -> dict:
    """Спека агента (ADR-032): роль семьи + навыки + become-переходы + конверт + data-scope
    (резолв рецептов по entity). §6 авторинг узла-агента на канве."""
    try:
        return ape.build_agent_spec(family, member)
    except ValueError as ex:
        raise HTTPException(404, str(ex))


async def _refresh_skill_ds_cache() -> None:
    """Подтянуть data-need оверрайды из Postgres и инжектнуть в ape (build_agent_spec/lineage/assembly)."""
    try:
        ape.set_skill_ds_overrides(await skill_store.datasources_map())
    except Exception:  # noqa: BLE001
        pass


def _skill_families() -> dict:
    in_fam: dict = {}
    for fid, fam in ape.AGENT_FAMILIES.items():
        for _mk, (_mt, sk) in fam["members"].items():
            for s in sk:
                in_fam.setdefault(s, []).append(fid)
    return {k: sorted(set(v)) for k, v in in_fam.items()}


# поля навыка, которые оператор правит в UI и которые оверрайдятся из Postgres (skill_store.patch)
_SKILL_TEXT_FIELDS = ("title", "short", "flow", "when", "method", "dod", "anti")
_SKILL_SAFETY_FIELDS = ("mode", "egress", "cite")


def _skill_base_card(sid: str, fam: dict) -> dict:
    title, short, _instr = ape.SKILLS[sid]
    parsed = ape.parse_skill_md(sid)   # секции/шаги — чтобы drawer не был пустым
    return {"id": sid, "title": title, "short": short,
            "safety": ape.skill_safety(sid), "scope": ape.skill_scope(sid),
            "families": fam.get(sid, []),
            "sections": parsed["sections"], "flow": parsed["flow"],
            "when": parsed["when"], "method": parsed["method"],
            "dod": parsed["dod"], "anti": parsed["anti"],
            "datasources": ape.skill_datasources_resolved(sid)}


def _overlay_skill(card: dict, ov: dict | None) -> dict:
    """Наложить сохранённые в Postgres правки навыка (общие для всех) поверх базового каталога (.md).
    Governance-безопасность узла берётся АВТОРИТЕТНО из базового каталога (assembly), а не из правок."""
    if ov:
        patch = ov.get("patch") or {}
        for k in _SKILL_TEXT_FIELDS:
            if k in patch and patch[k]:
                card[k] = patch[k]
        saf = dict(card.get("safety") or {})
        for k in _SKILL_SAFETY_FIELDS:
            if k in patch:
                saf[k] = patch[k]
        card["safety"] = saf
        card["version"] = ov.get("version")
        card["editor"] = ov.get("editor")
        card["edited_at"] = ov.get("updated_at")
    else:
        card["version"] = "v1.0"
        card["editor"] = None
        card["edited_at"] = None
    return card


@app.get("/api/skills")
async def skills(u: dict = Depends(user)) -> dict:
    """Каталог навыков (.md) + сохранённые в Postgres правки (общие для всех, переживают перенакат). §4."""
    fam = _skill_families()
    overrides = await skill_store.all()
    out = [_overlay_skill(_skill_base_card(sid, fam), overrides.get(sid)) for sid in ape.SKILLS]
    return {"skills": out, "count": len(out)}


@app.get("/api/skills/{sid}")
async def skill(sid: str, u: dict = Depends(user)) -> dict:
    """Полное тело навыка (progressive disclosure = use_skill) + PG-правки. §4 drawer."""
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    card = _overlay_skill(_skill_base_card(sid, _skill_families()), await skill_store.get(sid))
    parsed = ape.parse_skill_md(sid)
    card["body"] = ape.load_skill_body(sid)
    card["intro"] = parsed["intro"]
    return card


@app.post("/api/skills/{sid}")
async def skill_save(sid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить правки навыка НОВОЙ версией в Postgres (общие для всех пользователей, переживают
    перенакат/рестарт — не localStorage). Редактор = текущий пользователь. Тело: {title?, short?,
    mode?, egress?, cite?, flow?, when?, method?, dod?, anti?}. §БД-фаза (persistence-localstorage-hole)."""
    require_level(u, "manager")  # analyst — только чтение
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    allowed = set(_SKILL_TEXT_FIELDS) | set(_SKILL_SAFETY_FIELDS)
    patch = {k: v for k, v in (body or {}).items() if k in allowed}
    if not patch:
        raise HTTPException(422, "нет полей для сохранения")
    editor = u.get("name") or u.get("sub") or "dev"
    ov = await skill_store.save_patch(sid, patch, editor=editor)
    await audit_store.record(editor, "skill.save", sid,
                             {"version": ov.get("version"), "fields": sorted(patch.keys())})
    return _overlay_skill(_skill_base_card(sid, _skill_families()), ov)


@app.post("/api/skills/{sid}/datasources")
async def skill_datasources_set(sid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Оператор правит data-need навыка (ADR-032): какие сущности/поля берёт навык → Postgres. §4 drawer.
    Тело: {datasources:[{entity, fields[], kind?, note?}]}. Возвращает резолвнутые (с рецептом по entity)."""
    require_level(u, "manager")
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    norm = ape.normalize_skill_datasources((body or {}).get("datasources") or [])
    editor = u.get("name") or u.get("sub") or "dev"
    await skill_store.save_datasources(sid, norm, editor=editor)
    await _refresh_skill_ds_cache()   # чтобы build_agent_spec/lineage сразу видели новые источники
    await audit_store.record(editor, "skill.datasources", sid, {"count": len(norm)})
    return {"id": sid, "datasources": ape.skill_datasources_resolved(sid)}


@app.get("/api/data/lineage")
def data_lineage(u: dict = Depends(user)) -> dict:
    """Карта Data Plane: сущность → рецепты(наполняют) → навыки(потребляют) → роли/агенты. §5 «Карта»."""
    return ape.data_lineage()


@app.get("/api/data/adapters")
def adapters(u: dict = Depends(user)) -> dict:
    """Каталог адаптеров с дескрипторами (label/category/src_fields/egress/badge/available)
    + сущности canonical. §5 селекты редактора строятся из этого (расширяем, не переделываем)."""
    return {"adapters": sorted(ape.SOURCE_ADAPTERS), "catalog": ape.adapter_catalog(),
            "entities": sorted(ape.CANONICAL_SCHEMAS)}


@app.get("/api/data/schema/{entity}")
def data_schema(entity: str, u: dict = Depends(user)) -> dict:
    """Data Contract сущности (обязательные поля). §5 редактор рецепта."""
    return ape.data_schema(entity)


async def _refresh_dataplane_cache() -> None:
    """Подтянуть рецепты/коннекторы из Postgres и инжектнуть в ape (общие для всех, переживают перенакат)."""
    try:
        ape.set_recipe_store(await dataplane_store.recipes_all())
        ape.set_connector_store(await dataplane_store.connectors_all())
    except Exception:  # noqa: BLE001
        pass


async def _backfill_dataplane_from_files() -> None:
    """Одноразовый перенос существующих файловых рецептов/коннекторов (~/.ape) в Postgres, чтобы
    демо-данные не исчезли при переходе на PG. Читает файлы, пока ape ещё в файловом режиме (стор
    не инжектнут); выполняется только если в PG соответствующая таблица пуста."""
    try:
        if not await dataplane_store.recipes_all():
            for name in ape.data_recipes():          # файловый режим
                try:
                    await dataplane_store.save_recipe(name, ape.data_load_recipe(name), editor="migrate")
                except Exception:  # noqa: BLE001
                    pass
        if not await dataplane_store.connectors_all():
            for c in ape.data_connectors():           # файловый режим
                if c.get("id"):
                    await dataplane_store.save_connector(c["id"], c, editor="migrate")
    except Exception:  # noqa: BLE001
        pass


# ── Коннекторы-инстансы (подключённые источники) — §5 таб «Коннекторы» ──
@app.get("/api/data/connectors")
async def connectors_list(u: dict = Depends(user)) -> dict:
    """Коннекторы + (если привязан system_id) резолв эндпоинта из реестра систем и флаг `allowed`
    для текущего отдела (ABAC). §5 таб «Коннекторы»."""
    key = access.scope_key(department=u.get("department"))
    out = []
    for c in ape.data_connectors():
        c = dict(c)
        sid = c.get("system_id")
        if sid:
            sysrec = await systems_store.get(sid)
            if sysrec:
                c["system"] = {"id": sid, "kind": sysrec["kind"], "egress": sysrec["egress"], "scope": sysrec.get("scope") or []}
                ok, reason = access.can_reach_system(key, sysrec)
                c["allowed"] = ok
                c["access_reason"] = reason
        out.append(c)
    return {"connectors": out}


@app.post("/api/data/connectors")
async def connector_save(body: dict, u: dict = Depends(user)) -> dict:
    """Подключить коннектор (сохранить инстанс источника в Postgres). §5 [＋ подключить]. Если задан
    system_id — эндпоинт/egress берутся из реестра систем (не хардкод URL); ABAC-гейт по scope."""
    require_level(u, "manager")
    if not str((body or {}).get("title", "")).strip():
        raise HTTPException(422, "нужен title коннектора")
    card = ape.build_connector(body)
    # slava/vector-коннектор = KNOWLEDGE-источник (не canonical): храним корпус (collection/family/top_k)
    if card.get("adapter") in ("slava", "vector"):
        fam = str((body or {}).get("family", "")).strip()
        col = str((body or {}).get("collection", "")).strip() or (slava.fam_collection(fam) if fam else "")
        card["corpus"] = {"collection": col, "family": fam or None, "top_k": int((body or {}).get("top_k") or 4)}
        card["kind_role"] = "knowledge"
    sid = str((body or {}).get("system_id", "")).strip()
    if sid:
        sysrec = await systems_store.get(sid)
        if not sysrec:
            raise HTTPException(422, f"нет системы «{sid}» в реестре")
        card["system_id"] = sid
        # эндпоинт из реестра: base_url + относительный путь (если target не задан явно)
        path = str((body or {}).get("path", "")).strip()
        if not card.get("target") or path:
            card["target"] = (sysrec.get("base_url") or "").rstrip("/") + ("/" + path.lstrip("/") if path else "")
        card["egress"] = sysrec.get("egress")
    editor = u.get("name") or u.get("sub") or "dev"
    await dataplane_store.save_connector(card["id"], card, editor=editor)
    await _refresh_dataplane_cache()
    await audit_store.record(editor, "data.connector", card["id"],
                             {"adapter": card.get("adapter"), "system_id": card.get("system_id")})
    return card


@app.post("/api/data/connectors/test")
def connector_test(body: dict, u: dict = Depends(user)) -> dict:
    """Тест-прогон коннектора: читает несколько строк источника (без записи). §5 [Тест-прогон]."""
    return ape.data_test_connector(body)


# ── Рецепты (источник → canonical) — §5 таб «Рецепты» ──
@app.get("/api/data/recipes")
def recipes_list(u: dict = Depends(user)) -> dict:
    return {"recipes": ape.data_recipes_cards()}


@app.get("/api/data/recipes/{name}")
def recipe_get(name: str, u: dict = Depends(user)) -> dict:
    try:
        return ape.data_load_recipe(name)
    except (OSError, ValueError):
        raise HTTPException(404, "нет рецепта")


@app.post("/api/data/recipes")
async def recipe_save(body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить рецепт (нормализует UI-форму в canonical) в Postgres. §5 [Сохранить]."""
    require_level(u, "manager")
    name = str((body or {}).get("recipe") or (body or {}).get("title", "")).strip()
    if not name:
        raise HTTPException(422, "нужно имя рецепта")
    try:
        r = ape.normalize_recipe(name, body)
    except Exception as ex:  # noqa: BLE001 — любой сбой нормализации → 400, не 500
        raise HTTPException(400, f"не удалось сохранить рецепт: {ex}")
    editor = u.get("name") or u.get("sub") or "dev"
    await dataplane_store.save_recipe(r["recipe"], r, editor=editor)
    await _refresh_dataplane_cache()
    await audit_store.record(editor, "data.recipe", r["recipe"], {"entity": r.get("entity")})
    return r


@app.post("/api/data/recipe/preview")
def recipe_preview(body: dict, u: dict = Depends(user)) -> dict:
    """Dry-run рецепта на выборке БЕЗ записи. §5 [Проверить] → предпросмотр canonical + счётчики."""
    try:
        limit = int((body or {}).get("limit", 20) or 20)
        return ape.data_preview(body, limit=limit)
    except Exception as ex:  # noqa: BLE001 — сбой адаптера/нормализации/ввода → 400 с текстом, НИКОГДА не 500
        raise HTTPException(400, str(ex))


@app.post("/api/data/recipes/{name}/run")
def recipe_run(name: str, u: dict = Depends(user)) -> dict:
    """Применить сохранённый рецепт и записать в canonical store. §5 публикация."""
    try:
        entity, written, dropped, invalid = ape.data_run(name)
    except Exception as ex:  # noqa: BLE001 — сбой источника/рецепта → 400
        raise HTTPException(400, str(ex))
    return {"entity": entity, "written": written, "dropped": dropped, "invalid": invalid}


@app.post("/api/data/recipes/{name}/rebind")
async def recipe_rebind(name: str, body: dict, u: dict = Depends(user)) -> dict:
    """Перепривязка рецепта на графе «Данные»: сменить целевую сущность (entity) и опц. запустить.
    Тело: {entity: <новая сущность>, run?: bool}. Меняет recipe.entity+emit.schema, сохраняет,
    при run=true сразу пишет в canonical store. Питает drag-перепривязку в графе Data Plane."""
    require_level(u, "manager")  # перепривязка = мутация Data Plane (analyst read-only)
    entity = str((body or {}).get("entity", "")).strip()
    if not entity:
        raise HTTPException(422, "нужна целевая entity")
    if entity not in ape.CANONICAL_SCHEMAS:
        raise HTTPException(422, f"неизвестная сущность {entity!r} (нет в canonical schemas)")
    try:
        r = ape.data_load_recipe(name)
    except (OSError, ValueError):
        raise HTTPException(404, "нет рецепта")
    r["entity"] = entity
    r.setdefault("emit", {})["schema"] = entity
    saved = ape.normalize_recipe(name, r)
    editor = u.get("name") or u.get("sub") or "dev"
    await dataplane_store.save_recipe(saved["recipe"], saved, editor=editor)
    await _refresh_dataplane_cache()   # чтобы data_run ниже прочитал перепривязанный рецепт
    out = {"recipe": saved.get("recipe"), "entity": entity, "rebound": True}
    await audit_store.record(editor, "data.rebind", saved.get("recipe"), {"entity": entity})
    if (body or {}).get("run"):
        try:
            ent, written, dropped, invalid = ape.data_run(saved.get("recipe"))
            out.update({"ran": True, "written": written, "dropped": dropped, "invalid": invalid})
        except Exception as ex:  # noqa: BLE001 — перепривязка удалась, прогон нет → сообщаем
            out.update({"ran": False, "run_error": str(ex)})
    return out


@app.get("/api/data/query/{entity}")
def data_query(entity: str, limit: int = 30, u: dict = Depends(user)) -> dict:
    """Чтение canonical store (только свежие). §5 предпросмотр."""
    return {"entity": entity, "records": ape.data_query(entity, limit=limit)}


# ── Раскладка канвы (координаты узлов) в Postgres, НЕ localStorage (директива владельца) ──
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


@app.post("/api/plan")
def plan(body: dict, u: dict = Depends(user)) -> dict:
    """Превью декомпозиции цели по семьям (детерминированно, без LLM). §9.1 запуск."""
    goal = str((body or {}).get("goal", "")).strip()
    if not goal:
        raise HTTPException(422, "нужна goal")
    fams = ape.detect_families(goal)
    waves = [[{"task": f"{goal} — аспект: {ape.AGENT_FAMILIES[f]['title']}", "family": f}
              for f in fams]] if len(fams) >= 2 else [[{"task": goal, "family": ape.route_family(goal)}]]
    return {"goal": goal, "families": fams, "preview_waves": waves,
            "note": "предварительный план (эвристика); финальный план строит LLM-планировщик при запуске"}


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
    await trigger_store.init()
    await reglament_store.init()
    import asyncio as _asyncio
    _asyncio.create_task(triggers.scheduler_loop(execute_agent_run))  # фоновый планировщик триггеров
    await _refresh_skill_ds_cache()  # инжект data-need оверрайдов из PG в ape
    await dataplane_store.init()
    await _backfill_dataplane_from_files()  # одноразовый перенос ~/.ape → PG (сохранить демо-рецепты)
    await _refresh_dataplane_cache()  # инжект рецептов/коннекторов из PG в ape


@app.post("/api/contracts/ingest")
async def contracts_ingest(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Принять handoff-бандл LUDA: валидация схем luda.*/1.0 → сохранение ContractSet.

    Тело — сам бандл {capability_request, deployment_contract, baseline_measurement,
    evidence_pack}. Невалидный → 422 со списком ошибок (SDD §4.3, не доверяем вслепую).
    """
    res = ingress.validate(body)
    if not res.accepted:
        return JSONResponse(
            {"accepted": False, "errors": res.errors, "warnings": res.warnings},
            status_code=422,
        )
    saved = await contract_store.save(body, res.intake, ingested_by=u.get("name") or u.get("sub") or "dev")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "contract.ingest", saved["audit_id"],
                             {"family": res.intake.get("family"), "autonomy_ceiling": res.intake.get("autonomy_ceiling"),
                              "skills": len(res.intake.get("skills") or [])})
    return JSONResponse({
        "accepted": True, "warnings": res.warnings,
        "audit_id": saved["audit_id"], "intake": res.intake,
        "ingested_at": saved.get("ingested_at"),
    }, status_code=201)


@app.get("/api/contracts")
async def contracts_list(u: dict = Depends(user)) -> dict:
    """Список принятых ContractSet (краткие карточки). §4/§6 — источник для канвы."""
    return {"contracts": await contract_store.list_all()}


@app.get("/api/contracts/{audit_id}")
async def contracts_get(audit_id: str, u: dict = Depends(user)) -> dict:
    """Полный ContractSet по audit_id (бандл + intake) — для посева канвы."""
    cs = await contract_store.get(audit_id)
    if not cs:
        raise HTTPException(404, "нет такого ContractSet")
    return cs


# ═══════════════ AGENTS: сборка агента на канве → AgentVersion (ADR-013/014, SDD §4.4) ═══════════════

@app.post("/api/agents")
async def agent_save(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Сохранить собранный на канве граф как AgentVersion (draft), привязав к ContractSet.

    Тело: {name, contract_audit_id, graph:{nodes[],edges[]}}. До сохранения — governance:
    автономия узла ≤ потолок (ADR-013), внешнее действие под HITL (ADR-014), покрытие (SDD §4.4).
    Жёсткие нарушения → 422; сохранение только чистого графа.
    """
    name = str((body or {}).get("name", "")).strip()
    audit_id = str((body or {}).get("contract_audit_id", "")).strip()
    graph = (body or {}).get("graph") or {}
    if not name or not audit_id:
        raise HTTPException(422, "нужны name и contract_audit_id")
    cs = await contract_store.get(audit_id)
    if not cs:
        raise HTTPException(404, "нет ContractSet для привязки")

    check = assembly.check_graph(graph, cs.get("intake") or {}, ape.skill_safety)
    if check["errors"]:
        return JSONResponse({"saved": False, "errors": check["errors"],
                             "warnings": check["warnings"]}, status_code=422)

    # семья/роль — из контракта LUDA (intake), чтобы агент корректно отражался в «Агентах»,
    # проходил ABAC (can_see_family) и обогащал журнал/Флот (ADR-032).
    intake = cs.get("intake") or {}
    fam = intake.get("family") or ""
    role = ""
    if fam in ape.AGENT_FAMILIES:
        _members = list(ape.AGENT_FAMILIES[fam]["members"].keys())
        if len(_members) == 1:
            role = _members[0]
    # ADR-024: обычное сохранение/автосейв ПЕРЕЗАПИСЫВАЕТ draft (не плодит версии).
    # Новая версия — только при осознанном Пересмотре (revise=true) поверх НЕ-draft.
    revise = bool((body or {}).get("revise"))
    if revise:
        version = await agent_store.next_version(audit_id)
        saved = await agent_store.save(name=name, audit_id=audit_id, version=version, graph=graph,
                                       autonomy_max=check["autonomy_max"],
                                       created_by=u.get("name") or u.get("sub") or "dev",
                                       family=fam, role=role)
    else:
        saved = await agent_store.save_draft(name=name, audit_id=audit_id, graph=graph,
                                             autonomy_max=check["autonomy_max"],
                                             created_by=u.get("name") or u.get("sub") or "dev",
                                             family=fam, role=role)
    version = saved.get("version")
    await audit_store.record(u.get("name") or u.get("sub") or "dev",
                             "agent.revise" if revise else "agent.save", saved["id"],
                             {"family": fam, "role": role, "autonomy_max": check["autonomy_max"],
                              "hitl": check["hitl_count"]})
    return JSONResponse({"saved": True, "id": saved["id"], "version": version, "status": "draft",
                         "family": fam, "role": role,
                         "autonomy_max": check["autonomy_max"], "hitl_count": check["hitl_count"],
                         "warnings": check["warnings"]}, status_code=201)


@app.get("/api/agents")
async def agents_list(contract: str = "", archived: bool = False, u: dict = Depends(user)) -> dict:
    """Список AgentVersion. ABAC: пользователь видит только агентов своего отдела (family==department);
    admin/support (область *) — всех. archived=1 → Лимб (retired). §7/§8а."""
    items = await agent_store.list_for(contract or None, archived=archived)
    return {"agents": [a for a in items if can_see_family(u, a.get("family"))]}


@app.post("/api/agents/{agent_id}/retire")
async def agent_retire(agent_id: str, u: dict = Depends(user)) -> dict:
    """Вывести агента из эксплуатации → Лимб (архив, status=retired; ADR-024). Обратимо через restore."""
    require_level(u, "manager")  # analyst — только чтение
    a = await agent_store.set_status(agent_id, "retired")
    if not a:
        raise HTTPException(404, "нет такого агента")
    return {"id": agent_id, "status": "retired", "archived": True}


@app.post("/api/agents/{agent_id}/restore")
async def agent_restore(agent_id: str, u: dict = Depends(user)) -> dict:
    """Вернуть агента из Лимба обратно в draft (ADR-024)."""
    require_level(u, "manager")
    a = await agent_store.set_status(agent_id, "draft")
    if not a:
        raise HTTPException(404, "нет такого агента")
    return {"id": agent_id, "status": "draft", "archived": False}


@app.delete("/api/agents/{agent_id}")
async def agent_delete(agent_id: str, u: dict = Depends(user)) -> dict:
    """Полное удаление агента (жёсткое, минуя Лимб). Для черновиков/ошибочных сборок."""
    require_level(u, "manager")
    ok = await agent_store.delete(agent_id)
    if not ok:
        raise HTTPException(404, "нет такого агента")
    return {"id": agent_id, "deleted": True}


@app.post("/api/agents/check")
async def agent_check(body: dict, u: dict = Depends(user)) -> dict:
    """Dry-run governance-проверка графа против контракта БЕЗ сохранения (ADR-013/014, SDD §4.4).

    Тело: {contract_audit_id, graph}. Возвращает {ok, errors[], warnings[], autonomy_max, hitl_count}.
    Питает кнопку «Проверить» в канве настоящим вердиктом конверта.
    """
    audit_id = str((body or {}).get("contract_audit_id", "")).strip()
    graph = (body or {}).get("graph") or {}
    cs = await contract_store.get(audit_id)
    if not cs:
        raise HTTPException(404, "нет ContractSet для проверки")
    check = assembly.check_graph(graph, cs.get("intake") or {}, ape.skill_safety)
    return {"ok": not check["errors"], "errors": check["errors"], "warnings": check["warnings"],
            "autonomy_max": check["autonomy_max"], "hitl_count": check["hitl_count"]}


@app.post("/api/agents/author")
async def agent_author(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Сохранить агента, собранного авторингом БЕЗ контракта (ADR-024 draft, ADR-032):
    роль семьи + навыки → AgentVersion. Конверт/автономия — из навыков (консервативно);
    прод-развёртывание всё равно потребует контракт LUDA (ADR-029). Тело: {family, member, skills?, name?}."""
    require_level(u, "manager")  # сборка агента — не для analyst (read-only)
    family = str((body or {}).get("family", "")).strip()
    member = str((body or {}).get("member", "")).strip()
    if family and not can_see_family(u, family):  # ABAC: только свой отдел (кроме admin/support)
        raise HTTPException(403, f"нельзя собирать агента вне своего отдела ({u.get('department')})")
    skills = (body or {}).get("skills")
    if not family:
        raise HTTPException(422, "нужна family")
    try:
        spec = ape.build_agent_spec(family, member, skills)
    except ValueError as ex:
        raise HTTPException(404, str(ex))
    env = spec["envelope"]
    nodes = [{"id": s["id"], "kind": "skill", "skill": s["id"], "autonomy": env["autonomy_max"],
              "hitl": s["safety"]["mode"] == "action"} for s in spec["skills"]]
    graph = {"nodes": nodes, "edges": []}
    name = str((body or {}).get("name", "")).strip() or f"{spec['family_title']} · {spec['role_title']}"
    audit_id = "authored"
    version = await agent_store.next_version(audit_id)
    saved = await agent_store.save(name=name, audit_id=audit_id, version=version, graph=graph,
                                   autonomy_max=env["autonomy_max"], created_by=u.get("name") or "dev",
                                   family=family, role=spec["role"], transitions=spec["transitions"],
                                   source="authored")
    await audit_store.record(u.get("name") or "dev", "agent.author", saved["id"],
                             {"family": family, "role": spec["role"], "autonomy_max": env["autonomy_max"]})
    return JSONResponse({"saved": True, "id": saved["id"], "version": version, "status": "draft",
                         "family": family, "role": spec["role"], "autonomy_max": env["autonomy_max"],
                         "skills": [s["id"] for s in spec["skills"]], "data_scope": spec["data_scope"]},
                        status_code=201)


@app.get("/api/agents/{agent_id}")
async def agent_get(agent_id: str, u: dict = Depends(user)) -> dict:
    """Полный AgentVersion (граф + метаданные) — для паспорта/повторного открытия в канве."""
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого AgentVersion")
    return a


# ═══════════════ RUN / STREAM / HITL: контракт (исполнение — следующий инкремент) ═══════════════

_NOT_IMPL = ("Прогон через API требует выноса cmd_agents в фон + SSE-стрим (следующий инкремент). "
             "Форма ответа зафиксирована в docs/ABOP_API.md; сейчас — 501.")


async def _gate_agent_data(agent: dict, fam_key: str, actor: str) -> tuple[set, list]:
    """ABAC на пути ДАННЫХ: сущности агента, чьи наполняющие рецепты привязаны к системам (system_id)
    вне scope семьи, — закрываем. Возвращает (blocked_entities, data_denied[]). Отказы → аудит."""
    ents = set()
    for n in (agent.get("graph") or {}).get("nodes") or []:
        if n.get("kind") == "skill":
            sid = n.get("skill") or n.get("title")
            for ds in (ape.skill_datasources_resolved(sid) or []):
                if ds.get("entity"):
                    ents.add(ds["entity"])
    ent_sys: dict = {}
    for r in await dataplane_store.recipes_all():
        e, sid = r.get("entity"), r.get("system_id")
        if e and sid:
            ent_sys.setdefault(e, set()).add(sid)
    blocked, denied = set(), []
    for e in ents:
        sids = ent_sys.get(e) or set()
        if not sids:
            continue  # сущность не привязана к системе реестра (локально/публично) — не гейтим
        allowed_any = False
        for sid in sids:
            ok, _r = access.can_reach_system(fam_key, await systems_store.get(sid) or {})
            allowed_any = allowed_any or ok
        if not allowed_any:
            blocked.add(e)
            denied.append({"entity": e, "systems": sorted(sids)})
            await access.audit_denial(actor, fam_key, ",".join(sorted(sids)), "data:" + e, "система сущности вне scope")
    return blocked, denied


def _agent_knowledge_fn(agent: dict, actor: str):
    """Собрать knowledge_fn для прогона: RAG-запрос к корпусу семьи (sLAVA) с ABAC-гейтом. Корпус —
    из knowledge-source узла графа (node.corpus{collection|family,top_k}); нет узла ⇒ None (без RAG)."""
    corpus_col = corpus_fam = None
    top_k = 4
    fam = agent.get("family")
    for n in (agent.get("graph") or {}).get("nodes") or []:
        if n.get("kind") in ("source", "doc"):
            c = n.get("corpus") or {}
            if c.get("collection") or c.get("family"):
                corpus_fam = c.get("family")
                corpus_col = c.get("collection") or slava.fam_collection(c.get("family"))
                top_k = int(c.get("top_k") or 4)
                break
    # фолбэк: если source-узла с corpus нет в сохранённом графе — берём корпус СЕМЬИ агента (slava_fam_<family>)
    if not corpus_col and fam and fam != "*":
        corpus_col = slava.fam_collection(fam)
        corpus_fam = fam
    if not corpus_col:
        return None
    fam_key = access.scope_key(family=fam)
    # ABAC: корпус привязан к СЕМЬЕ и семья агента её не достигает → знание закрыто (аналитик ≠ архитектура)
    if corpus_fam and fam != "*" and not access.can_reach_family(fam, corpus_fam):
        async def _denied(sid, entities):
            await access.audit_denial(actor, fam_key, corpus_col, "knowledge", "семья без доступа к корпусу")
            return []
        return _denied

    async def _kfn(sid, entities, hint: str = ""):
        # запрос из методики навыка (домен-релевантный) + сущности — иначе sLAVA отсекает по релевантности
        q = ((hint or "")[:400] + " " + " ".join(entities)).strip() or f"нормы для {sid}"
        try:
            res = await slava.query(corpus_col, q, top_k=top_k, tenant="abop")
            return [s.get("text", "") for s in (res.get("sources") or []) if s.get("text")]
        except Exception:  # noqa: BLE001 — корпус недоступен → без RAG, прогон не падает
            return []
    return _kfn


async def execute_agent_run(agent: dict, contract: dict, started_by: str, *, trigger: dict | None = None) -> dict:
    """Ядро прогона (LLM-раскладка + находки audit1c + сохранение + аудит). Переиспользуется
    POST /api/runs и планировщиком триггеров (server/triggers.py). Возвращает {saved, result}."""
    fam_key = access.scope_key(family=agent.get("family"))
    blocked, data_denied = await _gate_agent_data(agent, fam_key, started_by)  # ABAC на данных
    result = await runner.run_live(agent, contract, ape.skill_safety,
                                   data_query=ape.data_query,
                                   skill_sources=ape.skill_datasources_resolved,
                                   load_body=ape.load_skill_body,
                                   chat_fn=clients.chat, blocked_entities=blocked,
                                   knowledge_fn=_agent_knowledge_fn(agent, started_by))
    # Петля прогон→канва: для аудит-агента доносим СТРУКТУРИРОВАННЫЕ находки (детерминир. движок, не LLM).
    _skills = [n.get("skill") for n in (agent.get("graph") or {}).get("nodes", [])]
    if "audit1c-checks" in _skills:
        try:
            _g = ape.audit1c_build_graph()
            _findings = ape.audit1c_run_checks(_g)
            result["findings"] = _findings
            result["findings_summary"] = {
                "total": len(_findings),
                "by_class": {c: sum(1 for f in _findings if f.get("класс") == c) for c in ("A", "B", "C", "D")},
            }
        except Exception as ex:  # noqa: BLE001 — находки опциональны, прогон не падает
            result["findings"] = []
            result["findings_error"] = f"{type(ex).__name__}: {ex}"
    # Обогащение находок НОРМАМИ из корпуса семьи (sLAVA): запрос ПО ТЕКСТУ находки (специфичный →
    # sLAVA-retrieval срабатывает, в отличие от generic per-skill). Так объяснение получает реальную норму.
    kfn = _agent_knowledge_fn(agent, started_by)
    if kfn and isinstance(result.get("findings"), list):
        enriched = 0
        for f in result["findings"][:10]:
            if not isinstance(f, dict):
                continue
            q = " ".join(str(f.get(k) or "") for k in ("проверка", "описание")).strip()
            if not q:
                continue
            try:
                norms = await kfn("audit1c-explain", [], q)
            except Exception:  # noqa: BLE001
                norms = []
            if norms:
                f["нормы_rag"] = norms[:2]
                enriched += 1
        result["norms_enriched"] = enriched
    result["started_by"] = started_by
    # Least-privilege манифест агента (ABAC): какие системы реестра доступны его семье, Qdrant-тенант,
    # и какие сущности закрыты на пути данных (система вне scope).
    try:
        result["access"] = await access.manifest(fam_key)
        if data_denied:
            result["access"]["data_denied"] = data_denied
    except Exception:  # noqa: BLE001 — манифест опционален
        pass
    if trigger:  # прогон запущен триггером — фиксируем происхождение (наблюдаемость цепочек)
        result["trigger"] = {"id": trigger.get("id"), "type": (trigger.get("trig") or {}).get("type"),
                             "title": trigger.get("title")}
    saved = await run_store.save(result)
    _v = result.get("verdict") or {}
    await audit_store.record(started_by, "agent.run", saved["id"],
                             {"agent_id": agent.get("id"), "verdict_ok": bool(_v.get("ok")),
                              "autonomy_used": _v.get("autonomy_used"),
                              "findings": (result.get("findings_summary") or {}).get("total"),
                              "trigger": (trigger or {}).get("id")},
                             severity=("info" if _v.get("ok") else "warn"))
    return {"saved": saved, "result": result}


@app.post("/api/runs")
async def run_start(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Запуск прогона агента. Тело: {agent_id} ИЛИ {contract_audit_id}. Возвращает 201."""
    agent_id = str((body or {}).get("agent_id", "")).strip()
    audit_id = str((body or {}).get("contract_audit_id", "")).strip()
    if not agent_id and audit_id:
        lst = await agent_store.list_for(audit_id)
        if not lst:
            raise HTTPException(404, "нет сохранённого агента для контракта")
        agent_id = lst[0]["id"]
    agent = await agent_store.get(agent_id) if agent_id else None
    if not agent:
        raise HTTPException(404, "нет такого AgentVersion")
    contract = await contract_store.get(agent.get("contract_audit_id") or audit_id)
    if not contract:
        contract = {"intake": {"autonomy_ceiling": agent.get("autonomy_max") or "A2"}, "bundle": {}}
    out = await execute_agent_run(agent, contract, u.get("name") or u.get("sub") or "dev")
    return JSONResponse({"run_id": out["saved"]["id"], **out["result"]}, status_code=201)


@app.get("/api/runs")
async def runs_list(agent_id: str = "", u: dict = Depends(user)) -> dict:
    """Журнал прогонов (последние 100). ABAC: не-admin видит только прогоны агентов своего отдела.
    Обогащается именем/семьёй агента для отображения во Флоте/Обзоре."""
    items = await run_store.list_runs(agent_id=agent_id or None, limit=100)
    _fam_cache: dict = {}
    out = []
    for it in items:
        aid = it.get("agent_id")
        if aid not in _fam_cache:
            ag = await agent_store.get(aid) if aid else None
            _fam_cache[aid] = ag or {}
        ag = _fam_cache[aid]
        fam = ag.get("family")
        if not can_see_family(u, fam):
            continue
        it["agent_name"] = ag.get("name") or aid
        it["family"] = fam
        it["version"] = ag.get("version")
        it["role"] = ag.get("role")
        out.append(it)
    return {"runs": out}


@app.get("/api/runs/{run_id}")
async def run_get(run_id: str, u: dict = Depends(user)) -> dict:
    """Полная запись прогона (волны, доска, вердикт, run_metrics)."""
    r = await run_store.get(run_id)
    if not r:
        raise HTTPException(404, "нет такого прогона")
    return r


@app.get("/api/runs/{run_id}/metrics")
async def run_metrics(run_id: str, u: dict = Depends(user)) -> dict:
    """RunMetrics прогона (abop.run_metrics/1.0) — обратная петля к LUDA (SDD §4-bis)."""
    r = await run_store.get(run_id)
    if not r:
        raise HTTPException(404, "нет такого прогона")
    return r.get("run_metrics") or {}


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
    return {"spent_rub": total_rub, "quota_limit_rub": quota,
            "quota_pct": min(100, round(total_rub / quota * 100)) if quota else 0,
            "input_tokens": tin, "output_tokens": tout, "tokens": tin + tout,
            "calls": calls, "avg_call_rub": round(total_rub / calls, 4) if calls else 0.0,
            "by_model": by_model, "by_run": by_run[:12], "source": "run_metrics"}


@app.get("/api/runs/{run_id}/stream", status_code=501)
def run_stream(run_id: str, u: dict = Depends(user)) -> JSONResponse:
    """SSE-стрим прогона. Контракт-события: wave_start/agent_step/board_event/handoff/
    audit_verdict/hitl_request/run_done. Сейчас 501."""
    return JSONResponse({"detail": _NOT_IMPL,
                         "sse_events": ["wave_start", "agent_step", "board_event", "handoff",
                                        "audit_verdict", "hitl_request", "run_done"]},
                        status_code=501)


@app.get("/api/hitl/queue")
def hitl_queue(u: dict = Depends(user)) -> dict:
    """Очередь HITL-подтверждений (action-навыки в dry_run). §3 HITL-полоса, §9.2."""
    return {"queue": [], "note": "наполняется при исполнении прогонов через API (инкремент run/stream)"}


@app.post("/api/hitl/{item_id}/approve", status_code=501)
def hitl_approve(item_id: str, body: dict = None, u: dict = Depends(user)) -> JSONResponse:
    """Одобрить/отклонить действие. Контракт: {decision: approve|reject, reason?}. Сейчас 501."""
    return JSONResponse({"detail": _NOT_IMPL,
                         "contract": {"request": {"decision": "approve|reject", "reason": "str?"}}},
                        status_code=501)


# ═══════════════ Статика: buildless-React фронт ABOP (webapp/) ═══════════════
# Монтируется ПОСЛЕ всех /api-роутов, чтобы они имели приоритет. html=True → SPA-fallback.
_WEBAPP = Path(__file__).resolve().parents[1] / "webapp"
if _WEBAPP.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEBAPP), html=True), name="webapp")
