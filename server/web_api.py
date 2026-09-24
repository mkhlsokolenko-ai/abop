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
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# ── ядро: импорт функций `ape` без запуска REPL (верхний уровень чист) ──
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
import ape  # noqa: E402

from . import access, admin_store, agent_store, assembly, audit_store, cachebus, clients, contract_store, dataplane_store, families_store, hitl_store, identity_store, ingress, langfuse_trace, layout_store, observability as obs, reglament_store, run_cache_store, run_store, runner, skill_store, slava, systems_store, trigger_store, triggers, userdata_store  # noqa: E402
from .config import settings  # noqa: E402

BIZ_FAMILIES = {"analytics", "finance", "credit", "architecture", "management"}


# ── Auth: Keycloak JWT. Dev-режим без JWKS. Multi-issuer: смычка с desktop (realm ai-product-engineer)
# требует принимать JWT ДВУХ realm — свой `abop` (demo-логин ABOP) и desktop-realm — оба ВЕРИФИЦИРУЯ
# по своему JWKS. ABOP_EXTRA_JWKS = "<issuer>|<jwks_url>,..." добавляет доп. доверенные issuer.
# См. docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 0, auth-фундамент).
def _jwks_url() -> str:
    return os.getenv("KEYCLOAK_JWKS_URI") or os.getenv("KEYCLOAK_JWKS_INTERNAL") or ""


def _extra_issuers() -> dict:
    """{issuer: jwks_url} доп. доверенных realm (напр. desktop ai-product-engineer)."""
    out = {}
    for pair in (os.getenv("ABOP_EXTRA_JWKS", "") or "").split(","):
        pair = pair.strip()
        if "|" in pair:
            iss, jwks = pair.split("|", 1)
            out[iss.strip()] = jwks.strip()
    return out


_jwk_clients: dict = {}   # jwks_url -> PyJWKClient (кэш по URL, чтобы не плодить клиентов)


def _client(jwks_url: str | None = None):
    from jwt import PyJWKClient
    url = jwks_url or _jwks_url()
    cli = _jwk_clients.get(url)
    if cli is None:
        cli = PyJWKClient(url)
        _jwk_clients[url] = cli
    return cli


# ── Кэш верифицированных JWT: verify (RS256) один раз, дальше запросы того же токена — из кэша ──
# Ключ = хэш токена (не храним сырой токен и не привязываем к юзеру → чужим токеном не прокатиться).
# TTL = min(остаток жизни токена, потолок) — НИКОГДА не отдаём после exp. Первый запрос токена всё
# равно полностью верифицирует подпись; в кэш попадает только успех. Потолок мал → окно на ротацию/
# отзыв ключей крошечное (revocation-list нет → паритет с текущим «принимаем до exp»). Per-worker.
import hashlib as _hashlib
import time as _t
_JWT_CACHE: dict = {}                                   # tokhash -> (identity, deadline_ts)
_JWT_CACHE_TTL = max(5, int(os.getenv("ABOP_JWT_CACHE_TTL", "60")))
_JWT_CACHE_MAX = max(256, int(os.getenv("ABOP_JWT_CACHE_MAX", "5000")))


def _jwt_cache_get(token: str):
    h = _hashlib.sha256(token.encode()).hexdigest()
    hit = _JWT_CACHE.get(h)
    if hit and _t.time() < hit[1]:                      # не отдаём после дедлайна (учитывает exp)
        return hit[0]
    if hit:
        _JWT_CACHE.pop(h, None)                         # протухло — выкидываем
    return None


def _jwt_cache_put(token: str, identity: dict, exp) -> None:
    if len(_JWT_CACHE) >= _JWT_CACHE_MAX:               # bounded: не растём бесконечно
        _JWT_CACHE.clear()
    deadline = _t.time() + _JWT_CACHE_TTL
    try:
        if exp:                                         # не пережить exp токена
            deadline = min(deadline, float(exp))
    except (TypeError, ValueError):
        pass
    _JWT_CACHE[_hashlib.sha256(token.encode()).hexdigest()] = (identity, deadline)


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
        cached = _jwt_cache_get(token) if _jwks_url() else None   # verify один раз на токен
        if cached is not None:
            return cached
        extra = _extra_issuers()
        try:
            if _jwks_url() or extra:  # строгая верификация подписи по JWKS (свой realm + доп. issuer)
                unv = jwt.decode(token, options={"verify_signature": False})  # issuer до верификации
                iss = unv.get("iss") or ""
                if iss in extra:                       # доп. доверенный realm (desktop) → его JWKS
                    jwks_url, exp_iss = extra[iss], iss
                else:                                   # свой realm ABOP
                    jwks_url, exp_iss = _jwks_url(), (os.getenv("KEYCLOAK_ISSUER") or None)
                if not jwks_url:
                    raise HTTPException(401, "issuer не доверен")
                key = _client(jwks_url).get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=["RS256"],
                                    audience=os.getenv("KEYCLOAK_AUDIENCE") or None,
                                    issuer=exp_iss,
                                    options={"verify_aud": bool(os.getenv("KEYCLOAK_AUDIENCE"))})
            else:  # демо/переключатель: decode без проверки подписи (токен от нашего Keycloak)
                claims = jwt.decode(token, options={"verify_signature": False})
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(401, "невалидный токен")
        ident = _identity(claims, dev=False)
        if _jwks_url() or extra:                          # кэшируем только верифицированный успех
            _jwt_cache_put(token, ident, claims.get("exp"))
        return ident
    if _jwks_url() or _extra_issuers():
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
            obs.inc("abop_http_requests_total", route=path, status=resp.status_code)
            obs.observe("abop_http_request_seconds", dt, route=path)
            obs.log_event("info", "http.request", route=path, method=request.method,
                          status=resp.status_code, ms=round(dt * 1000, 1))
        resp.headers["X-Trace-Id"] = tid
        return resp
    except Exception as ex:  # noqa: BLE001 — фиксируем ошибку в метрики/лог и пробрасываем
        obs.inc("abop_http_requests_total", route=request.url.path, status="500")
        obs.log_event("error", "http.error", route=request.url.path,
                      error=f"{type(ex).__name__}: {ex}")
        raise
    finally:
        obs.trace_id_var.reset(token)


# ═══════════════ READ: реальные вызовы ядра ═══════════════

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "families": len(ape.AGENT_FAMILIES), "skills": len(ape.SKILLS),
            "adapters": sorted(ape.SOURCE_ADAPTERS)}


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


@app.get("/api/admin/llm")
async def admin_llm_get(u: dict = Depends(user)) -> dict:
    """Текущий self-host LLM-эндпоинт (для UI-настройки «эндпоинт бокса»)."""
    return {"llm": clients.llm_active()}


@app.post("/api/admin/llm")
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


@app.get("/api/me")
def me(u: dict = Depends(user)) -> dict:
    return {"user": u}


def _uid_of(u: dict) -> str:
    """Мастер-UID пользователя для сквозного ID: preferred_username/name/sub из JWT."""
    return str(u.get("name") or u.get("preferred_username") or u.get("sub") or "").strip()


@app.get("/api/identity/me")
async def identity_me(u: dict = Depends(user)) -> dict:
    """Сквозной профиль ТЕКУЩЕГО пользователя: мастер-UID + department/roles (ABAC/RBAC из JWT) +
    карта его аккаунтов во внешних системах (почта/Redmine/CRM/1С). Это адресный контекст, который
    наследует агент, запущенный от имени пользователя."""
    return await identity_store.bundle(_uid_of(u), department=u.get("department"), roles=u.get("roles"))


@app.get("/api/identity/{uid}")
async def identity_get(uid: str, u: dict = Depends(user)) -> dict:
    """Сквозной профиль пользователя по UID. Свой профиль — всем; чужой — только admin/support (*)."""
    if uid != _uid_of(u) and u.get("department") not in ("*", None):
        raise HTTPException(403, "нет доступа к чужой идентичности")
    return await identity_store.bundle(uid)


