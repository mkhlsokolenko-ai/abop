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

from . import agent_store, assembly, clients, contract_store, ingress, run_store, runner  # noqa: E402

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


@app.get("/api/skills")
def skills(u: dict = Depends(user)) -> dict:
    """Каталог навыков с безопасностью (permission-scoping видимо). §4 ABOP_SCREENS."""
    in_fam = {}
    for fid, fam in ape.AGENT_FAMILIES.items():
        for mk, (_mt, sk) in fam["members"].items():
            for s in sk:
                in_fam.setdefault(s, []).append(fid)
    out = []
    for sid, (title, short, _instr) in ape.SKILLS.items():
        parsed = ape.parse_skill_md(sid)   # секции/шаги — чтобы drawer не был пустым
        out.append({"id": sid, "title": title, "short": short,
                    "safety": ape.skill_safety(sid), "scope": ape.skill_scope(sid),
                    "families": sorted(set(in_fam.get(sid, []))),
                    "sections": parsed["sections"], "flow": parsed["flow"],
                    "when": parsed["when"], "method": parsed["method"],
                    "dod": parsed["dod"], "anti": parsed["anti"],
                    "datasources": ape.skill_datasources_resolved(sid)})
    return {"skills": out, "count": len(out)}


@app.get("/api/skills/{sid}")
def skill(sid: str, u: dict = Depends(user)) -> dict:
    """Полное тело навыка (progressive disclosure = use_skill). §4 drawer."""
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    title, short, _ = ape.SKILLS[sid]
    parsed = ape.parse_skill_md(sid)
    return {"id": sid, "title": title, "short": short, "safety": ape.skill_safety(sid),
            "scope": ape.skill_scope(sid), "body": ape.load_skill_body(sid),
            "sections": parsed["sections"], "flow": parsed["flow"], "intro": parsed["intro"],
            "when": parsed["when"], "method": parsed["method"],
            "dod": parsed["dod"], "anti": parsed["anti"],
            "datasources": ape.skill_datasources_resolved(sid)}


@app.post("/api/skills/{sid}/datasources")
def skill_datasources_set(sid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Оператор правит data-need навыка (ADR-032): какие сущности/поля берёт навык. §4 drawer.
    Тело: {datasources:[{entity, fields[], kind?, note?}]}. Возвращает резолвнутые (с рецептом по entity)."""
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    ape.set_skill_datasources(sid, (body or {}).get("datasources") or [])
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


# ── Коннекторы-инстансы (подключённые источники) — §5 таб «Коннекторы» ──
@app.get("/api/data/connectors")
def connectors_list(u: dict = Depends(user)) -> dict:
    return {"connectors": ape.data_connectors()}


@app.post("/api/data/connectors")
def connector_save(body: dict, u: dict = Depends(user)) -> dict:
    """Подключить коннектор (сохранить инстанс источника). §5 [＋ подключить]."""
    if not str((body or {}).get("title", "")).strip():
        raise HTTPException(422, "нужен title коннектора")
    return ape.data_save_connector(body)


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
def recipe_save(body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить рецепт (нормализует UI-форму в canonical). §5 [Сохранить]."""
    name = str((body or {}).get("recipe") or (body or {}).get("title", "")).strip()
    if not name:
        raise HTTPException(422, "нужно имя рецепта")
    try:
        return ape.data_save_recipe(name, body)
    except Exception as ex:  # noqa: BLE001 — любой сбой нормализации → 400, не 500
        raise HTTPException(400, f"не удалось сохранить рецепт: {ex}")


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


@app.get("/api/data/query/{entity}")
def data_query(entity: str, limit: int = 30, u: dict = Depends(user)) -> dict:
    """Чтение canonical store (только свежие). §5 предпросмотр."""
    return {"entity": entity, "records": ape.data_query(entity, limit=limit)}


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
    version = await agent_store.next_version(audit_id)
    saved = await agent_store.save(name=name, audit_id=audit_id, version=version, graph=graph,
                                   autonomy_max=check["autonomy_max"],
                                   created_by=u.get("name") or u.get("sub") or "dev",
                                   family=fam, role=role)
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


@app.post("/api/runs")
async def run_start(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Запуск прогона агента (детерминированно, без LLM в раскладке).

    Тело: {agent_id} ИЛИ {contract_audit_id} (берётся последний AgentVersion контракта).
    Возвращает 201 {run_id, waves, board, verdict, run_metrics}.
    """
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
        # авторинг-агент (source=authored) без контракта LUDA → песочница-конверт (ADR-029: только A0/тест)
        contract = {"intake": {"autonomy_ceiling": agent.get("autonomy_max") or "A2"}, "bundle": {}}
    # НАСТОЯЩИЙ LLM-прогон: навыки читают canonical store и анализируют через RouteAI (агентский движок)
    result = await runner.run_live(agent, contract, ape.skill_safety,
                                   data_query=ape.data_query,
                                   skill_sources=ape.skill_datasources_resolved,
                                   load_body=ape.load_skill_body,
                                   chat_fn=clients.chat)
    # Петля прогон→канва: для аудит-агента доносим СТРУКТУРИРОВАННЫЕ находки (детерминир. движок, не LLM)
    # прямо в результат Run — чтобы «Строю» идемпотентно подхватывал их из последнего прогона.
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
    saved = await run_store.save(result)
    return JSONResponse({"run_id": saved["id"], **result}, status_code=201)


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
