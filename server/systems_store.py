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
-- scope: разрешённые отделы/семьи (ABAC-изоляция доступа агента); пусто ⇒ доступно всем. Добавляем
-- идемпотентно (таблица могла быть создана раньше без колонки). См. agent-rbac-mcp-gateway.
ALTER TABLE systems ADD COLUMN IF NOT EXISTS scope JSONB NOT NULL DEFAULT '[]'::jsonb;
-- role: источник / приёмник / оба / сервис / инфраструктура. Нужна, чтобы отличить настоящую дыру
-- (в систему некому или нечем писать) от системы, которой выход не нужен. Добавляем идемпотентно.
ALTER TABLE systems ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT '';
"""

_MEM: dict[str, dict] = {}

_COLS = ("id,kind,base_url,brokers,topics,auth_ref,tenant,egress,note,editor,"
         "updated_at,scope,role")


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "kind": r[1], "base_url": r[2], "brokers": r[3] or [], "topics": r[4] or [],
            "auth_ref": r[5], "tenant": r[6], "egress": r[7], "note": r[8], "editor": r[9],
            "updated_at": r[10].isoformat() if r[10] else None, "scope": r[11] or [],
            "role": (r[12] if len(r) > 12 else "") or ""}


def allowed_for(system: dict, department: str | None) -> bool:
    """ABAC: агент/пользователь отдела `department` вправе обращаться к системе? Пустой scope ⇒ всем;
    admin/support (область '*') ⇒ везде; иначе department должен быть в scope системы."""
    scope = (system or {}).get("scope") or []
    if not scope or department in ("*", None):
        return True
    return department in scope


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
    scope = spec.get("scope") or []
    if not isinstance(scope, list):
        scope = []
    return {"id": sid, "kind": spec.get("kind") or "rest", "base_url": spec.get("base_url") or "",
            "brokers": spec.get("brokers") or [], "topics": spec.get("topics") or [],
            "auth_ref": spec.get("auth_ref") or "", "tenant": spec.get("tenant") or "",
            "egress": spec.get("egress") or "external", "note": spec.get("note") or "",
            "scope": scope,
            # Роль системы: «источник» (её читают рецепты), «приёмник» (в неё пишет коннектор),
            # «оба» или «инфраструктура» (шина, БД, auth, LLM — туда агенты не ходят). Без роли
            # нельзя отличить настоящую дыру (нет ни входа, ни выхода) от системы, которой выход
            # и не нужен; сторожит tests/test_systems_wiring.py.
            "role": spec.get("role") or ""}


async def save(sid: str, spec: dict, editor: str = "dev") -> dict:
    card = normalize(sid, spec)
    if not _has_pg():
        card["editor"] = editor
        _MEM[sid] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO systems (id,kind,base_url,brokers,topics,auth_ref,tenant,egress,note,scope,role,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (id) DO UPDATE SET kind=EXCLUDED.kind, base_url=EXCLUDED.base_url, "
            "brokers=EXCLUDED.brokers, topics=EXCLUDED.topics, auth_ref=EXCLUDED.auth_ref, "
            "tenant=EXCLUDED.tenant, egress=EXCLUDED.egress, note=EXCLUDED.note, scope=EXCLUDED.scope, "
            "role=EXCLUDED.role, editor=EXCLUDED.editor, updated_at=now()",
            (sid, card["kind"], card["base_url"], json.dumps(card["brokers"]), json.dumps(card["topics"]),
             card["auth_ref"], card["tenant"], card["egress"], card["note"], json.dumps(card["scope"]),
             card["role"], editor))
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
                  "egress": "external", "note": "Трекер задач (аналог Jira) → issue. Хедер X-Redmine-API-Key. /issues.json", "role": "оба"}),
    # 1С и ERP были заведены руками прямо на стенде: адаптеры команд для них есть, а в
    # поставляемом реестре записи не было — на чистой установке команда упёрлась бы в ABAC
    # («системы нет в реестре»). Нашёл tests/test_systems_wiring.py.
    ("1c", {"kind": "rest", "base_url": "http://201.51.5.24:8092", "auth_ref": "",
            "egress": "external", "scope": ["audit", "finance"], "role": "оба",
            "note": "Выгрузка 1С (документы и справочники) → doc1c/ref1c; приём заявок на "
                    "выгрузку командой export.request"}),
    ("dolibarr", {"kind": "rest", "base_url": "http://5.129.192.63:8095",
                  "auth_ref": "DOLIBARR_API_KEY", "egress": "external",
                  "scope": ["finance", "analytics", "credit", "management"], "role": "оба",
                  "note": "ERP (аналог 1С:УНФ) → transaction. Хедер DOLAPIKEY. "
                          "/api/index.php/invoices; команда invoice.create"}),
    ("openproject", {"kind": "rest", "base_url": "http://5.129.192.63:8096",
                     "auth_ref": "OPENPROJECT_TOKEN", "egress": "external",
                     "note": "Проектный сервер (аналог MS Project/Jira) → work_package. Basic "
                             "apikey:<ключ>. /api/v3/projects/<проект>/work_packages. Источник "
                             "дорожной карты и приёмник задач по итогам смещения сроков", "role": "оба"}),
    ("bookstack", {"kind": "rest", "base_url": "http://5.129.192.63:6875", "auth_ref": "BOOKSTACK_TOKEN",
                   "egress": "external", "note": "Вики (аналог Confluence) → document. Authorization: Token. /api/pages", "role": "оба"}),
    ("twenty",   {"kind": "rest", "base_url": "http://5.129.192.63:3002", "auth_ref": "TWENTY_API_KEY",
                  "egress": "external", "scope": ["management", "analytics"],
                  "note": "CRM → customer. Bearer JWT. /rest/opportunities (root data.opportunities). Доступ: продажи/аналитика", "role": "источник"}),
    ("mailpit",  {"kind": "rest", "base_url": "http://5.129.192.63:8025", "auth_ref": "",
                  "egress": "external", "note": "Почта → email. /api/v1/messages (без auth). SMTP :1025", "role": "оба"}),
    ("minio",    {"kind": "s3", "base_url": "http://5.129.192.63:9000", "auth_ref": "MINIO_CREDS",
                  "egress": "external", "note": "S3-хранилище (мок 1С/документы). Бакет abop-demo public-read", "role": "оба"}),
    ("nocodb",   {"kind": "rest", "base_url": "http://5.129.192.63:8090", "auth_ref": "NOCODB_TOKEN",
                  "egress": "external", "note": "No-code БД/таблицы (как база 1С: invoices/payments/vendors)", "role": "приёмник"}),
    ("gitea",    {"kind": "rest", "base_url": "http://5.129.192.63:3001", "auth_ref": "GITEA_TOKEN",
                  "egress": "external", "scope": ["architecture", "engineering"],
                  "note": "Git-хостинг. Доступ: архитектура/инженерия (аналитик/финансы не видят)", "role": "приёмник"}),
    ("kroki",    {"kind": "rest", "base_url": "http://5.129.192.63:8000", "auth_ref": "",
                  "egress": "external", "scope": ["architecture", "engineering"],
                  "note": "Рендер диаграмм. Доступ: архитектура/инженерия", "role": "сервис"}),
    ("postgres", {"kind": "db", "base_url": "postgresql://127.0.0.1:5433/abop", "auth_ref": "POSTGRES_DSN",
                  "egress": "internal", "note": "Canonical store / агенты / контракты / прогоны (persist)", "role": "инфраструктура"}),
    ("qdrant",   {"kind": "vector", "base_url": "http://127.0.0.1:6333", "auth_ref": "QDRANT_API_KEY",
                  "egress": "internal", "note": "Векторный store (RAG, sLAVA-нормы)", "role": "инфраструктура"}),
    ("keycloak", {"kind": "auth", "base_url": "http://127.0.0.1:8811/realms/abop", "auth_ref": "KEYCLOAK_JWKS_URI",
                  "egress": "internal", "note": "SSO/RBAC (realm abop). JWKS /protocol/openid-connect/certs", "role": "инфраструктура"}),
    ("routeai",  {"kind": "llm", "base_url": "https://routerai.ru/api/v1", "auth_ref": "ROUTEAI_API_KEY",
                  "egress": "external", "note": "LLM-шлюз (каскады моделей). /chat/completions", "role": "инфраструктура"}),
    ("kafka",    {"kind": "kafka", "base_url": "", "brokers": [], "topics": [],
                  "egress": "internal", "note": "Событийная шина (Хартия v3.5) — ещё не развёрнута; каталог топиков для триггеров", "role": "инфраструктура"}),
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