@app.post("/api/identity/{uid}")
async def identity_link(uid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Привязать/обновить внешний аккаунт пользователя (account-linking). Admin-уровень.
    Тело: {system, external_id?, display?, attrs?}. Пример: {"system":"redmine","external_id":"pm.manager","attrs":{"assignee_id":4}}."""
    require_level(u, "admin")
    system = str((body or {}).get("system") or "").strip()
    if not system:
        raise HTTPException(422, "нужен system")
    editor = _uid_of(u) or "dev"
    row = await identity_store.link(uid, system, str((body or {}).get("external_id") or ""),
                                    str((body or {}).get("display") or ""),
                                    (body or {}).get("attrs") or {}, editor=editor)
    await audit_store.record(editor, "identity.link", f"{uid}:{system}", {"external_id": row.get("external_id")})
    return row


@app.delete("/api/identity/{uid}/{system}")
async def identity_unlink(uid: str, system: str, u: dict = Depends(user)) -> dict:
    """Отвязать аккаунт пользователя в системе. Admin-уровень."""
    require_level(u, "admin")
    await identity_store.unlink(uid, system)
    await audit_store.record(_uid_of(u) or "dev", "identity.unlink", f"{uid}:{system}", {})
    return {"ok": True}


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
    messages: list[dict] = []
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
    messages: list[dict] = []
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


@app.post("/api/family/seed")
async def family_seed(body: dict, u: dict = Depends(user)) -> dict:
    """Наполнить корпуса семей РЕАЛЬНЫМ знанием из каталога навыков: тело каждого навыка (методика .md)
    → slava_fam_<family> той семьи, что несёт навык. Тело: {family?} (пусто ⇒ все семьи). Admin-уровень."""
    require_level(u, "admin")
    only = str((body or {}).get("family") or "").strip()
    out = {}
    for fid, fam in ape.AGENT_FAMILIES.items():
        if only and fid != only:
            continue
        col = slava.fam_collection(fid)
        sids = []
        for _mk, (_mt, sk) in (fam.get("members") or {}).items():
            for s in sk:
                if s not in sids:
                    sids.append(s)
        n = 0
        for sid in sids:
            if sid not in ape.SKILLS:
                continue
            title, short, _instr = ape.SKILLS[sid]
            try:
                body_txt = (ape.load_skill_body(sid) or "")[:4000]
            except Exception:  # noqa: BLE001
                body_txt = ""
            text = f"[{fam.get('title', fid)}] Навык «{title}». {short}\n\n{body_txt}".strip()
            if len(text) < 20:
                continue
            try:
                await slava.ingest(col, f"{sid}.md", text, tenant="abop", replace=False, doc_id=sid)
                n += 1
            except Exception:  # noqa: BLE001 — пропускаем сбойный навык, продолжаем
                pass
        out[fid] = {"collection": col, "skills_ingested": n}
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.seed", only or "all",
                             {"families": len(out)})
    return {"seeded": out, "families": len(out)}


# СПЕЦИФИЧНЫЕ под-запросы (sLAVA отсекает широкие/generic по релевантности — нужны точные темы норм)
_FAMILY_NORM_QUERY = {
    "finance": ["вычет НДС при наличии счёта-фактуры", "признание выручки и себестоимости ФСБУ",
                "налог на прибыль расходы уменьшают доход", "уточнённая декларация по НДС"],
    "audit": ["реализация без счёта-фактуры выданного НДС", "поступление без счёта-фактуры вычет НДС",
              "переходящая операция разные периоды", "сделки взаимозависимых лиц деловая цель"],
    "credit": ["резервы на возможные потери по ссудам", "оценка кредитоспособности заёмщика",
               "обеспечение по кредиту залог"],
    "analytics": ["финансовые показатели отчётность", "оценка эффективности рентабельность"],
    "management": ["договорные условия сроки ответственность", "регламент бизнес-процесса"],
    "architecture": ["требования к интеграции API", "стандарты проектирования систем"],
    "engineering": ["требования к разработке тестирование", "качество кода стандарты"],
    "research": ["методология исследования источники", "анализ данных достоверность"],
    "critic": ["контроль соответствия требованиям", "проверка качества результата"],
    "decisions": ["полномочия и согласование решений", "governance принятие решений"],
}


@app.post("/api/family/seed-norms")
async def family_seed_norms(body: dict, u: dict = Depends(user)) -> dict:
    """Раздать ЗАКОНОДАТЕЛЬНЫЕ НОРМЫ по корпусам семей из большого корпуса регламента (slava_reglament_ru):
    семантический запрос по домену семьи → top-N чанков → в slava_fam_<family>. Тело: {family?, query?,
    count?, source?}. Пусто family ⇒ норм-релевантные семьи (finance/audit/credit). Admin-уровень.
    NB: запускать ПО ОДНОЙ семье с паузой (sLAVA cooldown)."""
    require_level(u, "admin")
    src = str((body or {}).get("source") or "slava_reglament_ru").strip()
    count = int((body or {}).get("count") or 6)
    fam = str((body or {}).get("family") or "").strip()
    fams = [fam] if fam else ["finance", "audit", "credit"]
    out = {}
    for fid in fams:
        qs = ([str((body or {}).get("query"))] if (body or {}).get("query")
              else _FAMILY_NORM_QUERY.get(fid, [fid]))
        seen_txt, chunks = set(), []
        for q in qs:  # специфичные под-запросы: собираем уникальные чанки
            try:
                for s in (await slava.query(src, q, top_k=max(2, count // len(qs)), tenant="abop")).get("sources") or []:
                    txt = (s.get("text") or "").strip()
                    if len(txt) >= 30 and txt[:120] not in seen_txt:
                        seen_txt.add(txt[:120])
                        chunks.append(txt)
            except Exception:  # noqa: BLE001
                pass
        col = slava.fam_collection(fid)
        n = 0
        for i, txt in enumerate(chunks[:count * 2]):
            try:
                await slava.ingest(col, f"norm_reg_{i}.txt", "[закон/норма] " + txt, tenant="abop",
                                   replace=False, doc_id=f"reg_{fid}_{i}")
                n += 1
            except Exception:  # noqa: BLE001
                pass
        out[fid] = {"collection": col, "norms_ingested": n, "from": src, "subqueries": len(qs)}
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.seed_norms", fam or "norm-fams",
                             {"families": len(out)})
    return {"seeded_norms": out}


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
    ops = [o for o in ((body or {}).get("ops") or []) if o.get("nsi_key") or o.get("label")]
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
    op_embs = await clients.embed([o.get("label") or "" for o in ops]) if ops else []
    # match='label': сопоставление по ЛУЧШЕМУ cosine (без обязательного nsi_key на узлах) — для карты
    # прогона, где узлы = навыки без ключей НСИ. Матчим операцию сборки к чанку регламента по смыслу.
    if str((body or {}).get("match") or "").strip() == "label":
        report = []
        for o, emb in zip(ops, op_embs):
            best, best_sim = None, 0.0
            for r in reg:
                sim = _cosine(emb, r.get("embedding") or [])
                if sim > best_sim:
                    best, best_sim = r, sim
            if not best or best_sim < 0.4:
                report.append({"nsi_key": o.get("nsi_key"), "label": o.get("label"), "status": "нет в регламенте", "sim": round(best_sim, 3)})
            else:
                status = "соответствует" if best_sim >= 0.7 else "расходится по сути"
                report.append({"nsi_key": best.get("nsi_key"), "label": o.get("label"),
                               "reglament_op": best.get("op"), "status": status, "sim": round(best_sim, 3)})
        summary = {"total": len(report), "ok": sum(1 for x in report if x["status"] == "соответствует"),
                   "drift": sum(1 for x in report if x["status"] != "соответствует")}
        return {"tenant": tenant, "report": report, "summary": summary, "match": "label"}
    reg_by_key: dict = {}
    for r in reg:
        reg_by_key.setdefault(r["nsi_key"], r)
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
async def families(u: dict = Depends(user)) -> dict:
    """Ростер Семья→Роль→Навык из ЕДИНОГО реестра families_store (сид из кода, правки/кастомные в БД).
    §4/§6 ABOP_SCREENS. Единый источник семей/отделов (ABAC department==family)."""
    reg = await families_store.all()
    if not reg:                          # реестр ещё не засеян (первый старт до seed) → отдаём код
        reg = [{"id": fid, "title": fam["title"], "profile": fam["profile"], "mission": fam["mission"],
                "kind": "business" if fid in BIZ_FAMILIES else "engineering",
                "members": {mk: [mt, sk] for mk, (mt, sk) in fam["members"].items()}}
               for fid, fam in ape.AGENT_FAMILIES.items()]
    out = []
    for f in reg:
        mem = f.get("members") or {}
        members = [{"key": mk, "title": (v[0] if isinstance(v, (list, tuple)) else mk),
                    "skills": (v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else [])}
                   for mk, v in mem.items()]
        out.append({"id": f["id"], "title": f.get("title") or f["id"], "profile": f.get("profile") or "research",
                    "mission": f.get("mission") or "", "kind": f.get("kind") or "custom",
                    "builtin": bool(f.get("builtin")), "members": members})
    return {"families": out}


@app.post("/api/families")
async def family_save(body: dict, u: dict = Depends(user)) -> dict:
    """Создать/править семью (в т.ч. СВОЮ кастомную) в едином реестре. Кастомная семья = валидный
    отдел для ABAC (department==family) и тег навыка. Тело: {id, title?, mission?, profile?, members?}.
    id — slug [a-z0-9_-]. Admin-уровень (реестр семей = чувствительно к доступу)."""
    require_level(u, "admin")
    import re as _re
    fid = _re.sub(r"[^a-z0-9_-]", "", str((body or {}).get("id") or "").strip().lower())
    if not fid:
        raise HTTPException(422, "нужен id семьи (slug a-z0-9_-)")
    existing = await families_store.get(fid)
    kind = "custom" if not existing else (existing.get("kind") or "custom")
    editor = u.get("name") or u.get("sub") or "dev"
    card = await families_store.save(fid, {"title": (body or {}).get("title") or fid,
                                           "mission": (body or {}).get("mission") or "",
                                           "profile": (body or {}).get("profile") or "research",
                                           "kind": kind, "members": (body or {}).get("members") or (existing or {}).get("members") or {}},
                                     editor=editor, builtin=bool(existing and existing.get("builtin")))
    await audit_store.record(editor, "family.save", fid, {"title": card.get("title")})
    return card


@app.delete("/api/families/{fid}")
async def family_delete(fid: str, u: dict = Depends(user)) -> dict:
    """Удалить кастомную семью (встроенные защищены). Admin-уровень."""
    require_level(u, "admin")
    ok = await families_store.delete(fid)
    if not ok:
        raise HTTPException(400, "нельзя удалить (нет такой или встроенная семья)")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.delete", fid, {})
    return {"ok": True}


@app.get("/api/agents/spec")
def agent_spec(family: str, member: str = "", u: dict = Depends(user)) -> dict:
    """Спека агента (ADR-032): роль семьи + навыки + become-переходы + конверт + data-scope
    (резолв рецептов по entity). §6 авторинг узла-агента на канве."""
    try:
        return ape.build_agent_spec(family, member)
    except ValueError as ex:
        raise HTTPException(404, str(ex))


async def _refresh_skill_ds_cache() -> None:
    """Подтянуть data-need + флаг формата вывода (output) оверрайды из Postgres → инжект в ape."""
    try:
        ape.set_skill_ds_overrides(await skill_store.datasources_map())
        ape.set_skill_output_overrides(await skill_store.output_map())
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
# output — формат вывода навыка (structured|freeform), редактируется в UI. mode/egress/cite —
# отображаются, но governance-безопасность узла берётся авторитетно из каталога (assembly), не из правок.
_SKILL_SAFETY_FIELDS = ("mode", "egress", "cite", "output")


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
        # привязка навыка к семьям из UI (тег семьи): PG-список перекрывает вычисленный из ростера.
        # Влияет на ABAC-видимость (_skill_visible) и фильтр по семьям на экране навыков.
        if isinstance(patch.get("families"), list):
            card["families"] = sorted(set(patch["families"]))
            card["families_edited"] = True
        card["version"] = ov.get("version")
        card["editor"] = ov.get("editor")
        card["edited_at"] = ov.get("updated_at")
    else:
        card["version"] = "v1.0"
        card["editor"] = None
        card["edited_at"] = None
    return card


def _skill_visible(u: dict, card: dict) -> bool:
    """ABAC для навыков: admin/support (область *) видят все; общий навык (вне семей) виден всем;
    иначе — только если ХОТЯ БЫ одна семья навыка совпадает с отделом пользователя (department == family).
    Так менеджер видит менеджерские навыки, финансист — финансовые (изоляция периметра по данным)."""
    fams = card.get("families") or []
    if not fams:                      # навык вне семей — общий инструмент, доступен всем ролям
        return True
    return any(can_see_family(u, f) for f in fams)


@app.get("/api/skills")
async def skills(u: dict = Depends(user), all: bool = False) -> dict:
    """Каталог навыков (.md) + PG-правки. По умолчанию ФИЛЬТРУЕТСЯ по ABAC (навыки своей семьи +
    общие); `?all=1` — полный каталог (инженерная платформа/сборка агентов). §4."""
    fam = _skill_families()
    overrides = await skill_store.all()
    cards = [_overlay_skill(_skill_base_card(sid, fam), overrides.get(sid)) for sid in ape.SKILLS]
    total = len(cards)
    if not all:
        cards = [c for c in cards if _skill_visible(u, c)]
    return {"skills": cards, "count": len(cards), "total": total}


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
    # тег семьи навыка: привязка к семьям из ЕДИНОГО реестра (известным ИЛИ своей кастомной через «＋»).
    # Кастомные — slug [a-z0-9_-] (для корректного ABAC: department==family).
    if isinstance((body or {}).get("families"), list):
        import re as _re
        known = await families_store.ids() or set(ape.AGENT_FAMILIES.keys())
        fams = []
        for f in body["families"]:
            f = str(f).strip()
            if not f:
                continue
            if f in known:
                fams.append(f)                       # семья из реестра
            else:
                slug = _re.sub(r"[^a-z0-9_-]", "", f.lower())  # кастомная — санитизируем в slug
                if slug:
                    fams.append(slug)
        patch["families"] = sorted(set(fams))
    if not patch:
        raise HTTPException(422, "нет полей для сохранения")
    editor = u.get("name") or u.get("sub") or "dev"
    ov = await skill_store.save_patch(sid, patch, editor=editor)
    if "output" in patch or "families" in patch:  # формат вывода / привязка семьи → на другие реплики
        await _refresh_skill_ds_cache()
        await cachebus.notify("skills")
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
    await cachebus.notify("skills")   # инвалидировать кэш навык-источников на других репликах
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
    await cachebus.notify("dataplane")
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
    await cachebus.notify("dataplane")
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
    await cachebus.notify("dataplane")
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
    await identity_store.init()
    await identity_store.seed_if_empty()  # сквозной ID: связка мастер-UID → аккаунты в системах (демо)
    await families_store.init()
    await families_store.seed_from_code(ape.AGENT_FAMILIES, BIZ_FAMILIES)  # единый реестр семей (сид из кода)
    await trigger_store.init()
    await reglament_store.init()
    await run_cache_store.init()
    await hitl_store.init()
    import asyncio as _asyncio
    _asyncio.create_task(triggers.scheduler_loop(execute_agent_run))  # фоновый планировщик (leader-election)
    await _refresh_skill_ds_cache()  # инжект data-need оверрайдов из PG в ape
    await dataplane_store.init()
    await _backfill_dataplane_from_files()  # одноразовый перенос ~/.ape → PG (сохранить демо-рецепты)
    await _refresh_dataplane_cache()  # инжект рецептов/коннекторов из PG в ape
    # LLM-override (эндпоинт бокса из UI) — восстановить из PG при старте, инжектить в clients
    await _refresh_llm_override()
    # Cache-bus: инвалидация in-process кэшей между репликами (LISTEN/NOTIFY). Разблокирует 2+ реплики.
    cachebus.register("skills", _refresh_skill_ds_cache)
    cachebus.register("dataplane", _refresh_dataplane_cache)
    cachebus.register("llm", _refresh_llm_override)
    _asyncio.create_task(cachebus.listen_loop())


async def _refresh_llm_override() -> None:
    """Подтянуть self-host LLM-override из admin_config (PG) → инжект в clients (переживает рестарт)."""
    try:
        cfg = (await admin_store.all()).get("llmOverride") or {}
        clients.set_llm_override(cfg if isinstance(cfg, dict) else {})
    except Exception:  # noqa: BLE001
        pass


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

def _resolve_role(fam: str, intake: dict) -> str:
    """Роль в семье по контракту: единственная — она; иначе — по макс. совпадению навыков intake."""
    if fam not in ape.AGENT_FAMILIES:
        return ""
    members = ape.AGENT_FAMILIES[fam]["members"]
    if len(members) == 1:
        return next(iter(members))
    want = set(intake.get("skills") or [])
    best, best_score = "", 0
    for rid, (_title, _sk) in members.items():
        score = len(want & set(_sk))
        if score > best_score:
            best, best_score = rid, score
    return best


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
    check = assembly.check_graph(graph, intake, ape.skill_safety)
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
    role = _resolve_role(fam, intake)
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


def _agent_match_doc(a: dict) -> str:
    """Текст-документ агента для матча: имя + семья + роль + названия/описания его навыков."""
    parts = [a.get("name") or "", a.get("family") or "", a.get("role") or ""]
    for n in (a.get("graph") or {}).get("nodes") or []:
        sid = n.get("skill")
        if sid and sid in ape.SKILLS:
            t = ape.SKILLS[sid]
            parts.append(f"{t[0]} {t[1]}")   # заголовок + краткое
    return " · ".join(p for p in parts if p)


_AGENT_EMB_CACHE: dict = {}   # id → (doc_hash, embedding), чтобы не эмбедить агентов каждый раз


@app.post("/api/agents/match")
async def agents_match(body: dict, u: dict = Depends(user)) -> dict:
    """Подбор агента под задачу пользователя: ЛЕКСИКА (пересечение слов, надёжно/мгновенно) + СЕМАНТИКА
    (эмбеддинги BGE-M3, best-effort). Возвращает ранжированный топ агентов (ABAC по отделу) + их каналы
    доставки (из OUT-узлов графа). Питает дерево решений чата: «это задача для агента X, куда результат?»."""
    import re
    q = str((body or {}).get("q") or "").strip()
    if not q:
        return {"matches": []}
    briefs = await agent_store.list_for(None)
    agents = []
    for b in briefs:
        if not can_see_family(u, b.get("family")):
            continue
        full = await agent_store.get(b["id"])
        if full:
            agents.append(full)
    if not agents:
        return {"matches": []}
    ql = q.lower()
    qtokens = set(t for t in re.split(r"[^\wа-яё]+", ql) if len(t) > 2)
    docs = {a["id"]: _agent_match_doc(a) for a in agents}
    # лексика: доля слов агента, встреченных в запросе + бонус за вхождение имени
    lex = {}
    for a in agents:
        dl = docs[a["id"]].lower()
        dtokens = set(t for t in re.split(r"[^\wа-яё]+", dl) if len(t) > 2)
        inter = len(qtokens & dtokens)
        name_hit = 2 if (a.get("name") or "").lower() in ql else 0
        lex[a["id"]] = inter + name_hit
    # семантика (best-effort): эмбеддим запрос + документы агентов (кэш по документу)
    sem = {a["id"]: 0.0 for a in agents}
    try:
        import hashlib as _h
        need_emb, need_ids = [], []
        for a in agents:
            dh = _h.md5(docs[a["id"]].encode()).hexdigest()
            cached = _AGENT_EMB_CACHE.get(a["id"])
            if not cached or cached[0] != dh:
                need_emb.append(docs[a["id"]]); need_ids.append((a["id"], dh))
        if need_emb:
            embs = await clients.embed(need_emb)
            for (aid, dh), e in zip(need_ids, embs):
                _AGENT_EMB_CACHE[aid] = (dh, e)
        qemb = (await clients.embed([q]))[0]
        for a in agents:
            c = _AGENT_EMB_CACHE.get(a["id"])
            if c:
                sem[a["id"]] = _cosine(qemb, c[1])
    except Exception:  # noqa: BLE001 — семантика опциональна, лексики достаточно
        pass
    # итоговый скор: лексика (норм.) + семантика
    maxlex = max(lex.values()) or 1
    scored = []
    for a in agents:
        score = 0.55 * (lex[a["id"]] / maxlex) + 0.45 * sem[a["id"]]
        channels = [n.get("channel") or (n.get("out") or {}).get("channel")
                    for n in (a.get("graph") or {}).get("nodes") or [] if n.get("kind") in ("output", "out")]
        channels = [c for c in channels if c]
        scored.append({"id": a["id"], "name": a.get("name"), "family": a.get("family"),
                       "role": a.get("role"), "score": round(score, 3),
                       "lex": lex[a["id"]], "sem": round(sem[a["id"]], 3), "channels": channels})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"matches": scored[:5], "query": q}


def _verify_envelope(graph: dict, autonomy_max: str) -> dict:
    """Авто-верификация authored-агента против ПРОИЗВОДНОГО конверта (не контракт LUDA): та же
    governance-проверка, что и `/api/agents/check`, но интейк выводится из графа (потолок = автономия
    агента, требуемые навыки = навыки графа). Ловит нарушения ADR-013/014 (автономия>потолок,
    внешнее действие без HITL). verified=true → конверт соблюдён; для прод-деплоя всё равно нужен контракт."""
    import datetime as _datetime
    skills = [n.get("skill") for n in (graph or {}).get("nodes") or [] if n.get("kind") == "skill" and n.get("skill")]
    intake = {"autonomy_ceiling": autonomy_max or "A2", "skills": skills}
    try:
        res = assembly.check_graph(graph, intake, ape.skill_safety)
    except Exception as ex:  # noqa: BLE001 — верификация опциональна, не валим сборку
        return {"verified": False, "mode": "auto-envelope", "error": str(ex)}
    errs = res.get("errors") or []
    return {"verified": not errs, "mode": "auto-envelope", "against": "производный конверт (не контракт LUDA)",
            "autonomy_max": res.get("autonomy_max"), "hitl_count": res.get("hitl_count"),
            "errors": errs, "warnings": res.get("warnings") or [],
            "checked_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat()}


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
    verdict = _verify_envelope(graph, env["autonomy_max"])   # авто-верификация против производного конверта
    saved = await agent_store.save(name=name, audit_id=audit_id, version=version, graph=graph,
                                   autonomy_max=env["autonomy_max"], created_by=u.get("name") or "dev",
                                   family=family, role=spec["role"], transitions=spec["transitions"],
                                   source="authored", verification=verdict)
    await audit_store.record(u.get("name") or "dev", "agent.author", saved["id"],
                             {"family": family, "role": spec["role"], "autonomy_max": env["autonomy_max"],
                              "verified": verdict.get("verified")})
    return JSONResponse({"saved": True, "id": saved["id"], "version": version, "status": "draft",
                         "family": family, "role": spec["role"], "autonomy_max": env["autonomy_max"],
                         "verification": verdict,
                         "skills": [s["id"] for s in spec["skills"]], "data_scope": spec["data_scope"]},
                        status_code=201)


@app.get("/api/agents/{agent_id}")
async def agent_get(agent_id: str, u: dict = Depends(user)) -> dict:
    """Полный AgentVersion (граф + метаданные) — для паспорта/повторного открытия в канве."""
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого AgentVersion")
    return a


@app.post("/api/agents/{agent_id}/triggers")
async def agent_add_trigger(agent_id: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сделать задачу агента РЕГУЛЯРНОЙ: добавить триггер-узел «расписание» в граф агента и сохранить
    НОВОЙ версией (версия меняется). Узел виден на канве «Строю», планировщик (scheduler_loop) фаерит
    его по cron. Тело: {cron, title?, enabled?, hitl?}. cron: 'HH:MM' (ежедневно) | '*/N' (каждые N мин).
    Внешняя доставка всё равно под HITL узлов агента. manager+ (создание триггера = мутация среды)."""
    require_level(u, "manager")
    import uuid as _uuid
    import re as _re
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого AgentVersion")
    cron = str((body or {}).get("cron") or "").strip()
    if not (_re.match(r"^\d{1,2}:\d{2}$", cron) or _re.match(r"^\*/\d+$", cron)
            or len(cron.split()) == 5):
        raise HTTPException(422, "cron: ожидается 'HH:MM' (ежедневно) или '*/N' (каждые N минут)")
    graph = dict(a.get("graph") or {})
    nodes = list(graph.get("nodes") or [])
    tid = "trg-" + _uuid.uuid4().hex[:8]
    title = str((body or {}).get("title") or f"Расписание {cron}").strip()
    deliver = str((body or {}).get("deliver") or "chat").strip()  # chat | system
    node = {"id": tid, "kind": "trigger", "title": title,
            "trig": {"type": "schedule", "cron": cron,
                     "enabled": bool((body or {}).get("enabled", True)),
                     "source": "chat.recurring", "deliver": deliver if deliver in ("chat", "system") else "chat",
                     "owner": (u.get("name") or u.get("sub") or "dev"),
                     "spawn": {"autonomy_max": a.get("autonomy_max") or "A1",
                               "hitl": bool((body or {}).get("hitl", False))}}}
    nodes.append(node)
    graph["nodes"] = nodes
    audit_id = a.get("contract_audit_id") or agent_id.split(".v")[0]
    version = await agent_store.next_version(audit_id)
    _verif = a.get("verification") if a.get("verification") is not None else \
        (_verify_envelope(graph, a.get("autonomy_max") or "A1") if a.get("source") == "authored" else None)
    saved = await agent_store.save(
        name=a.get("name") or "Агент", audit_id=audit_id, version=version, graph=graph,
        autonomy_max=a.get("autonomy_max") or "A1", created_by=(u.get("name") or u.get("sub") or "dev"),
        family=a.get("family") or "", role=a.get("role") or "",
        transitions=a.get("transitions") or [], source=a.get("source") or "contract", verification=_verif)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "agent.trigger.add", saved["id"],
                             {"cron": cron, "from_version": a.get("version"), "trigger_id": tid})
    return {"ok": True, "agent_id": saved["id"], "version": saved.get("version"),
            "trigger_id": tid, "cron": cron, "title": title}


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


