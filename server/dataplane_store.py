"""Хранилище Data Plane — рецепты (source→canonical) и коннекторы-инстансы — в Postgres.

Раньше рецепты/коннекторы лежали файлами в ~/.ape (переживали редеплой на одном хосте, но не PG
и не для stateless-масштабирования по Хартии v3.5). Теперь — Postgres (общие для всех пользователей,
переживают перенакат/рестарт). Без DSN — фолбэк в память. Логика (нормализация/применение/preview/тест)
остаётся в ape; здесь только ПЕРСИСТЕНТНОСТЬ определений. См. беклог persistence-localstorage-hole.

recipe{name PK, spec JSONB (canonical), editor, updated_at}; connector{id PK, spec JSONB (карточка),
editor, updated_at}. Ингест данных (canonical store) — отдельный слой (data lake), тут не трогается.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS dp_recipes (
    name        TEXT PRIMARY KEY,
    spec        JSONB NOT NULL,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dp_connectors (
    id          TEXT PRIMARY KEY,
    spec        JSONB NOT NULL,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM_RECIPES: dict[str, dict] = {}
_MEM_CONNECTORS: dict[str, dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


# ─── рецепты ───
async def recipes_all() -> list[dict]:
    """Все canonical-рецепты (специи) — для инъекции в ape (data_recipes/data_load_recipe)."""
    if not _has_pg():
        return list(_MEM_RECIPES.values())
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT spec FROM dp_recipes")
        return [r[0] for r in await cur.fetchall()]


async def save_recipe(name: str, spec: dict, editor: str = "dev") -> dict:
    if not _has_pg():
        _MEM_RECIPES[name] = spec
        return spec
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dp_recipes (name,spec,editor,updated_at) VALUES (%s,%s,%s,now()) "
            "ON CONFLICT (name) DO UPDATE SET spec=EXCLUDED.spec, editor=EXCLUDED.editor, updated_at=now()",
            (name, json.dumps(spec), editor))
    return spec


# ─── коннекторы ───
async def connectors_all() -> list[dict]:
    """Все коннекторы-инстансы (карточки) — для инъекции в ape (data_connectors)."""
    if not _has_pg():
        return list(_MEM_CONNECTORS.values())
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT spec FROM dp_connectors")
        return [r[0] for r in await cur.fetchall()]


async def save_connector(cid: str, card: dict, editor: str = "dev") -> dict:
    if not _has_pg():
        _MEM_CONNECTORS[cid] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dp_connectors (id,spec,editor,updated_at) VALUES (%s,%s,%s,now()) "
            "ON CONFLICT (id) DO UPDATE SET spec=EXCLUDED.spec, editor=EXCLUDED.editor, updated_at=now()",
            (cid, json.dumps(card), editor))
    return card
