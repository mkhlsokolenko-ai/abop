# -*- coding: utf-8 -*-
"""Общая доска: долгая память человека и отдела.

Факт на доске имеет автора-человека, даже если текст готовил агент: решение вынести
что-то в общую память принимает человек. Доска переживает прогоны и видна отделу.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import audit_store, blackboard

from ..deps import _sub, user

from .runs import _board_scope

router = APIRouter()


@router.get("/api/board")
async def board_read(kind: str = "user", family: str = "", stale: bool = False,
                     u: dict = Depends(user)) -> dict:
    """Факты доски: личной или отдела. Просроченные не отдаём, пока не попросят явно."""
    scope = _board_scope(kind, u, family)
    rows = await blackboard.load(scope, include_stale=bool(stale))
    return {"scope": scope, "facts": rows, "count": len(rows)}


@router.post("/api/board/facts")
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


@router.delete("/api/board/facts/{key}")
async def board_drop(key: str, kind: str = "user", family: str = "", author: str = "",
                     u: dict = Depends(user)) -> dict:
    scope = _board_scope(kind, u, family)
    n = await blackboard.drop_fact(scope, key, author)
    await audit_store.record(u.get("name") or _sub(u), "board.drop", scope, {"key": key})
    return {"ok": True, "removed": n}
