"""Пользовательские данные (per-user) — черновики агентов (userScenarios) — в Postgres.

В отличие от общих для всех данных (agents/skills/recipes/admin_config), это ПЕР-ЮЗЕР:
незаконченная сборка агента на канве принадлежит конкретному оператору и не должна лезть
на канву другому. Ключ — `sub` из JWT (стабильный id пользователя Keycloak). Раньше лежало
в localStorage браузера (per-браузер, терялось при перенакате). Без DSN — фолбэк в память.
См. беклог persistence-localstorage-hole. Память агента (memory) — ОТДЕЛЬНО (пер-агент, не тут).

user_scenarios{user_sub PK, data JSONB (dict {key: scenario}), updated_at}.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_scenarios (
    user_sub    TEXT PRIMARY KEY,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_memory (
    scope       TEXT PRIMARY KEY,   -- сценарий/процесс агента (общая память, не пер-юзер)
    data        JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}
_MEM_AGMEM: dict[str, list] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def get_scenarios(user_sub: str) -> dict:
    """Черновики агентов конкретного пользователя (dict {key: scenario}). Нет записи ⇒ {}."""
    if not user_sub:
        return {}
    if not _has_pg():
        return dict(_MEM.get(user_sub) or {})
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT data FROM user_scenarios WHERE user_sub=%s", (user_sub,))
        r = await cur.fetchone()
    return dict(r[0]) if r and r[0] else {}


async def save_scenarios(user_sub: str, data: dict) -> dict:
    data = data or {}
    if not _has_pg():
        _MEM[user_sub] = dict(data)
        return data
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO user_scenarios (user_sub,data,updated_at) VALUES (%s,%s,now()) "
            "ON CONFLICT (user_sub) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
            (user_sub, json.dumps(data)))
    return data


# ─── Память агента (per-АГЕНТ/процесс, ОБЩАЯ для всех операторов — решение владельца 2026-09-15) ───
# Ключ scope = сценарий/процесс агента. Это не личные заметки, а знание агента (находки прогонов).
async def get_memory(scope: str) -> list | None:
    """Память процесса (list элементов) или None, если записи нет (⇒ клиент берёт свой сид)."""
    if not scope:
        return None
    if not _has_pg():
        v = _MEM_AGMEM.get(scope)
        return list(v) if v is not None else None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT data FROM agent_memory WHERE scope=%s", (scope,))
        r = await cur.fetchone()
    return list(r[0]) if r and r[0] is not None else None


async def save_memory(scope: str, data: list) -> list:
    data = data or []
    if not _has_pg():
        _MEM_AGMEM[scope] = list(data)
        return data
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO agent_memory (scope,data,updated_at) VALUES (%s,%s,now()) "
            "ON CONFLICT (scope) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
            (scope, json.dumps(data)))
    return data
