"""Whitelisted-расчёты над Data Plane (безопасный compute-tool). Философия ABOP: «код считает истину,
LLM объясняет» — набор ФИКСИРОВАННЫХ операций (не произвольный код, никакого exec/eval), читает только
через ape.data_query (read-only, ABAC уже на слое данных). Результат детерминирован и сразу ложится в
находки и график (server.charts).

Операции (op):
  stats      — {count,sum,avg,min,max} по числовому полю
  group_by   — сводка (pivot): сумма/кол-во поля по ключу → [{key,value}] (+ bar-график)
  top        — топ-N записей по числовому полю
  reconcile  — сверка двух выборок по ключу: сумма поля слева vs справа, расхождение Δ по ключу (+ график)

Спека: {op, entity, field?, key?, agg?('sum'|'count'), filters?, limit?,
        right_entity?, right_filters?, title?}
Безопасность: entity ∈ canonical-схемы; field/key — ИМЕНА полей (строки), не выражения. egress=internal.
"""
from __future__ import annotations

import re

import ape  # ядро Data Plane (в контейнере /app/cli в PYTHONPATH); сервер инжектит сторы через set_recipe_store

from . import charts

_OPS = ("stats", "group_by", "top", "reconcile")
_MONEY_RE = re.compile(r"[^\d.,\-]")


def _num(v):
    """Число из значения (в т.ч. «1 200,50 ₽» → 1200.5). Нечисло → None (запись не учитывается в агрегате)."""
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return None
    s = _MONEY_RE.sub("", str(v)).replace(" ", "").replace(",", ".")
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _get(rec, path):
    """Значение по dotted-path (Проводки.0.Сумма) в канонической записи."""
    cur = rec
    for part in str(path).split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _rows(entity, filters, limit):
    if entity not in ape.CANONICAL_SCHEMAS:
        raise ValueError(f"неизвестная сущность {entity!r} (нет в canonical-схемах)")
    return ape.data_query(entity, filters=filters or None, limit=int(limit or 100000))


def _stats(rows, field):
    vals = [n for n in (_num(_get(r, field)) for r in rows) if n is not None]
    if not vals:
        return {"count": 0, "sum": 0, "avg": 0, "min": 0, "max": 0}
    return {"count": len(vals), "sum": round(sum(vals), 2), "avg": round(sum(vals) / len(vals), 2),
            "min": min(vals), "max": max(vals)}


def _group(rows, key, field, agg):
    acc: dict[str, float] = {}
    for r in rows:
        k = _get(r, key)
        k = "—" if k in (None, "") else str(k)
        if agg == "count":
            acc[k] = acc.get(k, 0) + 1
        else:
            n = _num(_get(r, field))
            if n is not None:
                acc[k] = acc.get(k, 0) + n
    items = sorted(({"key": k, "value": round(v, 2)} for k, v in acc.items()), key=lambda x: -x["value"])
    return items


def run(spec: dict) -> dict:
    """Выполнить whitelisted-расчёт. Возвращает {op, result, chart?} — chart в формате server.charts."""
    spec = spec or {}
    op = str(spec.get("op", "")).lower()
    if op not in _OPS:
        raise ValueError(f"операция {op!r} не в белом списке: {', '.join(_OPS)}")
    entity = str(spec.get("entity", "")).strip()
    field = spec.get("field")
    key = spec.get("key")
    limit = spec.get("limit", 100000)
    rows = _rows(entity, spec.get("filters"), limit)

    if op == "stats":
        if not field:
            raise ValueError("stats: нужно поле (field)")
        return {"op": op, "entity": entity, "field": field, "result": _stats(rows, field)}

    if op == "top":
        if not field:
            raise ValueError("top: нужно поле (field)")
        n = int(spec.get("limit_top", spec.get("n", 10)) or 10)
        scored = [(r, _num(_get(r, field))) for r in rows]
        scored = [(r, v) for r, v in scored if v is not None]
        scored.sort(key=lambda x: -x[1])
        top = [{"id": r.get("id"), "value": v, **{k: _get(r, k) for k in ([key] if key else [])}} for r, v in scored[:n]]
        chart = {"type": "bar", "title": spec.get("title") or f"Топ-{n} по {field}",
                 "x": [str(t.get(key) if key else t.get("id"))[:16] for t in top],
                 "series": [{"name": field, "data": [t["value"] for t in top]}]}
        return {"op": op, "entity": entity, "field": field, "result": top, "chart": chart}

    if op == "group_by":
        if not key:
            raise ValueError("group_by: нужен ключ (key)")
        agg = "count" if str(spec.get("agg", "sum")).lower() == "count" else "sum"
        if agg == "sum" and not field:
            raise ValueError("group_by agg=sum: нужно поле (field)")
        items = _group(rows, key, field, agg)
        top = items[:20]
        chart = {"type": "bar", "title": spec.get("title") or (f"{'Кол-во' if agg == 'count' else 'Сумма ' + str(field)} по {key}"),
                 "x": [i["key"][:16] for i in top], "series": [{"name": agg, "data": [i["value"] for i in top]}]}
        return {"op": op, "entity": entity, "key": key, "field": field, "agg": agg,
                "result": items, "total": round(sum(i["value"] for i in items), 2), "chart": chart}

    # reconcile: сверка двух выборок по ключу (сумма поля слева vs справа) → расхождения Δ
    if op == "reconcile":
        if not (key and field):
            raise ValueError("reconcile: нужны key и field")
        right_entity = str(spec.get("right_entity") or entity).strip()
        right_rows = _rows(right_entity, spec.get("right_filters"), limit)
        left = {i["key"]: i["value"] for i in _group(rows, key, field, "sum")}
        right = {i["key"]: i["value"] for i in _group(right_rows, key, field, "sum")}
        keys = sorted(set(left) | set(right))
        breaks = []
        for k in keys:
            lv, rv = left.get(k, 0.0), right.get(k, 0.0)
            d = round(lv - rv, 2)
            if abs(d) > 0.005:
                breaks.append({"key": k, "left": round(lv, 2), "right": round(rv, 2), "delta": d})
        breaks.sort(key=lambda x: -abs(x["delta"]))
        res = {"matched": sum(1 for k in keys if abs(left.get(k, 0) - right.get(k, 0)) <= 0.005),
               "breaks": breaks, "total_delta": round(sum(b["delta"] for b in breaks), 2),
               "only_left": [k for k in left if k not in right], "only_right": [k for k in right if k not in left]}
        top = breaks[:15]
        chart = {"type": "bar", "title": spec.get("title") or f"Расхождения Δ {field} по {key}",
                 "x": [str(b["key"])[:16] for b in top], "series": [{"name": "Δ", "data": [b["delta"] for b in top]}]}
        return {"op": op, "entity": entity, "right_entity": right_entity, "key": key, "field": field,
                "result": res, "chart": chart}

    raise ValueError(f"операция {op!r} не реализована")


def run_html(spec: dict) -> dict:
    """run() + отрендеренный SVG графика (для быстрого показа в UI/отчёте)."""
    out = run(spec)
    if out.get("chart"):
        out["chart_svg"] = charts.render_spec(out["chart"])
    return out
