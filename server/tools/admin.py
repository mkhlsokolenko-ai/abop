"""Инструменты для лектора: сводный cost-отчёт по группе.

В проде роль проверяется по claim'у Keycloak (realm role 'lecturer'). Здесь — простая
проверка; ужесточить перед потоком (см. docs/architecture.md).
"""
from __future__ import annotations

from ..auth import current_student
from ..config import settings
from ..db import _conn  # noqa: PLC2701 — внутренний помощник, переиспользуем пул


def register(mcp) -> None:
    @mcp.tool
    async def cost_report(top: int = 30) -> dict:
        """Сводка расхода по всем студентам за текущую неделю (командный центр лектора)."""
        # NB: в проде — gate по realm-role 'lecturer' из JWT.
        _ = current_student()
        async with _conn() as conn:
            cur = await conn.execute(
                "SELECT username, "
                "       SUM(input_tokens+output_tokens) AS tokens, "
                "       SUM(cost_rub) AS cost, "
                "       COUNT(DISTINCT session_id) AS sessions, "
                "       COUNT(*) AS calls "
                "FROM cost_journal "
                "WHERE ts >= date_trunc('week', now()) "
                "GROUP BY username ORDER BY tokens DESC LIMIT %s",
                (top,),
            )
            rows = await cur.fetchall()
        return {
            "week_limits": {
                "per_session": settings.per_session_token_limit,
                "sessions_per_week": settings.sessions_per_week,
                "weekly": settings.weekly_token_limit,
            },
            "students": [
                {
                    "username": r[0],
                    "tokens": int(r[1]),
                    "cost_rub": float(r[2]),
                    "sessions": int(r[3]),
                    "calls": int(r[4]),
                    "weekly_used_pct": round(100 * int(r[1]) / settings.weekly_token_limit, 1),
                }
                for r in rows
            ],
        }
