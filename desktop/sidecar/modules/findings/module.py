"""Модуль «Находки» — журнал расхождений пилота 1С по последним прогонам аудитора и следователя.

В журнале прогонов находки лежат внутри конкретного прогона: чтобы увидеть картину по всем, надо
было открыть десяток карточек подряд. Здесь они сведены в один список со своим смыслом: что не
сходится, откуда это видно, чем грозит и что проверить — с ссылкой на первичный документ 1С.

Разметка находок (подтвердил / отклонил эксперт) показывается, но не ставится: слепая разметка —
работа эксперта в веб-интерфейсе, и делать её в двух местах значит получить две разные истины.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ... import abop_client as abop

MANIFEST = {"id": "findings", "title": "Находки", "icon": "findings", "ui": "findings", "order": 27}
router = APIRouter()


def _err(e: "abop.AbopError") -> JSONResponse:
    return JSONResponse({"ok": False, "error": e.detail, "status": e.status},
                        status_code=e.status if 400 <= e.status < 600 else 502)


@router.get("/list")
def findings_list(limit: int = 120, runs: int = 12, agent_id: str = "",
                  cls: str = "", severity: str = "", cross: int = 0, q: str = ""):
    """Находки последних прогонов с фильтрами.

    Фильтры считаем здесь, а не на сервере: сервер отдаёт журнал целиком с метриками пилота, и
    пересчитывать их под каждый фильтр значило бы показывать метрики по выборке, а не по пилоту.
    """
    try:
        data = abop.findings_journal(limit=limit, runs=runs, agent_id=agent_id)
    except abop.AbopError as e:
        return _err(e)
    items = data.get("items") or []
    ql = (q or "").strip().lower()

    def keep(c: dict) -> bool:
        if cls and str(c.get("cls") or "") != cls:
            return False
        if severity and str(c.get("severity") or "") != severity:
            return False
        if cross and not c.get("cross"):
            return False
        if ql:
            hay = " ".join(str(c.get(k) or "") for k in ("id", "check", "kind", "doc_line", "agent_name"))
            hay += " " + str((c.get("explain") or {}).get("what") or "")
            if not all(w in hay.lower() for w in ql.split()):
                return False
        return True

    shown = [c for c in items if keep(c)]
    classes = sorted({str(c.get("cls") or "") for c in items if c.get("cls")})
    sevs = sorted({str(c.get("severity") or "") for c in items if c.get("severity")})
    return {"items": shown, "total": len(items), "shown": len(shown),
            "metrics": data.get("metrics") or {}, "runs": data.get("runs") or [],
            "classes": classes, "severities": sevs}
