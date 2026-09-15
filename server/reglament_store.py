"""Хранилище регламента (размеченные чанки с ключом НСИ + эмбеддинги) — Postgres.

Регламент-конформанс (идея владельца): сверять, как процесс ОПИСАН в регламенте и как СОБРАН в
ABOP, через иерархический ключ НСИ [P][SS][OOO] (процесс/подпроцесс/операция). Чанки регламента
размечаются ЛЛМ (RouteAI DeepSeek v4) — ключ ставится на чанк; эмбеддинг (BGE-M3) хранится для
смысловой сверки (cosine с описанием операции ABOP). MVP: PG + RouteAI-эмбеддинги (полный sLAVA
граф-RAG — позже). См. reglament-conformance-slava.

reglament{id, tenant, nsi_key, process, subprocess, op, text, embedding JSONB, editor, updated_at}.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS reglament (
    id          BIGSERIAL PRIMARY KEY,
    tenant      TEXT NOT NULL DEFAULT 'default',
    nsi_key     TEXT NOT NULL,
    process     TEXT,
    subprocess  TEXT,
    op          TEXT,
    text        TEXT NOT NULL,
    embedding   JSONB,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reglament_key ON reglament (tenant, nsi_key);
-- граф-слой: рёбра между узлами (nsi_key). relation: contains (иерархия НСИ) | entails/cites/action
-- (кросс-цитирование норм: нарушение одной нормы влечёт проверку связанной / требуемое действие).
CREATE TABLE IF NOT EXISTS reglament_edges (
    tenant      TEXT NOT NULL,
    from_key    TEXT NOT NULL,
    to_key      TEXT NOT NULL,
    relation    TEXT NOT NULL DEFAULT 'entails',
    note        TEXT,
    PRIMARY KEY (tenant, from_key, to_key, relation)
);
"""

_MEM: list[dict] = []
_MEM_EDGES: list[dict] = []


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def clear(tenant: str) -> None:
    if not _has_pg():
        _MEM[:] = [c for c in _MEM if c.get("tenant") != tenant]
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM reglament WHERE tenant=%s", (tenant,))


async def save_chunk(tenant: str, nsi_key: str, process: str, subprocess: str, op: str,
                     text: str, embedding: list, editor: str = "ingest") -> None:
    if not _has_pg():
        _MEM.append({"tenant": tenant, "nsi_key": nsi_key, "process": process, "subprocess": subprocess,
                     "op": op, "text": text, "embedding": embedding})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO reglament (tenant,nsi_key,process,subprocess,op,text,embedding,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())",
            (tenant, nsi_key, process, subprocess, op, text,
             json.dumps(embedding) if embedding is not None else None, editor))


async def all_for(tenant: str) -> list[dict]:
    if not _has_pg():
        return [c for c in _MEM if c.get("tenant") == tenant]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT nsi_key,process,subprocess,op,text,embedding FROM reglament WHERE tenant=%s ORDER BY nsi_key",
            (tenant,))
        rows = await cur.fetchall()
    return [{"nsi_key": r[0], "process": r[1], "subprocess": r[2], "op": r[3], "text": r[4],
             "embedding": r[5]} for r in rows]


# ─── граф-слой: рёбра (иерархия НСИ + кросс-цитирование норм) ───
async def save_edge(tenant: str, from_key: str, to_key: str, relation: str = "entails", note: str = "") -> None:
    if not _has_pg():
        _MEM_EDGES.append({"tenant": tenant, "from_key": from_key, "to_key": to_key,
                           "relation": relation, "note": note})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO reglament_edges (tenant,from_key,to_key,relation,note) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant,from_key,to_key,relation) DO UPDATE SET note=EXCLUDED.note",
            (tenant, from_key, to_key, relation, note))


async def edges_for(tenant: str, relation: str | None = None) -> list[dict]:
    if not _has_pg():
        return [e for e in _MEM_EDGES if e["tenant"] == tenant and (relation is None or e["relation"] == relation)]
    from .db import _conn
    async with _conn() as conn:
        if relation:
            cur = await conn.execute(
                "SELECT from_key,to_key,relation,note FROM reglament_edges WHERE tenant=%s AND relation=%s",
                (tenant, relation))
        else:
            cur = await conn.execute(
                "SELECT from_key,to_key,relation,note FROM reglament_edges WHERE tenant=%s", (tenant,))
        rows = await cur.fetchall()
    return [{"from_key": r[0], "to_key": r[1], "relation": r[2], "note": r[3]} for r in rows]


async def clear_edges(tenant: str, relation: str | None = None) -> None:
    if not _has_pg():
        _MEM_EDGES[:] = [e for e in _MEM_EDGES if not (e["tenant"] == tenant and (relation is None or e["relation"] == relation))]
        return
    from .db import _conn
    async with _conn() as conn:
        if relation:
            await conn.execute("DELETE FROM reglament_edges WHERE tenant=%s AND relation=%s", (tenant, relation))
        else:
            await conn.execute("DELETE FROM reglament_edges WHERE tenant=%s", (tenant,))
