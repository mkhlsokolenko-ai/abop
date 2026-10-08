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

from .. import audit_store, identity_store, userdata_store
from ..deps import _uid_of, require_level, user

router = APIRouter()


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
