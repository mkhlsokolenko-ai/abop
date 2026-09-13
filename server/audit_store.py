"""Аудит ИБ — неизменяемый лог governance-событий среды (Postgres + фолбэк в память).
Пишется в ключевых точках: ingest контракта, сборка/авторинг/retire агента, прогон,
перепривязка рецепта, правка RBAC. Экран «Безопасность · Аудит ИБ» читает отсюда (не хардкод)."""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     JSONB,
    severity   TEXT NOT NULL DEFAULT 'info'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts DESC);
"""

_MEM: list[dict] = []
_SEQ = {"n": 0}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def record(actor: str, action: str, target: str = "", detail: dict | None = None,
                 severity: str = "info") -> None:
    """Записать событие аудита. Никогда не роняет вызывающий код (аудит опционален)."""
    try:
        if not _has_pg():
            import datetime as _dt
            _SEQ["n"] += 1
            _MEM.append({"id": _SEQ["n"], "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                         "actor": actor or "система", "action": action, "target": target,
                         "detail": detail or {}, "severity": severity})
            return
        from .db import _conn
        async with _conn() as conn:
            await conn.execute(
                "INSERT INTO audit_log (actor,action,target,detail,severity) VALUES (%s,%s,%s,%s,%s)",
                (actor or "система", action, target, json.dumps(detail or {}), severity))
    except Exception:  # noqa: BLE001 — аудит не должен ломать основную операцию
        pass


async def list_events(limit: int = 100) -> list[dict]:
    """Свежие события сверху (для экрана Аудит ИБ)."""
    if not _has_pg():
        return sorted(_MEM, key=lambda e: e["id"], reverse=True)[:limit]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,ts,actor,action,target,detail,severity FROM audit_log ORDER BY ts DESC LIMIT %s",
            (limit,))
        rows = await cur.fetchall()
    return [{"id": r[0], "ts": r[1].isoformat() if r[1] else None, "actor": r[2],
             "action": r[3], "target": r[4], "detail": r[5] or {}, "severity": r[6]} for r in rows]
