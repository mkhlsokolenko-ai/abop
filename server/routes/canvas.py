# -*- coding: utf-8 -*-
"""Канва: раскладка узлов, которую человек двигал руками.

Положение блоков — часть работы автора, а не косметика: в следующий раз он ищет узлы
там, где их оставил. Раскладка хранится отдельно от графа, чтобы правка координат не
плодила версий агента.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import layout_store

from ..deps import require_level, user

router = APIRouter()


@router.get("/api/canvas/layout/{key}")
async def canvas_layout_get(key: str, u: dict = Depends(user)) -> dict:
    """Прочитать сохранённую раскладку канвы (узлы с x/y + рёбра) по ключу (контракт/сценарий)."""
    row = await layout_store.get(key)
    return row or {"key": key, "nodes": None, "edges": None}


@router.post("/api/canvas/layout/{key}")
async def canvas_layout_save(key: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить раскладку канвы в Postgres. Тело: {nodes:[{id,kind,x,y,...}], edges:[...]}."""
    require_level(u, "manager")  # правка раскладки — мутация (analyst read-only)
    nodes = (body or {}).get("nodes") or []
    edges = (body or {}).get("edges") or []
    saved = await layout_store.save(key, nodes, edges)
    return {"key": key, "saved": True, "updated_at": saved.get("updated_at")}
