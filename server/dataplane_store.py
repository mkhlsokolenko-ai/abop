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

from . import tenancy
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
# Арендатор в ПЕРВИЧНОМ ключе: имя рецепта приходит из пакета вертикали и у двух заказчиков
# одинаково («omninet_feed»), а внутри — адрес источника и креды конкретной компании.
SCHEMA += tenancy.миграция_ключа("dp_recipes", "name")
SCHEMA += tenancy.миграция_ключа("dp_connectors", "id")

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
        return [v for k, v in _MEM_RECIPES.items() if k.startswith(tenancy.ключ_памяти(""))]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT spec FROM dp_recipes WHERE tenant=%s", (tenancy.текущий(),))
        return [r[0] for r in await cur.fetchall()]


async def save_recipe(name: str, spec: dict, editor: str = "dev") -> dict:
    if not _has_pg():
        _MEM_RECIPES[tenancy.ключ_памяти(name)] = spec
        return spec
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dp_recipes (name,spec,editor,updated_at,tenant) VALUES (%s,%s,%s,now(),%s) "
            "ON CONFLICT (tenant, name) DO UPDATE SET spec=EXCLUDED.spec, editor=EXCLUDED.editor, updated_at=now()",
            (name, json.dumps(spec), editor, tenancy.текущий()))
    return spec


# ─── коннекторы ───
async def connectors_all() -> list[dict]:
    """Все коннекторы-инстансы (карточки) — для инъекции в ape (data_connectors)."""
    if not _has_pg():
        return [v for k, v in _MEM_CONNECTORS.items() if k.startswith(tenancy.ключ_памяти(""))]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT spec FROM dp_connectors WHERE tenant=%s",
                                 (tenancy.текущий(),))
        return [r[0] for r in await cur.fetchall()]


async def delete_recipe(name: str) -> bool:
    """Удалить рецепт (UX-аудит W-H5: раньше UI удалял локально, «призрак» возвращался после F5)."""
    if not _has_pg():
        return _MEM_RECIPES.pop(tenancy.ключ_памяти(name), None) is not None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM dp_recipes WHERE name=%s AND tenant=%s",
                                 (name, tenancy.текущий()))
        return (cur.rowcount or 0) > 0


async def delete_connector(cid: str) -> bool:
    if not _has_pg():
        return _MEM_CONNECTORS.pop(tenancy.ключ_памяти(cid), None) is not None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM dp_connectors WHERE id=%s AND tenant=%s",
                                 (cid, tenancy.текущий()))
        return (cur.rowcount or 0) > 0


async def save_connector(cid: str, card: dict, editor: str = "dev") -> dict:
    if not _has_pg():
        _MEM_CONNECTORS[tenancy.ключ_памяти(cid)] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dp_connectors (id,spec,editor,updated_at,tenant) VALUES (%s,%s,%s,now(),%s) "
            "ON CONFLICT (tenant, id) DO UPDATE SET spec=EXCLUDED.spec, editor=EXCLUDED.editor, updated_at=now()",
            (cid, json.dumps(card), editor, tenancy.текущий()))
    return card
