"""Рендер графиков из ДЕКЛАРАТИВНОЙ спеки в inline-SVG (без внешних зависимостей — matplotlib не нужен).
Философия ABOP «код считает истину, LLM объясняет»: числа для графика берутся из результата прогона
(детерминированно), спека лишь задаёт вид. SVG встраивается прямо в HTML-отчёт и корректно рендерится
и в браузере, и в PDF (Gotenberg/Chromium).

Спека графика (chart_spec):
  {"type": "bar"|"line"|"pie", "title": str,
   "x": [подписи], "series": [{"name": str, "data": [числа], "color"?: str}],
   "unit"?: str}

Публичное:
  render_spec(spec) -> str        # <figure>…<svg>…</svg></figure> (или "" при некорректной спеке)
  auto_specs(result) -> [spec]    # детерминированные графики из результата прогона (находки/расследования/задачи)
  charts_html(result) -> str      # готовый HTML-блок всех графиков (auto + эмитированные навыком)
"""
from __future__ import annotations

import html as _html

# Палитра и гарнитура — те же, что у документа (reports/base.css, блок :root). График внутри отчёта
# не самостоятельная картинка: пока он жил своей жизнью (насыщенные индиго и алый, скруглённые
# столбцы в пол-страницы, заголовок кеглем крупнее заголовка раздела), он ломал документ сильнее,
# чем помогал его понять.
_INK, _INK_2, _INK_3, _LINE = "#16191f", "#454b58", "#737a89", "#d7dbe3"
_FONT = "'Noto Sans','Segoe UI','Liberation Sans',Arial,sans-serif"
# Серии общего назначения: акцент документа и приглушённые соседи. Красный в этом ряду последний —
# в документе он означает «критично», и тратить его на третью серию нельзя.
_PALETTE = ["#2f4b7c", "#5a7ca8", "#1a7f4b", "#a2590b", "#8a6d1f", "#1f5f8b", "#b3261e", "#737a89"]
# Классы существенности — ровно те же цвета, что у бейджей .b.A–.b.D и карточек находок.
_CLASS_COLOR = {"A": "#b3261e", "B": "#a2590b", "C": "#8a6d1f", "D": "#1f5f8b"}
_W, _H = 760, 190                       # холст: широкий и низкий — по пропорции полосы набора
_ML, _MR, _MT, _MB = 40, 12, 24, 28     # поля (место под подписи и заголовок)
_HEADROOM = 1.14                        # запас над самым высоким столбцом: иначе подпись значения
                                        # налезает на заголовок графика


def _esc(x) -> str:
    return _html.escape(str(x if x is not None else ""))


def _nums(data) -> list[float]:
    out = []
    for v in data or []:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _fmt(v: float) -> str:
    if v == int(v):
        return f"{int(v):,}".replace(",", " ")
    return f"{v:,.1f}".replace(",", " ")


def _wrap(title: str, body: str) -> str:
    """Заголовок графика — слева и кеглем подписи: это название таблицы, а не заголовок раздела.
    Отступы задаёт CSS документа (figure), а не инлайн-стиль: иначе график живёт по своим правилам."""
    t = (f"<text x='0' y='11' font-size='12' font-weight='600' fill='{_INK_2}'>{_esc(title)}</text>"
         if title else "")
    return (f"<figure><svg width='100%' viewBox='0 0 {_W} {_H}' "
            f"xmlns='http://www.w3.org/2000/svg' font-family=\"{_FONT}\">"
            f"{t}{body}</svg></figure>")


