"""Хранилище ContractSet — принятые из LUDA бандлы (ADR-029, SDD §4.3).

ContractSet{audit_id, capability, deployment, baseline, evidence_pack, intake, version}.
Персистентность — Postgres (тот же инстанс, что cost_journal). Если POSTGRES_DSN пуст
(локальный dev без БД) — прозрачный фолбэк в память, чтобы экран Ingress работал без стека.
Позже к ContractSet привязывается AgentVersion/Deployment (следующие инкременты).
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_sets (
    audit_id      TEXT PRIMARY KEY,
    segment       TEXT,
    family        TEXT,
    autonomy      TEXT,                         -- потолок автономии A0–A4 (ADR-013)
    schema_ver    TEXT,                         -- версия схемы capability_request
    bundle        JSONB NOT NULL,               -- три контракта + evidence_pack (как принято)
    intake        JSONB NOT NULL,               -- семя Project Graph
    ingested_by   TEXT,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cs_family ON contract_sets (family);
CREATE INDEX IF NOT EXISTS idx_cs_ingested ON contract_sets (ingested_at);
"""

_MEM: dict[str, dict] = {}  # фолбэк без Postgres


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    """Создать таблицу, если есть Postgres. Без DSN — no-op (работаем в памяти)."""
    if not _has_pg():
        return
    from .db import _conn  # ленивый импорт: psycopg нужен только при наличии DSN
    async with _conn() as conn:
        await conn.execute(SCHEMA)


def _row(bundle: dict, intake: dict, ingested_by: str) -> dict:
    cap = bundle.get("capability_request", {})
    dc = bundle.get("deployment_contract", {})
    return {
        "audit_id": intake["audit_id"],
        "segment": intake.get("segment"),
        "family": intake.get("family"),
        "autonomy": intake.get("autonomy_ceiling"),
        "schema_ver": cap.get("schema"),
        "bundle": bundle,
        "intake": intake,
        "ingested_by": ingested_by,
    }


async def save(bundle: dict, intake: dict, ingested_by: str = "dev") -> dict:
    """Сохранить принятый ContractSet (upsert по audit_id). Возвращает сохранённую запись."""
    row = _row(bundle, intake, ingested_by)
    if not _has_pg():
        import datetime as _dt  # локально: фолбэк-метка времени только для памяти
        row["ingested_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _MEM[row["audit_id"]] = row
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO contract_sets "
            "(audit_id, segment, family, autonomy, schema_ver, bundle, intake, ingested_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (audit_id) DO UPDATE SET "
            "segment=EXCLUDED.segment, family=EXCLUDED.family, autonomy=EXCLUDED.autonomy, "
            "schema_ver=EXCLUDED.schema_ver, bundle=EXCLUDED.bundle, intake=EXCLUDED.intake, "
            "ingested_by=EXCLUDED.ingested_by, ingested_at=now()",
            (row["audit_id"], row["segment"], row["family"], row["autonomy"],
             row["schema_ver"], json.dumps(row["bundle"]), json.dumps(row["intake"]),
             row["ingested_by"]),
        )
    return await get(row["audit_id"]) or row


async def get(audit_id: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(audit_id)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT audit_id, segment, family, autonomy, schema_ver, bundle, intake, "
            "ingested_by, ingested_at FROM contract_sets WHERE audit_id = %s",
            (audit_id,),
        )
        r = await cur.fetchone()
    if not r:
        return None
    return {"audit_id": r[0], "segment": r[1], "family": r[2], "autonomy": r[3],
            "schema_ver": r[4], "bundle": r[5], "intake": r[6],
            "ingested_by": r[7], "ingested_at": r[8].isoformat() if r[8] else None}


async def list_all(limit: int = 100) -> list[dict]:
    """Список принятых контрактов (краткие карточки, без полного бандла)."""
    if not _has_pg():
        items = sorted(_MEM.values(), key=lambda x: x.get("ingested_at") or "", reverse=True)
        return [_brief(x) for x in items[:limit]]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT audit_id, segment, family, autonomy, intake, ingested_at "
            "FROM contract_sets ORDER BY ingested_at DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    return [{"audit_id": r[0], "segment": r[1], "family": r[2], "autonomy": r[3],
             "skills": (r[4] or {}).get("skills", []),
             "ingested_at": r[5].isoformat() if r[5] else None} for r in rows]


def _brief(row: dict) -> dict:
    return {"audit_id": row["audit_id"], "segment": row.get("segment"),
            "family": row.get("family"), "autonomy": row.get("autonomy"),
            "skills": (row.get("intake") or {}).get("skills", []),
            "ingested_at": row.get("ingested_at")}
