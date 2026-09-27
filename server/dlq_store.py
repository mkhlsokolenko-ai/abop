"""Разбор DLQ (27.09.2026): отметки оператора по сообщениям `abop.dlq` — Postgres + mem.

Kafka-топик нельзя «удалить сообщение», поэтому состояние разбора живём отдельно: ключ = partition:offset в DLQ,
state ∈ {acked (списано, разобрано руками), replayed (команда переопубликована коннектору)}, кто и когда, заметка.
`GET /api/bus/dlq` подмешивает эти отметки к хвосту топика — оператор видит, что уже разобрано.
"""
from __future__ import annotations

import datetime as _dt

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS dlq_acks (
    msg_key   TEXT PRIMARY KEY,
    state     TEXT NOT NULL,
    actor     TEXT,
    note      TEXT,
    replay_id TEXT,
    at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def key_of(partition, offset) -> str:
    return f"{int(partition)}:{int(offset)}"


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def mark(msg_key: str, state: str, actor: str, note: str = "", replay_id: str = "") -> dict:
    rec = {"msg_key": msg_key, "state": state, "actor": actor, "note": note or "", "replay_id": replay_id or "",
           "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    if not _has_pg():
        _MEM[msg_key] = rec
        return rec
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dlq_acks (msg_key,state,actor,note,replay_id,at) VALUES (%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (msg_key) DO UPDATE SET state=EXCLUDED.state, actor=EXCLUDED.actor, note=EXCLUDED.note, "
            "replay_id=EXCLUDED.replay_id, at=now()",
            (msg_key, state, actor, note or "", replay_id or ""))
    return rec


async def all() -> dict[str, dict]:
    if not _has_pg():
        return dict(_MEM)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT msg_key,state,actor,note,replay_id,at FROM dlq_acks ORDER BY at DESC LIMIT 2000")
        rows = await cur.fetchall()
    return {r[0]: {"msg_key": r[0], "state": r[1], "actor": r[2], "note": r[3], "replay_id": r[4],
                   "at": r[5].isoformat(timespec="seconds") if r[5] else None} for r in rows}
