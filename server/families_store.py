"""Реестр семей (Families Registry) — ЕДИНЫЙ источник семей в Postgres. Раньше семьи были
константой `ape.AGENT_FAMILIES` в коде, а «отдел» (department из Keycloak-JWT) сопоставлялся с
именем семьи ПО СОГЛАШЕНИЮ (совпадение имён), без общего списка. Теперь семья определяется в одном
месте: этот стор — и список валидных семей/отделов (ABAC `department==family`), и ростер (роли→навыки).

Сид — из `ape.AGENT_FAMILIES` при первом старте (код = дефолт-роспись). Дальше семьи правятся/создаются
из UI (POST) — включая СВОИ (кастомные, напр. sales/hr), которые становятся валидными отделами для ABAC
и тегами навыков. Встроенные семьи сохраняют code-ростер для сборки агентов (build_agent_spec); у
кастомных ростера нет — они работают как ось доступа (department==family) и теги.

families{id, title, mission, profile, kind(business|engineering|custom), members(JSON role→[title,[skills]]),
builtin(bool), editor, updated_at}.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    mission     TEXT,
    profile     TEXT DEFAULT 'research',
    kind        TEXT DEFAULT 'custom',
    members     JSONB NOT NULL DEFAULT '{}'::jsonb,
    builtin     BOOLEAN NOT NULL DEFAULT false,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}
_COLS = "id,title,mission,profile,kind,members,builtin,editor,updated_at"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "title": r[1], "mission": r[2], "profile": r[3], "kind": r[4],
            "members": r[5] or {}, "builtin": bool(r[6]), "editor": r[7],
            "updated_at": r[8].isoformat() if r[8] else None}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def all() -> list[dict]:
    if not _has_pg():
        return sorted(_MEM.values(), key=lambda f: f["id"])
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM families ORDER BY id")
        return [_row(r) for r in await cur.fetchall()]


async def get(fid: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(fid)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM families WHERE id=%s", (fid,))
        r = await cur.fetchone()
    return _row(r) if r else None


async def ids() -> set[str]:
    return {f["id"] for f in await all()}


async def save(fid: str, spec: dict, editor: str = "dev", builtin: bool = False) -> dict:
    spec = spec or {}
    card = {"id": fid, "title": spec.get("title") or fid, "mission": spec.get("mission") or "",
            "profile": spec.get("profile") or "research", "kind": spec.get("kind") or "custom",
            "members": spec.get("members") or {}, "builtin": builtin}
    if not _has_pg():
        card["editor"] = editor
        _MEM[fid] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO families (id,title,mission,profile,kind,members,builtin,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, mission=EXCLUDED.mission, "
            "profile=EXCLUDED.profile, kind=EXCLUDED.kind, members=EXCLUDED.members, editor=EXCLUDED.editor, "
            "updated_at=now()",
            (fid, card["title"], card["mission"], card["profile"], card["kind"],
             json.dumps(card["members"]), builtin, editor))
    return await get(fid)


async def delete(fid: str) -> bool:
    """Удалить кастомную семью. Встроенные (builtin) удалять нельзя (защита ростера)."""
    cur = await get(fid)
    if not cur or cur.get("builtin"):
        return False
    if not _has_pg():
        _MEM.pop(fid, None)
        return True
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM families WHERE id=%s", (fid,))
    return True


async def seed_from_code(agent_families: dict, biz: set) -> None:
    """Одноразовый сид реестра из ape.AGENT_FAMILIES (код = дефолт-роспись), если реестр пуст."""
    try:
        if await all():
            return
        for fid, fam in (agent_families or {}).items():
            members = {mk: [mt, list(sk)] for mk, (mt, sk) in (fam.get("members") or {}).items()}
            await save(fid, {"title": fam.get("title"), "mission": fam.get("mission"),
                             "profile": fam.get("profile") or "research",
                             "kind": "business" if fid in (biz or set()) else "engineering",
                             "members": members}, editor="seed", builtin=True)
    except Exception:  # noqa: BLE001 — сид опционален, не валим старт
        pass