@app.delete("/api/agents/{agent_id}/triggers/{trigger_id}")
async def agent_del_trigger(agent_id: str, trigger_id: str, u: dict = Depends(user)) -> dict:
    """Убрать расписание: сохранить НОВУЮ версию агента без этого триггер-узла (версия меняется)."""
    require_level(u, "manager")
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого AgentVersion")
    graph = dict(a.get("graph") or {})
    nodes = [n for n in (graph.get("nodes") or []) if not (n.get("kind") == "trigger" and n.get("id") == trigger_id)]
    graph["nodes"] = nodes
    audit_id = a.get("contract_audit_id") or agent_id.split(".v")[0]
    version = await agent_store.next_version(audit_id)
    saved = await agent_store.save(
        name=a.get("name") or "Агент", audit_id=audit_id, version=version, graph=graph,
        autonomy_max=a.get("autonomy_max") or "A1", created_by=(u.get("name") or u.get("sub") or "dev"),
        family=a.get("family") or "", role=a.get("role") or "",
        transitions=a.get("transitions") or [], source=a.get("source") or "contract",
        verification=a.get("verification"))
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "agent.trigger.del", saved["id"],
                             {"trigger_id": trigger_id})
    return {"ok": True, "agent_id": saved["id"], "version": saved.get("version")}


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


