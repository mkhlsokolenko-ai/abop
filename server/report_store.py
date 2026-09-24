"""Реестр шаблонов отчётов (Report Templates) в Postgres — «меняешь шаблон в БД → меняется вид отчёта»
без передеплоя (Schema-driven, разделение содержания и представления). Раньше HTML отчёта был
захардкожен в web_api._build_report_html; теперь OUT-узел может ссылаться на report_template_id, и
рендер берёт HTML/CSS из БД. LLM/код дают ДАННЫЕ, шаблон — только ВИД.

Безопасность: НЕ выполняем код из БД (никакого exec/Jinja-eval). Только подстановка плейсхолдеров
{{ключ}} из подготовленного контекста (title/date/agent/summary/findings/deliveries) — контент
готовит ABOP, шаблон задаёт вёрстку/CSS.

report_templates{id, name, html, css, pdf_options JSONB, builtin, editor, updated_at}.
"""
from __future__ import annotations

import json
import re

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    html        TEXT NOT NULL,
    css         TEXT NOT NULL DEFAULT '',
    pdf_options JSONB NOT NULL DEFAULT '{}'::jsonb,
    builtin     BOOLEAN NOT NULL DEFAULT false,
    editor      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MEM: dict[str, dict] = {}
_COLS = "id,name,html,css,pdf_options,builtin,editor,updated_at"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "name": r[1], "html": r[2], "css": r[3] or "", "pdf_options": r[4] or {},
            "builtin": bool(r[5]), "editor": r[6], "updated_at": r[7].isoformat() if r[7] else None}


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
        cur = await conn.execute(f"SELECT {_COLS} FROM report_templates ORDER BY id")
        return [_row(r) for r in await cur.fetchall()]


async def get(tid: str) -> dict | None:
    if not tid:
        return None
    if not _has_pg():
        return _MEM.get(tid)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM report_templates WHERE id=%s", (tid,))
        r = await cur.fetchone()
    return _row(r) if r else None


async def save(tid: str, spec: dict, editor: str = "dev", builtin: bool = False) -> dict:
    spec = spec or {}
    card = {"id": tid, "name": spec.get("name") or tid, "html": spec.get("html") or "",
            "css": spec.get("css") or "", "pdf_options": spec.get("pdf_options") or {}, "builtin": builtin}
    if not _has_pg():
        card["editor"] = editor
        _MEM[tid] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO report_templates (id,name,html,css,pdf_options,builtin,editor,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, html=EXCLUDED.html, css=EXCLUDED.css, "
            "pdf_options=EXCLUDED.pdf_options, editor=EXCLUDED.editor, updated_at=now()",
            (tid, card["name"], card["html"], card["css"], json.dumps(card["pdf_options"]), builtin, editor))
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
        await conn.execute("DELETE FROM report_templates WHERE id=%s", (tid,))
    return True


def render(template: dict, ctx: dict) -> str:
    """Безопасная подстановка {{ключ}} из ctx в шаблон HTML (+ вставка CSS в {{css}}). Без выполнения
    кода: неизвестные плейсхолдеры → пусто. ctx готовит ABOP (title/date/agent/summary/findings/deliveries)."""
    html = template.get("html") or ""
    data = dict(ctx or {})
    data.setdefault("css", template.get("css") or "")

    def _sub(m):
        key = m.group(1).strip()
        v = data.get(key, "")
        return str(v if v is not None else "")
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", _sub, html)


# ── Встроенный дефолт-шаблон (вид как у прежнего _build_report_html, но теперь редактируемый в БД) ──
_DEFAULT_CSS = (
    "body{font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:28px;color:#0f172a}"
    ".hd{border-bottom:3px solid #6366f1;padding-bottom:14px;margin-bottom:22px}"
    "h1{color:#4338ca;margin:0 0 4px;font-size:24px}.sub{color:#64748b;font-size:13px}"
    ".sum{display:flex;gap:14px;margin:18px 0}.card{flex:1;background:#f1f5f9;border-radius:10px;padding:14px}"
    ".card h3{margin:0 0 6px;color:#64748b;font-size:12px;font-weight:600;text-transform:uppercase}"
    ".num{font-size:26px;font-weight:800;color:#4338ca}"
    ".fnd{border-left:3px solid #6366f1;padding:8px 12px;margin:8px 0;background:#f8fafc;border-radius:0 8px 8px 0;font-size:13px}"
    ".dl{font-size:12px;color:#475569;margin:4px 0}"
)
_DEFAULT_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><style>{{css}}</style></head><body>"
    "<div class='hd'><h1>{{title}}</h1><div class='sub'>Агент: {{agent}} · {{date}}</div></div>"
    "<div class='sum'><div class='card'><h3>Находок</h3><div class='num'>{{findings_total}}</div></div>"
    "<div class='card'><h3>Расследований</h3><div class='num'>{{investigations_total}}</div></div></div>"
    "<div>{{findings}}</div>"
    "<h3>Доставка</h3><div>{{deliveries}}</div>"
    "</body></html>"
)


async def seed_if_empty() -> None:
    try:
        if await all():
            return
        await save("default", {"name": "Стандартный отчёт ABOP", "html": _DEFAULT_HTML, "css": _DEFAULT_CSS,
                               "pdf_options": {"format": "A4", "orientation": "portrait"}},
                   editor="seed", builtin=True)
    except Exception:  # noqa: BLE001
        pass
