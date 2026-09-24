"""Реестр шаблонов извлечения (Extraction Schema Templates) в Postgres — JSON Schema по id, на которую
ссылается навык одной строкой. «Меняешь схему в БД → меняется структура данных у всех навыков с этим id»
без передеплоя (Schema-driven extraction: ЛЛМ только раскладывает данные по схеме, не выдумывает).

Безопасность: храним ТОЛЬКО JSON Schema (не Python/Pydantic-код, без exec). ABOP отдаёт её модели как
response_format (structured output self-host Qwen) + инструкцию-парсер. Валидна и безопасна.

schema_templates{id, name, json_schema JSONB, instruction TEXT, builtin, editor, updated_at}.
"""
from __future__ import annotations

import json

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    json_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    instruction TEXT NOT NULL DEFAULT '',
    builtin     BOOLEAN NOT NULL DEFAULT false,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}
_COLS = "id,name,json_schema,instruction,builtin,editor,updated_at"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "name": r[1], "json_schema": r[2] or {}, "instruction": r[3] or "",
            "builtin": bool(r[4]), "editor": r[5], "updated_at": r[6].isoformat() if r[6] else None}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def all() -> list[dict]:
    if not _has_pg():
        return sorted(_MEM.values(), key=lambda x: x["id"])
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM schema_templates ORDER BY id")
        return [_row(r) for r in await cur.fetchall()]


async def get(tid: str) -> dict | None:
    if not tid:
        return None
    if not _has_pg():
        return _MEM.get(tid)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM schema_templates WHERE id=%s", (tid,))
        r = await cur.fetchone()
    return _row(r) if r else None


async def save(tid: str, spec: dict, editor: str = "dev", builtin: bool = False) -> dict:
    spec = spec or {}
    card = {"id": tid, "name": spec.get("name") or tid, "json_schema": spec.get("json_schema") or {},
            "instruction": spec.get("instruction") or "", "builtin": builtin}
    if not _has_pg():
        card["editor"] = editor
        _MEM[tid] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO schema_templates (id,name,json_schema,instruction,builtin,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, json_schema=EXCLUDED.json_schema, "
            "instruction=EXCLUDED.instruction, editor=EXCLUDED.editor, updated_at=now()",
            (tid, card["name"], json.dumps(card["json_schema"]), card["instruction"], builtin, editor))
    return await get(tid)


async def delete(tid: str) -> bool:
    cur = await get(tid)
    if not cur or cur.get("builtin"):
        return False
    if not _has_pg():
        _MEM.pop(tid, None)
        return True
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM schema_templates WHERE id=%s", (tid,))
    return True


def response_format(template: dict) -> dict:
    """JSON Schema → OpenAI-совместимый response_format (strict structured output)."""
    sch = (template or {}).get("json_schema") or {}
    return {"type": "json_schema", "json_schema": {"name": "extraction", "schema": sch, "strict": True}}


# ── Встроенный дефолт: схема находок ABOP (та, что рантайм использует по умолчанию) ──
_DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {
        "находки": {"type": "array", "items": {"type": "object", "properties": {
            "запись": {"type": "string"}, "наблюдение": {"type": "string"},
            "сумма": {"type": "string"}, "норма": {"type": "string"}}}},
        "итог": {"type": "string"},
    },
}


async def seed_if_empty() -> None:
    try:
        if await all():
            return
        await save("findings", {"name": "Находки ABOP (по умолчанию)", "json_schema": _DEFAULT_SCHEMA,
                                "instruction": "Верни находки строго по схеме: каждая — с id записи, суммой и нормой (только конкретная статья). Ничего не выдумывай."},
                   editor="seed", builtin=True)
    except Exception:  # noqa: BLE001
        pass
