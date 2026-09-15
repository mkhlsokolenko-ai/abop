"""Реестр систем/подключений (Connection Registry) — единый каталог эндпоинтов и Kafka-топиков в Postgres.

Раньше эндпоинт (URL/путь) хранился ИНЛАЙН в каждом коннекторе/рецепте (dp_connectors.target,
recipe.source.url) — дублирование, негде ротировать хост/креды, нет каталога топиков для событийки.
Теперь — центральный реестр: коннектор/рецепт/ТРИГГЕР ссылаются на `system_id` + относительный
путь/топик. Креды в реестре НЕ хранятся — только `auth_ref` (имя переменной .env/секрета). Это
третья ось-джойн интеграции (рядом с entity=данные и nsi_key=процесс). Сид — из server-inventory
(демо-стенд 5.129.192.63). См. беклог persistence-localstorage-hole и идею владельца 2026-09-15.

system{id PK, kind(rest|kafka|db|vector|smtp|s3|auth|llm), base_url, brokers[], topics[],
auth_ref, tenant, egress(internal|external), note, editor, updated_at}.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS systems (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    base_url    TEXT,
    brokers     JSONB NOT NULL DEFAULT '[]'::jsonb,
    topics      JSONB NOT NULL DEFAULT '[]'::jsonb,
    auth_ref    TEXT,
    tenant      TEXT,
    egress      TEXT NOT NULL DEFAULT 'external',
    note        TEXT,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}

_COLS = "id,kind,base_url,brokers,topics,auth_ref,tenant,egress,note,editor,updated_at"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "kind": r[1], "base_url": r[2], "brokers": r[3] or [], "topics": r[4] or [],
            "auth_ref": r[5], "tenant": r[6], "egress": r[7], "note": r[8], "editor": r[9],
            "updated_at": r[10].isoformat() if r[10] else None}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def all() -> list[dict]:
    if not _has_pg():
        return list(_MEM.values())
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM systems ORDER BY id")
        return [_row(r) for r in await cur.fetchall()]


async def get(sid: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(sid)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM systems WHERE id=%s", (sid,))
        r = await cur.fetchone()
    return _row(r) if r else None


def normalize(sid: str, spec: dict) -> dict:
    """UI/сид-форма → каноническая карточка системы (без записи)."""
    spec = spec or {}
    return {"id": sid, "kind": spec.get("kind") or "rest", "base_url": spec.get("base_url") or "",
            "brokers": spec.get("brokers") or [], "topics": spec.get("topics") or [],
            "auth_ref": spec.get("auth_ref") or "", "tenant": spec.get("tenant") or "",
            "egress": spec.get("egress") or "external", "note": spec.get("note") or ""}


async def save(sid: str, spec: dict, editor: str = "dev") -> dict:
    card = normalize(sid, spec)
    if not _has_pg():
        card["editor"] = editor
        _MEM[sid] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO systems (id,kind,base_url,brokers,topics,auth_ref,tenant,egress,note,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (id) DO UPDATE SET kind=EXCLUDED.kind, base_url=EXCLUDED.base_url, "
            "brokers=EXCLUDED.brokers, topics=EXCLUDED.topics, auth_ref=EXCLUDED.auth_ref, "
            "tenant=EXCLUDED.tenant, egress=EXCLUDED.egress, note=EXCLUDED.note, editor=EXCLUDED.editor, updated_at=now()",
            (sid, card["kind"], card["base_url"], json.dumps(card["brokers"]), json.dumps(card["topics"]),
             card["auth_ref"], card["tenant"], card["egress"], card["note"], editor))
    return await get(sid)


async def delete(sid: str) -> None:
    if not _has_pg():
        _MEM.pop(sid, None)
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM systems WHERE id=%s", (sid,))


# ── Сид демо-стенда (server-inventory 5.129.192.63) — одноразовый, если реестр пуст ──
_SEED = [
    ("redmine",  {"kind": "rest", "base_url": "http://5.129.192.63:3000", "auth_ref": "REDMINE_API_KEY",
                  "egress": "external", "note": "Трекер задач (аналог Jira) → issue. Хедер X-Redmine-API-Key. /issues.json"}),
    ("bookstack", {"kind": "rest", "base_url": "http://5.129.192.63:6875", "auth_ref": "BOOKSTACK_TOKEN",
                   "egress": "external", "note": "Вики (аналог Confluence) → document. Authorization: Token. /api/pages"}),
    ("twenty",   {"kind": "rest", "base_url": "http://5.129.192.63:3002", "auth_ref": "TWENTY_API_KEY",
                  "egress": "external", "note": "CRM → customer. Bearer JWT. /rest/opportunities (root data.opportunities)"}),
    ("mailpit",  {"kind": "rest", "base_url": "http://5.129.192.63:8025", "auth_ref": "",
                  "egress": "external", "note": "Почта → email. /api/v1/messages (без auth). SMTP :1025"}),
    ("minio",    {"kind": "s3", "base_url": "http://5.129.192.63:9000", "auth_ref": "MINIO_CREDS",
                  "egress": "external", "note": "S3-хранилище (мок 1С/документы). Бакет abop-demo public-read"}),
    ("nocodb",   {"kind": "rest", "base_url": "http://5.129.192.63:8090", "auth_ref": "NOCODB_TOKEN",
                  "egress": "external", "note": "No-code БД/таблицы (как база 1С: invoices/payments/vendors)"}),
    ("gitea",    {"kind": "rest", "base_url": "http://5.129.192.63:3001", "auth_ref": "GITEA_TOKEN",
                  "egress": "external", "note": "Git-хостинг"}),
    ("kroki",    {"kind": "rest", "base_url": "http://5.129.192.63:8000", "auth_ref": "",
                  "egress": "external", "note": "Рендер диаграмм"}),
    ("postgres", {"kind": "db", "base_url": "postgresql://127.0.0.1:5433/abop", "auth_ref": "POSTGRES_DSN",
                  "egress": "internal", "note": "Canonical store / агенты / контракты / прогоны (persist)"}),
    ("qdrant",   {"kind": "vector", "base_url": "http://127.0.0.1:6333", "auth_ref": "QDRANT_API_KEY",
                  "egress": "internal", "note": "Векторный store (RAG, sLAVA-нормы)"}),
    ("keycloak", {"kind": "auth", "base_url": "http://127.0.0.1:8811/realms/abop", "auth_ref": "KEYCLOAK_JWKS_URI",
                  "egress": "internal", "note": "SSO/RBAC (realm abop). JWKS /protocol/openid-connect/certs"}),
    ("routeai",  {"kind": "llm", "base_url": "https://routerai.ru/api/v1", "auth_ref": "ROUTEAI_API_KEY",
                  "egress": "external", "note": "LLM-шлюз (каскады моделей). /chat/completions"}),
    ("kafka",    {"kind": "kafka", "base_url": "", "brokers": [], "topics": [],
                  "egress": "internal", "note": "Событийная шина (Хартия v3.5) — ещё не развёрнута; каталог топиков для триггеров"}),
]


async def seed_if_empty() -> None:
    """Одноразовый сид реестра из server-inventory (демо-стенд), если таблица пуста."""
    try:
        if await all():
            return
        for sid, spec in _SEED:
            await save(sid, spec, editor="seed")
    except Exception:  # noqa: BLE001 — сид опционален, не валим старт
        pass
