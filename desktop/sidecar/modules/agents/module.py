"""Модуль «Агенты» — ТОНКИЙ ПРОКСИ к ABOP (единый источник истины).

Раньше desktop держал свои агенты в локальной SQLite и гонял их как промпт-цепочки через MCP.
Теперь каталог/навыки/запуск/HITL берутся из ABOP Web API под JWT пользователя — RBAC/ABAC,
governance-конверт, HITL, кэш и наблюдаемость работают на сервере ABOP. См.
docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 1). Локальные роуты — те же префиксы, чтобы UI не менять.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ... import abop_client as abop

MANIFEST = {"id": "agents", "title": "Агенты", "icon": "agents", "ui": "agents", "order": 20}
router = APIRouter()


def _err(e: "abop.AbopError") -> JSONResponse:
    """Ошибку ABOP отдаём наверх честно (не глушим): статус + деталь для UI."""
    return JSONResponse({"ok": False, "error": e.detail, "status": e.status},
                        status_code=e.status if 400 <= e.status < 600 else 502)


class RunIn(BaseModel):
    agent_id: str = ""
    scenario: str = ""
    context: str = ""       # выделенный текст/файл из host-системы (хоткей/Office)
    no_cache: bool = False


class HitlIn(BaseModel):
    decision: str = "approve"
    reason: str = ""


class AuthorIn(BaseModel):
    family: str
    skills: list[str] = []
    name: str = ""
    member: str = ""


@router.get("/catalog")
def catalog():
    """Каталог агентов ABOP (ABAC по семье пользователя — сервер отдаёт только доступное)."""
    try:
        return abop.agents()
    except abop.AbopError as e:
        return _err(e)


@router.get("/detail/{agent_id}")
def detail(agent_id: str):
    """Полный агент (граф узлов) из ABOP."""
    try:
        return abop.agent(agent_id)
    except abop.AbopError as e:
        return _err(e)


@router.get("/skills")
def skills():
    """Каталог навыков ABOP (mode/egress/cite/output и т.д.)."""
    try:
        return abop.skills()
    except abop.AbopError as e:
        return _err(e)


@router.get("/families")
def families():
    """Палитра для конструктора: семьи → роли → навыки (ABAC)."""
    try:
        return abop.families()
    except abop.AbopError as e:
        return _err(e)


@router.get("/recipes")
def recipes():
    """Рецепты Data Plane — какие данные (entity) доступны агенту."""
    try:
        return abop.recipes()
    except abop.AbopError as e:
        return _err(e)


@router.post("/author")
def author(body: AuthorIn):
    """Собрать агента-цепочку из выбранных навыков (простой менеджерский агент, без контракта)."""
    try:
        return {"ok": True, "agent": abop.author(body.family, body.skills, body.name, body.member)}
    except abop.AbopError as e:
        return _err(e)


@router.post("/run")
def run(body: RunIn):
    """Запуск прогона агента ABOP. scenario → demo/prepare+runs; context = подсказка из host-системы."""
    try:
        r = abop.run(agent_id=body.agent_id, scenario=body.scenario,
                     context=body.context, no_cache=body.no_cache)
        return {"ok": True, "run": r}
    except abop.AbopError as e:
        return _err(e)


@router.get("/runs")
def runs(agent_id: str = ""):
    """Журнал прогонов (ABAC)."""
    try:
        return abop.runs(agent_id)
    except abop.AbopError as e:
        return _err(e)


@router.get("/hitl")
def hitl_queue():
    """Очередь HITL-подтверждений (действия наружу ждут человека)."""
    try:
        return abop.hitl_queue()
    except abop.AbopError as e:
        return _err(e)


@router.post("/hitl/{item_id}/approve")
def hitl_approve(item_id: str, body: HitlIn):
    """Подтвердить/отклонить HITL-заявку → реальная доставка (approve)."""
    try:
        return {"ok": True, "result": abop.hitl_approve(item_id, body.decision, body.reason)}
    except abop.AbopError as e:
        return _err(e)
