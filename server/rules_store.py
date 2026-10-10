# -*- coding: utf-8 -*-
"""Правки наборов правил (check_rules) — Postgres + фолбэк в память.

Шаг 3 плана «правила — данные». Набор правил приезжает с пакетом вертикали (`demo/*/rules/*.json`)
— это поставочная, неизменяемая часть. Методолог правит набор в интерфейсе, и правка ложится
ПОВЕРХ пакета отдельной записью. Отсюда три свойства, которых не было бы при правке файла:

* **«вернуть версию пакета» всегда возможно** — поставочный набор никуда не делся, снять правку
  значит удалить одну строку;
* **правка переживает перенакат** — файл в образе при деплое заменится, запись в Postgres нет;
* **видно, кто и когда** — набор, по которому считались находки, должен быть атрибутируем: аудитору
  отвечать «правило изменил такой-то такого числа», а не «правила изменились».

`check_rules{id, rules JSONB, version, editor, updated_at}`. Нет записи ⇒ набор как в пакете.
"""
from __future__ import annotations

import json

from . import tenancy
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS check_rules (
    id         TEXT PRIMARY KEY,
    rules      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- полный набор правил как данные
    version    INTEGER NOT NULL DEFAULT 1,           -- номер правки, растёт с каждым сохранением
    editor     TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
# Арендатор в ПЕРВИЧНОМ ключе: набор «showcase-b» правят у себя все, кто поставил вертикаль.
SCHEMA += tenancy.миграция_ключа("check_rules", "id")

_MEM: dict[str, dict] = {}
_COLS = "id,rules,version,editor,updated_at"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "набор": r[1] or {}, "версия": int(r[2] or 1), "кто": r[3],
            "когда": r[4].isoformat() if r[4] else None}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def all() -> list[dict]:
    """Все правки. Поставочные наборы сюда НЕ попадают: их источник — пакет вертикали."""
    if not _has_pg():
        return sorted((v for k, v in _MEM.items() if k.startswith(tenancy.ключ_памяти(""))),
                      key=lambda x: x["id"])
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM check_rules WHERE tenant=%s ORDER BY id",
                                 (tenancy.текущий(),))
        return [_row(r) for r in await cur.fetchall()]


async def get(rid: str) -> dict | None:
    if not rid:
        return None
    if not _has_pg():
        return _MEM.get(tenancy.ключ_памяти(rid))
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM check_rules WHERE id=%s AND tenant=%s",
                                 (rid, tenancy.текущий()))
        r = await cur.fetchone()
    return _row(r) if r else None


async def save(rid: str, набор: dict, editor: str = "dev") -> dict:
    """Сохранить правку набора. Версия правки растёт — по ней в журнале прогона видно, чем считали."""
    было = await get(rid)
    версия = int((было or {}).get("версия") or 0) + 1
    if not _has_pg():
        import datetime as _dt
        _MEM[tenancy.ключ_памяти(rid)] = {
            "id": rid, "набор": набор, "версия": версия, "кто": editor,
            "когда": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        return _MEM[tenancy.ключ_памяти(rid)]
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO check_rules (id,rules,version,editor,updated_at,tenant) "
            "VALUES (%s,%s,%s,%s,now(),%s) "
            "ON CONFLICT (tenant, id) DO UPDATE SET rules=EXCLUDED.rules, version=EXCLUDED.version, "
            "editor=EXCLUDED.editor, updated_at=now()",
            (rid, json.dumps(набор, ensure_ascii=False), версия, editor, tenancy.текущий()))
    return await get(rid)


async def revert(rid: str) -> bool:
    """Снять правку: набор снова такой, каким приехал в пакете вертикали."""
    if not _has_pg():
        return _MEM.pop(tenancy.ключ_памяти(rid), None) is not None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM check_rules WHERE id=%s AND tenant=%s",
                                 (rid, tenancy.текущий()))
        return bool(getattr(cur, "rowcount", 0))


class Эффективные:
    """То, по чему реально считается прогон: правка из стора, иначе поставочный набор из пакета.

    Ядру (`ape.набор_правил`) нужен синхронный `get` — он зовётся внутри прогона в потоке. Поэтому
    правки держим снимком в памяти процесса и обновляем его при каждом сохранении и откате.
    """

    def __init__(self) -> None:
        self._правки: dict[str, dict] = {}

    def обновить(self, правки: dict[str, dict]) -> None:
        self._правки = dict(правки or {})

    def get(self, имя: str) -> dict:
        from cli import ape
        return self._правки.get(имя) or ape.набор_из_пакета(имя) or {}

    def правка(self, имя: str) -> dict | None:
        return self._правки.get(имя)


ЭФФЕКТИВНЫЕ = Эффективные()


async def refresh() -> None:
    """Перечитать правки в снимок, по которому считает ядро."""
    ЭФФЕКТИВНЫЕ.обновить({r["id"]: r["набор"] for r in await all() if r.get("набор")})
