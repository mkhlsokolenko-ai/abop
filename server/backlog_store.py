# -*- coding: utf-8 -*-
"""Связка «прогон ↔ задача трекера ↔ эпик» — память агента тактического бэклога.

Ни журнал прогонов, ни трекер по отдельности её не содержат: журнал знает, что агент сделал, трекер
знает, что поручено, а «вот этот прогон закрывает вот эту задачу» не знает никто. Поэтому свой
стор (PRD, 5.2).

**Состав эпика мы НЕ ведём** (решение владельца 4 от 13.10): подзадачи и их родителя ведут люди в
трекере, мы только сопоставляем. Поле `epic_id` здесь — слепок того, что сказал трекер на момент
связки, а не наш список: при расхождении правда в трекере.

**Каждая связка объяснима.** `основание` говорит, ПОЧЕМУ прогон отнесён к задаче («прогон создал
эту задачу», «задача ссылается на прогон», «участок карты»), а `подтверждение` — чем подтверждено
выполнение. Связка без основания не создаётся: иначе агент начнёт закрывать задачи по догадке, а
это ровно то, за что мы критикуем чужие продукты.

`backlog_links{tenant, issue_id, run_id, epic_id, основание, подтверждение, решение, created_at}`
— арендатор в ключе: задачи и прогоны разных компаний смешивать нельзя.
"""
from __future__ import annotations

import json

from . import tenancy
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS backlog_links (
    tenant        TEXT NOT NULL DEFAULT 'default',
    issue_id      TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    epic_id       TEXT,
    "основание"   TEXT NOT NULL,
    "подтверждение" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "решение"     TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, issue_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_backlog_issue ON backlog_links (tenant, issue_id);
CREATE INDEX IF NOT EXISTS idx_backlog_epic ON backlog_links (tenant, epic_id)
    WHERE epic_id IS NOT NULL;
"""

_MEM: dict[tuple, dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def связать(*, issue_id: str, run_id: str, основание: str, epic_id: str = "",
                  подтверждение: dict | None = None, решение: str = "") -> dict:
    """Запомнить связку. Повторный вызов обновляет основание и подтверждение, а не плодит строки."""
    if not основание:
        raise ValueError("связка без основания не создаётся: иначе задача закроется по догадке")
    строка = {"tenant": tenancy.текущий(), "issue_id": str(issue_id), "run_id": str(run_id),
              "epic_id": str(epic_id or "") or None, "основание": основание,
              "подтверждение": подтверждение or {}, "решение": решение}
    if not _has_pg():
        _MEM[(строка["tenant"], строка["issue_id"], строка["run_id"])] = строка
        return строка
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            'INSERT INTO backlog_links (tenant,issue_id,run_id,epic_id,"основание",'
            '"подтверждение","решение") VALUES (%s,%s,%s,%s,%s,%s,%s) '
            "ON CONFLICT (tenant,issue_id,run_id) DO UPDATE SET "
            'epic_id=EXCLUDED.epic_id, "основание"=EXCLUDED."основание", '
            '"подтверждение"=EXCLUDED."подтверждение", "решение"=EXCLUDED."решение"',
            (строка["tenant"], строка["issue_id"], строка["run_id"], строка["epic_id"],
             основание, json.dumps(подтверждение or {}, ensure_ascii=False), решение))
    return строка


async def связки(*, issue_id: str = "", epic_id: str = "", limit: int = 500) -> list[dict]:
    """Связки арендатора: все, по задаче или по эпику."""
    т = tenancy.текущий()
    if not _has_pg():
        из = [dict(v) for k, v in _MEM.items() if k[0] == т
              and (not issue_id or v["issue_id"] == str(issue_id))
              and (not epic_id or (v.get("epic_id") or "") == str(epic_id))]
        return из[:limit]
    from .db import _conn
    усл, пар = ["tenant=%s"], [т]
    if issue_id:
        усл.append("issue_id=%s")
        пар.append(str(issue_id))
    if epic_id:
        усл.append("epic_id=%s")
        пар.append(str(epic_id))
    async with _conn() as conn:
        cur = await conn.execute(
            'SELECT issue_id,run_id,epic_id,"основание","подтверждение","решение",created_at '
            "FROM backlog_links WHERE " + " AND ".join(усл)
            + " ORDER BY created_at DESC LIMIT %s", (*пар, int(limit)))
        rows = await cur.fetchall()
    return [{"issue_id": r[0], "run_id": r[1], "epic_id": r[2], "основание": r[3],
             "подтверждение": r[4] or {}, "решение": r[5],
             "created_at": r[6].isoformat() if r[6] else None} for r in rows]


async def отметить_решение(issue_id: str, run_id: str, решение: str) -> None:
    """Записать, что агент сделал с задачей: закрыл, подвинул или оставил без подтверждения."""
    т = tenancy.текущий()
    if not _has_pg():
        if (т, str(issue_id), str(run_id)) in _MEM:
            _MEM[(т, str(issue_id), str(run_id))]["решение"] = решение
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            'UPDATE backlog_links SET "решение"=%s WHERE tenant=%s AND issue_id=%s AND run_id=%s',
            (решение, т, str(issue_id), str(run_id)))
