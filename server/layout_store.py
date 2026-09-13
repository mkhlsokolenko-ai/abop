"""Хранилище раскладки канвы (координаты узлов + рёбра) — Postgres + фолбэк в память.
Директива владельца: координаты блоков хранить в Postgres, не в localStorage.
Ключ = идентификатор графа на канве (contract_audit_id или имя сценария)."""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS canvas_layouts (
    key         TEXT PRIMARY KEY,
    nodes       JSONB NOT NULL,
    edges       JSONB NOT NULL,
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


async def save(key: str, nodes: list, edges: list) -> dict:
    row = {"key": key, "nodes": nodes, "edges": edges}
    if not _has_pg():
        import datetime as _dt
        row["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _MEM[key] = row
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO canvas_layouts (key,nodes,edges,updated_at) VALUES (%s,%s,%s,now()) "
            "ON CONFLICT (key) DO UPDATE SET nodes=EXCLUDED.nodes, edges=EXCLUDED.edges, updated_at=now()",
            (key, json.dumps(nodes), json.dumps(edges)))
    return row


async def get(key: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(key)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT nodes, edges, updated_at FROM canvas_layouts WHERE key=%s", (key,))
        r = await cur.fetchone()
    if not r:
        return None
    return {"key": key, "nodes": r[0], "edges": r[1],
            "updated_at": r[2].isoformat() if r[2] else None}
