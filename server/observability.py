"""Observability spine (Фаза 0 «большого скачка»): trace_id, структурные логи и лёгкие метрики
БЕЗ внешних зависимостей (prometheus_client не тянем — реестр in-process, hand-rolled).

Фундамент ПЕРЕД async-исполнением/мультинодами: нельзя масштабировать то, что не видно.
Даёт сквозной trace_id (запрос→прогон→доставка), тайминги, счётчики прогонов/находок/стоимости/
доставки и эндпоинт /metrics (формат Prometheus). См. docs/CONCEPT_SCALING_OBSERVABILITY.md §12а.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from collections import defaultdict

from .config import settings

# ── сквозной идентификатор трассировки (контекст запроса) ──
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    return trace_id_var.get()


# ── лёгкий реестр метрик (in-process; переживёт до Prometheus-экспорта) ──
_counters: dict = defaultdict(float)
_hist: dict = defaultdict(lambda: {"count": 0, "sum": 0.0, "buckets": defaultdict(int)})
_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300]  # секунды


def inc(name: str, val: float = 1.0, **labels) -> None:
    _counters[(name, tuple(sorted((k, str(v)) for k, v in labels.items())))] += val


_gauges: dict = {}


def gauge(name: str, val: float, **labels) -> None:
    """Текущее значение (глубина очереди, RSS, число воркеров) — перезаписывается, не суммируется."""
    _gauges[(name, tuple(sorted((k, str(v)) for k, v in labels.items())))] = float(val)


def observe(name: str, val: float, **labels) -> None:
    h = _hist[(name, tuple(sorted((k, str(v)) for k, v in labels.items())))]
    h["count"] += 1
    h["sum"] += val
    for b in _BUCKETS:
        if val <= b:
            h["buckets"][b] += 1
    h["buckets"]["+Inf"] += 1


def _fmt_labels(labels) -> str:
    if not labels:
        return ""
    return "{" + ",".join('%s="%s"' % (k, v) for k, v in labels) + "}"


def render_prometheus() -> str:
    """Метрики в текстовом формате Prometheus (для scrape)."""
    lines: list[str] = []
    typed: set[str] = set()
    for (name, labels), val in sorted(_counters.items(), key=lambda x: x[0][0]):
        if name not in typed:
            lines.append(f"# TYPE {name} counter")
            typed.add(name)
        lines.append(f"{name}{_fmt_labels(labels)} {val}")
    for (name, labels), val in sorted(_gauges.items(), key=lambda x: x[0][0]):
        if name not in typed:
            lines.append(f"# TYPE {name} gauge")
            typed.add(name)
        lines.append(f"{name}{_fmt_labels(labels)} {val}")
    for (name, labels), h in sorted(_hist.items(), key=lambda x: x[0][0]):
        if name not in typed:
            lines.append(f"# TYPE {name} histogram")
            typed.add(name)
        base = dict(labels)
        for b in _BUCKETS + ["+Inf"]:
            bl = _fmt_labels(tuple(sorted({**base, "le": str(b)}.items())))
            lines.append(f"{name}_bucket{bl} {h['buckets'].get(b, 0)}")
        lines.append(f"{name}_count{_fmt_labels(labels)} {h['count']}")
        lines.append(f"{name}_sum{_fmt_labels(labels)} {round(h['sum'], 4)}")
    return "\n".join(lines) + "\n"


def snapshot() -> dict:
    """JSON-срез метрик (для /api/observability и дашбордов внутри ABOP)."""
    return {
        "counters": [{"name": n, "labels": dict(l), "value": v} for (n, l), v in _counters.items()],
        "gauges": [{"name": n, "labels": dict(l), "value": v} for (n, l), v in _gauges.items()],
        "histograms": [{"name": n, "labels": dict(l), "count": h["count"], "sum": round(h["sum"], 4)}
                       for (n, l), h in _hist.items()],
    }


# ── структурный лог (одна JSON-строка на событие, с trace_id) ──
# Свой handler на stdout — чтобы события были видны в `docker logs` независимо от конфигурации uvicorn
# (logging.basicConfig под uvicorn не вызывается → без этого INFO-события ABOP не печатались).
_log = logging.getLogger("abop")
if not _log.handlers:
    import sys as _sys
    _h = logging.StreamHandler(_sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_h)
    _log.setLevel(getattr(logging, str(settings.log_level).upper(), logging.INFO))
    _log.propagate = False


def log_event(level: str, event: str, **fields) -> None:
    rec = {"ts": round(time.time(), 3), "trace_id": current_trace_id(), "event": event}
    rec.update({k: v for k, v in fields.items() if v is not None})
    _log.log(getattr(logging, level.upper(), logging.INFO), json.dumps(rec, ensure_ascii=False))
