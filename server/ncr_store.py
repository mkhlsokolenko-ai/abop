# -*- coding: utf-8 -*-
"""NCR — карточки несоответствия (шаг 7 «Отделов агентов», docs/OTDELY_AGENTOV.md §8).

NCR несёт: что нарушено (цитата правила/регламента с его id), на чём (артефакт: прогон, факт
доски, находка), тяжесть. Аудитор обязан цитировать источник — NCR без источника сам является
галлюцинацией, и такой аудитор не нужен. Аудитор НИЧЕГО не чинит: разделение обязанностей —
чинит супервайзер (шаг 5) или человек.

Дедуп по (тип, предмет): повторный прогон аудита не плодит второй NCR на тот же провал, пока
первый открыт; закрытый не переоткрывается — закрытие это решение человека, аудитор спорить
с ним не станет. Капы на число NCR за прогон — урок B3: поток из 799 карточек бесполезен.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS ncr (
    id         TEXT PRIMARY KEY,
    тип        TEXT NOT NULL,
    предмет    TEXT NOT NULL,
    отдел      TEXT NOT NULL,
    что_нарушено TEXT NOT NULL,
    источник   TEXT NOT NULL DEFAULT '',
    тяжесть    TEXT NOT NULL DEFAULT 'средняя',
    статус     TEXT NOT NULL DEFAULT 'открыто',
    кем_закрыто TEXT,
    причина_закрытия TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ncr_open ON ncr (тип, предмет) WHERE статус = 'открыто';
CREATE INDEX IF NOT EXISTS idx_ncr_dept ON ncr (отдел, статус);
"""

_MEM: list = []
КАП_ЗА_ПРОГОН = 20


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r, full=True) -> dict:
    cols = ("id", "тип", "предмет", "отдел", "что_нарушено", "источник", "тяжесть", "статус",
            "кем_закрыто", "причина_закрытия", "created_at", "closed_at")
    out = {}
    for i, c in enumerate(cols[: (len(r))]):
        v = r[i]
        out[c] = str(v)[:19] if c in ("created_at", "closed_at") and v is not None else v
    return out


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def создать(тип: str, предмет: str, отдел: str, что_нарушено: str, *,
                  источник: str = "", тяжесть: str = "средняя") -> dict | None:
    """Создать NCR, если открытого на тот же провал ещё нет. Повтор — не ошибка, а «уже видно»."""
    if not тип or not предмет or not что_нарушено:
        return None
    if not _has_pg():
        for x in _MEM:
            if x["тип"] == тип and x["предмет"] == предмет and x["статус"] == "открыто":
                return None
        row = {"id": "ncr-%d" % (len(_MEM) + 1), "тип": тип, "предмет": предмет, "отдел": отдел,
               "что_нарушено": что_нарушено, "источник": источник, "тяжесть": тяжесть,
               "статус": "открыто", "кем_закрыто": None, "причина_закрытия": None,
               "created_at": None, "closed_at": None}
        _MEM.append(row)
        return row
    from .db import _conn
    import uuid
    rid = uuid.uuid5(uuid.NAMESPACE_URL, f"{тип}|{предмет}").hex[:16]
    async with _conn() as conn:
        cur = await conn.execute(
            "INSERT INTO ncr(id,тип,предмет,отдел,что_нарушено,источник,тяжесть) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (тип,предмет) WHERE статус='открыто' DO NOTHING RETURNING id",
            (rid, тип, предмет, отдел, что_нарушено, источник, тяжесть))
        r = await cur.fetchone()
    if not r:
        return None
    все = await список(отдел, "открыто")
    return next((x for x in все if x["id"] == r[0]), {"id": r[0], "тип": тип, "предмет": предмет})


async def список(отдел: str = "", статус: str = "открыто") -> list[dict]:
    if not _has_pg():
        rows = [dict(x) for x in _MEM
                if (not отдел or x["отдел"] == отдел) and (not статус or x["статус"] == статус)]
        return rows
    from .db import _conn
    conds, args = [], []
    if отдел:
        conds.append("отдел=%s"); args.append(отдел)
    if статус:
        conds.append("статус=%s"); args.append(статус)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,тип,предмет,отдел,что_нарушено,источник,тяжесть,статус,кем_закрыто,"
            "причина_закрытия,created_at,closed_at FROM ncr " + where +
            " ORDER BY created_at DESC LIMIT 200", tuple(args))
        rows = await cur.fetchall()
    return [_row(r) for r in rows]


async def закрыть(ncr_id: str, кем: str, причина: str = "") -> dict | None:
    if not _has_pg():
        for x in _MEM:
            if x["id"] == ncr_id and x["статус"] == "открыто":
                x["статус"] = "закрыто"; x["кем_закрыто"] = кем
                x["причина_закрытия"] = причина
                return dict(x)
        return None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "UPDATE ncr SET статус='закрыто', кем_закрыто=%s, причина_закрытия=%s, closed_at=now() "
            "WHERE id=%s AND статус='открыто' RETURNING id", (кем, причина, ncr_id))
        r = await cur.fetchone()
    if not r:
        return None
    все = await список("")
    return next((x for x in все if x["id"] == ncr_id), {"id": ncr_id, "статус": "закрыто"})