def _build_report_html(agent: dict, result: dict) -> str:
    """Детерминированный HTML-отчёт из результата прогона (находки A/B/C/D, цепочки-расследования,
    результаты навыков). Используется OUT-узлом для доставки (PDF/BookStack/почта)."""
    import html as _html
    esc = lambda x: _html.escape(str(x if x is not None else ""))  # noqa: E731
    name = esc(agent.get("name") or "Агент ABOP")
    v = result.get("verdict") or {}
    parts = [f"<h1>Отчёт агента: {name}</h1>",
             f"<p>Вердикт: <b>{'пройден' if v.get('ok') else 'есть замечания'}</b> · "
             f"автономия {esc(v.get('autonomy_used'))} · волн {len(result.get('waves') or [])}</p>"]
    fs = result.get("findings_summary")
    if fs:
        bc = fs.get("by_class") or {}
        parts.append(f"<h2>Находки аудита: {esc(fs.get('total'))}</h2>")
        parts.append("<p>" + " · ".join(f"{k}: {esc(bc.get(k, 0))}" for k in ("A", "B", "C", "D")) + "</p>")
    fnds = result.get("findings") or []
    struct = [f for f in fnds if isinstance(f, dict) and f.get("проверка")]
    if struct:
        parts.append("<ul>")
        for f in struct[:30]:
            norm = (f.get("нормы_rag") or [""])[0]
            parts.append(f"<li><b>[{esc(f.get('класс'))}] {esc(f.get('проверка'))}</b> — {esc(f.get('описание'))}"
                         + (f"<br><i>§ {esc(norm[:200])}</i>" if norm else "") + "</li>")
        parts.append("</ul>")
    invs = result.get("investigations") or []
    if invs:
        parts.append(f"<h2>Расследования от симптома: {len(invs)}</h2><ul>")
        for iv in invs[:30]:
            chain = " → ".join(f"{esc(l.get('звено'))}: {esc(l.get('статус'))}" for l in (iv.get("цепочка") or []))
            rec = iv.get("сверка") or {}
            norm = (iv.get("нормы_rag") or [""])[0]
            parts.append(f"<li><b>{esc(iv.get('id'))} [{esc(iv.get('серьёзность'))}]</b> — {esc(iv.get('симптом'))}"
                         f"<br>{chain}<br>расхождение Δ {esc(rec.get('разница_₽'))} ₽"
                         + (f"<br><i>§ {esc(norm[:200])}</i>" if norm else "") + "</li>")
        parts.append("</ul>")
    llm = [f for f in fnds if isinstance(f, dict) and f.get("skill") and f.get("text")]
    if llm and not struct:
        parts.append("<h2>Результаты навыков</h2>")
        for f in llm[:20]:
            parts.append(f"<h3>{esc(f.get('skill'))}</h3>"
                         f"<pre style='white-space:pre-wrap'>{esc((f.get('text') or '')[:2000])}</pre>")
    body = "".join(parts)
    return ("<!doctype html><html><head><meta charset='utf-8'><style>"
            "body{font-family:Arial,sans-serif;max-width:800px;margin:24px auto;color:#111;line-height:1.5}"
            "h1{font-size:22px}h2{font-size:17px;margin-top:20px}li{margin:6px 0}i{color:#0a6}</style></head>"
            f"<body>{body}<hr><p style='color:#888;font-size:12px'>Сформировано ABOP · {name}</p></body></html>")


