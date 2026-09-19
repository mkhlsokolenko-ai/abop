"""Кэш результатов прогона — Postgres + mem. Multi-user: много юзеров гоняют ТЕ ЖЕ сценарии на ТЕХ ЖЕ
данных → первый считает (LLM ~сек), остальные получают результат мгновенно из кэша.

Ключ VERSION-AWARE: agent_id + отпечаток версии данных (счёт+max fetched_at сущностей) + конфиг
(max_tokens/structured/модель). Смена данных (перезалили рецепт)/агента/настроек → новый ключ →
пересчёт (старые записи осиротеют, чистятся по возрасту). Корректность без «слепого» TTL.
Общий для всех реплик (PG). Тумблер ABOP_RUN_CACHE=0. См. docs/CONCEPT §кэш.

run_cache{cache_key PK, payload JSONB, created_at}.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_cache (
    cache_key   TEXT PRIMARY KEY,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_cache_ts ON run_cache (created_at);
"""

_MEM: dict[str, dict] = {}
_TTL_DAYS = 7  # осиротевшие (по старым версиям данных) записи чистим по возрасту


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def get(key: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(key)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT payload FROM run_cache WHERE cache_key=%s", (key,))
        row = await cur.fetchone()
    return row[0] if row else None


async def put(key: str, payload: dict) -> None:
    if not _has_pg():
        _MEM[key] = payload
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO run_cache (cache_key,payload,created_at) VALUES (%s,%s,now()) "
            "ON CONFLICT (cache_key) DO UPDATE SET payload=EXCLUDED.payload, created_at=now()",
            (key, json.dumps(payload, ensure_ascii=False)))
        # лёгкая чистка осиротевших записей (старые версии данных больше не запрашиваются)
        await conn.execute("DELETE FROM run_cache WHERE created_at < now() - interval '%s days'" % _TTL_DAYS)
