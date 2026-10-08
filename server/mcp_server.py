# -*- coding: utf-8 -*-
"""MCP-сервер ABOP: чужой агент работает с процессами и данными организации под её правами.

**Зачем.** По обзору рынка 2026 первая из девяти критических способностей платформы управления
агентами — единый реестр агентов НЕЗАВИСИМО от того, где они развёрнуты. ABOP управлял только
теми, кого сам породил. ADR-028 отвечал на это замыслом «конверт НАД чужим рантаймом», но в коде
его не было. MCP — ровно тот протокол, по которому чужой агент (Claude, Copilot, LangGraph)
работает внутри нашего конверта, не переезжая в ABOP.

**Три правила, из которых следует всё остальное.**

1. *Ни одного самостоятельного обращения к сторам.* Каждый инструмент — обёртка над функцией,
   которой уже пользуется HTTP-API. Второй путь к данным означал бы второй набор проверок, то
   есть рано или поздно расхождение — ровно то, из-за чего в `server/` три недели жил чужой
   шлюз со своей аутентификацией и своей моделью пользователя.
2. *Та же аутентификация и те же гейты.* Bearer разбирает `web_api.user` — multi-issuer JWKS,
   кэш верифицированных токенов, fail-closed. ABAC по отделу отрабатывает внутри тех же функций.
   Внешний агент не получает того, чего не получает человек с тем же токеном.
3. *Наружу не действуем.* Письмо, задача, публикация идут в ABOP через OUT-узел с
   подтверждением человека; отдать их чужому агенту мимо этой цепочки значит отдать право
   действовать от имени организации. Отдельное решение и отдельный заход.

   Две записи всё же есть, и обе — в СОБСТВЕННЫЕ записи ABOP, а не во внешний мир:
   `register_agent` (агент заявляет о себе в реестр) и `report_run` (сообщает о работе на
   участке). Без них конверт над чужим рантаймом остаётся читалкой: агента некуда посадить и
   нечем подтвердить, что он работает. Отчёт помечается как отчёт и подтверждает участок слабее
   собственного прогона — эта разница доезжает до карты.

**Выключен по умолчанию** — монтируется только при `ABOP_MCP=1`. Продукт ещё не в эксплуатации
(тридцать прогонов за всё время), и новая сетевая поверхность поверх всех данных включается
осознанно, а не фактом обновления образа.
"""
from __future__ import annotations

import contextlib as _contextlib
import json
import os

# Инструменты чтения берём из набора, который УЖЕ есть у внутренних агентов. Список здесь — это
# выбор «что отдаём наружу», а не второе определение инструментов: тела и описания общие.
ЧТЕНИЕ_ИЗ_НАБОРА_АГЕНТА = ("data_query", "data_get", "data_schema",
                           "audit1c_graph", "audit1c_checks")
СВОИ = ("process_map", "process_liveness", "process_coverage", "agents_list")
# Записи в собственные записи ABOP. Наружу по-прежнему не действуем — см. докстринг и тест
# test_наружу_не_выставлено_ни_одного_действия.
ЗАПИСЬ = ("register_agent", "report_run")
ПРЕДЕЛ_ОТВЕТА = 20000


def включён() -> bool:
    return (os.getenv("ABOP_MCP") or "").strip().lower() in ("1", "true", "yes")


def имена_инструментов() -> list[str]:
    """Что отдаётся наружу. Отдельной функцией — чтобы тест сверял намерение с реальностью."""
    return list(ЧТЕНИЕ_ИЗ_НАБОРА_АГЕНТА) + list(СВОИ) + list(ЗАПИСЬ)


def _лог(событие: str, **поля) -> None:
    try:
        from . import observability as obs
        obs.log_event("info", событие, **поля)
    except Exception:  # noqa: BLE001 — наблюдаемость не вправе ронять вызов инструмента
        pass


def _ответ(данные) -> str:
    if isinstance(данные, str):
        return данные[:ПРЕДЕЛ_ОТВЕТА]
    return json.dumps(данные, ensure_ascii=False, default=str)[:ПРЕДЕЛ_ОТВЕТА]


