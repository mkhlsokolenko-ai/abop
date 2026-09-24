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


# ── Общий CSS отчётов (единый визуальный язык для всех кейсов) ─────────────────────────────────────
# Плейсхолдеры, которые готовит web_api._report_context:
#   title, agent, date, verdict, findings_total, investigations_total,
#   by_class (HTML-строка бейджей A/B/C/D), findings (HTML), investigations (HTML), skills (HTML), deliveries (HTML)
_BASE_CSS = (
    "body{font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:32px;color:#0f172a;line-height:1.5;max-width:860px}"
    ".hd{border-bottom:3px solid #6366f1;padding-bottom:14px;margin-bottom:20px}"
    "h1{color:#4338ca;margin:0 0 4px;font-size:24px}.sub{color:#64748b;font-size:13px}"
    ".verdict{display:inline-block;margin-top:8px;font-size:12.5px;color:#475569;background:#eef2ff;border-radius:20px;padding:4px 12px}"
    "h2{font-size:16px;color:#334155;margin:24px 0 10px;border-left:4px solid #6366f1;padding-left:9px}"
    ".sum{display:flex;gap:14px;margin:18px 0}.card{flex:1;background:#f1f5f9;border-radius:12px;padding:14px;text-align:center}"
    ".card h3{margin:0 0 6px;color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.03em}"
    ".num{font-size:28px;font-weight:800;color:#4338ca}"
    ".badges{margin:6px 0 14px}.b{display:inline-block;font-size:12px;font-weight:700;border-radius:8px;padding:3px 10px;margin-right:6px;color:#fff}"
    ".b.A{background:#dc2626}.b.B{background:#ea580c}.b.C{background:#ca8a04}.b.D{background:#0891b2}"
    ".fnd{border-left:3px solid #6366f1;padding:9px 13px;margin:8px 0;background:#f8fafc;border-radius:0 8px 8px 0;font-size:13px}"
    ".fnd .cls{display:inline-block;font-weight:700;font-size:11px;color:#fff;background:#6366f1;border-radius:6px;padding:1px 7px;margin-right:6px}"
    ".fnd .norm{color:#0a7c66;font-size:11.5px;font-style:italic;display:block;margin-top:4px}"
    ".inv{border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin:10px 0;background:#fff}"
    ".inv .sym{font-weight:700;font-size:13.5px}.inv .sev{font-size:11px;color:#b91c1c;font-weight:700;text-transform:uppercase}"
    ".inv .chain{font-size:12px;color:#475569;margin:6px 0;font-family:'Cascadia Code',Consolas,monospace}"
    ".inv .delta{font-size:12px;color:#0f172a;background:#fff7ed;border-radius:6px;padding:3px 8px;display:inline-block}"
    ".task{border-left:3px solid #10b981;padding:8px 12px;margin:7px 0;background:#f0fdf4;border-radius:0 8px 8px 0;font-size:13px}"
    ".sk{margin:10px 0}.sk h3{font-size:13.5px;color:#4338ca;margin:0 0 4px}.sk pre{white-space:pre-wrap;background:#f8fafc;border-radius:8px;padding:10px;font-size:12.5px;margin:0}"
    ".dl{font-size:12px;color:#475569;margin:4px 0}"
    ".ft{color:#94a3b8;font-size:11px;border-top:1px solid #e2e8f0;margin-top:26px;padding-top:10px}"
)
_HEAD = ("<!DOCTYPE html><html><head><meta charset='utf-8'><style>{{css}}</style></head><body>"
         "<div class='hd'><h1>{{title}}</h1><div class='sub'>Агент: {{agent}} · {{date}}</div>"
         "<div class='verdict'>{{verdict}}</div></div>")
_FOOT = "<div class='ft'>Сформировано ABOP · {{agent}} · {{date}}</div></body></html>"

# Дефолт: показывает ВСЁ, что есть в результате (пустые секции просто не рендерятся — подстановка «пусто»).
_DEFAULT_HTML = (_HEAD +
    "<div class='sum'><div class='card'><h3>Находок</h3><div class='num'>{{findings_total}}</div></div>"
    "<div class='card'><h3>Расследований</h3><div class='num'>{{investigations_total}}</div></div></div>"
    "{{by_class}}{{charts}}"
    "{{findings}}{{investigations}}{{skills}}"
    "<h2>Доставка</h2>{{deliveries}}" + _FOOT)

# Аудитор 1С: акцент на находках A/B/C/D + нормы + график по классам.
_AUDIT_HTML = (_HEAD +
    "<div class='sum'><div class='card'><h3>Находок аудита</h3><div class='num'>{{findings_total}}</div></div></div>"
    "{{by_class}}{{charts}}"
    "<h2>Находки аудита</h2>{{findings}}"
    "{{skills}}"
    "<h2>Доставка</h2>{{deliveries}}" + _FOOT)

# Расследование от симптома: акцент на цепочках реализация→взаиморасчёты→НДС + график расхождений.
_INVEST_HTML = (_HEAD +
    "<div class='sum'><div class='card'><h3>Расследований</h3><div class='num'>{{investigations_total}}</div></div></div>"
    "{{charts}}"
    "<h2>Расследования от симптома</h2>{{investigations}}"
    "{{findings}}"
    "<h2>Доставка</h2>{{deliveries}}" + _FOOT)

# Дайджест задач: акцент на списке задач (structured-вывод навыка) + график по приоритетам.
_DIGEST_HTML = (_HEAD +
    "{{charts}}"
    "<h2>Задачи и сводка</h2>{{skills}}{{findings}}"
    "<h2>Доставка</h2>{{deliveries}}" + _FOOT)

_SEED = [
    ("default", "Стандартный отчёт ABOP", _DEFAULT_HTML),
    ("audit1c", "Отчёт аудита 1С (A/B/C/D)", _AUDIT_HTML),
    ("invest", "Отчёт расследования от симптома", _INVEST_HTML),
    ("digest", "Дайджест задач", _DIGEST_HTML),
]


async def seed_if_empty() -> None:
    """Досев/освежение встроенных шаблонов из кода. Наши builtin-шаблоны (editor='seed') обновляем при
    каждом старте — так «канонический вид» едет с кодом; пользовательские правки (editor≠'seed') не трогаем."""
    try:
        cur = {t["id"]: t for t in await all()}
        for tid, name, html in _SEED:
            ex = cur.get(tid)
            if ex and not (ex.get("builtin") and (ex.get("editor") in (None, "seed"))):
                continue             # шаблон отредактирован пользователем — не перезатираем
            await save(tid, {"name": name, "html": html, "css": _BASE_CSS,
                             "pdf_options": {"format": "A4", "orientation": "portrait"}},
                       editor="seed", builtin=True)
    except Exception:  # noqa: BLE001
        pass