def _bar(spec: dict) -> str:
    xs = [str(x) for x in (spec.get("x") or [])]
    series = spec.get("series") or []
    if not xs or not series:
        return ""
    vals = _nums(series[0].get("data"))
    n = min(len(xs), len(vals))
    xs, vals = xs[:n], vals[:n]
    vmax = max(vals + [1]) * _HEADROOM
    plot_w, plot_h = _W - _ML - _MR, _H - _MT - _MB
    step = plot_w / n
    bw = min(step * 0.52, 46)           # столбец уже промежутка: ряд читается как ряд, а не как стена
    base_y = _MT + plot_h
    parts = [f"<line x1='{_ML}' y1='{base_y}' x2='{_W - _MR}' y2='{base_y}' stroke='{_LINE}'/>"]
    per_color = spec.get("color")
    for i, (lab, v) in enumerate(zip(xs, vals)):
        h = (v / vmax) * plot_h if vmax else 0
        cx = _ML + step * i + step / 2
        x = cx - bw / 2
        y = base_y - h
        color = _CLASS_COLOR.get(lab.strip().upper()) or per_color or _PALETTE[i % len(_PALETTE)]
        parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{h:.1f}' fill='{color}'/>")
        parts.append(f"<text x='{cx:.1f}' y='{y - 5:.1f}' text-anchor='middle' font-size='10.5' "
                     f"font-weight='600' fill='{_INK}'>{_esc(_fmt(v))}</text>")
        parts.append(f"<text x='{cx:.1f}' y='{base_y + 13:.1f}' text-anchor='middle' font-size='10.5' "
                     f"fill='{_INK_3}'>{_esc(lab[:16])}</text>")
    return _wrap(spec.get("title") or "", "".join(parts))


def _line(spec: dict) -> str:
    xs = [str(x) for x in (spec.get("x") or [])]
    series = spec.get("series") or []
    if not xs or not series:
        return ""
    plot_w, plot_h = _W - _ML - _MR, _H - _MT - _MB
    base_y = _MT + plot_h
    allv = [v for s in series for v in _nums(s.get("data"))]
    vmax = max(allv + [1]) * _HEADROOM
    parts = [f"<line x1='{_ML}' y1='{base_y}' x2='{_W - _MR}' y2='{base_y}' stroke='{_LINE}'/>"]
    n = len(xs)
    xstep = plot_w / max(n - 1, 1)
    for si, s in enumerate(series):
        vals = _nums(s.get("data"))[:n]
        color = s.get("color") or _PALETTE[si % len(_PALETTE)]
        pts = []
        for i, v in enumerate(vals):
            px = _ML + xstep * i
            py = base_y - (v / vmax) * plot_h if vmax else base_y
            pts.append(f"{px:.1f},{py:.1f}")
        if pts:
            parts.append(f"<polyline fill='none' stroke='{color}' stroke-width='1.8' points='{' '.join(pts)}'/>")
            for p in pts:
                x, y = p.split(",")
                parts.append(f"<circle cx='{x}' cy='{y}' r='2.4' fill='{color}'/>")
    for i, lab in enumerate(xs):
        px = _ML + xstep * i
        parts.append(f"<text x='{px:.1f}' y='{base_y + 13:.1f}' text-anchor='middle' font-size='10.5' "
                     f"fill='{_INK_3}'>{_esc(lab[:12])}</text>")
    return _wrap(spec.get("title") or "", "".join(parts))


