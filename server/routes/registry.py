# -*- coding: utf-8 -*-
"""Реестр систем и least-privilege манифест области.

Домен вынесен из монолита (ADR-052, docs/RASKLADKA_SERVERA.md). Запись системы — единственное
место, где объявлено, куда вообще можно выйти наружу: эндпоинт, вид, отделы и семьи, которым он
открыт (agent-rbac-mcp-gateway). Манифест области отвечает на тот же вопрос с другой стороны —
что доступно конкретному отделу и что для него закрыто.

Зависимости наружу: `server/deps` и сторы; `web_api` не импортируется.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import access, audit_store, systems_store
from ..deps import require_level, user

router = APIRouter()


@router.get("/api/systems")
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


@router.get("/api/systems/{sid}")
async def system_get(sid: str, u: dict = Depends(user)) -> dict:
    s = await systems_store.get(sid)
    if not s:
        raise HTTPException(404, "нет такой системы")
    return s


@router.post("/api/systems/{sid}")
async def system_save(sid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить систему в реестр (эндпоинт/топики/auth_ref/egress). Тело: {kind, base_url, brokers[],
    topics[], auth_ref, tenant, egress, note}. Правка интеграций — уровень manager+ (RBAC-гейт)."""
    require_level(u, "manager")
    saved = await systems_store.save(sid, body or {}, editor=u.get("name") or u.get("sub") or "dev")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "system.save", sid,
                             {"kind": saved.get("kind"), "egress": saved.get("egress")})
    return saved


@router.delete("/api/systems/{sid}")
async def system_delete(sid: str, u: dict = Depends(user)) -> dict:
    require_level(u, "manager")
    await systems_store.delete(sid)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "system.delete", sid, {})
    return {"id": sid, "deleted": True}

@router.get("/api/access/manifest")
async def access_manifest(department: str = "", family: str = "", u: dict = Depends(user)) -> dict:
    """Least-privilege манифест области (отдел/семья): доступные/закрытые системы реестра + Qdrant-тенант.
    Основа гейта агентов на MCP-шлюзе (agent-rbac-mcp-gateway). Без параметров — область текущего юзера."""
    key = access.scope_key(department=department or None, family=family or None) if (department or family) \
        else access.scope_key(department=u.get("department"))
    return await access.manifest(key)


@router.post("/api/access/check")
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
