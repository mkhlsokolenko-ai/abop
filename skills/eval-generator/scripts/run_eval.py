#!/usr/bin/env python3
"""Раннер eval-датасета через курсовой MCP. Reference implementation — адаптируй под свой домен.

Датасет — JSONL, по строке на кейс:
    {"input": "...", "expect": "...", "grader": "contains|equals|judge", "criterion": "...опц для judge..."}

Запуск:
    export MCP_URL=https://mcp.engineer-ai.pro/mcp
    export MCP_TOKEN=<твой JWT>
    python run_eval.py dataset.jsonl --profile research

Грейдеры:
    contains — ответ содержит expect (подстрока, регистронезависимо)
    equals   — ответ == expect после strip/lower
    judge    — LLM-as-judge: отдельный вызов MCP оценивает по criterion (да/нет)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.auth import BearerAuth

JUDGE_SYS = (
    "Ты — строгий грейдер. Ответь СТРОГО одним словом: PASS или FAIL. "
    "PASS только если ответ удовлетворяет критерию. Никаких пояснений."
)


async def ask(mcp: Client, prompt: str, profile: str, session: str, system: str = "") -> str:
    r = await mcp.call_tool(
        "chat",
        {"prompt": prompt, "session_id": session, "profile": profile, "system": system, "max_tokens": 600},
    )
    return (r.data or {}).get("text", "")


async def grade(mcp: Client, case: dict, answer: str) -> bool:
    g = case.get("grader", "contains")
    exp = str(case.get("expect", ""))
    if g == "equals":
        return answer.strip().lower() == exp.strip().lower()
    if g == "judge":
        crit = case.get("criterion", exp)
        verdict = await ask(
            mcp,
            f"КРИТЕРИЙ: {crit}\n\nОТВЕТ МОДЕЛИ:\n{answer}\n\nВердикт (PASS/FAIL):",
            profile="research", session="eval-judge", system=JUDGE_SYS,
        )
        return "PASS" in verdict.upper()
    return exp.lower() in answer.lower()  # contains


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--profile", default="research")
    args = ap.parse_args()

    cases = [json.loads(x) for x in open(args.dataset, encoding="utf-8") if x.strip()]
    passed = 0
    async with Client(os.environ["MCP_URL"], auth=BearerAuth(os.environ["MCP_TOKEN"])) as mcp:
        for i, case in enumerate(cases):
            ans = await ask(mcp, case["input"], args.profile, f"eval-{i}")
            ok = await grade(mcp, case, ans)
            passed += ok
            print(f"[{'PASS' if ok else 'FAIL'}] case {i}: {case['input'][:50]!r}")
    rate = 100 * passed / len(cases) if cases else 0
    print(f"\npass-rate: {passed}/{len(cases)} = {rate:.1f}%")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    asyncio.run(main())