def _pie(spec: dict) -> str:
    import math
    xs = [str(x) for x in (spec.get("x") or [])]
    series = spec.get("series") or []
    if not xs or not series:
        return ""
    vals = _nums(series[0].get("data"))[:len(xs)]
    total = sum(vals)
    if total <= 0:
        return ""
    cx, cy, r = 86, _MT + (_H - _MT - _MB) / 2, 62
    parts, legend = [], []
    ang = -90.0
    for i, (lab, v) in enumerate(zip(xs, vals)):
        frac = v / total
        color = _CLASS_COLOR.get(lab.strip().upper()) or _PALETTE[i % len(_PALETTE)]
        a2 = ang + frac * 360
        large = 1 if frac > 0.5 else 0
        x1 = cx + r * math.cos(math.radians(ang)); y1 = cy + r * math.sin(math.radians(ang))
        x2 = cx + r * math.cos(math.radians(a2)); y2 = cy + r * math.sin(math.radians(a2))
        if frac >= 0.999:   # единственный сектор — полный круг (path на 360° вырождается)
            parts.append(f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='{color}'/>")
        else:
            parts.append(f"<path d='M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} Z' fill='{color}'/>")
        ly = _MT + 4 + i * 19
        legend.append(f"<rect x='190' y='{ly}' width='10' height='10' fill='{color}'/>"
                      f"<text x='206' y='{ly + 9}' font-size='11' fill='{_INK_2}'>"
                      f"{_esc(lab[:28])} — {_esc(_fmt(v))} ({frac * 100:.0f}%)</text>")
        ang = a2
    return _wrap(spec.get("title") or "", "".join(parts) + "".join(legend))


_RENDERERS = {"bar": _bar, "line": _line, "pie": _pie, "donut": _pie}


def render_spec(spec: dict) -> str:
    """Спека → inline-SVG. Некорректная/пустая спека → "" (тихо, отчёт не падает)."""
    if not isinstance(spec, dict):
        return ""
    fn = _RENDERERS.get(str(spec.get("type", "bar")).lower())
    if not fn:
        return ""
    try:
        return fn(spec)
    except Exception:  # noqa: BLE001 — график не критичен для отчёта
        return ""


def auto_specs(result: dict) -> list[dict]:
    """Детерминированные графики из результата прогона (числа — из находок/расследований/задач, не из LLM)."""
    specs: list[dict] = []
    bc = (result.get("findings_summary") or {}).get("by_class") or {}
    if bc and sum(int(bc.get(k, 0) or 0) for k in ("A", "B", "C", "D")):
        specs.append({"type": "bar", "title": "Находки по классам критичности",
                      "x": ["A", "B", "C", "D"], "series": [{"name": "шт", "data": [bc.get(k, 0) for k in ("A", "B", "C", "D")]}]})
    invs = result.get("investigations") or []
    deltas = [(iv.get("id") or f"INV-{i+1}", abs(float((iv.get("сверка") or {}).get("разница_₽") or 0)))
              for i, iv in enumerate(invs) if isinstance(iv, dict)]
    deltas = [d for d in deltas if d[1] > 0][:8]
    # Один столбец на весь холст — не график, а число, которому зачем-то нарисовали прямоугольник.
    # Сравнивать там не с чем, а место он занимает как полноценная диаграмма. Само число и так стоит
    # в карточке расследования.
    if len(deltas) >= 2:
        specs.append({"type": "bar", "title": "Расхождения по расследованиям, ₽", "unit": "₽",
                      "x": [d[0] for d in deltas], "series": [{"name": "₽", "data": [d[1] for d in deltas]}]})
    # Задачи по приоритету (structured-вывод навыка вроде mail-triage)
    prio: dict[str, int] = {}
    for f in result.get("findings") or []:
        if not (isinstance(f, dict) and f.get("structured")):
            continue
        st = f["structured"]
        items = None
        if isinstance(st, dict):
            for v in st.values():
                if isinstance(v, list) and v:
                    items = v; break
        elif isinstance(st, list):
            items = st
        for it in (items or []):
            if isinstance(it, dict):
                p = str(it.get("приоритет") or it.get("priority") or "").strip().lower() or "без приоритета"
                prio[p] = prio.get(p, 0) + 1
    if len(prio) >= 2 or (prio and sum(prio.values()) >= 3):
        order = [k for k in ("высокий", "средний", "низкий", "без приоритета") if k in prio] + \
                [k for k in prio if k not in ("высокий", "средний", "низкий", "без приоритета")]
        specs.append({"type": "pie", "title": "Задачи по приоритету",
                      "x": order, "series": [{"name": "шт", "data": [prio[k] for k in order]}]})
    return specs


def _emitted_specs(result: dict) -> list[dict]:
    """Графики, ЭМИТИРОВАННЫЕ навыком: f.structured.charts / f.structured.chart (LLM предлагает ВИД,
    но данные должны приходить из грунтованного результата — ответственность автора навыка)."""
    out = []
    for f in result.get("findings") or []:
        if not (isinstance(f, dict) and isinstance(f.get("structured"), dict)):
            continue
        st = f["structured"]
        if isinstance(st.get("chart"), dict):
            out.append(st["chart"])
        for c in (st.get("charts") or []):
            if isinstance(c, dict):
                out.append(c)
    return out


def charts_html(result: dict) -> str:
    """Готовый HTML-блок всех графиков прогона (детерминированные + эмитированные навыком)."""
    specs = auto_specs(result) + _emitted_specs(result)
    return "".join(render_spec(s) for s in specs)
