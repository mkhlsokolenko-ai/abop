"""Инструменты MCP. Каждый модуль экспортирует register(mcp)."""
from . import admin, llm, rag


def register_all(mcp) -> None:
    llm.register(mcp)
    rag.register(mcp)
    admin.register(mcp)
