"""Агент владельца кофейни — демонстрационный пример для лекции 1.

Весь стек курса через курсовой MCP: auth (JWT) -> RAG (Qdrant+BGE) -> LLM (DeepSeek)
-> cost/квоты. Ни одного прямого вызова LLM-провайдера. ~80 строк, читается вслух.

Запуск:
    export MCP_URL="https://mcp.example.ru/mcp"
    export MCP_TOKEN="<JWT>"
    python agent.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastmcp import Client
from fastmcp.client.auth import BearerAuth

MCP_URL = os.environ["MCP_URL"]
MCP_TOKEN = os.environ["MCP_TOKEN"]
SESSION_ID = "demo-coffee-lecture-1"      # одна сессия -> считается в лимит 5M/сессия

SYSTEM = (
    "Ты — операционный помощник владельца кофейни. На вход — жалобы гостей за неделю. "
    "Верни СТРОГО JSON: {\"actions\": [three strings]} — ровно 3 конкретных, "
    "выполнимых за понедельник действия, отсортированных по важности боли."
)


async def main() -> None:
    reviews = [
        json.loads(line)["text"]
        for line in Path(__file__).with_name("sample_reviews.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]

    async with Client(MCP_URL, auth=BearerAuth(MCP_TOKEN)) as mcp:
        # 1) Индексируем отзывы недели в свою коллекцию Qdrant (BGE-M3 embed).
        idx = await mcp.call_tool("rag_index", {"documents": reviews, "session_id": SESSION_ID})
        print(f"Проиндексировано отзывов: {idx.data['indexed']}")

        # 2) Достаём главные боли: vector search -> BGE-rerank -> top-5.
        found = await mcp.call_tool(
            "rag_search",
            {"query": "на что жалуются гости чаще всего", "session_id": SESSION_ID, "top_k": 5},
        )
        pains = [r["text"] for r in found.data["results"]]
        print("\nТоп-жалобы (после reranking):")
        for p in pains:
            print(f"  • {p}")

        # 3) LLM формулирует 3 действия. profile=research -> DeepSeek (политика курса).
        prompt = "Жалобы гостей за неделю:\n- " + "\n- ".join(pains)
        res = await mcp.call_tool(
            "chat",
            {
                "prompt": prompt,
                "session_id": SESSION_ID,
                "profile": "research",
                "system": SYSTEM,
                "prompt_version": "coffee-v1",
                "max_tokens": 500,
            },
        )
        data = res.data
        actions = json.loads(data["text"]).get("actions", [])

        print("\n3 действия на понедельник:")
        for i, a in enumerate(actions, 1):
            print(f"  {i}. {a}")

        # 4) Cost + остаток квоты — cost с первой лекции.
        q = data["quota"]["week"]
        print(
            f"\nМодель: {data['model']} | стоимость прогона: {data['cost_rub']} ₽ | "
            f"неделя: {q['tokens_used']:,}/{q['limit']:,} токенов "
            f"({q['used_pct']}%), осталось {q['remaining']:,}"
        )


if __name__ == "__main__":
    asyncio.run(main())
