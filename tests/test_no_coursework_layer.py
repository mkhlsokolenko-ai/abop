# -*- coding: utf-8 -*-
"""Курсового слоя LUDA внутри ABOP больше нет.

В `server/` жил целиком чужой продукт: точка входа курсового FastMCP-шлюза, своя аутентификация
со `student_id`, свой RAG и квоты «5M на сессию». Он нигде не запускался, но при чтении кода
выглядел работающей частью платформы — и дважды вводил в заблуждение при инвентаризации, в том
числе автоматической. Хуже того, `rag_search` из `ape.AGENT_TOOLS` вёл на внешний курсовой хост
`mcp.engineer-ai.pro`: ловушка не срабатывала только потому, что ни один навык не объявлял этот
инструмент во frontmatter.

Тест сторожит не «файлов нет» ради чистоты, а два свойства: в коде платформы нет чужой модели
пользователя, и агенту нельзя выдать инструмент, уходящий на посторонний хост.
"""
from __future__ import annotations

import pathlib

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
ИСХОДНИКИ = [p for p in (КОРЕНЬ / "server").rglob("*.py")] + \
            [p for p in (КОРЕНЬ / "cli").rglob("*.py")] + \
            [p for p in (КОРЕНЬ / "desktop" / "sidecar").rglob("*.py")]


def _код(текст: str) -> str:
    """Только исполняемый код: комментарии и строковые литералы выброшены.

    Иначе тест роняет честное пояснение о том, ЧТО именно удалено и почему, — а такое пояснение
    в коде ценнее самого запрета.
    """
    import io
    import tokenize
    куски = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(текст).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            куски.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return текст
    return " ".join(куски)


def test_файлов_курсового_шлюза_нет():
    for путь in ("server/main.py", "server/auth.py", "server/tools/rag.py",
                 "server/tools/llm.py", "desktop/sidecar/gateway.py", "Dockerfile"):
        assert not (КОРЕНЬ / путь).exists(), "вернулся курсовой файл: " + путь


def test_чужой_модели_пользователя_нет():
    """`student_id` — ключевое понятие курсового продукта, в ABOP пользователь другой."""
    грязные = []
    for p in ИСХОДНИКИ:
        t = _код(p.read_text(encoding="utf-8"))
        if "student_id" in t or "current_student" in t:
            грязные.append(str(p.relative_to(КОРЕНЬ)))
    assert not грязные, "курсовая модель пользователя вернулась в: " + str(грязные)


def test_пул_соединений_цел():
    """db.py удалять было нельзя: его половина держит все сторы."""
    сырой = (КОРЕНЬ / "server" / "db.py").read_text(encoding="utf-8")
    for нужное in ("async def init_pool", "async def close_pool", "def _conn"):
        assert нужное in сырой, "из db.py пропало " + нужное
    код = _код(сырой)
    for лишнее in ("check_quota", "log_usage", "student_report", "cost_journal"):
        assert лишнее not in код, "курсовая механика осталась: " + лишнее


def test_агенту_не_выдают_инструмент_на_чужой_хост():
    """rag_search ходил на mcp.engineer-ai.pro. Настоящий RAG платформы — sLAVA/Qdrant."""
    ape = (КОРЕНЬ / "cli" / "ape.py").read_text(encoding="utf-8")
    начало = ape.index("AGENT_TOOLS = {")
    таблица = ape[начало:ape.index("\n}", начало)]
    assert "rag_search" not in таблица, "rag_search снова в наборе инструментов агента"


def test_манифест_курсового_пакета_убран():
    сырой = (КОРЕНЬ / "pyproject.toml").read_text(encoding="utf-8")
    код = chr(10).join(x for x in сырой.splitlines() if not x.lstrip().startswith("#"))
    for лишнее in ("ai-product-engineer-mcp", "ape-mcp", "portal_api", "fastmcp"):
        assert лишнее not in код, "в pyproject вернулось: " + лишнее
    assert "[tool.ruff]" in код, "настройка линтера потеряна вместе с манифестом"


def test_документация_не_обещает_курсовой_стек():
    t = (КОРЕНЬ / "skills" / "rag-architect" / "SKILL.md").read_text(encoding="utf-8")
    assert "Курсовой стек" not in t
    assert "slava" in t.lower(), "навык не называет настоящий RAG платформы"
