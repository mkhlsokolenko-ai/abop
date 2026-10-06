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

import html as _html
import json
import os
import re
from pathlib import Path

from .config import settings

REPORTS_DIR = Path(os.environ.get("APE_REPORTS_DIR") or (Path(__file__).resolve().parent.parent / "reports"))

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
-- Область применения формы: чьи результаты она оформляет. Добавляется отдельно, чтобы базы,
-- созданные раньше, получили колонку без пересоздания таблицы.
ALTER TABLE report_templates ADD COLUMN IF NOT EXISTS for_skills JSONB NOT NULL DEFAULT '[]'::jsonb;
-- Раскладка бланка: порядок разделов и графы документа этой вертикали. Живёт рядом с формой в базе —
-- значит вид документа меняют правкой записи, а не передеплоем web_api.
ALTER TABLE report_templates ADD COLUMN IF NOT EXISTS layout JSONB NOT NULL DEFAULT '[]'::jsonb;
"""

_MEM: dict[str, dict] = {}
_COLS = "id,name,html,css,pdf_options,builtin,editor,updated_at,for_skills,layout"


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _row(r) -> dict:
    return {"id": r[0], "name": r[1], "html": r[2], "css": r[3] or "", "pdf_options": r[4] or {},
            "builtin": bool(r[5]), "editor": r[6], "updated_at": r[7].isoformat() if r[7] else None,
            "for_skills": list(r[8] or []) if len(r) > 8 else [],
            "layout": list(r[9] or []) if len(r) > 9 else []}


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        # SCHEMA — две инструкции (создание таблицы и добавление колонки): драйвер выполняет по одной.
        # Строки-комментарии снимаем с начала инструкции, а не отбрасываем вместе с ней.
        for chunk in SCHEMA.split(";"):
            stmt = chr(10).join(ln for ln in chunk.splitlines() if not ln.strip().startswith("--")).strip()
            if stmt:
                await conn.execute(stmt)


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
            "css": spec.get("css") or "", "pdf_options": spec.get("pdf_options") or {}, "builtin": builtin,
            "for_skills": [str(x) for x in (spec.get("for_skills") or []) if str(x).strip()],
            "layout": spec.get("layout") or []}
    if not _has_pg():
        card["editor"] = editor
        _MEM[tid] = card
        return card
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO report_templates (id,name,html,css,pdf_options,builtin,editor,updated_at,for_skills,layout) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,now(),%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, html=EXCLUDED.html, css=EXCLUDED.css, "
            "pdf_options=EXCLUDED.pdf_options, editor=EXCLUDED.editor, updated_at=now(), "
            "for_skills=EXCLUDED.for_skills, layout=EXCLUDED.layout",
            (tid, card["name"], card["html"], card["css"], json.dumps(card["pdf_options"]), builtin, editor,
             json.dumps(card["for_skills"]), json.dumps(card["layout"])))
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
    # Бланк вертикали: раскладка документа объявлена в этой же записи БД. Результат прогона приходит
    # в контексте служебным ключом — он не плейсхолдер и в документ попасть не должен.
    _res = data.pop("_result", None)
    if template.get("layout"):
        from . import report_form
        data["blank"] = report_form.render(template.get("layout"), _res or {}, data)
        if not data["blank"].strip():
            # Форму поставили прогону не её навыков: графы бланка пусты. Пустой документ хуже
            # общего — показываем то, что в результате есть, а не чистый лист с подписями. Но РАМКУ
            # сохраняем: шапку, оговорку и подписи. Без них получатель видит простыню текста и не
            # может понять, чей это документ, на чём он стоит и кто за него отвечает.
            рамка = [b for b in (template.get("layout") or [])
                     if isinstance(b, dict) and str(b.get("t")) in ("head", "note", "sign")]
            шапка = report_form.frame(рамка, _res or {}, data, kinds=("head",))
            хвост = report_form.frame(рамка, _res or {}, data, kinds=("note", "sign"))
            содержимое = "".join(str(data.get(k) or "") for k in
                                 ("summary", "skills", "findings", "investigations"))
            data["blank"] = шапка + содержимое + хвост

    def _sub(m):
        key = m.group(1).strip()
        v = data.get(key, "")
        return str(v if v is not None else "")
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", _sub, html)


# ── Общий CSS отчётов (единый визуальный язык для всех кейсов) ─────────────────────────────────────
# Плейсхолдеры, которые готовит web_api._report_context:
#   title, agent, date, verdict, findings_total, investigations_total,
#   by_class (HTML-строка бейджей A/B/C/D), findings (HTML), investigations (HTML), skills (HTML), deliveries (HTML)
def _packaged_css() -> str:
    """Общий CSS отчётов. Источник правды — `reports/base.css` рядом с формами; читаем его один раз.

    Запасной вариант ниже нужен ровно для одного случая: папка `reports/` не доехала в сборку. Он
    намеренно короткий — только токены и типографика, — и это видно по документу: лучше простой, но
    согласованный лист, чем копия большого файла, которая через месяц разойдётся с оригиналом и
    начнёт красить отчёты по-своему.
    """
    try:
        p = REPORTS_DIR / "base.css"
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — нет доступа к файлу: отдадим запасной
        pass
    return (
        ":root{--font:'Noto Sans','Segoe UI','Liberation Sans',Arial,sans-serif;"
        "--mono:'DejaVu Sans Mono',Consolas,monospace;--paper:#fff;--fill:#f6f7fa;--fill-2:#fafbfc;"
        "--ink:#16191f;--ink-2:#454b58;--ink-3:#737a89;--line:#d7dbe3;--line-2:#eceef3;"
        "--accent:#2f4b7c;--bad:#b3261e;--warn:#a2590b;--mid:#8a6d1f;--info:#1f5f8b;--ok:#1a7f4b;"
        "--t-cap:10.5px;--t-sm:12px;--t-base:13.5px;--t-lg:16px;--t-xl:19px;--t-2xl:23px;"
        "--s-1:4px;--s-2:8px;--s-3:12px;--s-4:16px;--s-5:24px}"
        "@page{size:A4;margin:18mm 16mm}"
        "body{margin:0 auto;padding:var(--s-5) var(--s-4);max-width:860px;background:var(--paper);"
        "color:var(--ink);font:var(--t-base)/1.55 var(--font)}"
        "h1{font-size:var(--t-2xl);margin:0 0 var(--s-2)}h2{font-size:var(--t-lg);margin:var(--s-5) 0 var(--s-2)}"
        "h3{font-size:var(--t-base);margin:var(--s-4) 0 var(--s-1)}"
        "table{width:100%;border-collapse:collapse;font-size:var(--t-sm);font-variant-numeric:tabular-nums}"
        "th,td{border:1px solid var(--line);padding:var(--s-1) var(--s-2);text-align:left;vertical-align:top}"
        "th{background:var(--fill);color:var(--ink-2);font-weight:600}"
        ".req,.ahd,.hd{border-bottom:2px solid var(--ink);margin-bottom:var(--s-5)}"
        ".req th,.req td,.svc th,.svc td{border:0}"
        ".line,.meth,.cnt,.foot,.ft{font-size:var(--t-sm);color:var(--ink-2)}"
        ".ac,.fcard,.inv,.vb{border:1px solid var(--line);border-left:3px solid var(--ink-3);"
        "padding:var(--s-3) var(--s-4);margin:var(--s-2) 0}"
        ".b,.cls,.sev,.vb-b{display:inline-block;font-size:var(--t-cap);font-weight:700;color:var(--paper);"
        "background:var(--ink-3);padding:2px var(--s-2)}"
        ".b.A,.cls.A,.sev.hi{background:var(--bad)}.b.B,.cls.B,.sev.mid{background:var(--warn)}"
        ".b.C,.cls.C{background:var(--mid)}.b.D,.cls.D,.sev.low{background:var(--info)}"
        "pre.code,.sk pre{white-space:pre-wrap;background:var(--fill);border:1px solid var(--line);"
        "padding:var(--s-3);font:var(--t-sm)/1.45 var(--mono)}"
        "@media print{thead{display:table-header-group}tr,.ac,.fcard,.inv,.vb{break-inside:avoid}}"
    )


_BASE_CSS = _packaged_css()
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

# Аудитор 1С. Форма отчёта согласована с заказчиком: шапка с объёмом проверки и методом, затем
# находки карточками «что не сходится / откуда / чем грозит / что проверить». Отчёт читает главный
# бухгалтер, а не разработчик: из строки «класс B · проверка» решение принять нельзя.
_AUDIT_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><style>{{css}}</style></head><body>"
    "<div class='ahd'>"
    "<h1>{{title}}</h1>"
    "<div class='line'>Автоматический аудит · агентный процесс ABOP · агент «{{agent}}» · {{date}}</div>"
    "<div class='line'>{{audit_scope}}</div>"
    "<div class='line'>{{audit_found}}</div>"
    "<div class='meth'>Метод: находки вычислены детерминированно по реальным ссылкам 1С; "
    "нормативное обоснование — из базы знаний (НК РФ / ФСБУ). Числа не оцениваются моделью.</div>"
    "</div>"
    "{{summary}}"
    "<h2>Находки по существенности</h2>{{audit_cards}}"
    "{{charts}}"
    "{{schema_notes}}"
    "<h2>Доставка</h2>{{deliveries}}"
    "<div class='ft'>Сформировано автоматически агентным процессом ABOP. Каждая находка сопровождается "
    "ссылкой на первичный документ и подлежит подтверждению ответственным (HITL) перед принятием "
    "решения. Отчёт не заменяет заключение аудитора. · {{agent}} · {{date}}</div>"
    "</body></html>")

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


async def template_for_skills(skills) -> str:
    """Форма, объявившая себя для этих навыков. Пусто — подходящей нет, решает форма результата.

    Если подходят несколько, берём ту, что объявлена для меньшего числа навыков: специальная форма
    точнее общей. При равенстве — по идентификатору, чтобы выбор не зависел от порядка строк в базе.
    """
    want = {str(s) for s in (skills or []) if s}
    if not want:
        return ""
    best = []
    for t in await all():
        # Формы-кэши раскладок (`auto-…`) в подборе не участвуют: они сохранены под конкретное
        # сочетание навыков и спорили бы с бланками вертикалей.
        if str(t.get("id") or "").startswith("auto-"):
            continue
        fs = {str(x) for x in (t.get("for_skills") or [])}
        if fs & want:
            best.append((len(fs), str(t.get("id") or "")))
    return sorted(best)[0][1] if best else ""


def load_files() -> dict[str, dict]:
    """Шаблоны из репо `reports/<id>.html` + `_base.css` + `index.json` (имена/pdf_options). Пусто — нет папки."""
    out: dict[str, dict] = {}
    if not REPORTS_DIR.is_dir():
        return out
    css = (REPORTS_DIR / "base.css").read_text(encoding="utf-8") if (REPORTS_DIR / "base.css").is_file() else _BASE_CSS
    meta = {}
    if (REPORTS_DIR / "index.json").is_file():
        try:
            meta = json.loads((REPORTS_DIR / "index.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    for p in sorted(REPORTS_DIR.glob("*.html")):
        tid = p.stem
        if tid.startswith("_") or tid == "base":
            continue
        m = meta.get(tid) or {}
        out[tid] = {"name": m.get("name") or tid, "html": p.read_text(encoding="utf-8"), "css": css,
                    "pdf_options": m.get("pdf_options") or {"format": "A4", "orientation": "portrait"},
                    "for_skills": m.get("for_skills") or [], "layout": m.get("layout") or []}
    return out


async def seed_if_empty() -> None:
    """Досев/освежение встроенных шаблонов: из репо `reports/` (источник по умолчанию), иначе из кода.
    builtin-шаблоны (editor='seed') обновляем при старте, если содержимое изменилось; пользовательские
    правки (editor≠'seed' / builtin=false) не трогаем — в рантайме источник правды БД."""
    try:
        cur = {t["id"]: t for t in await all()}
        files = load_files()
        seeds = ({tid: files[tid] for tid in files} if files else
                 {tid: {"name": name, "html": html, "css": _BASE_CSS, "pdf_options": {"format": "A4", "orientation": "portrait"}}
                  for tid, name, html in _SEED})
        for tid, spec in seeds.items():
            ex = cur.get(tid)
            if ex and not (ex.get("builtin") and (ex.get("editor") in (None, "seed"))):
                continue             # шаблон отредактирован пользователем — не перезатираем
            if (ex and ex.get("html") == spec["html"] and ex.get("css") == spec["css"]
                    and ex.get("name") == spec["name"]
                    and list(ex.get("for_skills") or []) == list(spec.get("for_skills") or [])
                    and list(ex.get("layout") or []) == list(spec.get("layout") or [])):
                continue
            await save(tid, spec, editor="seed", builtin=True)
        # Форма, убранная из поставки (вертикаль разделилась на виды документов), должна уйти и из
        # базы: иначе она остаётся в списке и её можно поставить OUT-узлу — а бланка под ней уже нет.
        # Трогаем только посевные записи: пользовательские формы не наши.
        if files:
            for tid, ex in cur.items():
                if tid in seeds or not ex.get("builtin") or ex.get("editor") not in (None, "seed"):
                    continue
                await _drop_seed(tid)
    except Exception:  # noqa: BLE001
        pass


async def _drop_seed(tid: str) -> None:
    """Снять посевную форму, которой больше нет в поставке. `delete()` для встроенных закрыт
    намеренно (чтобы её не снёс пользователь), поэтому посев убирает свои записи сам."""
    if not _has_pg():
        _MEM.pop(tid, None)
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM report_templates WHERE id=%s AND builtin=true", (tid,))


# Поля-заголовки карточки: чем назвать находку, если навык не дал явного заголовка.
_TITLE_KEYS = ("заголовок", "название", "тема", "истори", "сценарий", "вопрос", "роль", "симптом", "id")
# Короткие пометки, которые выносим бейджами в шапку карточки, а не абзацем.
_BADGE_KEYS = ("id", "класс", "ранг", "статус", "критичность", "приоритет", "уверенность", "сумма",
               "существенность", "частота", "ценность", "срочность")
_NORM_KEYS = ("норма", "основание", "статья")


def label(key) -> str:
    """Имя поля человеку: «часы_план_факт» → «часы план факт». В отчёте имя колонки базы неуместно."""
    return str(key if key is not None else "").replace("_", " ").strip()


def is_number(x) -> bool:
    """Значение — число? Нужно для выключки вправо: столбец цифр читается только так."""
    if isinstance(x, bool):
        return False
    if isinstance(x, (int, float)):
        return True
    t = str(x or "").strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def kpi_html(d: dict, esc) -> str:
    """Сводка числами — плашками. Эти цифры переносят в отчёт выше по иерархии, им нужен размер."""
    cells = "".join(f"<div class='kpi'><span class='k'>{esc(label(k))}</span>"
                    f"<span class='v'>{esc(v)}</span></div>" for k, v in d.items())
    return f"<div class='kpis'>{cells}</div>"


def _wordy(items: list, cols: list) -> bool:
    """Список объектов «многословный»? Тогда таблица нечитаема: длинные пояснения схлопываются
    в ячейки и отчёт выглядит поверхностным, хотя данные на месте.

    Считаем ПО ГРАФАМ, а не по отдельным значениям. Таблицу ломает не доля длинных строк вообще, а
    конкретная графа с абзацем текста: она растягивает строку на пол-страницы, и остальные графы
    превращаются в узкие колонки по два слова. Зато реестр чисел — это таблица, сколько бы граф в нём
    ни было: пока действовало правило «граф больше пяти → карточки», три периода P&L по одиннадцать
    числовых граф печатались тремя карточками по одиннадцать строк, и сравнить периоды — то, ради
    чего эти числа и считали, — было невозможно.
    """
    if not items:
        return False
    длина: dict = {}
    вложенные = set()
    числовых = всего = 0
    for it in items[:20]:
        for k, v in it.items():
            if isinstance(v, (dict, list)):
                if v:
                    вложенные.add(k)
                continue
            всего += 1
            if is_number(v):
                числовых += 1
            длина.setdefault(k, []).append(len(str(v if v is not None else "")))
    if вложенные:
        return True                                   # вложенная структура в ячейку не помещается
    средние = [sum(v) / len(v) for v in длина.values() if v]
    if not средние:
        return False
    if max(средние) > 90:
        return True                                   # есть графа-абзац — только карточки
    if sum(1 for a in средние if a > 55) >= 2:
        return True                                   # две графы с пояснениями — тоже
    if всего and числовых / всего >= 0.5:
        return False                                  # реестр чисел — всегда таблица
    return len(cols) > 8                              # очень широкий текстовый реестр


def _card_html(it: dict, esc) -> str:
    """Одна находка карточкой: заголовок, бейджи, норма, затем поля абзацами."""
    def pick(keys):
        # порядок важен: «заголовок» должен побеждать «id», иначе карточка называется «A»
        for p in keys:
            for k in it:
                if p in str(k).lower() and it[k] not in (None, "", [], {}):
                    return k
        return None

    used = set()
    tk = pick(_TITLE_KEYS)
    head = esc(it.get(tk)) if tk else ""
    if tk:
        used.add(tk)
    badges = []
    for k in list(it):
        kl = str(k).lower()
        v = it[k]
        if k in used or isinstance(v, (dict, list)) or v in (None, "", [], {}):
            continue
        if any(p in kl for p in _BADGE_KEYS) and len(str(v)) <= 40:
            badges.append(f"<span class='cb'>{esc(label(k))}: {esc(v)}</span>")
            used.add(k)
    nk = pick(_NORM_KEYS)
    norm = ""
    if nk and nk not in used:
        nv = it[nk]
        # норма бывает объектом {статья, цитата, источник}: без разбора в отчёт уезжал питоновский
        # словарь с кавычками — именно это читалось как «сырьё», а не как ссылка на норму
        if isinstance(nv, dict):
            _st = " · ".join(str(nv.get(k2)) for k2 in ("статья", "источник") if nv.get(k2))
            _ct = str(nv.get("цитата") or "")
            norm = f"<div class='cnorm'>{esc(_st or nv)}</div>" + (f"<div class='cf'>{esc(_ct)}</div>" if _ct else "")
        elif isinstance(nv, list):
            norm = "<div class='cnorm'>" + esc(" · ".join(str(x) for x in nv if not isinstance(x, (dict, list)))) + "</div>"
        else:
            norm = f"<div class='cnorm'>{esc(nv)}</div>"
        used.add(nk)
    body = []
    for k, v in it.items():
        if k in used or v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            body.append(f"<div class='cf'><b>{esc(label(k))}</b>{struct_html(v, 2)}</div>")
        else:
            body.append(f"<div class='cf'><b>{esc(label(k))}:</b> {esc(v)}</div>")
    return ("<div class='fcard'>"
            + (f"<div class='ch'>{head}</div>" if head else "")
            + (f"<div class='cbs'>{''.join(badges)}</div>" if badges else "")
            + norm + "".join(body) + "</div>")


def struct_html(obj, depth: int = 0) -> str:
    """Структурированный ответ навыка (по шаблону извлечения) → HTML: объект — строки «ключ: значение»,
    список объектов — таблица (колонки = скалярные поля), список строк — маркированный список. Экранируется всё."""
    esc = lambda x: _html.escape(str(x if x is not None else ""))  # noqa: E731
    if obj in (None, "", [], {}):
        return ""
    if isinstance(obj, dict):
        if depth == 0 and obj.get("_truncated"):
            obj = {k: v for k, v in obj.items() if k != "_truncated"}
        plain = {k: v for k, v in obj.items() if not isinstance(v, (dict, list)) and v not in (None, "")}
        nums = [k for k, v in plain.items() if is_number(v)]
        rows = []
        # Сводка из цифр — плашками: эти значения переносят в отчёт выше по иерархии, и строкой
        # «ключ: значение» они теряются среди пояснений.
        if depth <= 1 and len(plain) >= 3 and len(nums) >= len(plain) - 1:
            rows.append(kpi_html(plain, esc))
            plain = {}
        for k, v in obj.items():
            if v in (None, "", [], {}):
                continue
            if isinstance(v, (dict, list)):
                rows.append(f"<div class='kv'><b>{esc(label(k))}</b>{struct_html(v, depth + 1)}</div>")
            elif k in plain:
                cls = "lead" if depth == 0 and isinstance(v, str) and len(v) > 120 else "kv"
                rows.append(f"<div class='{cls}'>"
                            + (f"<b>{esc(label(k))}:</b> " if cls == "kv" else f"<b>{esc(label(k))}.</b> ")
                            + esc(v) + "</div>")
        return "".join(rows)
    if isinstance(obj, list):
        import builtins
        if obj and builtins.all(isinstance(x, dict) for x in obj):   # модульная all() — список шаблонов
            cols: list[str] = []
            for it in obj:
                for k, v in it.items():
                    if k not in cols and not isinstance(v, (dict, list)):
                        cols.append(k)
            if _wordy(obj, cols):
                return "".join(_card_html(it, esc) for it in obj[:60])
            # Отрезанная графа — потерянные данные, поэтому предел шире, а об отрезанном сказано
            # прямо под таблицей: читатель должен знать, что видит не всё.
            _all_cols = len(cols)
            cols = cols[:12]
            # Числовая колонка выключается вправо: иначе столбец цифр не читается, а именно по нему
            # отчёт и просматривают — «где просрочка», «где перерасход».
            # builtins.all — в модуле есть своя all() (список шаблонов), она затеняет встроенную
            num = {c for c in cols
                   if builtins.all(is_number(it.get(c)) for it in obj if str(it.get(c, "")).strip() != "")
                   and builtins.any(str(it.get(c, "")).strip() != "" for it in obj)}
            head = "".join(f"<th{' class=n' if c in num else ''}>{esc(label(c))}</th>" for c in cols)
            body = []
            for it in obj[:60]:
                tds = "".join(f"<td{' class=n' if c in num else ''}>{esc(it.get(c))}</td>" for c in cols)
                nested = "".join(f"<div class='kv'><b>{esc(label(k))}</b>{struct_html(v, depth + 1)}</div>"
                                 for k, v in it.items() if isinstance(v, (dict, list)) and v)
                body.append(f"<tr>{tds}</tr>" + (f"<tr><td colspan='{len(cols)}'>{nested}</td></tr>" if nested else ""))
            more = f" · показаны первые 60 из {len(obj)}" if len(obj) > 60 else ""
            more += (f" · граф {_all_cols}, показаны первые {len(cols)}" if _all_cols > len(cols) else "")
            широкая = " wide" if len(cols) >= 7 else ""
            return (f"<table class='tbl{широкая}'><thead><tr>{head}</tr></thead>"
                    f"<tbody>{''.join(body)}</tbody></table>"
                    f"<div class='cnt'>строк: {len(obj)}{more}</div>")
        return "<ul>" + "".join(f"<li>{struct_html(x, depth + 1) if isinstance(x, (dict, list)) else esc(x)}</li>" for x in obj[:80]) + "</ul>"
    return esc(obj)
