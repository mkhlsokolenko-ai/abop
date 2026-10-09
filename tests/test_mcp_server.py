# -*- coding: utf-8 -*-
"""MCP-сервер ABOP: конверт над чужим рантаймом (ADR-028, стратегический п.16 исследования).

Чужой агент — Claude, Copilot, LangGraph — работает с процессами и данными организации, не
переезжая в ABOP. Это закрывает первую из девяти критических способностей платформы управления
агентами по обзору рынка 2026: единый реестр независимо от того, где агент развёрнут.

Риск здесь выше, чем у обычной ручки: внешний агент не ограничен интерфейсом и держит длинные
сессии. Поэтому тесты сторожат не «инструмент отвечает», а три свойства, нарушение которых
открывает дыру шире, чем HTTP-API:

* наружу не выставлено ни одного ДЕЙСТВИЯ — только чтение;
* ни один инструмент не ходит в сторы сам, мимо функций, где живут проверки;
* список инструментов не расходится с тем, что реально зарегистрировано.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

from cli import ape  # noqa: E402
from server import mcp_server, skill_tools  # noqa: E402

ИСХОДНИК = (КОРЕНЬ / "server" / "mcp_server.py").read_text(encoding="utf-8")
fastmcp = pytest.importorskip("fastmcp", reason="MCP-сервер собирается только с fastmcp")


# ── безопасность поверхности ─────────────────────────────────────────────────────────────────────

def test_наружу_не_выставлено_ни_одного_действия():
    """Главное. Действия в ABOP идут через OUT-узел под подтверждением человека; отдать их
    чужому агенту мимо этой цепочки — отдать право действовать от имени организации."""
    выставлено = set(mcp_server.имена_инструментов())
    пересечение = выставлено & skill_tools.ACTION_TOOLS
    assert not пересечение, "наружу выставлено действие: " + str(пересечение)


def test_записи_только_в_свои_записи():
    """Записи две, и обе — в собственные записи ABOP. Наружу MCP не действует."""
    assert set(mcp_server.ЗАПИСЬ) == {"register_agent", "report_run"}, (
        "набор записи изменился — это решение, а не правка: " + str(mcp_server.ЗАПИСЬ))
    assert not set(mcp_server.ЗАПИСЬ) & skill_tools.ACTION_TOOLS


def test_отчёт_о_прогоне_проходит_через_ручку_с_защитой():
    """Защита от накрутки покрытия живёт в ручке; MCP не вправе её обойти своей реализацией."""
    блок = ИСХОДНИК[ИСХОДНИК.index("async def report_run("):][:700]
    # Ручка уехала в домен карты при расщеплении монолита (ADR-052) — проверяем имя функции, а не
    # модуль, в котором она сегодня лежит: иначе тест держал бы раскладку файлов, а не правило.
    assert "runs_report_external" in блок, "MCP пишет прогон мимо проверок ручки"
    assert "process_seats" not in блок and "run_store.save" not in блок, (
        "MCP пишет прогон своей реализацией, минуя защиту ручки от накрутки покрытия")


def test_выключен_по_умолчанию():
    """Новая сетевая поверхность поверх всех данных включается осознанно, а не обновлением образа."""
    старое = os.environ.pop("ABOP_MCP", None)
    try:
        assert mcp_server.включён() is False
    finally:
        if старое is not None:
            os.environ["ABOP_MCP"] = старое


def test_ни_один_инструмент_не_ходит_в_сторы_сам():
    """Второй путь к данным = второй набор проверок = расхождение. Инструменты обязаны быть
    обёртками над функциями, где проверки уже живут."""
    дерево = ast.parse(ИСХОДНИК)
    запрещено = {"agent_store", "run_store", "hitl_store", "dataplane_store", "audit_store",
                 "admin_store", "blackboard", "run_queue", "dataplane_records"}
    найдено = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.ImportFrom):
            for a in узел.names:
                if a.name in запрещено:
                    найдено.add(a.name)
        if isinstance(узел, ast.Attribute) and isinstance(узел.value, ast.Name):
            if узел.value.id in запрещено:
                найдено.add(узел.value.id)
    assert not найдено, "MCP лезет в сторы напрямую: " + str(sorted(найдено))


def test_каждый_инструмент_узнаёт_пользователя():
    """Инструмент без _кто() работал бы анонимно — хуже, чем HTTP-ручка без Depends(user)."""
    дерево = ast.parse(ИСХОДНИК)
    собрать = next(n for n in ast.walk(дерево)
                   if isinstance(n, ast.FunctionDef) and n.name == "собрать")
    инструменты = [n for n in ast.walk(собрать)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name not in ("собрать", "_кто", "_обёртка")]
    assert len(инструменты) >= 4, "инструментов меньше, чем ожидалось: " + str(
        [n.name for n in инструменты])
    for f in инструменты:
        вызовы = {getattr(n.func, "id", getattr(n.func, "attr", ""))
                  for n in ast.walk(f) if isinstance(n, ast.Call)}
        assert "_кто" in вызовы, f.name + ": не определяет пользователя"


def test_аутентификация_не_переписана():
    """Своя копия разбора токена — та самая ошибка, из-за которой в server/ жил чужой шлюз."""
    assert "web_api.user(" in ИСХОДНИК, "пользователь определяется не функцией web_api"
    for своё in ("jwt.decode", "PyJWKClient", "verify_signature"):
        assert своё not in ИСХОДНИК, "MCP завёл свой разбор токена: " + своё


# ── согласованность с ядром ──────────────────────────────────────────────────────────────────────

def test_список_совпадает_с_зарегистрированным():
    """Объявленный набор и собранный сервер обязаны сходиться, иначе описание снова разойдётся
    с реализацией — болезнь, которую этот проект уже лечил."""
    os.environ["ABOP_MCP"] = "1"
    try:
        mcp = mcp_server.собрать()
    finally:
        os.environ.pop("ABOP_MCP", None)
    import asyncio
    список = asyncio.run(mcp.list_tools())
    зарегистрировано = {getattr(x, "name", x) for x in список}
    assert зарегистрировано == set(mcp_server.имена_инструментов()), (
        "расхождение: заявлено " + str(sorted(mcp_server.имена_инструментов()))
        + ", собрано " + str(sorted(зарегистрировано)))


def test_инструменты_чтения_существуют_в_ядре():
    """Выставить наружу имя, которого нет в AGENT_TOOLS, — отдать неработающий инструмент."""
    нет = [и for и in mcp_server.ЧТЕНИЕ_ИЗ_НАБОРА_АГЕНТА if и not in ape.AGENT_TOOLS]
    assert not нет, "в ядре нет инструментов: " + str(нет)


def test_описания_общие_с_внутренними_агентами():
    """Два описания одного инструмента разъедутся. Берём те же строки, что видит агент внутри."""
    for и in mcp_server.ЧТЕНИЕ_ИЗ_НАБОРА_АГЕНТА:
        assert и in skill_tools.DESCRIPTIONS, "нет общего описания для " + и
    assert "skill_tools.DESCRIPTIONS" in ИСХОДНИК, "описания пишутся заново, а не берутся общие"


def test_ответ_ограничен_по_размеру():
    """Выборка на 200 тыс. записей не должна уезжать в контекст чужой модели целиком."""
    assert mcp_server.ПРЕДЕЛ_ОТВЕТА <= 50000
    assert mcp_server._ответ({"x": "я" * 100000}).__len__() <= mcp_server.ПРЕДЕЛ_ОТВЕТА


def test_монтируется_только_под_флагом():
    class Заглушка:
        """Минимальный родитель: монтирование + события, в которые MCP вешает свой lifespan."""

        def __init__(self):
            self.пути = []
            self.события = []

        def mount(self, path, app):
            self.пути.append(path)

        def on_event(self, name):
            self.события.append(name)
            return lambda fn: fn

    os.environ.pop("ABOP_MCP", None)
    a = Заглушка()
    assert mcp_server.подключить(a) is False and a.пути == []
    os.environ["ABOP_MCP"] = "1"
    try:
        b = Заглушка()
        assert mcp_server.подключить(b) is True, "с флагом не смонтировался"
        assert b.пути == ["/mcp"]
        assert set(b.события) == {"startup", "shutdown"}, (
            "lifespan MCP не поднимается родителем — первый же вызов упадёт "
            "на «task group was not initialized»")
    finally:
        os.environ.pop("ABOP_MCP", None)
