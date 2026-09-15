"""Журнал срабатываний триггеров + курсоры событийных источников — Postgres + mem.

Сами триггеры живут в графе агента (kind:'trigger', agent_store). Здесь — НАБЛЮДАЕМОСТЬ
исполнения: лог фаеров (когда, чем, какой run_id, статус) и курсоры для event-источников
(сколько сообщений/задач уже видели, чтобы стрелять только по НОВЫМ). См. server/triggers.py
(планировщик) и persistence-localstorage-hole.

trigger_fires{id, agent_id, trigger_id, kind, run_id, status, note, fired_at}
trigger_cursors{key PK, value, updated_at}  -- ключ = agent:trigger:source
"""
from __future__ import annotations

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger_fires (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    trigger_id  TEXT NOT NULL,
    kind        TEXT,
    run_id      TEXT,
    status      TEXT NOT NULL DEFAULT 'fired',   -- fired|pending_hitl|skipped|error
    note        TEXT,
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fires_at ON trigger_fires (fired_at DESC);
CREATE TABLE IF NOT EXISTS trigger_cursors (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM_FIRES: list[dict] = []
_MEM_CURSORS: dict[str, str] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def record_fire(agent_id: str, trigger_id: str, kind: str, run_id: str | None,
                      status: str = "fired", note: str = "") -> None:
    if not _has_pg():
        import datetime as _dt
        _MEM_FIRES.insert(0, {"agent_id": agent_id, "trigger_id": trigger_id, "kind": kind,
                              "run_id": run_id, "status": status, "note": note,
                              "fired_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO trigger_fires (agent_id,trigger_id,kind,run_id,status,note) VALUES (%s,%s,%s,%s,%s,%s)",
            (agent_id, trigger_id, kind, run_id, status, note))


async def last_fire_at(agent_id: str, trigger_id: str):
    """Время последнего УСПЕШНОГО фаера (datetime) или None."""
    if not _has_pg():
        for f in _MEM_FIRES:
            if f["agent_id"] == agent_id and f["trigger_id"] == trigger_id and f["status"] == "fired":
                return f["fired_at"]
        return None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT fired_at FROM trigger_fires WHERE agent_id=%s AND trigger_id=%s AND status='fired' "
            "ORDER BY fired_at DESC LIMIT 1", (agent_id, trigger_id))
        r = await cur.fetchone()
    return r[0] if r else None


async def list_fires(limit: int = 50) -> list[dict]:
    if not _has_pg():
        return _MEM_FIRES[:limit]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT agent_id,trigger_id,kind,run_id,status,note,fired_at FROM trigger_fires "
            "ORDER BY fired_at DESC LIMIT %s", (limit,))
        rows = await cur.fetchall()
    return [{"agent_id": r[0], "trigger_id": r[1], "kind": r[2], "run_id": r[3], "status": r[4],
             "note": r[5], "fired_at": r[6].isoformat() if r[6] else None} for r in rows]


async def get_cursor(key: str) -> str | None:
    if not _has_pg():
        return _MEM_CURSORS.get(key)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT value FROM trigger_cursors WHERE key=%s", (key,))
        r = await cur.fetchone()
    return r[0] if r else None


async def set_cursor(key: str, value: str) -> None:
    if not _has_pg():
        _MEM_CURSORS[key] = value
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO trigger_cursors (key,value,updated_at) VALUES (%s,%s,now()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", (key, str(value)))