def _html_to_text(h: str) -> str:
    import re as _re
    t = _re.sub(r"<br\s*/?>", "\n", h or "")
    t = _re.sub(r"</(li|p|h1|h2|h3)>", "\n", t)
    t = _re.sub(r"<[^>]+>", "", t)
    return _re.sub(r"\n{3,}", "\n\n", t).strip()


def _out_nodes(agent: dict) -> list:
    """OUT-узлы графа с настроенным каналом доставки (kind:'out', out{channel,...})."""
    return [n for n in (agent.get("graph") or {}).get("nodes") or []
            if n.get("kind") == "out" and (n.get("out") or {}).get("channel")]


async def _send_channel(cfg: dict, agent_name: str, html_report: str, real: bool) -> str:
    """Отправка отчёта в канал OUT-узла (почта/BookStack/YouGile/Яндекс/PDF). real=False → dry_run
    (превью). Переиспользуется прогоном и подтверждением HITL (approve → real=True)."""
    import asyncio
    import re as _re
    channel = cfg.get("channel")
    title = cfg.get("subject") or (agent_name or "Отчёт ABOP")
    loop = asyncio.get_event_loop()
    rf = "true" if real else "false"
    try:
        if channel == "bookstack":
            return await loop.run_in_executor(None, ape._t_bookstack_publish,
                {"title": title, "html": html_report, "book_id": cfg.get("book_id") or 1, "run": rf})
        if channel in ("email", "yandex"):
            att = ""
            if (cfg.get("format") or "pdf") == "pdf":
                pr = await loop.run_in_executor(None, ape._t_pdf_render, {"html": html_report, "name": "report"})
                m = _re.search(r"PDF готов:\s*(\S+)", pr or "")
                att = m.group(1) if m else ""
            body_txt = "Отчёт агента ABOP во вложении." if att else _html_to_text(html_report)[:4000]
            fn = ape._t_yandex_email if channel == "yandex" else ape._t_email_send
            return await loop.run_in_executor(None, fn,
                {"to": cfg.get("to") or "audit@demo.local", "subject": title,
                 "body": body_txt, "attachment": att, "run": rf})
        if channel == "yougile":
            return await loop.run_in_executor(None, ape._t_yougile_task,
                {"title": title, "description": _html_to_text(html_report)[:6000],
                 "column_id": cfg.get("column_id") or cfg.get("to") or "", "run": rf})
        if channel == "redmine":
            return await loop.run_in_executor(None, ape._t_redmine_create_issue,
                {"subject": title, "description": _html_to_text(html_report)[:6000],
                 "project": cfg.get("project") or cfg.get("to") or "", "run": rf})
        if channel in ("pdf", "file"):
            return await loop.run_in_executor(None, ape._t_pdf_render,
                {"html": html_report, "name": cfg.get("to") or "report"})
        return f"неизвестный канал: {channel}"
    except Exception as ex:  # noqa: BLE001
        return f"ошибка доставки: {type(ex).__name__}: {ex}"


