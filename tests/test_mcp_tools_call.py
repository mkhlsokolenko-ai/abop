# -*- coding: utf-8 -*-
"""Каждый инструмент MCP должен ВЫЗЫВАТЬСЯ, а не только значиться в списке.

Расщепление монолита (ADR-052) увезло ручки карты в `server/routes/process.py`, а MCP-сервер
продолжал звать `web_api.process_coverage`. Статическая проверка имён этого не видит: обращение
идёт через точку к чужому модулю, и Python узнаёт о пропаже только в момент вызова. Нашлось это
не тестом, а живым запросом к проду — инструмент ответил
«module 'server.web_api' has no attribute 'process_coverage'».

Поэтому здесь каждый зарегистрированный инструмент действительно вызывается на пустых, но живых
сторах. Проверяем не содержимое ответа (оно зависит от данных), а что вызов доходит до конца и не
возвращает ошибку исполнения.

Второй тест — статический и шире: ЛЮБОЕ обращение `web_api.<имя>` из других модулей сервера
обязано разрешаться. Это тот же класс поломки, но ловится он мгновенно и без поднятия MCP.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import re
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

# Отказ по входу от нашей же ручки: «422: name: пусто». fastmcp заворачивает HTTPException
# в ToolError, поэтому различаем по коду в тексте, а не по типу исключения.
ОТКАЗ_ПО_ВХОДУ = re.compile("4[0-9][0-9]:")

ПОЛЬЗОВАТЕЛЬ = {"sub": "qa", "name": "qa", "roles": ["support"], "level": "support",
                "department": "*", "dev": True}

# Аргументы, без которых инструмент отвечает отказом по существу (а не падает).
АРГУМЕНТЫ = {
    "data_query": {"entity": "transaction", "limit": 1},
    "data_get": {"entity": "transaction", "id": "нет-такого"},
    "data_schema": {"entity": "transaction"},
    "audit1c_graph": {},
    "audit1c_checks": {},
    "process_map": {"layer": "as-is"},
    "process_liveness": {"window": "7d"},
    "process_coverage": {"window": "7d"},
    "agents_list": {},
    # Записи не трогаем: у них свои тесты, а здесь важно, что вызов доходит до проверки входа.
    "register_agent": {},
    "report_run": {},
}


def test_каждый_инструмент_вызывается(monkeypatch):
    """Ни один вызов не должен падать на пропавшем имени.

    Заглушки ставятся ДО сборки сервера: `собрать()` импортирует `get_http_request` в момент
    вызова и замыкает его в каждом инструменте — подменять после сборки поздно.
    """
    pytest.importorskip("fastmcp")
    import fastmcp.server.dependencies as deps
    from server import agent_store, mcp_server, process_store, run_store, web_api

    class _Запрос:                       # телеметрия fastmcp читает scope/headers запроса
        scope: dict = {}
        headers: dict = {}

    monkeypatch.setattr(deps, "get_http_request", lambda: _Запрос())
    monkeypatch.setattr(web_api, "user", lambda _req: dict(ПОЛЬЗОВАТЕЛЬ))

    async def прогнать() -> dict:
        await agent_store.init()
        await run_store.init()
        await process_store.init()
        mcp = mcp_server.собрать()
        плохо = {}
        for tool in await mcp.list_tools():
            try:
                ответ = await mcp.call_tool(tool.name, {"args": АРГУМЕНТЫ.get(tool.name, {})})
            except Exception as ex:  # noqa: BLE001 — падение вызова и есть то, что ищем
                # Отказ по входу («422: name: пусто») — это ДОШЕДШИЙ вызов: инструмент жив и
                # проверил аргументы. fastmcp заворачивает HTTPException в свою ToolError, так что
                # различаем по коду в тексте: 4xx — дошло, всё остальное — поломка.
                if not ОТКАЗ_ПО_ВХОДУ.search(str(ex)):
                    плохо[tool.name] = f"{type(ex).__name__}: {ex}"
                continue
            блоки = getattr(ответ, "content", None) or []
            строка = getattr(блоки[0], "text", "") if блоки else (ответ if isinstance(ответ, str) else "")
            if "has no attribute" in (строка or "") or "Traceback" in (строка or ""):
                плохо[tool.name] = строка[:160]
        return плохо

    беда = asyncio.run(прогнать())
    assert not беда, "инструменты MCP не вызываются: " + json.dumps(беда, ensure_ascii=False)


def test_все_обращения_к_монолиту_разрешаются():
    """`web_api.<имя>` из чужого модуля — то же, что импорт: имя обязано существовать."""
    сервер = КОРЕНЬ / "server"
    дерево = ast.parse((сервер / "web_api.py").read_text(encoding="utf-8"))
    объявлены = set()
    for n in дерево.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            объявлены.add(n.name)
        elif isinstance(n, ast.Assign):
            объявлены.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            объявлены.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.For):                      # циклы подключения роутеров
            объявлены.update(x.id for x in ast.walk(n)
                             if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store))

    нет = []
    for файл in sorted(сервер.rglob("*.py")):
        if файл.name == "web_api.py":
            continue
        for узел in ast.walk(ast.parse(файл.read_text(encoding="utf-8"))):
            if (isinstance(узел, ast.Attribute) and isinstance(узел.value, ast.Name)
                    and узел.value.id == "web_api" and узел.attr not in объявлены):
                нет.append(f"{файл.name}:{узел.lineno} → web_api.{узел.attr}")
    assert not нет, "обращение к имени, которого в web_api больше нет: " + "; ".join(нет)
