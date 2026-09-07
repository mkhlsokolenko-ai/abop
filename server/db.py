"""Postgres: cost_journal + трёхуровневые квоты (сессия / сессии-в-неделю / неделя).

Каждый LLM/embed/rerank вызов пишется строкой в cost_journal с привязкой к студенту,
сессии, модели, версии промпта и стоимости. Отсюда же считаются лимиты:
    * PER_SESSION_TOKEN_LIMIT — потолок на одну сессию (по умолчанию 5M)
    * SESSIONS_PER_WEEK       — сколько сессий можно открыть за ISO-неделю (5)
    * WEEKLY_TOKEN_LIMIT      — суммарный потолок за неделю (25M)

«Неделя» = ISO-неделя по времени сервера (date_trunc('week', now())).
«Сессия» = session_id, который передаёт клиент (OpenCode/агент) на каждый вызов.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from .config import settings

_pool: AsyncConnectionPool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_journal (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    student_id     TEXT NOT NULL,
    username       TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,               -- llm | embed | rerank
    model          TEXT NOT NULL,
    profile        TEXT,                         -- code | research | standard
    prompt_version TEXT,
    input_tokens   BIGINT NOT NULL DEFAULT 0,
    output_tokens  BIGINT NOT NULL DEFAULT 0,
    cost_rub       NUMERIC(12,4) NOT NULL DEFAULT 0,
    meta           JSONB
);
CREATE INDEX IF NOT EXISTS idx_cj_student ON cost_journal (student_id);
CREATE INDEX IF NOT EXISTS idx_cj_session ON cost_journal (session_id);
CREATE INDEX IF NOT EXISTS idx_cj_ts ON cost_journal (ts);
"""


class QuotaExceeded(Exception):
    """Студент упёрся в один из лимитов (сессия / сессии-в-неделю / неделя)."""


async def init_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(settings.pg_dsn, min_size=1, max_size=8, open=False)
        await _pool.open()
        async with _pool.connection() as conn:
            await conn.execute(SCHEMA)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def _conn():
    pool = await init_pool()
    async with pool.connection() as conn:
        yield conn


async def _session_tokens(conn, session_id: str) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) "
        "FROM cost_journal WHERE session_id = %s",
        (session_id,),
    )
    return int((await cur.fetchone())[0])


async def _week_tokens(conn, student_id: str) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM cost_journal "
        "WHERE student_id = %s AND ts >= date_trunc('week', now())",
        (student_id,),
    )
    return int((await cur.fetchone())[0])


async def _week_sessions(conn, student_id: str) -> set[str]:
    cur = await conn.execute(
        "SELECT DISTINCT session_id FROM cost_journal "
        "WHERE student_id = %s AND ts >= date_trunc('week', now())",
        (student_id,),
    )
    return {r[0] for r in await cur.fetchall()}


async def check_quota(student_id: str, session_id: str) -> None:
    """Единственный лимит — недельный потолок токенов на студента. Бросает QuotaExceeded.

    Лимиты «на сессию» и «сессий в неделю» сняты: session_id — это техническая
    группировка вызовов (переподключения CLI не должны упираться в лимит подключений).
    Число сессий остаётся в отчёте как справочная метрика, но не ограничивается.
    """
    async with _conn() as conn:
        week_used = await _week_tokens(conn, student_id)
        if week_used >= settings.weekly_token_limit:
            raise QuotaExceeded(
                f"Недельный лимит {settings.weekly_token_limit:,} токенов исчерпан "
                f"(израсходовано {week_used:,}). Сброс в понедельник."
            )


async def log_usage(
    *,
    student_id: str,
    username: str,
    session_id: str,
    kind: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_rub: float,
    profile: str | None = None,
    prompt_version: str | None = None,
    meta: dict | None = None,
) -> None:
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO cost_journal "
            "(student_id, username, session_id, kind, model, profile, prompt_version, "
            " input_tokens, output_tokens, cost_rub, meta) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                student_id,
                username,
                session_id,
                kind,
                model,
                profile,
                prompt_version,
                input_tokens,
                output_tokens,
                cost_rub,
                json.dumps(meta or {}),
            ),
        )


async def student_report(student_id: str, session_id: str | None = None) -> dict:
    """Сводка для личного кабинета: расход за неделю, по текущей сессии, остатки."""
    async with _conn() as conn:
        week_used = await _week_tokens(conn, student_id)
        sessions = await _week_sessions(conn, student_id)
        cur = await conn.execute(
            "SELECT COALESCE(SUM(cost_rub),0), COUNT(*) FROM cost_journal "
            "WHERE student_id = %s AND ts >= date_trunc('week', now())",
            (student_id,),
        )
        cost, calls = await cur.fetchone()
        session_used = await _session_tokens(conn, session_id) if session_id else 0

    wk_limit = settings.weekly_token_limit
    return {
        "week": {
            "tokens_used": week_used,
            "limit": wk_limit,
            "remaining": max(0, wk_limit - week_used),
            "used_pct": round(100 * week_used / wk_limit, 1) if wk_limit else 0.0,
            "cost_rub": float(cost),
            "calls": int(calls),
        },
        "sessions_this_week": {
            "opened": len(sessions),
            "limit": settings.sessions_per_week,
            "remaining": max(0, settings.sessions_per_week - len(sessions)),
        },
        "current_session": {
            "session_id": session_id,
            "tokens_used": session_used,
            "limit": settings.per_session_token_limit,
            "remaining": max(0, settings.per_session_token_limit - session_used),
        },
    }
