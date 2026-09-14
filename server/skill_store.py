"""Хранилище правок навыков (skill overrides) — Postgres + фолбэк в память.

Навыки определяются `.md`-файлами (ape.SKILLS + parse_skill_md) — это база. Пользователь
в UI правит навык («Сохранить новую версию»: название/описание/шаги/режим и т.п.) и настраивает
data-need (источники). Эти правки — ОБЩИЕ для всех пользователей и обязаны переживать
перенакат/рестарт, поэтому хранятся в Postgres (а не в localStorage браузера, ADR-024-подобно
жизненному циклу; см. беклог persistence-localstorage-hole). Без DSN — фолбэк в память.

skill_override{sid, patch{title,short,mode,egress,cite,flow,when,method,dod,anti}, datasources[],
version, editor, updated_at}. Отсутствие записи ⇒ навык как в каталоге кода.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_overrides (
    sid          TEXT PRIMARY KEY,
    patch        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- правки контента (название/описание/шаги/...)
    datasources  JSONB,                                 -- data-need навыка (null ⇒ дефолт из кода)
    version      TEXT NOT NULL DEFAULT 'v1.0',
    editor       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _bump(version: str | None) -> str:
    """v1.0 → v1.1 (минорная версия при каждом сохранении, как это делал клиент)."""
    try:
        maj, _, minor = (version or "v1.0").replace("v", "").partition(".")
        return "v" + (maj or "1") + "." + str(int(minor or 0) + 1)
    except ValueError:
        return "v1.1"


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


def _row(sid: str, r: dict) -> dict:
    return {"sid": sid, "patch": r.get("patch") or {}, "datasources": r.get("datasources"),
            "version": r.get("version") or "v1.0", "editor": r.get("editor"),
            "updated_at": r.get("updated_at")}


async def get(sid: str) -> dict | None:
    if not _has_pg():
        r = _MEM.get(sid)
        return _row(sid, r) if r else None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT sid,patch,datasources,version,editor,updated_at FROM skill_overrides WHERE sid=%s", (sid,))
        r = await cur.fetchone()
    if not r:
        return None
    return {"sid": r[0], "patch": r[1] or {}, "datasources": r[2], "version": r[3],
            "editor": r[4], "updated_at": r[5].isoformat() if r[5] else None}


async def all() -> dict[str, dict]:
    """Все оверрайды: sid → {patch, datasources, version, editor, updated_at}. Для оверлея каталога."""
    if not _has_pg():
        return {sid: _row(sid, r) for sid, r in _MEM.items()}
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT sid,patch,datasources,version,editor,updated_at FROM skill_overrides")
        rows = await cur.fetchall()
    return {r[0]: {"sid": r[0], "patch": r[1] or {}, "datasources": r[2], "version": r[3],
                   "editor": r[4], "updated_at": r[5].isoformat() if r[5] else None} for r in rows}


async def datasources_map() -> dict[str, list]:
    """sid → datasources (только те, у кого оверрайд источников не null) — для инъекции в ape."""
    out = {}
    for sid, r in (await all()).items():
        if r.get("datasources") is not None:
            out[sid] = r["datasources"]
    return out


async def _upsert(sid: str, *, patch=None, datasources="__keep__", editor: str = "dev") -> dict:
    cur = await get(sid) or {"patch": {}, "datasources": None, "version": "v1.0"}
    new_patch = dict(cur.get("patch") or {})
    if patch:
        new_patch.update(patch)
    new_ds = cur.get("datasources") if datasources == "__keep__" else datasources
    version = _bump(cur.get("version"))
    ts = _now_iso()
    if not _has_pg():
        _MEM[sid] = {"patch": new_patch, "datasources": new_ds, "version": version,
                     "editor": editor, "updated_at": ts}
        return _row(sid, _MEM[sid])
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO skill_overrides (sid,patch,datasources,version,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (sid) DO UPDATE SET patch=EXCLUDED.patch, datasources=EXCLUDED.datasources, "
            "version=EXCLUDED.version, editor=EXCLUDED.editor, updated_at=now()",
            (sid, json.dumps(new_patch),
             None if new_ds is None else json.dumps(new_ds), version, editor))
    return await get(sid)


async def save_patch(sid: str, patch: dict, editor: str = "dev") -> dict:
    """Сохранить правки контента навыка новой версией (editor — реальный пользователь)."""
    return await _upsert(sid, patch=patch or {}, editor=editor)


async def save_datasources(sid: str, datasources: list, editor: str = "dev") -> dict:
    """Сохранить data-need навыка (источники) в PG (заменяет файловый override ape)."""
    return await _upsert(sid, datasources=datasources or [], editor=editor)
