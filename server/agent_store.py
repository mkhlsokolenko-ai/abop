"""Хранилище AgentVersion — собранные на канве агенты (ADR-024 жизненный цикл).

AgentVersion{id, name, contract_audit_id, version, status, graph{nodes,edges}, autonomy_max}.
status: draft → tested → deployed → retired (пока сохраняем draft). Привязан к ContractSet
(audit_id) — автономия агента ограничена его DeploymentContract (ADR-013, проверка в assembly).
Персистентность — Postgres; без DSN — фолбэк в память (как contract_store).
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_versions (
    id            TEXT PRIMARY KEY,             -- <audit_id>.v<N>
    name          TEXT NOT NULL,
    contract_audit_id TEXT NOT NULL,
    version       INT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',-- draft|tested|deployed|retired
    autonomy_max  TEXT,                          -- фактический потолок автономии графа
    graph         JSONB NOT NULL,                -- {nodes[], edges[]}
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_av_contract ON agent_versions (contract_audit_id);
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


async def next_version(audit_id: str) -> int:
    if not _has_pg():
        return 1 + sum(1 for a in _MEM.values() if a["contract_audit_id"] == audit_id)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM agent_versions WHERE contract_audit_id=%s",
            (audit_id,))
        return int((await cur.fetchone())[0])


async def save(*, name: str, audit_id: str, version: int, graph: dict,
               autonomy_max: str, created_by: str = "dev") -> dict:
    aid = f"{audit_id}.v{version}"
    row = {"id": aid, "name": name, "contract_audit_id": audit_id, "version": version,
           "status": "draft", "autonomy_max": autonomy_max, "graph": graph, "created_by": created_by}
    if not _has_pg():
        import datetime as _dt
        row["created_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _MEM[aid] = row
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO agent_versions (id,name,contract_audit_id,version,status,autonomy_max,graph,created_by) "
            "VALUES (%s,%s,%s,%s,'draft',%s,%s,%s)",
            (aid, name, audit_id, version, autonomy_max, json.dumps(graph), created_by))
    return await get(aid) or row


async def get(agent_id: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(agent_id)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,name,contract_audit_id,version,status,autonomy_max,graph,created_by,created_at "
            "FROM agent_versions WHERE id=%s", (agent_id,))
        r = await cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "contract_audit_id": r[2], "version": r[3], "status": r[4],
            "autonomy_max": r[5], "graph": r[6], "created_by": r[7],
            "created_at": r[8].isoformat() if r[8] else None}


async def list_for(audit_id: str | None = None, limit: int = 100) -> list[dict]:
    if not _has_pg():
        items = [a for a in _MEM.values() if not audit_id or a["contract_audit_id"] == audit_id]
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return [_brief(a) for a in items[:limit]]
    from .db import _conn
    where, args = ("WHERE contract_audit_id=%s", (audit_id,)) if audit_id else ("", ())
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,name,contract_audit_id,version,status,autonomy_max,created_at "
            f"FROM agent_versions {where} ORDER BY created_at DESC LIMIT %s", (*args, limit))
        rows = await cur.fetchall()
    return [{"id": r[0], "name": r[1], "contract_audit_id": r[2], "version": r[3], "status": r[4],
             "autonomy_max": r[5], "created_at": r[6].isoformat() if r[6] else None} for r in rows]


def _brief(a: dict) -> dict:
    return {"id": a["id"], "name": a["name"], "contract_audit_id": a["contract_audit_id"],
            "version": a["version"], "status": a["status"], "autonomy_max": a.get("autonomy_max"),
            "created_at": a.get("created_at")}