async def _deliver_out_nodes(agent: dict, result: dict, actor: str, deliver_filter: str = "") -> None:
    """Проброс OUT-узла в доставку. Три режима на узел:
    - hitl=true → создаём ЗАЯВКУ в очередь HITL (pending), наружу НЕ шлём (подтвердит человек);
    - run=true, без hitl → РЕАЛЬНАЯ отправка;
    - иначе → dry_run (превью). (ADR-014: наружу — под подтверждением/явным флагом.)
    deliver_filter (дерево решений чата, «куда результат»): '' — все каналы; 'chat' — НИ одного
    (результат только в чат); имя канала (redmine/email/bookstack) — только этот канал."""
    nodes = _out_nodes(agent)
    if deliver_filter == "chat":
        result["delivery"] = []       # пользователь выбрал «только в чат» — наружу ничего
        return
    if deliver_filter:                # выбран конкретный канал — фильтруем OUT-узлы
        nodes = [n for n in nodes if (n.get("out") or {}).get("channel") == deliver_filter]
    if not nodes:
        return
    html_report = _build_report_html(agent, result)
    fam = agent.get("family") or ""
    run_id = result.get("run_id") or (result.get("verdict") or {}).get("run_id") or ""
    # Сквозной ID: агент действует «от имени» пользователя — подмешиваем его аккаунты в системах
    # (Redmine assignee, почта). Так задача назначается на него, письмо адресно. Best-effort.
    idsys = {}
    try:
        idsys = (await identity_store.bundle(actor)).get("systems") or {}
    except Exception:  # noqa: BLE001
        idsys = {}
    def _enrich(cfg: dict, channel: str) -> dict:
        c = dict(cfg)
        if channel == "redmine":
            rm = idsys.get("redmine") or {}
            attrs = rm.get("attrs") or {}
            if attrs.get("assignee_id") and not c.get("assigned_to"):
                c["assigned_to"] = attrs.get("assignee_id")
            if attrs.get("project") and not c.get("project"):
                c["project"] = attrs.get("project")
        elif channel in ("email", "yandex"):
            em = idsys.get("email") or {}
            if em.get("external_id") and not c.get("to"):
                c["to"] = em.get("external_id")   # по умолчанию — себе (свой ящик)
        return c
    deliveries = []
    for n in nodes:
        cfg = _enrich(n.get("out") or {}, (n.get("out") or {}).get("channel"))
        channel = cfg.get("channel")
        if cfg.get("hitl"):
            # заявка в очередь HITL — оператор подтвердит, тогда отправим реально
            item = await hitl_store.create(
                run_id=str(run_id), agent_id=agent.get("id") or "", family=fam,
                node=n.get("id") or "", title=n.get("title") or channel,
                channel=channel or "", to_addr=str(cfg.get("to") or cfg.get("book_id") or ""),
                payload={"cfg": cfg, "html": html_report, "agent_name": agent.get("name")},
                requested_by=actor)
            deliveries.append({"node": n.get("id"), "title": n.get("title"), "channel": channel,
                               "to": cfg.get("to"), "format": cfg.get("format"),
                               "mode": "awaiting_hitl", "hitl_id": item["id"],
                               "result": f"ожидает подтверждения оператора (заявка {item['id']})"})
            await audit_store.record(actor, "agent.deliver", agent.get("id"),
                                     {"channel": channel, "mode": "awaiting_hitl", "hitl_id": item["id"]},
                                     severity="info")
            continue
        real = str(cfg.get("run")).lower() == "true"
        out = await _send_channel(cfg, agent.get("name"), html_report, real)
        deliveries.append({"node": n.get("id"), "title": n.get("title"), "channel": channel,
                           "to": cfg.get("to"), "format": cfg.get("format"),
                           "mode": "real" if real else "dry_run", "result": str(out)[:400]})
        await audit_store.record(actor, "agent.deliver", agent.get("id"),
                                 {"channel": channel, "to": cfg.get("to"),
                                  "mode": "real" if real else "dry_run"}, severity="info")
    result["delivery"] = deliveries


# ─── Multi-user: per-user rate-limit + идемпотентность (дедуп двойных сабмитов) ───
import asyncio as _aio
import time as _time
_USER_RATE_MAX = max(1, int(os.getenv("ABOP_USER_RATE_MAX", "30")))     # прогонов на юзера за окно
_USER_RATE_WINDOW = max(10, int(os.getenv("ABOP_USER_RATE_WINDOW", "300")))  # окно, сек
_user_hits: dict = {}          # user -> [timestamps] (скользящее окно, per-replica)
_run_locks: dict = {}          # ключ дедупа -> asyncio.Lock (сериализация одинаковых сабмитов)


def _rate_check(user: str) -> bool:
    """Скользящее окно: не более _USER_RATE_MAX прогонов на пользователя за _USER_RATE_WINDOW сек."""
    now = _time.time()
    hits = [t for t in _user_hits.get(user, []) if now - t < _USER_RATE_WINDOW]
    if len(hits) >= _USER_RATE_MAX:
        _user_hits[user] = hits
        return False
    hits.append(now)
    _user_hits[user] = hits
    return True


def _run_lock(key: str) -> "_aio.Lock":
    """Per-key lock: одинаковые сабмиты (юзер+агент[+idempotency_key]) сериализуются — второй дождётся
    первого и получит его результат из кэша (дедуп двойного клика без двойного тяжёлого прогона)."""
    lk = _run_locks.get(key)
    if lk is None:
        lk = _aio.Lock()
        _run_locks[key] = lk
    return lk


def _findings_context_text(findings: list, investigations: list) -> str:
    """Компактный текст детерминированных находок/расследований для grounded-объяснения навыками
    (LLM объясняет РЕАЛЬНЫЕ находки кода, а не ищет заново на сэмпле-дайджесте)."""
    lines = []
    for f in (findings or [])[:30]:
        if not isinstance(f, dict):
            continue
        doc = f.get("документ") or {}
        num = doc.get("Номер") if isinstance(doc, dict) else None
        lines.append(f"[{f.get('класс')}] {f.get('проверка')} ({f.get('серьёзность')}) — {f.get('описание')}"
                     + (f" · док {num}" if num else ""))
    for iv in (investigations or [])[:20]:
        if isinstance(iv, dict):
            lines.append(f"[расследование {iv.get('id')}] {iv.get('симптом')}")
    return "\n".join(lines)


def _build_run_trace(agent: dict, result: dict) -> list:
    """Аудит-трасса прогона: по каждому навыку (в порядке волн) — что он читал (сущности+происхождение),
    какая модель отработала, сколько токенов/времени. Отвечает «как рассуждал / откуда данные».
    Provenance сущностей кэшируется в пределах прогона (одна сущность у многих навыков)."""
    findings = {f.get("skill"): f for f in (result.get("findings") or []) if isinstance(f, dict) and f.get("skill")}
    prov_cache: dict = {}

    def _prov(ent: str) -> dict:
        if ent not in prov_cache:
            try:
                prov_cache[ent] = ape.entity_provenance(ent)
            except Exception:  # noqa: BLE001
                prov_cache[ent] = {"entity": ent, "records": 0}
        return prov_cache[ent]

    steps = []
    for n in (agent.get("graph") or {}).get("nodes") or []:
        if n.get("kind") != "skill":
            continue
        sid = n.get("skill") or n.get("id")
        f = findings.get(sid) or {}
        ents = f.get("entities") or [ds.get("entity") for ds in (ape.skill_datasources_resolved(sid) or []) if ds.get("entity")]
        sources = [_prov(e) for e in ents if e]
        steps.append({
            "skill": sid,
            "reads_entities": ents,
            "data_sources": sources,                       # откуда данные: рецепт/источник/объём/свежесть
            "model": f.get("model") or None,               # какая модель рассуждала
            "input_tokens": f.get("input_tokens"), "output_tokens": f.get("output_tokens"),
            "ms": f.get("ms"),
            "error": f.get("error"),
            "output_kind": "structured" if f.get("structured") else ("text" if f.get("text") else None),
        })
    return steps


def _collect_soft_errors(result: dict) -> list:
    """Собирает МЯГКИЕ (не фатальные) ошибки прогона в один список — чтобы опциональные шаги
    (находки/расследования/доставка/нормы) не отваливались МОЛЧА. Каждая логируется с trace_id;
    выводится в результат (soft_errors) и в модалку прогона. Прогон при этом не падает."""
    soft = []
    for key, stage in (("findings_error", "находки"), ("investigations_error", "расследования"),
                       ("delivery_error", "доставка"), ("manifest_error", "манифест доступа")):
        if result.get(key):
            soft.append({"stage": stage, "error": str(result.get(key))[:300]})
    for d in result.get("delivery") or []:
        r = str(d.get("result") or "")
        if "ошибка" in r.lower():
            soft.append({"stage": "доставка·" + (d.get("channel") or "?"), "error": r[:300]})
    if result.get("norms_error"):
        soft.append({"stage": "нормы (RAG)", "error": str(result["norms_error"])[:300]})
    for f in result.get("findings") or []:
        if isinstance(f, dict) and f.get("error"):
            soft.append({"stage": "навык·" + str(f.get("skill") or "?"), "error": str(f["error"])[:300]})
    for s in soft:
        obs.log_event("warn", "run.soft_error", stage=s["stage"], error=s["error"])
    return soft


_ENT_SIG_CACHE: dict = {}   # entity -> (сигнатура, ts) — короткий TTL, чтобы не читать jsonl на каждый прогон
_ENT_SIG_TTL = float(os.getenv("ABOP_ENT_SIG_TTL", "5"))


