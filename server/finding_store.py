"""Экспертная разметка находок (слепая разметка пилота 1С): подтверждаю / ложная / не уверен,
«вручную бы не нашли», комментарий. Postgres + фолбэк в память. Ключ — (run_id, finding_id)."""
from __future__ import annotations

import datetime as _dt

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_labels (
    run_id      TEXT NOT NULL,
    finding_id  TEXT NOT NULL,
    expert      TEXT NOT NULL,
    decision    TEXT NOT NULL,
    manual_miss BOOLEAN NOT NULL DEFAULT FALSE,
    comment     TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, finding_id)
);
"""
DECISIONS = ("confirmed", "rejected", "unsure")
_MEM: dict[tuple[str, str], dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


def _row(r) -> dict:
    return {"run_id": r[0], "finding_id": r[1], "expert": r[2], "decision": r[3], "manual_miss": bool(r[4]),
            "comment": r[5] or "", "updated_at": r[6].isoformat() if hasattr(r[6], "isoformat") else r[6]}


async def set_label(run_id: str, finding_id: str, expert: str, decision: str, manual_miss: bool, comment: str) -> dict:
    if decision not in DECISIONS:
        raise ValueError("decision")
    now = _dt.datetime.now(_dt.timezone.utc)
    if not _has_pg():
        row = {"run_id": run_id, "finding_id": finding_id, "expert": expert, "decision": decision,
               "manual_miss": bool(manual_miss), "comment": comment or "", "updated_at": now.isoformat()}
        _MEM[(run_id, finding_id)] = row
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO finding_labels(run_id,finding_id,expert,decision,manual_miss,comment,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id,finding_id) DO UPDATE SET expert=EXCLUDED.expert, "
            "decision=EXCLUDED.decision, manual_miss=EXCLUDED.manual_miss, comment=EXCLUDED.comment, updated_at=EXCLUDED.updated_at",
            (run_id, finding_id, expert, decision, bool(manual_miss), comment or "", now))
        cur = await conn.execute("SELECT run_id,finding_id,expert,decision,manual_miss,comment,updated_at FROM finding_labels WHERE run_id=%s AND finding_id=%s", (run_id, finding_id))
        r = await cur.fetchone()
    return _row(r)


async def delete_label(run_id: str, finding_id: str) -> bool:
    if not _has_pg():
        return _MEM.pop((run_id, finding_id), None) is not None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM finding_labels WHERE run_id=%s AND finding_id=%s", (run_id, finding_id))
    return getattr(cur, "rowcount", 0) > 0


async def labels_for_runs(run_ids: list[str]) -> dict[str, dict[str, dict]]:
    """{run_id: {finding_id: label}}"""
    out: dict[str, dict[str, dict]] = {rid: {} for rid in run_ids}
    if not run_ids:
        return out
    if not _has_pg():
        for (rid, fid), row in _MEM.items():
            if rid in out:
                out[rid][fid] = row
        return out
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT run_id,finding_id,expert,decision,manual_miss,comment,updated_at FROM finding_labels WHERE run_id = ANY(%s)", (run_ids,))
        rows = await cur.fetchall()
    for r in rows:
        out.setdefault(r[0], {})[r[1]] = _row(r)
    return out
