"""Хранилище прогонов (Run) — результаты исполнения агентов. Postgres + фолбэк в память."""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    contract_audit_id TEXT,
    verdict_ok    BOOLEAN,
    autonomy_used TEXT,
    hitl_count    INT,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs (agent_id);
"""

_MEM: dict[str, dict] = {}
_SEQ = {"n": 0}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def _next_id(agent_id: str) -> str:
    if _has_pg():  # in-memory счётчик сбрасывается при рестарте → коллизия с PG; случайный суффикс безопасен
        import secrets
        return f"run-{agent_id}-{secrets.token_hex(3)}"
    _SEQ["n"] += 1
    return f"run-{agent_id}-{_SEQ['n']:03d}"


async def save(run: dict) -> dict:
    rid = await _next_id(run["agent_id"])
    row = dict(run); row["id"] = rid
    if not _has_pg():
        import datetime as _dt
        row["created_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _MEM[rid] = row
        return row
    from .db import _conn
    v = run.get("verdict") or {}
    g = (run.get("run_metrics") or {}).get("governance") or {}
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO runs (id,agent_id,contract_audit_id,verdict_ok,autonomy_used,hitl_count,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (rid, run["agent_id"], run.get("contract_audit_id"), bool(v.get("ok")),
             v.get("autonomy_used"), int(g.get("hitl_count") or 0), json.dumps(run)))
    out = await get(rid)
    if out is not None:
        out["id"] = rid  # payload хранит run без id — восстанавливаем
        return out
    return row


async def list_runs(agent_id: str | None = None, limit: int = 100) -> list[dict]:
    """Сводки прогонов (для журнала): без тяжёлого payload, свежие сверху."""
    if not _has_pg():
        rows = [r for r in _MEM.values() if not agent_id or r.get("agent_id") == agent_id]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        out = []
        for r in rows[:limit]:
            v = r.get("verdict") or {}
            g = (r.get("run_metrics") or {}).get("governance") or {}
            out.append({"id": r["id"], "agent_id": r.get("agent_id"),
                        "contract_audit_id": r.get("contract_audit_id"),
                        "verdict_ok": bool(v.get("ok")), "autonomy_used": v.get("autonomy_used"),
                        "hitl_count": int(g.get("hitl_count") or 0), "created_at": r.get("created_at"),
                        "started_by": r.get("started_by"),
                        "cost": (r.get("run_metrics") or {}).get("cost") or {}})
        return out
    from .db import _conn
    cols = ("id,agent_id,contract_audit_id,verdict_ok,autonomy_used,hitl_count,created_at,"
            "payload->>'started_by',payload->'run_metrics'->'cost'")
    async with _conn() as conn:
        if agent_id:
            cur = await conn.execute(
                f"SELECT {cols} FROM runs WHERE agent_id=%s ORDER BY created_at DESC LIMIT %s", (agent_id, limit))
        else:
            cur = await conn.execute(
                f"SELECT {cols} FROM runs ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = await cur.fetchall()
    return [{"id": r[0], "agent_id": r[1], "contract_audit_id": r[2], "verdict_ok": r[3],
             "autonomy_used": r[4], "hitl_count": r[5],
             "created_at": r[6].isoformat() if r[6] else None, "started_by": r[7],
             "cost": r[8] or {}} for r in rows]


async def get(run_id: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(run_id)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT payload, created_at FROM runs WHERE id=%s", (run_id,))
        r = await cur.fetchone()
    if not r:
        return None
    out = dict(r[0]); out["created_at"] = r[1].isoformat() if r[1] else None
    return out