def собрать():
    """Собрать FastMCP со всеми инструментами. Импорты внутри: без ABOP_MCP=1 они не нужны."""
    from fastmcp import FastMCP
    from fastmcp.server.dependencies import get_http_request

    from cli import ape

    from . import process_store, skill_tools, web_api

    mcp = FastMCP("ABOP")

    def _кто() -> dict:
        """Пользователь из Bearer — тем же кодом, что у HTTP-API, без своей копии разбора."""
        return web_api.user(get_http_request())

    # ── инструменты чтения, общие с внутренними агентами ────────────────────────────────────
    def _обёртка(tool_name: str):
        async def инструмент(args: dict | None = None) -> str:
            u = _кто()
            web_api.require_level(u, "analyst")   # явный гейт даже на чтении: см. тест
            fn = (ape.AGENT_TOOLS.get(tool_name) or (None,))[0]
            if fn is None:
                return _ответ({"error": "инструмент не зарегистрирован в ядре: " + tool_name})
            _лог("mcp.tool", tool=tool_name, actor=u.get("name") or u.get("sub"),
                 department=u.get("department"))
            return _ответ(fn(args or {}))
        return инструмент

    for имя in ЧТЕНИЕ_ИЗ_НАБОРА_АГЕНТА:
        mcp.tool(name=имя, description=skill_tools.DESCRIPTIONS.get(имя) or имя)(_обёртка(имя))

    # ── то, ради чего к ABOP и приходят: процесс, в который посажены агенты ──────────────────
    @mcp.tool(name="process_map",
              description='карта процессов организации: уровни, операции и посаженные на них '
                          'агенты — args: {"layer": "as-is|to-be"}, по умолчанию as-is')
    async def process_map(args: dict | None = None) -> str:
        u = _кто()
        web_api.require_level(u, "analyst")
        слой = str((args or {}).get("layer") or "as-is").lower()
        if слой not in ("as-is", "to-be"):
            return _ответ({"error": "layer: as-is или to-be"})
        # Карту целиком видят все — процесс стоит между чужими и без них не читается (ADR-045).
        # Правка чужой области закрыта, но сюда наружу правки и не выставлены.
        срез = await process_store.projection(process_store.КАРТА, layer=слой)
        _лог("mcp.tool", tool="process_map", actor=u.get("name") or u.get("sub"), layer=слой)
        return _ответ({"layer": слой, "revision": срез.get("revision"),
                       "tree": срез.get("tree"), "seats": срез.get("seats") or []})

    @mcp.tool(name="process_liveness",
              description='подтверждение карты прогонами: по каждому участку число прогонов за '
                          'окно и состояние подтверждён|тихо|осиротел — args: {"window": "24h|7d|30d"}')
    async def process_liveness(args: dict | None = None) -> str:
        from fastapi import Response
        u = _кто()
        окно = str((args or {}).get("window") or "7d")
        данные = await web_api.process_liveness(Response(), окно, u)
        _лог("mcp.tool", tool="process_liveness", actor=u.get("name") or u.get("sub"), window=окно)
        return _ответ(данные)

    @mcp.tool(name="process_coverage",
              description='где агенты и где дыры: по каждой операции агент и состояние, '
                          'доля подтверждённых прогонами, перечень пробелов — '
                          'args: {"window": "24h|7d|30d"}')
    async def process_coverage(args: dict | None = None) -> str:
        from fastapi import Response
        u = _кто()
        окно = str((args or {}).get("window") or "7d")
        данные = await web_api.process_coverage(Response(), окно, u)
        _лог("mcp.tool", tool="process_coverage", actor=u.get("name") or u.get("sub"))
        return _ответ(данные)

    @mcp.tool(name="agents_list",
              description="агенты, доступные вашему отделу (ABAC): имя, семья, версия, "
                          "автономия, статус развёртывания")
    async def agents_list(args: dict | None = None) -> str:
        u = _кто()
        данные = await web_api.agents_list("", False, u)   # ABAC по отделу — внутри ручки
        _лог("mcp.tool", tool="agents_list", actor=u.get("name") or u.get("sub"))
        return _ответ(данные)

    @mcp.tool(name="register_agent",
              description='заявить о себе в реестр ABOP: агент чужого рантайма получает место '
                          'в организации — args: {"name":"...","runtime":"langgraph|copilot-studio|'
                          'agentforce|bedrock|crewai|autogen|n8n|другой","family":"...",'
                          '"autonomy_max":"A0..A4","endpoint":"..."} . Конверт объявленный: '
                          'графа у ABOP нет, автоматически он не проверяется')
    async def register_agent(args: dict | None = None) -> str:
        u = _кто()
        данные = await web_api.agents_register_external(dict(args or {}), u)
        _лог("mcp.tool", tool="register_agent", actor=u.get("name") or u.get("sub"))
        return _ответ(данные)

    @mcp.tool(name="report_run",
              description='сообщить о выполненной работе на участке карты — args: '
                          '{"audit_id":"...","op_ids":["o1"],"ok":true,"summary":"..."} . '
                          'Принимается только по операциям, на которые агент посажен; в карте '
                          'участок помечается «со слов агента»')
    async def report_run(args: dict | None = None) -> str:
        u = _кто()
        данные = await web_api.runs_report_external(dict(args or {}), u)
        _лог("mcp.tool", tool="report_run", actor=u.get("name") or u.get("sub"))
        return _ответ(данные)

    return mcp


def подключить(app) -> bool:
    """Смонтировать MCP в основное приложение. False — выключен или не собрался."""
    if not включён():
        return False
    try:
        # path="/" — иначе fastmcp вешает свою ручку ещё раз внутри и адрес становится /mcp/mcp.
        под = собрать().http_app(path="/")
        app.mount("/mcp", под)

        # Менеджер сессий MCP живёт в lifespan СВОЕГО приложения. При монтировании в чужое
        # приложение этот lifespan не запускается сам, и первый же вызов падает с «task group was
        # not initialized». Поднимаем его событиями родителя — так же, как это делают остальные
        # фоновые части сервиса.
        стек = _contextlib.AsyncExitStack()

        @app.on_event("startup")
        async def _mcp_старт() -> None:   # noqa: ANN202
            await стек.enter_async_context(под.lifespan(под))
            _лог("mcp.lifespan_started")

        @app.on_event("shutdown")
        async def _mcp_стоп() -> None:    # noqa: ANN202
            await стек.aclose()

        _лог("mcp.mounted", tools=len(имена_инструментов()), path="/mcp")
        return True
    except Exception as ex:  # noqa: BLE001 — отсутствие fastmcp не должно ронять весь сервис
        _лог("mcp.mount_fail", error=f"{type(ex).__name__}: {ex}")
        return False
