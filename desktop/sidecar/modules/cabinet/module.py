"""Модуль «Кабинет» — расход/квота и (позже) RBAC + рабочие источники.

v1: usage через шлюз (my_usage → недельные токены/остаток/стоимость). RBAC и коннекторы
рабочих источников (Word/Excel/ИС) — заглушки статуса, разворачиваются в отдельные модули.
"""
from __future__ import annotations

from fastapi import APIRouter

from ... import abop_client as abop

MANIFEST = {"id": "cabinet", "title": "Кабинет", "icon": "cabinet", "ui": "cabinet", "order": 90}

router = APIRouter()


@router.get("/guides")
def guides() -> dict:
    """Руководства поставки, которые отдаёт ABOP. Ссылки делаем абсолютными: десктоп открывает их
    во внешнем браузере, а относительный путь там не разрешится."""
    from ... import config  # noqa: PLC0415 — локальный импорт: реестр модулей не должен тянуть конфиг
    try:
        data = abop._req("GET", "/api/guides")
    except Exception as e:  # noqa: BLE001 — отсутствие руководств не должно ронять кабинет
        return {"guides": [], "error": str(e)[:200]}
    base = config.ABOP.rstrip("/")
    out = []
    for g in ((data or {}).get("guides") or []) if isinstance(data, dict) else []:
        out.append({**g,
                    "html": (base + g["html"]) if g.get("html") else "",
                    "pdf": (base + g["pdf"]) if g.get("pdf") else ""})
    return {"guides": out}


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
