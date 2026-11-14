# -*- coding: utf-8 -*-
"""Матрица методологии (шаг 6 «Отделов агентов», ADR-084).

Связь «пункт регламента → параметр конфигурации» — ДАННЫЕ, объявляемые один раз и
утверждаемые человеком. Методолог не выдумывает зависимости — он сверяет матрицу с регламентом
и с фактической конфигурацией. Паттерн — «правила — данные»: то, что раньше было мнением
программы (какой параметр от какого пункта зависит), становится записью с автором.

Запись: {отдел, ключ_нси (пункт регламента [P][SS][OOO]), параметр (правило_активно |
роль_в_отделе), цель (id набора правил / id роли), значение ("да"/"нет"), автор}.
"""
from __future__ import annotations

import json

from .config import settings

ПАРАМЕТРЫ = ("правило_активно", "роль_в_отделе")

SCHEMA = """
CREATE TABLE IF NOT EXISTS методология (
    id        TEXT PRIMARY KEY,
    отдел     TEXT NOT NULL,
    ключ_нси  TEXT NOT NULL,
    параметр  TEXT NOT NULL,
    цель      TEXT NOT NULL,
    значение  TEXT NOT NULL,
    автор     TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_matrix_dept ON методология (отдел, ключ_нси);
"""

_MEM: dict[str, list] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "отдел": r[1], "ключ_нси": r[2], "параметр": r[3], "цель": r[4],
            "значение": r[5], "автор": r[6] or ""}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def записи(отдел: str) -> list[dict]:
    if not _has_pg():
        return [dict(x) for x in _MEM.get(отдел, [])]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,отдел,ключ_нси,параметр,цель,значение,автор FROM методология "
            "WHERE отдел=%s ORDER BY ключ_нси,цель", (отдел,))
        return [_row(r) for r in await cur.fetchall()]


async def сохранить(отдел: str, ключ_нси: str, параметр: str, цель: str, значение: str,
                    автор: str) -> dict:
    """Записать/обновить строку матрицы. Параметр вне перечня — отказ: молча завести
    несуществующую связь «пункт → параметр» хуже отсутствия записи."""
    if параметр not in ПАРАМЕТРЫ:
        raise ValueError("параметр «%s» не из %s" % (параметр, ", ".join(ПАРАМЕТРЫ)))
    if not ключ_нси.strip():
        raise ValueError("нужен ключ НСИ пункта регламента")
    import uuid
    rid = uuid.uuid5(uuid.NAMESPACE_URL, f"{отдел}|{ключ_нси}|{параметр}|{цель}").hex[:16]
    row = {"id": rid, "отдел": отдел, "ключ_нси": ключ_нси.strip(), "параметр": параметр,
           "цель": цель.strip(), "значение": "да" if str(значение).strip() in ("да", "1", "true") else "нет",
           "автор": автор}
    if not _has_pg():
        cur = _MEM.setdefault(отдел, [])
        cur[:] = [x for x in cur if not (x["ключ_нси"] == row["ключ_нси"] and x["параметр"] == row["параметр"]
                                         and x["цель"] == row["цель"])]
        cur.append(dict(row))
        return dict(row)
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO методология(id,отдел,ключ_нси,параметр,цель,значение,автор) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET значение=EXCLUDED.значение, автор=EXCLUDED.автор, "
            "updated_at=now()", (row["id"], отдел, row["ключ_нси"], параметр, row["цель"],
                                 row["значение"], автор))
    return row


async def удалить(отдел: str, rid: str) -> bool:
    if not _has_pg():
        cur = _MEM.get(отдел, [])
        before = len(cur)
        _MEM[отдел] = [x for x in cur if x["id"] != rid]
        return len(_MEM[отдел]) < before
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM методология WHERE отдел=%s AND id=%s", (отдел, rid))
        return getattr(cur, "rowcount", 0) > 0
