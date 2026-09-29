"""Модуль «Прогоны» — журнал работы агентов: список, карточка целиком, сравнение, отчёт.

До него история прогонов в десктопе жила только карточками внутри чатов: удалил чат — потерял
прогоны, а оборвавшийся опрос уносил результат навсегда. Здесь — прямой журнал с сервера ABOP
(источник истины — Postgres), поиск и фильтры считаются на сервере, а не в интерфейсе.
"""
from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ... import abop_client as abop

MANIFEST = {"id": "runs", "title": "Прогоны", "icon": "runs", "ui": "runs", "order": 25}
router = APIRouter()


def _err(e: "abop.AbopError") -> JSONResponse:
    return JSONResponse({"ok": False, "error": e.detail, "status": e.status},
                        status_code=e.status if 400 <= e.status < 600 else 502)


def _matches(run: dict, q: str) -> bool:
    """Поиск по журналу: имя агента, идентификатор прогона, семья, роль, вердикт."""
    if not q:
        return True
    hay = " ".join(str(run.get(k) or "") for k in
                   ("id", "agent_id", "agent_name", "family", "role", "started_by")).lower()
    ok = run.get("verdict_ok")
    hay += " пройден" if ok else " замечания"
    return all(w in hay for w in q.lower().split())


@router.get("/list")
def runs_list(q: str = "", agent_id: str = "", verdict: str = "", days: int = 0, limit: int = 200):
    """Журнал прогонов с фильтрами. verdict: ok | issues | пусто; days: за сколько последних суток."""
    try:
        items = abop.runs(agent_id)
    except abop.AbopError as e:
        return _err(e)
    out = []
    for r in items:
        if verdict == "ok" and not r.get("verdict_ok"):
            continue
        if verdict == "issues" and r.get("verdict_ok"):
            continue
        if not _matches(r, q):
            continue
        out.append(r)
    if days:
        import datetime as _dt
        edge = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(days))).isoformat()
        out = [r for r in out if str(r.get("created_at") or "") >= edge]
    agents = sorted({(r.get("agent_id"), r.get("agent_name") or r.get("agent_id")) for r in items})
    return {"runs": out[:max(1, min(500, int(limit or 200)))], "total": len(items),
            "agents": [{"id": a, "name": n} for a, n in agents if a]}


@router.get("/item/{run_id:path}")
def run_item(run_id: str):
    """Полная запись прогона: находки целиком, доставка, вердикт, метрики."""
    try:
        return abop.run_full(run_id)
    except abop.AbopError as e:
        return _err(e)


@router.get("/diff/{run_id:path}")
def run_diff(run_id: str):
    """Что изменилось против предыдущего прогона того же агента."""
    try:
        return abop.run_diff(run_id)
    except abop.AbopError as e:
        return _err(e)


@router.get("/metrics/{run_id:path}")
def run_metrics(run_id: str):
    """Время, токены и стоимость прогона по навыкам."""
    try:
        return abop.run_metrics(run_id)
    except abop.AbopError as e:
        return _err(e)


@router.get("/templates")
def report_templates():
    """Шаблоны отчётов — чтобы выбрать вид отчёта перед выгрузкой."""
    try:
        return abop.report_templates()
    except abop.AbopError as e:
        return _err(e)


@router.get("/report-html/{run_id:path}")
def report_html(run_id: str, template: str = ""):
    """Отчёт как HTML — показать прямо в приложении, когда сборка PDF недоступна."""
    try:
        return {"html": abop.run_report_html(run_id, template)}
    except abop.AbopError as e:
        return _err(e)


@router.post("/report/{run_id:path}")
def report_pdf(run_id: str, template: str = ""):
    """PDF-отчёт в «Загрузки» пользователя; путь возвращаем, чтобы его можно было открыть."""
    from pathlib import Path
    try:
        data = abop.run_report_pdf(run_id, template or "")
    except abop.AbopError as e:
        return _err(e)
    d = Path.home() / "Downloads"
    d = d if d.exists() else Path.home()
    p = d / (re.sub(r"[^\w\-.]+", "_", "abop_" + run_id)[:80] + ".pdf")
    p.write_bytes(data)
    return {"ok": True, "path": str(p), "bytes": len(data)}