def _entity_sig(e: str) -> str:
    """Сигнатура версии сущности (счёт + max fetched_at) с коротким TTL-кэшем — горячий cached-путь
    не должен перечитывать весь jsonl на каждый прогон (при всплеске юзеров данные те же)."""
    now = _time.time()
    hit = _ENT_SIG_CACHE.get(e)
    if hit and now - hit[1] < _ENT_SIG_TTL:
        return hit[0]
    try:
        recs = ape.data_query(e, limit=100000)
        mx = max((float((r.get("provenance") or {}).get("fetched_at", 0) or 0) for r in recs), default=0)
        sig = f"{e}:{len(recs)}:{mx}"
    except Exception:  # noqa: BLE001
        sig = f"{e}:err"
    _ENT_SIG_CACHE[e] = (sig, now)
    return sig


def _data_fingerprint(agent: dict) -> str:
    """Отпечаток ВЕРСИИ данных, которые читает агент (счёт + max fetched_at по сущностям навыков).
    Меняется при обновлении Data Plane → инвалидирует кэш результатов автоматически."""
    import hashlib
    ents = set()
    for n in (agent.get("graph") or {}).get("nodes") or []:
        sid = n.get("skill")
        if not sid:
            continue
        for ds in (ape.skill_datasources_resolved(sid) or []):
            e = ds.get("entity")
            if e:
                ents.add(e)
    parts = [_entity_sig(e) for e in sorted(ents)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _run_cache_key(agent: dict) -> str:
    """Ключ кэша прогона: агент(версия) + отпечаток данных + конфиг LLM (влияет на вывод)."""
    import hashlib
    cfg = f"mt={runner._LIM.get('max_tokens')};st={int(runner._STRUCTURED)};m={settings.local_llm_model or ''}"
    return f"{agent.get('id')}::{_data_fingerprint(agent)}::{hashlib.sha256(cfg.encode()).hexdigest()[:8]}"


async def execute_agent_run(agent: dict, contract: dict, started_by: str, *, trigger: dict | None = None,
                            use_cache: bool = True, user_context: str = "", deliver_filter: str = "") -> dict:
    """Ядро прогона (LLM-раскладка + находки audit1c + сохранение + аудит). Переиспользуется
    POST /api/runs и планировщиком триггеров (server/triggers.py). Возвращает {saved, result}.
    Кэш результатов (multi-user): при попадании возвращает сохранённый вывод без LLM/доставки."""
    _t0 = time.perf_counter()
    _trace = obs.current_trace_id()
    fam_key = access.scope_key(family=agent.get("family"))
    blocked, data_denied = await _gate_agent_data(agent, fam_key, started_by)  # ABAC на данных
    # КЭШ результатов (multi-user): тот же агент+данные+конфиг → отдаём сохранённый вывод без LLM/доставки.
    _cache_key = None
    if use_cache and os.getenv("ABOP_RUN_CACHE", "1") != "0":
        try:
            _cache_key = _run_cache_key(agent)
            _cached = await run_cache_store.get(_cache_key)
        except Exception:  # noqa: BLE001 — кэш опционален
            _cache_key, _cached = None, None
        if _cached:
            result = dict(_cached)
            result["cached"] = True
            result["trace_id"] = _trace
            result["started_by"] = started_by
            _rm = dict(result.get("run_metrics") or {})
            _cost = dict(_rm.get("cost") or {})
            _cost["cached"], _cost["rub"] = True, 0.0  # повтор из кэша — токены не тратились
            _rm["cost"] = _cost
            result["run_metrics"] = _rm
            result.pop("delivery", None)  # доставку НЕ повторяем из кэша (побочные эффекты)
            if trigger:
                result["trigger"] = {"id": trigger.get("id"), "type": (trigger.get("trig") or {}).get("type"),
                                     "title": trigger.get("title")}
            saved = await run_store.save(result)
            _dt = time.perf_counter() - _t0
            obs.inc("abop_run_cache_total", hit="true")
            obs.observe("abop_run_seconds", _dt, family=agent.get("family") or "-")
            obs.log_event("info", "run.cache_hit", run_id=saved["id"], agent=agent.get("id"),
                          ms=round(_dt * 1000, 1))
            await audit_store.record(started_by, "agent.run", saved["id"],
                                     {"agent_id": agent.get("id"), "cached": True,
                                      "findings": (result.get("findings_summary") or {}).get("total")},
                                     severity="info")
            return {"saved": saved, "result": result}
    # Детерминированные находки/расследования считаем ДО прогона (истина, считает КОД) — чтобы навыки в LLM
    # их ОБЪЯСНЯЛИ (grounded), а не искали заново на сэмпле-дайджесте (иначе LLM ложно пишет «расхождений нет»).
    _skills = [n.get("skill") for n in (agent.get("graph") or {}).get("nodes", [])]
    _det_findings, _det_findings_err = [], None
    if "audit1c-checks" in _skills:
        try:
            _det_findings = ape.audit1c_run_checks(ape.audit1c_build_graph())
        except Exception as ex:  # noqa: BLE001
            _det_findings_err = f"{type(ex).__name__}: {ex}"
    _det_invs, _det_invs_err = [], None
    if "invest1c-trace" in _skills:
        try:
            _det_invs = ape.audit1c_trace_chains()
        except Exception as ex:  # noqa: BLE001
            _det_invs_err = f"{type(ex).__name__}: {ex}"
    _ctx = _findings_context_text(_det_findings, _det_invs) or None
    result = await runner.run_live(agent, contract, ape.skill_safety,
                                   data_query=ape.data_query,
                                   skill_sources=ape.skill_datasources_resolved,
                                   load_body=ape.load_skill_body,
                                   chat_fn=clients.chat, blocked_entities=blocked,
                                   knowledge_fn=_agent_knowledge_fn(agent, started_by),
                                   findings_context=_ctx, user_context=user_context)
    # Петля прогон→канва: прикрепляем детерминированные находки (истина, не LLM).
    if "audit1c-checks" in _skills:
        if _det_findings_err:
            result["findings"] = []
            result["findings_error"] = _det_findings_err
        else:
            result["findings"] = _det_findings
            result["findings_summary"] = {
                "total": len(_det_findings),
                "by_class": {c: sum(1 for f in _det_findings if f.get("класс") == c) for c in ("A", "B", "C", "D")},
            }
    # Демо-сценарий №2 «расследование от симптома»: цепочки реализация→взаиморасчёты→НДС.
    if "invest1c-trace" in _skills:
        if _det_invs_err:
            result["investigations"] = []
            result["investigations_error"] = _det_invs_err
        else:
            _invs = _det_invs
            result["investigations"] = _invs
            result["investigations_summary"] = {
                "total": len(_invs),
                "broken": sum(1 for i in _invs
                              if any(not l.get("есть") for l in i.get("цепочка", []))),
                "by_sev": {s: sum(1 for i in _invs if i.get("серьёзность") == s)
                           for s in ("высокая", "средняя")},
            }
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
        # обогащаем нормами и цепочки-расследования (по тексту симптома + проверки)
        for inv in (result.get("investigations") or [])[:10]:
            if not isinstance(inv, dict):
                continue
            q = " ".join(str(inv.get(k) or "") for k in ("симптом", "проверка")).strip()
            if not q:
                continue
            try:
                norms = await kfn("invest1c-verdict", [], q)
            except Exception:  # noqa: BLE001
                norms = []
            if norms:
                inv["нормы_rag"] = norms[:2]
                enriched += 1
        result["norms_enriched"] = enriched
    # Проброс OUT-узла в реальную доставку (почта/BookStack/PDF) — dry_run по умолчанию.
    try:
        await _deliver_out_nodes(agent, result, started_by, deliver_filter=deliver_filter)
    except Exception as ex:  # noqa: BLE001 — доставка опциональна, прогон не падает
        result["delivery_error"] = f"{type(ex).__name__}: {ex}"
    result["started_by"] = started_by
    # Least-privilege манифест агента (ABAC): какие системы реестра доступны его семье, Qdrant-тенант,
    # и какие сущности закрыты на пути данных (система вне scope).
    try:
        result["access"] = await access.manifest(fam_key)
        if data_denied:
            result["access"]["data_denied"] = data_denied
    except Exception as ex:  # noqa: BLE001 — манифест опционален, но НЕ молча: фиксируем
        result["manifest_error"] = f"{type(ex).__name__}: {ex}"
    if trigger:  # прогон запущен триггером — фиксируем происхождение (наблюдаемость цепочек)
        result["trigger"] = {"id": trigger.get("id"), "type": (trigger.get("trig") or {}).get("type"),
                             "title": trigger.get("title")}
    # Observability: сквозной trace_id + тайминг + мягкие ошибки прогона (НЕ падаем молча — см. ниже).
    result["trace_id"] = _trace
    _dur = time.perf_counter() - _t0
    result.setdefault("run_metrics", {}).setdefault("timings", {})["total_ms"] = round(_dur * 1000, 1)
    # АУДИТ-ТРАССА: как модель рассуждала и ОТКУДА взяла данные — по каждому навыку: сущности →
    # происхождение (рецепт/источник/сколько записей/свежесть) + модель/токены/тайминг. Для аудитора.
    try:
        result["trace"] = _build_run_trace(agent, result)
    except Exception as ex:  # noqa: BLE001 — трасса опциональна
        result["trace_error"] = f"{type(ex).__name__}: {ex}"
    _soft = _collect_soft_errors(result)  # опциональные шаги, что отвалились (доставка/находки/нормы/…)
    if _soft:
        result["soft_errors"] = _soft
    saved = await run_store.save(result)
    _v = result.get("verdict") or {}
    await audit_store.record(started_by, "agent.run", saved["id"],
                             {"agent_id": agent.get("id"), "verdict_ok": bool(_v.get("ok")),
                              "autonomy_used": _v.get("autonomy_used"),
                              "findings": (result.get("findings_summary") or {}).get("total"),
                              "soft_errors": len(_soft), "trigger": (trigger or {}).get("id")},
                             severity=("warn" if (_soft or not _v.get("ok")) else "info"))
    # метрики прогона
    _fam = agent.get("family") or "-"
    obs.inc("abop_runs_total", family=_fam, ok=str(bool(_v.get("ok"))).lower())
    obs.observe("abop_run_seconds", _dur, family=_fam)
    _ft = (result.get("findings_summary") or {}).get("total")
    if _ft:
        obs.inc("abop_findings_total", val=float(_ft))
    _cost = ((result.get("run_metrics") or {}).get("cost") or {}).get("rub") or 0
    if _cost:
        obs.inc("abop_run_cost_rub_total", val=float(_cost))
    for _d in result.get("delivery") or []:
        obs.inc("abop_deliveries_total", channel=_d.get("channel") or "-", mode=_d.get("mode") or "-")
    if _soft:
        obs.inc("abop_run_soft_errors_total", val=float(len(_soft)))
    obs.log_event("warn" if _soft else "info", "agent.run.done", run_id=saved["id"],
                  agent=agent.get("id"), family=_fam, ok=bool(_v.get("ok")),
                  ms=round(_dur * 1000, 1), findings=_ft, soft_errors=(_soft or None))
    await langfuse_trace.emit_run(_trace, saved["id"], agent, result)  # LLM-трейс (best-effort, no-op без ключей)
    # Сохранить результат в кэш (multi-user): без волатильных/побочных полей (доставку не кэшируем).
    if _cache_key:
        try:
            _volatile = {"trace_id", "started_by", "cached", "delivery", "delivery_error",
                         "trigger", "soft_errors"}
            await run_cache_store.put(_cache_key, {k: v for k, v in result.items() if k not in _volatile})
            obs.inc("abop_run_cache_total", hit="false")
        except Exception as ex:  # noqa: BLE001 — кэш опционален
            obs.log_event("warn", "run.cache_put_fail", error=f"{type(ex).__name__}: {ex}")
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
    actor = u.get("name") or u.get("sub") or "dev"
    # per-user rate-limit (защита от «стучания» одним пользователем)
    if not _rate_check(actor):
        obs.inc("abop_rate_limited_total")
        raise HTTPException(429, f"слишком много прогонов: лимит {_USER_RATE_MAX} за {_USER_RATE_WINDOW}с — подождите")
    user_context = str((body or {}).get("context") or "").strip()[:20000]  # задача/файл/ссылка из чата
    # Сквозной ID: агент адресен под пользователя — подмешиваем его профиль (кто он, отдел, аккаунты
    # в системах: какой ящик/Redmine-исполнитель/CRM-владелец). Наследует RBAC/ABAC (department из JWT).
    try:
        _idb = await identity_store.bundle(_uid_of(u), department=u.get("department"), roles=u.get("roles"))
        _sys = _idb.get("systems") or {}
        if _sys or _idb.get("department"):
            _who = [f"Пользователь: {_idb.get('uid')}", f"отдел (ABAC): {_idb.get('department')}",
                    f"роли: {', '.join(_idb.get('roles') or []) or '—'}"]
            for s, info in _sys.items():
                _who.append(f"  · {s}: {info.get('external_id') or ''} {info.get('display') or ''}".rstrip())
            user_context = ("=== АДРЕСНОСТЬ (от имени кого работаем) ===\n" + "\n".join(_who)
                            + "\n\n" + user_context).strip()
    except Exception:  # noqa: BLE001 — identity опционален, не валим прогон
        pass
    deliver_filter = str((body or {}).get("deliver") or "").strip()  # дерево решений: '', chat, redmine, email…
    use_cache = not bool((body or {}).get("no_cache"))  # {no_cache:true} → форс свежий прогон
    if user_context or deliver_filter:
        use_cache = False   # контекст/выбор доставки влияют на прогон → кэш обходим
    # идемпотентность: одинаковые сабмиты (юзер+агент[+idempotency_key]) сериализуются per-key lock →
    # второй дождётся первого и заберёт результат из кэша (двойной клик не запускает двойной прогон)
    idem = str((body or {}).get("idempotency_key", "")).strip()
    lock_key = f"{actor}:{agent_id}:{idem}"
    async with _run_lock(lock_key):
        out = await execute_agent_run(agent, contract, actor, use_cache=use_cache,
                                      user_context=user_context, deliver_filter=deliver_filter)
    return JSONResponse({"run_id": out["saved"]["id"], **out["result"]}, status_code=201)


@app.get("/api/runs")
async def runs_list(agent_id: str = "", u: dict = Depends(user)) -> dict:
    """Журнал прогонов (последние 100). ABAC: не-admin видит только прогоны агентов своего отдела.
    Обогащается именем/семьёй агента для отображения во Флоте/Обзоре."""
    items = await run_store.list_runs(agent_id=agent_id or None, limit=100)
    # обогащение агентом: берём ЛЁГКИЕ сводки одним запросом (id/name/family/version/role без graph),
    # вместо полного agent_store.get() на каждого агента (тот тянул тяжёлый graph-JSONB ради 4 полей).
    briefs = {a["id"]: a for a in await agent_store.list_for(None)}
    briefs.update({a["id"]: a for a in await agent_store.list_for(None, archived=True)})  # + Лимб
    out = []
    for it in items:
        ag = briefs.get(it.get("agent_id"), {})
        fam = ag.get("family")
        if not can_see_family(u, fam):
            continue
        it["agent_name"] = ag.get("name") or it.get("agent_id")
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
async def hitl_queue(u: dict = Depends(user)) -> dict:
    """Очередь HITL-подтверждений: pending-заявки на доставку наружу (ABAC по семье агента)."""
    items = await hitl_store.list_pending()
    return {"queue": [i for i in items if can_see_family(u, i.get("family"))]}


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
    if decision == "reject":
        await hitl_store.decide(item_id, "rejected", actor, reason)
        await audit_store.record(actor, "hitl.reject", item_id,
                                 {"agent_id": item.get("agent_id"), "channel": item.get("channel")}, severity="warn")
        obs.inc("abop_hitl_total", decision="reject")
        return JSONResponse({"id": item_id, "state": "rejected"})
    # approve → реальная отправка в канал
    payload = item.get("payload") or {}
    cfg = payload.get("cfg") or {}
    out = await _send_channel(cfg, payload.get("agent_name"), payload.get("html") or "", real=True)
    await hitl_store.decide(item_id, "approved", actor, reason)
    await audit_store.record(actor, "hitl.approve", item_id,
                             {"agent_id": item.get("agent_id"), "channel": item.get("channel"),
                              "result": str(out)[:200]}, severity="info")
    obs.inc("abop_hitl_total", decision="approve")
    obs.log_event("info", "hitl.approved", item_id=item_id, channel=item.get("channel"))
    return JSONResponse({"id": item_id, "state": "approved", "delivery": str(out)[:400]})


# ═══════════════ Статика: buildless-React фронт ABOP (webapp/) ═══════════════
# Монтируется ПОСЛЕ всех /api-роутов, чтобы они имели приоритет. html=True → SPA-fallback.
# UI ДЕСКТОПА (desktop/ui) раздаём по /desktop-ui/ — Electron грузит его по сети (APE_UI_URL),
# правки чат-панели прилетают через git-deploy БЕЗ пересборки .exe (см. ADR смычки, путь А).
_DESKTOP_UI = Path(__file__).resolve().parents[1] / "desktop" / "ui"
if _DESKTOP_UI.is_dir():
    app.mount("/desktop-ui", StaticFiles(directory=str(_DESKTOP_UI), html=True), name="desktop-ui")
_WEBAPP = Path(__file__).resolve().parents[1] / "webapp"
if _WEBAPP.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEBAPP), html=True), name="webapp")
