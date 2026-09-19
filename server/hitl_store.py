"""Очередь HITL-подтверждений — Postgres + mem. Human-in-the-loop для действий наружу.

Когда OUT-узел (или action-навык) помечен `hitl`, прогон НЕ отправляет наружу сразу, а кладёт
ЗАЯВКУ в очередь (state=pending) с уже готовым артефактом (отчёт+конфиг канала). Оператор видит
очередь (GET /api/hitl/queue), подтверждает/отклоняет (POST /api/hitl/{id}/approve) → при approve
выполняется РЕАЛЬНАЯ доставка. Так «Под HITL» перестаёт быть вечным ожиданием. ABAC по семье агента.

hitl_items{id PK, run_id, agent_id, family, node, title, channel, to_addr, payload JSONB,
           state, requested_by, decided_by, reason, created_at, decided_at}.
"""
from __future__ import annotations

import json
import uuid

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS hitl_items (
    id           TEXT PRIMARY KEY,
    run_id       TEXT,
    agent_id     TEXT,
    family       TEXT,
    node         TEXT,
    title        TEXT,
    channel      TEXT,
    to_addr      TEXT,
    payload      JSONB NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT,
    decided_by   TEXT,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_hitl_state ON hitl_items (state, created_at DESC);
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


async def create(*, run_id: str, agent_id: str, family: str, node: str, title: str,
                 channel: str, to_addr: str, payload: dict, requested_by: str) -> dict:
    item = {"id": "hitl-" + uuid.uuid4().hex[:12], "run_id": run_id, "agent_id": agent_id,
            "family": family, "node": node, "title": title, "channel": channel, "to_addr": to_addr,
            "payload": payload, "state": "pending", "requested_by": requested_by}
    if not _has_pg():
        _MEM[item["id"]] = item
        return item
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO hitl_items (id,run_id,agent_id,family,node,title,channel,to_addr,payload,"
            "state,requested_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)",
            (item["id"], run_id, agent_id, family, node, title, channel, to_addr,
             json.dumps(payload, ensure_ascii=False), requested_by))
    return item


async def get(item_id: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(item_id)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,run_id,agent_id,family,node,title,channel,to_addr,payload,state,requested_by,"
            "decided_by,reason FROM hitl_items WHERE id=%s", (item_id,))
        r = await cur.fetchone()
    if not r:
        return None
    keys = ["id", "run_id", "agent_id", "family", "node", "title", "channel", "to_addr", "payload",
            "state", "requested_by", "decided_by", "reason"]
    return dict(zip(keys, r))


async def list_pending() -> list[dict]:
    if not _has_pg():
        return [dict(v) for v in _MEM.values() if v.get("state") == "pending"]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,run_id,agent_id,family,node,title,channel,to_addr,created_at FROM hitl_items "
            "WHERE state='pending' ORDER BY created_at DESC LIMIT 100")
        rows = await cur.fetchall()
    keys = ["id", "run_id", "agent_id", "family", "node", "title", "channel", "to_addr", "created_at"]
    return [dict(zip(keys, r)) for r in rows]


async def decide(item_id: str, state: str, decided_by: str, reason: str = "") -> None:
    if not _has_pg():
        if item_id in _MEM:
            _MEM[item_id].update({"state": state, "decided_by": decided_by, "reason": reason})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "UPDATE hitl_items SET state=%s, decided_by=%s, reason=%s, decided_at=now() WHERE id=%s",
            (state, decided_by, reason, item_id))
