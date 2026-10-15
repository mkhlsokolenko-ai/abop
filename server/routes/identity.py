# -*- coding: utf-8 -*-
"""Сквозной ID и личное: кто я, мои аккаунты во внешних системах, мои сценарии и память.

Домен вынесен из монолита (ADR-052, docs/RASKLADKA_SERVERA.md). Связывает мастер-UID человека с
его учётками в почте, трекере, CRM и 1С — тот самый адресный контекст, который наследует агент,
запущенный от имени пользователя (crosscutting-identity).

Правило домена: чужую связку видит только тот, у кого область `*`. Иначе карта аккаунтов стала бы
способом узнать, под каким именем коллега ходит в учётную систему.

Зависимости наружу: `server/deps` (личность и гейты) и сторы; `web_api` не импортируется.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import audit_store, identity_store, personal_kb, userdata_store
from ..deps import _uid_of, require_level, user

from .. import admin_store, agent_store, run_store
from ..deps import can_see_family, user

router = APIRouter()


def _parse_rub(v, default: float) -> float:
    """Число рублей из значения квоты ('4 000 ₽' → 4000). Пусто/мусор → default."""
    if isinstance(v, (int, float)):
        return float(v)
    import re as _re
    digits = _re.sub(r"[^\d]", "", str(v or ""))
    return float(digits) if digits else default




@router.get("/api/me")
def me(u: dict = Depends(user)) -> dict:
    return {"user": u}


@router.get("/api/identity/me")
async def identity_me(u: dict = Depends(user)) -> dict:
    """Сквозной профиль ТЕКУЩЕГО пользователя: мастер-UID + department/roles (ABAC/RBAC из JWT) +
    карта его аккаунтов во внешних системах (почта/Redmine/CRM/1С). Это адресный контекст, который
    наследует агент, запущенный от имени пользователя."""
    return await identity_store.bundle(_uid_of(u), department=u.get("department"), roles=u.get("roles"))


@router.get("/api/identity/{uid}")
async def identity_get(uid: str, u: dict = Depends(user)) -> dict:
    """Сквозной профиль пользователя по UID. Свой профиль — всем; чужой — только admin/support (*)."""
    if uid != _uid_of(u) and u.get("department") not in ("*", None):
        raise HTTPException(403, "нет доступа к чужой идентичности")
    return await identity_store.bundle(uid)


@router.post("/api/identity/{uid}")
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


@router.delete("/api/identity/{uid}/{system}")
async def identity_unlink(uid: str, system: str, u: dict = Depends(user)) -> dict:
    """Отвязать аккаунт пользователя в системе. Admin-уровень."""
    require_level(u, "admin")
    await identity_store.unlink(uid, system)
    await audit_store.record(_uid_of(u) or "dev", "identity.unlink", f"{uid}:{system}", {})
    return {"ok": True}


@router.get("/api/me/scenarios")
async def my_scenarios(u: dict = Depends(user)) -> dict:
    """Черновики агентов ТЕКУЩЕГО пользователя (per-user, ключ = JWT sub) из Postgres — не localStorage,
    не видны другим. §БД-фаза (persistence-localstorage-hole)."""
    return {"scenarios": await userdata_store.get_scenarios(u.get("sub") or "")}


@router.post("/api/me/scenarios")
async def my_scenarios_save(body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить черновики агентов текущего пользователя в Postgres. Тело: {scenarios:{key:scenario}}."""
    data = (body or {}).get("scenarios")
    if not isinstance(data, dict):
        raise HTTPException(422, "нужен объект scenarios")
    saved = await userdata_store.save_scenarios(u.get("sub") or "", data)
    return {"scenarios": saved}

@router.get("/api/memory/{scope}")
async def memory_get(scope: str, u: dict = Depends(user)) -> dict:
    """Память агента/процесса (per-АГЕНТ, ОБЩАЯ для всех — не пер-юзер, не localStorage). scope =
    сценарий/процесс. None ⇒ клиент берёт свой сид. §БД-фаза (persistence-localstorage-hole)."""
    mem = await userdata_store.get_memory(scope)
    return {"scope": scope, "memory": mem, "seeded": mem is not None}


@router.post("/api/memory/{scope}")
async def memory_save(scope: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить память агента/процесса в Postgres (общая для всех операторов). Тело: {memory:[...]}."""
    require_level(u, "manager")
    data = (body or {}).get("memory")
    if not isinstance(data, list):
        raise HTTPException(422, "нужен массив memory")
    saved = await userdata_store.save_memory(scope, data)
    return {"scope": scope, "memory": saved}


@router.get("/api/billing")
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


# ── Личное пространство человека ──────────────────────────────────────────────────────────────
# Зачем: навыки вроде `bft-draft` объявляют вход «из контекста» — расшифровку интервью может
# принести только человек, вставив её в чат. Личное пространство кладёт этот материал на сервер,
# и агент находит его сам. Чужое личное не видно НИКОМУ, включая администратора: область `*` даёт
# право на системы организации, а не на чужие черновики.


@router.get("/api/me/kb")
async def my_kb(u: dict = Depends(user)) -> dict:
    """Мои личные документы (без текста) и имя личной коллекции."""
    sub = _uid_of(u)
    документы = await personal_kb.documents(sub)
    return {"документы": [personal_kb.карточка_для_ui(d) for d in документы],
            "коллекция": personal_kb.collection_for(sub),
            "правило": "личное годится, чтобы сформулировать и составить; считать и сверять по нему нельзя"}


@router.post("/api/me/kb")
async def my_kb_add(body: dict, u: dict = Depends(user)) -> dict:
    """Положить документ в личное пространство: {name, text, kind?, source?}."""
    sub = _uid_of(u)
    try:
        карточка = await personal_kb.add(sub, str((body or {}).get("name") or ""),
                                         str((body or {}).get("text") or ""),
                                         kind=str((body or {}).get("kind") or "note"),
                                         source=str((body or {}).get("source") or "desktop"))
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    except Exception as ex:  # noqa: BLE001 — хранилище знаний недоступно: честный отказ, не «ок»
        raise HTTPException(502, f"личное хранилище не приняло документ: {type(ex).__name__}")
    await audit_store.record(u.get("name") or sub, "me.kb.add", карточка["id"],
                             {"символов": карточка["chars"], "вид": карточка["kind"]})
    return {"документ": personal_kb.карточка_для_ui(карточка)}


@router.post("/api/me/kb/search")
async def my_kb_search(body: dict | None = None, u: dict = Depends(user)) -> dict:
    """Поиск по моему личному пространству: {q, top_k?}. Имя документа едет вместе с куском."""
    sub = _uid_of(u)
    тело = body or {}
    куски = await personal_kb.search(sub, str(тело.get("q") or ""),
                                     top_k=max(1, min(int(тело.get("top_k") or 4), 20)))
    return {"куски": куски, "сколько": len(куски)}


@router.delete("/api/me/kb/{doc_id}")
async def my_kb_remove(doc_id: str, u: dict = Depends(user)) -> dict:
    """Убрать свой документ из личного пространства."""
    sub = _uid_of(u)
    убрано = await personal_kb.remove(sub, doc_id)
    if not убрано:
        raise HTTPException(404, "нет такого документа в вашем личном пространстве")
    await audit_store.record(u.get("name") or sub, "me.kb.remove", doc_id, {})
    return {"id": doc_id, "удалён": True}
