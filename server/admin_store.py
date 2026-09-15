"""Хранилище админ-конфига среды (модели/арендаторы/квоты/пороги эскалации) — Postgres + mem.

Раньше эти настройки правились в UI и писались в localStorage браузера (per-браузер, не
мультиюзер, конфликтовали с БД, терялись при перенакате версий). Теперь — Postgres: общие для
всех операторов, переживают рестарт/редеплой. Без DSN — фолбэк в память. См. беклог
persistence-localstorage-hole (7.2 админ-панель).

app_config{key PK, value JSONB, editor, updated_at}. Ключи (whitelist в web_api): modelCfg
(профили моделей по периметру), defaultProfile, tenantMode (арендаторы byo/shared), quotaLimit,
quotaPolicy, escThresholds (пороги эскалации ИБ). Отсутствие ключа ⇒ клиент берёт свой дефолт
(сид в state) — так админ-настройки не хардкодятся на сервере, а лишь ПЕРСИСТЯТ правки.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def all() -> dict:
    """key → value (только сохранённые в PG правки; отсутствующие ⇒ дефолт на клиенте)."""
    if not _has_pg():
        return {k: v["value"] for k, v in _MEM.items()}
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT key,value FROM app_config")
        return {r[0]: r[1] for r in await cur.fetchall()}


async def save(key: str, value, editor: str = "dev"):
    if not _has_pg():
        _MEM[key] = {"value": value, "editor": editor}
        return value
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO app_config (key,value,editor,updated_at) VALUES (%s,%s,%s,now()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, editor=EXCLUDED.editor, updated_at=now()",
            (key, json.dumps(value), editor))
    return value
