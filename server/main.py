"""Точка входа курсового FastMCP-сервера.

Собирает FastMCP с Keycloak-верификатором JWT, регистрирует инструменты и поднимает
HTTP-транспорт (streamable-http). Наружу отдаётся через Caddy (TLS) — см. DEPLOY.md.

Запуск:
    ape-mcp                      # через console_script
    python -m server.main        # напрямую
"""
from __future__ import annotations

import logging

from fastmcp import FastMCP

from .auth import build_verifier
from .config import settings
from .tools import register_all


def build_app() -> FastMCP:
    logging.basicConfig(level=settings.log_level)
    mcp = FastMCP(
        name="AI Product Engineer — course gateway",
        instructions=(
            "Единая точка доступа студентов к LLM и RAG. Модели дёргаются только через "
            "инструмент chat (profile: code->Qwen3.8-27B, research->DeepSeek). Каждый "
            "вызов метрится в cost-журнал и лимитируется (5M/сессия, 5 сессий, 25M/неделя)."
        ),
        auth=build_verifier(),
    )
    register_all(mcp)
    return mcp


def main() -> None:
    app = build_app()
    app.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
