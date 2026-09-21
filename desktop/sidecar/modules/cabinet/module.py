"""Модуль «Кабинет» — расход/квота и (позже) RBAC + рабочие источники.

v1: usage через шлюз (my_usage → недельные токены/остаток/стоимость). RBAC и коннекторы
рабочих источников (Word/Excel/ИС) — заглушки статуса, разворачиваются в отдельные модули.
"""
from __future__ import annotations

from fastapi import APIRouter

from ... import gateway

MANIFEST = {"id": "cabinet", "title": "Кабинет", "icon": "cabinet", "ui": "cabinet", "order": 90}

router = APIRouter()


@router.get("/usage")
def usage() -> dict:
    try:
        rep = gateway.call("my_usage", {})
    except gateway.AuthRequired:
        return {"ok": False, "error": "auth_required"}
    except gateway.GatewayError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "report": rep}


@router.get("/sources")
def sources() -> dict:
    """Рабочие источники (Word/Excel/ИС) — пока каркас (см. roadmap коннекторов/ABOP Data Plane)."""
    return {"connectors": [
        {"id": "local-office", "title": "Локальные файлы Office (Word/Excel)", "status": "planned"},
        {"id": "recent-files", "title": "Последние рабочие файлы", "status": "planned"},
        {"id": "abop-data", "title": "ABOP Data Plane (источники→canonical)", "status": "planned"},
    ], "rbac": {"status": "planned", "note": "роли/доступ к инструментам и источникам — на этапе смычки с ABOP"}}
