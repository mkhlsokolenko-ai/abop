# -*- coding: utf-8 -*-
"""Задачи отдела и их подзадачи (шаг 5 «Отделов агентов», docs/OTDELY_AGENTOV.md §4).

Задача — то, что человек отдал отделу. Супервайзер раскладывает её на ПОДЗАДАЧИ, каждая из
которых объявляет РЕЗУЛЬТАТ («нужен такой-то артефакт»), а не шаги: у подзадачи есть вид
артефакта, критерии готовности (DoD), исполнитель и счётчик попыток. Подзадача — не прогон:
прогон — способ её выполнить, и исполнитель волен выбрать другой путь, если результат тот же.

Две попытки — обязательный предел из плана (§4.5): без него супервайзер и исполнитель гоняют
работу по кругу за деньги владельца. Не вышло за две — эскалация человеку, и это законный исход.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS dept_tasks (
    id         TEXT PRIMARY KEY,
    отдел      TEXT NOT NULL,
    текст      TEXT NOT NULL,
    предмет    JSONB NOT NULL DEFAULT '{}'::jsonb,
    статус     TEXT NOT NULL DEFAULT 'в работе',
    создал     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dept_tasks ON dept_tasks (отдел, created_at);
CREATE TABLE IF NOT EXISTS dept_subtasks (
    id        TEXT PRIMARY KEY,
    task_id   TEXT NOT NULL,
    вид       TEXT NOT NULL,
    критерии  JSONB NOT NULL DEFAULT '[]'::jsonb,
    навык     TEXT,
    агент     TEXT,
    прогон    TEXT,
    попытка   INT NOT NULL DEFAULT 0,
    статус    TEXT NOT NULL DEFAULT 'в очереди',
    причины   JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dept_subtasks ON dept_subtasks (task_id);
"""

_MEM_T: dict[str, list] = {}
_MEM_S: dict[str, list] = {}

ПОПЫТОК_МАКС = 2


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r, cols) -> dict:
    out = {}
    for i, c in enumerate(cols):
        v = r[i]
        if isinstance(v, str) and c in ("предмет", "критерии", "причины"):
            try:
                v = json.loads(v)
            except Exception:  # noqa: BLE001
                v = v
        out[c] = v
    return out


async def создать_задачу(отдел: str, текст: str, предмет: dict, создал: str) -> dict:
    import uuid
    tid = "task-" + uuid.uuid4().hex[:10]
    row = {"id": tid, "отдел": отдел, "текст": текст, "предмет": предмет or {},
           "статус": "в работе", "создал": создал, "подзадач": 0}
    if not _has_pg():
        _MEM_T.setdefault("t", []).append(dict(row))
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dept_tasks(id,отдел,текст,предмет,статус,создал) VALUES(%s,%s,%s,%s,%s,%s)",
            (tid, отдел, текст, json.dumps(row["предмет"], ensure_ascii=False), row["статус"], создал))
    return row


async def создать_подзадачу(task_id: str, вид: str, критерии: list, навык: str = "",
                            агент: str = "", прогон: str = "") -> dict:
    import uuid
    sid = "sub-" + uuid.uuid4().hex[:10]
    row = {"id": sid, "task_id": task_id, "вид": вид, "критерии": критерии, "навык": навык,
           "агент": агент, "прогон": прогон, "попытка": 0, "статус": "в очереди", "причины": []}
    if not _has_pg():
        _MEM_S.setdefault("s", []).append(dict(row))
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO dept_subtasks(id,task_id,вид,критерии,навык,агент,прогон,попытка,статус,причины) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (sid, task_id, вид, json.dumps(критерии, ensure_ascii=False), навык, агент, прогон,
             0, row["статус"], "[]"))
    return row


async def обновить_подзадачу(sid: str, **поля) -> dict | None:
    если = ", ".join(f"{k}=%s" for k in поля)
    if not _has_pg():
        for x in _MEM_S.get("s", []):
            if x["id"] == sid:
                x.update(поля)
                return dict(x)
        return None
    from .db import _conn
    args = []
    for k, v in поля.items():
        args.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
    args.append(sid)
    async with _conn() as conn:
        cur = await conn.execute(f"UPDATE dept_subtasks SET {если} WHERE id=%s RETURNING *", tuple(args))
        r = await cur.fetchone()
    if not r:
        return None
    return _row(r, ("id", "task_id", "вид", "критерии", "навык", "агент", "прогон", "попытка",
                    "статус", "причины", "created_at"))


async def задача(tid: str) -> dict | None:
    if not _has_pg():
        for x in _MEM_T.get("t", []):
            if x["id"] == tid:
                return dict(x)
        return None
    from .db import _conn
    cols = ("id", "отдел", "текст", "предмет", "статус", "создал", "created_at")
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,отдел,текст,предмет,статус,создал,created_at FROM dept_tasks WHERE id=%s", (tid,))
        r = await cur.fetchone()
    return _row(r, cols) if r else None


async def подзадачи(task_id: str) -> list[dict]:
    if not _has_pg():
        return [dict(x) for x in _MEM_S.get("s", []) if x["task_id"] == task_id]
    from .db import _conn
    cols = ("id", "task_id", "вид", "критерии", "навык", "агент", "прогон", "попытка", "статус",
            "причины", "created_at")
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,task_id,вид,критерии,навык,агент,прогон,попытка,статус,причины,created_at "
            "FROM dept_subtasks WHERE task_id=%s ORDER BY id", (task_id,))
        rows = await cur.fetchall()
    return [_row(r, cols) for r in rows]


async def задачи_отдела(отдел: str, limit: int = 50) -> list[dict]:
    if not _has_pg():
        return [dict(x) for x in reversed(_MEM_T.get("t", [])) if x["отдел"] == отдел][:limit]
    from .db import _conn
    cols = ("id", "отдел", "текст", "предмет", "статус", "создал", "created_at")
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,отдел,текст,предмет,статус,создал,created_at FROM dept_tasks "
            "WHERE отдел=%s ORDER BY created_at DESC LIMIT %s", (отдел, int(limit)))
        rows = await cur.fetchall()
    return [_row(r, cols) for r in rows]
