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

from . import contract_store, ingress  # noqa: E402

BIZ_FAMILIES = {"analytics", "finance", "architecture", "management"}


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


def user(request: Request) -> dict:
    """Текущий пользователь из Bearer-JWT. Без JWKS (dev) — dev-user."""
    if not _jwks_url():
        return {"sub": "dev", "name": "dev", "roles": ["developer"], "dev": True}
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "нужен Bearer-JWT")
    import jwt
    try:
        key = _client().get_signing_key_from_jwt(auth[7:]).key
        claims = jwt.decode(auth[7:], key, algorithms=["RS256"],
                            audience=os.getenv("KEYCLOAK_AUDIENCE") or None,
                            issuer=os.getenv("KEYCLOAK_ISSUER") or None,
                            options={"verify_aud": bool(os.getenv("KEYCLOAK_AUDIENCE"))})
    except Exception:  # noqa: BLE001
        raise HTTPException(401, "невалидный токен")
    roles = (claims.get("realm_access") or {}).get("roles") or []
    return {"sub": claims.get("sub"), "name": claims.get("preferred_username") or claims.get("name"),
            "roles": roles, "dev": False}


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
        out.append({"id": sid, "title": title, "short": short,
                    "safety": ape.skill_safety(sid), "scope": ape.skill_scope(sid),
                    "families": sorted(set(in_fam.get(sid, [])))})
    return {"skills": out, "count": len(out)}


@app.get("/api/skills/{sid}")
def skill(sid: str, u: dict = Depends(user)) -> dict:
    """Полное тело навыка (progressive disclosure = use_skill). §4 drawer."""
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    title, short, _ = ape.SKILLS[sid]
    return {"id": sid, "title": title, "short": short, "safety": ape.skill_safety(sid),
            "scope": ape.skill_scope(sid), "body": ape.load_skill_body(sid)}


@app.get("/api/data/adapters")
def adapters(u: dict = Depends(user)) -> dict:
    """Подключённые источники (реестр адаптеров). §5 Коннекторы."""
    return {"adapters": sorted(ape.SOURCE_ADAPTERS)}


@app.get("/api/data/schema/{entity}")
def data_schema(entity: str, u: dict = Depends(user)) -> dict:
    """Data Contract сущности (обязательные поля). §5 редактор рецепта."""
    return ape.data_schema(entity)


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
    """Создать таблицу contract_sets, если есть Postgres (иначе фолбэк в память)."""
    await contract_store.init()


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


# ═══════════════ RUN / STREAM / HITL: контракт (исполнение — следующий инкремент) ═══════════════

_NOT_IMPL = ("Прогон через API требует выноса cmd_agents в фон + SSE-стрим (следующий инкремент). "
             "Форма ответа зафиксирована в docs/ABOP_API.md; сейчас — 501.")


@app.post("/api/runs", status_code=501)
def run_start(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Запуск прогона. Контракт: {goal} → 202 {run_id, plan}. Сейчас 501 (см. note)."""
    return JSONResponse({"detail": _NOT_IMPL,
                         "contract": {"request": {"goal": "str"},
                                      "response": {"run_id": "str", "plan": "waves[]"}}},
                        status_code=501)


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
