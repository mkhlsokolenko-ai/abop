"""Реестр ЦЕПОЧЕК агентов (Pipelines) в Postgres — линейный конвейер: выход одного агента → контекст
следующего (Вариант А, ADR-«цепочки»). Владелец собирает цепочку в чате через модалку; раннер
(web_api.run_pipeline) исполняет шаги по порядку. Персистентность в PG (мультиюзер, переживает перенакат);
mem-фолбэк как у прочих сторов.

pipelines{id, name, steps JSONB, owner, created_at, updated_at}
  steps = [{agent_id, deliver?}]  — deliver: '' (как настроено) | 'chat' | канал; по умолчанию промежуточные
  шаги идут в chat (без внешней доставки), последний — как настроено.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipelines (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    steps       JSONB NOT NULL DEFAULT '[]'::jsonb,
    owner       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}
_COLS = "id,name,steps,owner,created_at,updated_at"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "name": r[1], "steps": r[2] or [], "owner": r[3],
            "created_at": r[4].isoformat() if r[4] else None,
            "updated_at": r[5].isoformat() if r[5] else None}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def all(owner: str | None = None) -> list[dict]:
    if not _has_pg():
        rows = sorted(_MEM.values(), key=lambda x: x.get("updated_at") or "", reverse=True)
        return [r for r in rows if not owner or r.get("owner") == owner]
    from .db import _conn
    async with _conn() as conn:
        if owner:
            cur = await conn.execute(f"SELECT {_COLS} FROM pipelines WHERE owner=%s ORDER BY updated_at DESC", (owner,))
        else:
            cur = await conn.execute(f"SELECT {_COLS} FROM pipelines ORDER BY updated_at DESC")
        return [_row(r) for r in await cur.fetchall()]


async def get(pid: str) -> dict | None:
    if not pid:
        return None
    if not _has_pg():
        return _MEM.get(pid)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM pipelines WHERE id=%s", (pid,))
        r = await cur.fetchone()
    return _row(r) if r else None


async def save(pid: str, name: str, steps: list, owner: str = "") -> dict:
    steps = [{"agent_id": str(s.get("agent_id")), "deliver": str(s.get("deliver") or "")}
             for s in (steps or []) if isinstance(s, dict) and s.get("agent_id")]
    card = {"id": pid, "name": name or pid, "steps": steps, "owner": owner}
    if not _has_pg():
        import time as _t
        card["updated_at"] = card.get("created_at") or str(_t.time())
        _MEM[pid] = {**_MEM.get(pid, {}), **card}
        return _MEM[pid]
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO pipelines (id,name,steps,owner,updated_at) VALUES (%s,%s,%s,%s,now()) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, steps=EXCLUDED.steps, updated_at=now()",
            (pid, card["name"], json.dumps(steps), owner))
    return await get(pid)


async def delete(pid: str) -> bool:
    if not _has_pg():
        return _MEM.pop(pid, None) is not None
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM pipelines WHERE id=%s", (pid,))
    return True
