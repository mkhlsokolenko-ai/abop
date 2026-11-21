"""Лимит расходов отдела за сутки (мокап-dark 21.11: «стоимость прогонов за сутки · лимит»).

Отдельный крошечный стор, а не колонка в families: реестр семей — живая поверхность ABAC,
и миграция его схемы ради одного числа не нужна. Смыслы: лимит задаёт manager+ (PUT), читает
пульт (GET); сам подсчёт «за сутки» — сумма cost_rub прогонов семьи за 24 ч из run_store,
не здесь.
"""
from __future__ import annotations

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS dept_budget (
    fid          TEXT PRIMARY KEY,
    лимит_руб    NUMERIC(12,2) NOT NULL,
    editor       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
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


async def лимит(fid: str) -> float | None:
    if not _has_pg():
        з = _MEM.get(fid)
        return None if з is None else float(з["лимит_руб"])
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT лимит_руб FROM dept_budget WHERE fid=%s", (fid,))
        r = await cur.fetchone()
    return float(r[0]) if r else None


async def задать_лимит(fid: str, руб: float, editor: str = "dev") -> float:
    assert руб >= 0, "лимит — неотрицательное число рублей"
    if not _has_pg():
        _MEM[fid] = {"лимит_руб": float(руб), "editor": editor}
        return float(руб)
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dept_budget (fid, лимит_руб, editor, updated_at) VALUES (%s,%s,%s,now()) "
            "ON CONFLICT (fid) DO UPDATE SET лимит_руб=EXCLUDED.лимит_руб, "
            "editor=EXCLUDED.editor, updated_at=now()",
            (fid, float(руб), editor))
    return float(руб)
