"""Модуль «Прогоны» — журнал работы агентов: список, карточка целиком, сравнение, отчёт.

До него история прогонов в десктопе жила только карточками внутри чатов: удалил чат — потерял
прогоны, а оборвавшийся опрос уносил результат навсегда. Здесь — прямой журнал с сервера ABOP
(источник истины — Postgres), поиск и фильтры считаются на сервере, а не в интерфейсе.
"""
from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ... import abop_client as abop, db

MANIFEST = {"id": "runs", "title": "Прогоны", "icon": "runs", "ui": "runs", "order": 25}
router = APIRouter()

# Предел одной отметки внимания: счётчик в окне останавливается не всегда (спящий компьютер,
# закрытый ноутбук), и без предела «вы у экрана» однажды покажет восемь часов.
ПРЕДЕЛ_ВНИМАНИЯ = 30 * 60.0


def _внимание(run_id: str) -> float:
    строки = db.q("SELECT sec FROM attention WHERE run_id = ?", (run_id,))
    return round(float(строки[0]["sec"]), 1) if строки else 0.0


def _время(сек: float) -> str:
    """То же правило, что на сервере (`run_card._время`): секунды до минуты, дальше минуты."""
    с = max(0, int(round(сек)))
    if с < 60:
        return f"{с} с"
    м, ост = divmod(с, 60)
    return f"{м} м {ост:02d} с" if ост else f"{м} м"


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


@router.get("/search")
def search(q: str = "", agent_id: str = "", verdict: str = "", days: int = 0, limit: int = 50):
    """Поиск по содержимому прогонов: за что нашлось, видно по фрагментам."""
    try:
        return abop.runs_search(q, agent_id, verdict, days, limit)
    except abop.AbopError as e:
        return _err(e)


@router.get("/item/{run_id:path}")
def run_item(run_id: str):
    """Полная запись прогона: находки целиком, доставка, вердикт, метрики."""
    try:
        return abop.run_full(run_id)
    except abop.AbopError as e:
        return _err(e)


@router.get("/board/{run_id:path}")
def run_board(run_id: str):
    """Доска прогона: что выложил каждый навык, где ветви разошлись и чем спор кончился."""
    try:
        return abop.run_board(run_id)
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


@router.get("/live")
def live():
    """Что сейчас идёт у ЭТОГО человека — для строки живой работы.

    Берём задания из местной очереди догона (`pending_runs`): это ровно то, что человек запустил
    сам. Сервер спрашиваем о состоянии каждого — их единицы. Недоступность сервера не ошибка
    строки: нечего показать — значит строки нет.
    """
    строки = []
    try:
        задания = db.q("SELECT job_id, agent_id FROM pending_runs ORDER BY created_at")
    except Exception:  # noqa: BLE001 — своя база не должна ронять оболочку
        задания = []
    for з in задания[:5]:
        try:
            j = abop.run_job(з["job_id"]) or {}
        except Exception:  # noqa: BLE001 — сервер недоступен: задание просто не показываем
            continue
        статус = str(j.get("status") or "")
        if статус not in ("queued", "running", "awaiting_hitl"):
            continue
        пр = j.get("progress") or {}
        шаг = ""
        if пр.get("steps_total"):
            шаг = f"шаг {int(пр.get('step') or 0) + 1} из {пр['steps_total']}"
        строки.append({
            "job_id": з["job_id"], "agent_id": з.get("agent_id") or j.get("agent_id") or "",
            "status": статус,
            "что": {"queued": "в очереди", "running": "идёт",
                    "awaiting_hitl": "ждёт решения"}.get(статус, статус),
            "шаг": шаг, "run_id": j.get("run_id") or "",
            "начато": j.get("started_at") or j.get("created_at") or "",
            "место": int(j.get("position") or 0),
        })
    return {"живое": строки}


class ВниманиеIn(BaseModel):
    sec: float = 0.0


@router.post("/attention/{run_id:path}")
def attention(run_id: str, body: ВниманиеIn):
    """Прибавить время, которое человек провёл у карточки прогона.

    Копится, а не перезаписывается: карточку открывают несколько раз, и «вы у экрана» — это всё
    потраченное время, а не последний заход. Отрицательное и неправдоподобно большое отбрасываем:
    счётчик в браузере останавливается на спящем компьютере, и час «внимания» означал бы, что
    ноутбук закрыли, а не что человек читал.
    """
    сек = max(0.0, min(float(body.sec or 0), ПРЕДЕЛ_ВНИМАНИЯ))
    if not сек:
        return {"ok": True, "sec": _внимание(run_id)}
    db.run("INSERT INTO attention (run_id, sec, at) VALUES (?,?,?) "
           "ON CONFLICT(run_id) DO UPDATE SET sec = sec + excluded.sec, at = excluded.at",
           (run_id, сек, db.now()))
    return {"ok": True, "sec": _внимание(run_id)}


@router.get("/summary/{run_id:path}")
def summary(run_id: str):
    """Итог прогона в числах: серверные числа + местное «вы у экрана».

    Числа и происхождение считает сервер (`server/run_card`) — одной функцией на все поверхности.
    Десктоп добавляет ОДНУ плитку, которую только он и может знать, и честно говорит в
    происхождении, что мерил он.
    """
    try:
        run = abop.run_full(run_id)
    except abop.AbopError as e:
        return _err(e)
    итог = dict((run or {}).get("итог") or {})
    числа = [list(x) for x in (итог.get("числа") or [])]
    сек = _внимание(run_id)
    if сек:
        числа.insert(min(2, len(числа)), [_время(сек), "вы у экрана"])
        итог["происхождение"] = ((итог.get("происхождение") or "")
                                 + " · время у экрана считает приложение на этом компьютере").strip(" ·")
    итог["числа"] = числа
    итог["внимание_сек"] = сек
    return итог


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
