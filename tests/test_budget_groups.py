"""Этап 5: бюджет прогона с честной остановкой и группы заданий.

До этого стоимость считалась постфактум: прогон мог израсходовать сколько угодно, а единственной
защитой был общий таймаут задания, который читается как «прогон не выполнен». Отмена работала по
одному заданию, поэтому веер из десятков ветвей нельзя было остановить целиком.
"""
from __future__ import annotations

import asyncio
import json

from server import run_queue, runner


def _run(agent, chat_fn, budget=None, schemas=None):
    contract = {"audit_id": "t-b", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    return asyncio.run(runner.run_live(
        agent, contract, lambda sid: {"mode": "read", "egress": "internal", "cite": False},
        data_query=lambda e, **kw: [{"id": "d1", "тип": "Реализация"}],
        skill_sources=lambda sid: [{"entity": "doc1c"}],
        load_body=lambda sid: "методика " + sid,
        chat_fn=chat_fn, skill_schemas=schemas or {}, user_context="задача", budget=budget))


def _agent(n: int, chain: bool = False) -> dict:
    """chain=True — навыки идут волнами друг за другом: бюджет останавливает работу между волнами."""
    nodes = [{"id": f"n{i}", "kind": "skill", "skill": f"s{i}"} for i in range(1, n + 1)]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(1, n)] if chain else []
    return {"id": "ag-b", "name": "Бюджет", "family": "audit", "graph": {"nodes": nodes, "edges": edges}}


async def _chat(messages=None, **kw):
    return {"text": json.dumps({"находки": [{"id": "A"}], "итог": "ок"}, ensure_ascii=False),
            "model": "local/test", "input_tokens": 500, "output_tokens": 500}


def test_no_budget_runs_everything():
    res = _run(_agent(3), _chat)
    fnd = res.get("findings") or []
    assert len(fnd) == 3 and not any(f.get("budget_stop") for f in fnd)
    assert "budget" not in (res.get("run_metrics") or {}), "без лимита блок бюджета не нужен"


def test_token_budget_stops_and_marks():
    """Лимит токенов останавливает работу и помечает, какие навыки не пошли."""
    res = _run(_agent(4, chain=True), _chat, budget={"max_tokens": 1500})
    fnd = res.get("findings") or []
    stopped = [f for f in fnd if f.get("budget_stop")]
    ran = [f for f in fnd if not f.get("budget_stop")]
    assert ran, "первый навык должен успеть"
    assert stopped, "после исчерпания лимита навыки помечаются"
    assert len(ran) <= 2, "лимита хватает на один-два навыка, не больше"
    assert "бюджет токенов исчерпан" in stopped[0]["text"]
    b = (res.get("run_metrics") or {}).get("budget") or {}
    assert b.get("limit", {}).get("max_tokens") == 1500
    assert b.get("spent", {}).get("tokens", 0) >= 1000
    assert b.get("stopped_skills"), "остановленные навыки перечислены в метриках"


def test_time_budget_present_in_metrics():
    res = _run(_agent(2), _chat, budget={"max_sec": 600})
    b = (res.get("run_metrics") or {}).get("budget") or {}
    assert b.get("limit", {}).get("max_sec") == 600
    assert "sec" in (b.get("spent") or {})


def test_group_cancel_and_status():
    """Группа отменяется целиком, статус показывает состав."""
    async def flow():
        gid = "grp-test"
        a = await run_queue.enqueue(agent_id="ag1", actor="t", payload={}, group_id=gid)
        b = await run_queue.enqueue(agent_id="ag2", actor="t", payload={}, group_id=gid)
        other = await run_queue.enqueue(agent_id="ag3", actor="t", payload={})
        st = await run_queue.group_status(gid)
        assert st["jobs"] == 2, st
        n = await run_queue.cancel_group(gid)
        assert n == 2, "оба задания группы отменены"
        assert run_queue.cancel_requested(a["id"]) and run_queue.cancel_requested(b["id"])
        assert not run_queue.cancel_requested(other["id"]), "чужое задание не тронуто"
        empty = await run_queue.group_status("нет-такой")
        assert empty["jobs"] == 0
    asyncio.run(flow())
