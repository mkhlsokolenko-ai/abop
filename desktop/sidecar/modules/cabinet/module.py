"""Модуль «Кабинет» — расход/квота и (позже) RBAC + рабочие источники.

v1: usage через шлюз (my_usage → недельные токены/остаток/стоимость). RBAC и коннекторы
рабочих источников (Word/Excel/ИС) — заглушки статуса, разворачиваются в отдельные модули.
"""
from __future__ import annotations

from fastapi import APIRouter

from ... import abop_client as abop

MANIFEST = {"id": "cabinet", "title": "Кабинет", "icon": "cabinet", "ui": "cabinet", "order": 90}

router = APIRouter()


@router.get("/usage")
def usage() -> dict:
    """Профиль пользователя из ABOP (отдел/роли/уровень). Детальный расход токенов/стоимость —
    в наблюдаемости ABOP (Grafana/метрики), а не в курсовом шлюзе."""
    try:
        r = abop.me()
        usr = (r or {}).get("user", r) or {}
        h = {}
        try:
            h = abop.health()
        except abop.AbopError:
            h = {}
        return {"ok": True, "user": usr, "department": usr.get("department"),
                "roles": usr.get("roles") or [], "level": usr.get("level"),
                "runtime": {"llm": h.get("llm"), "status": h.get("status") or h.get("ok")}}
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}


@router.get("/billing")
def billing() -> dict:
    """Счётчик токенов/квота: реальные токены из RunMetrics + остаток (токен-квота ABOP)."""
    try:
        return abop.billing()
    except abop.AbopError as e:
        return {"error": str(e)}


@router.get("/sources")
def sources() -> dict:
    """Рабочие источники: подключены в ABOP Data Plane (см. модуль «Источники»); локальные файлы —
    под правами ОС. RBAC/ABAC — на стороне ABOP (realm abop)."""
    return {"connectors": [
        {"id": "abop-data", "title": "ABOP Data Plane (коннекторы + рецепты)", "status": "ready"},
        {"id": "local-office", "title": "Локальные файлы Office (Word/Excel)", "status": "ready"},
        {"id": "recent-files", "title": "Последние рабочие файлы", "status": "ready"},
    ], "rbac": {"status": "enforced", "note": "роли/доступ к инструментам и источникам — RBAC/ABAC ABOP (realm abop)"}}
