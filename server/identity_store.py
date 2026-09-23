"""Сквозной ID (Identity Map) — связка одного мастер-UID пользователя со ВСЕМИ его аккаунтами
в других системах (почта, Redmine, CRM/Twenty, 1С, BookStack…). Как в организациях: по одному ID
выдаются все доступы. Агент, запущенный «от имени» пользователя, наследует его ролевую (RBAC) и
атрибутную (ABAC department) модерацию из Keycloak-JWT, а из этой таблицы получает АДРЕСНОСТЬ —
какой почтовый ящик читать, какому Redmine-пользователю назначать задачу, чья это сделка в CRM.

Разделение ответственности:
- RBAC/ABAC (кто что вправе) — из Keycloak-JWT (roles + department). НЕ дублируем здесь.
- Identity Map (в КАКИХ системах пользователь есть и под каким адресом/логином) — эта таблица.

identity{uid, system, external_id, display, attrs(JSON), editor, updated_at}, PK (uid, system).
Тестовая SQL (Postgres, как остальные сторы) + mem-fallback. Сид — демо-пользователи стенда.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (
    uid          TEXT NOT NULL,
    system       TEXT NOT NULL,
    external_id  TEXT,
    display      TEXT,
    attrs        JSONB NOT NULL DEFAULT '{}'::jsonb,
    editor       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, system)
);
CREATE INDEX IF NOT EXISTS identity_uid_idx ON identity (uid);
"""

_MEM: dict[str, dict] = {}   # key "uid|system" → row

_COLS = "uid,system,external_id,display,attrs,editor,updated_at"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"uid": r[0], "system": r[1], "external_id": r[2], "display": r[3],
            "attrs": r[4] or {}, "editor": r[5], "updated_at": r[6].isoformat() if r[6] else None}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def links_for(uid: str) -> list[dict]:
    """Все внешние аккаунты пользователя (по мастер-UID)."""
    if not _has_pg():
        return [v for k, v in _MEM.items() if v["uid"] == uid]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM identity WHERE uid=%s ORDER BY system", (uid,))
        return [_row(r) for r in await cur.fetchall()]


async def bundle(uid: str, *, department: str | None = None, roles: list | None = None) -> dict:
    """Сквозной профиль пользователя: мастер-UID + department/roles (из JWT, ABAC/RBAC) +
    карта внешних систем {system: {external_id, display, attrs}}. Это и есть «адресный» контекст
    агента, работающего от имени пользователя."""
    links = await links_for(uid)
    systems = {l["system"]: {"external_id": l["external_id"], "display": l["display"], "attrs": l["attrs"]}
               for l in links}
    return {"uid": uid, "department": department, "roles": roles or [], "systems": systems}


async def link(uid: str, system: str, external_id: str = "", display: str = "",
               attrs: dict | None = None, editor: str = "dev") -> dict:
    """Привязать/обновить внешний аккаунт пользователя в системе."""
    attrs = attrs or {}
    if not _has_pg():
        row = {"uid": uid, "system": system, "external_id": external_id, "display": display,
               "attrs": attrs, "editor": editor, "updated_at": None}
        _MEM[f"{uid}|{system}"] = row
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO identity (uid,system,external_id,display,attrs,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (uid,system) DO UPDATE SET external_id=EXCLUDED.external_id, "
            "display=EXCLUDED.display, attrs=EXCLUDED.attrs, editor=EXCLUDED.editor, updated_at=now()",
            (uid, system, external_id, display, json.dumps(attrs), editor))
    return {"uid": uid, "system": system, "external_id": external_id, "display": display, "attrs": attrs}


async def unlink(uid: str, system: str) -> None:
    if not _has_pg():
        _MEM.pop(f"{uid}|{system}", None)
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM identity WHERE uid=%s AND system=%s", (uid, system))


# ── Сид демо-пользователей стенда (server-inventory). Мастер-UID = username из Keycloak realm abop. ──
_SEED = [
    # pm.manager (management) — менеджер проекта: почта, Redmine-исполнитель, владелец сделки в CRM
    ("pm.manager", "email",    "pm@demo.local",     "Пётр Менеджеров",  {"inbox": "mailpit", "smtp": "1025"}),
    ("pm.manager", "redmine",  "pm.manager",        "Пётр Менеджеров",  {"assignee_id": 4, "project": "1"}),
    ("pm.manager", "twenty",   "pm@demo.local",     "Пётр Менеджеров",  {"role": "owner"}),
    ("pm.manager", "bookstack", "pm.manager",       "Пётр Менеджеров",  {}),
    # buh.analyst (finance) — бухгалтер-аналитик: почта + доступ к 1С-выгрузкам
    ("buh.analyst", "email",   "buh@demo.local",    "Белла Бухгалтер",  {"inbox": "mailpit"}),
    ("buh.analyst", "1c",      "buh.analyst",       "Белла Бухгалтер",  {"base": "audit1c", "role": "accountant"}),
    ("buh.analyst", "nocodb",  "buh.analyst",       "Белла Бухгалтер",  {}),
    # admin.abop — область *, полный доступ
    ("admin.abop", "email",    "admin@demo.local",  "Администратор",    {"inbox": "mailpit"}),
    ("admin.abop", "redmine",  "admin",             "Администратор",    {"assignee_id": 1}),
]


async def seed_if_empty() -> None:
    """Одноразовый сид Identity Map демо-пользователями, если таблица пуста."""
    try:
        # проверка «пусто» через первый сид-uid
        if await links_for("pm.manager"):
            return
        for uid, system, ext, disp, attrs in _SEED:
            await link(uid, system, ext, disp, attrs, editor="seed")
    except Exception:  # noqa: BLE001 — сид опционален, не валим старт
        pass
