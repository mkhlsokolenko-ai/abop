# -*- coding: utf-8 -*-
"""Блок 3: единый tool-calling навыков, DLQ-хук воркера, события шины → триггеры (in-memory, без брокера)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "ABOP_BUS", "ABOP_KAFKA_BROKERS"):
    os.environ.pop(k, None)


def test_tools_declared_for_every_skill():
    from cli import ape
    from server import skill_tools as st
    for sid in ape.SKILLS:
        tools = st.tools_for(sid)
        assert tools, sid
        assert all(t in st.DESCRIPTIONS for t in tools), (sid, tools)
    assert "audit1c_checks" in st.tools_for("audit1c-checks")
    assert "email_send" in st.tools_for("email-draft")


def test_parse_action_and_dry_run():
    from server import skill_tools as st
    assert st.parse_action('```json\n{"tool":"data_schema","args":{"entity":"doc1c"}}\n```')["tool"] == "data_schema"
    assert st.parse_action("текст без json") is None
    assert "final" in st.parse_action('{"final": true}')

    async def flow():
        # действие без run:true → dry_run; чужой инструмент → отказ; шина не подключена → честный ответ
        out = await st.run_tool("email-draft", "email_send", {"to": "a@b"}, safety={"mode": "write"})
        assert out.startswith("dry_run")
        out = await st.run_tool("email-draft", "email_send", {"to": "a@b", "run": True}, safety={"mode": "read"})
        assert out.startswith("dry_run")
        out = await st.run_tool("dcf-valuation", "email_send", {}, safety={"mode": "write"})
        assert "не разрешён" in out
        out = await st.run_tool("to-tickets", "bus_publish", {"system": "redmine", "type": "x", "run": True}, safety={"mode": "action"})
        assert "не подключена" in out
    asyncio.run(flow())


def test_tool_loop_uniform_pattern():
    from server import skill_tools as st
    calls = []

    async def chat_fn(messages, profile="standard", max_tokens=400, response_format=None):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            return {"text": '{"tool":"data_schema","args":{"entity":"doc1c"}}', "input_tokens": 10, "output_tokens": 5, "model": "m"}
        return {"text": '{"final": true}', "input_tokens": 1, "output_tokens": 1, "model": "m"}

    async def flow():
        block, log = await st.tool_loop("audit1c-checks", "=== МЕТОДИКА ===\n...\n", chat_fn, safety={"mode": "read"}, steps=3)
        assert len(log) == 1 and log[0]["tool"] == "data_schema"
        assert "НАБЛЮДЕНИЯ ИНСТРУМЕНТОВ" in block and "data_schema" in block
        assert "ИНСТРУМЕНТЫ НАВЫКА" in calls[0] and "audit1c_checks" in calls[0]
        assert len(calls) == 2   # 1 вызов + final, не 3
    asyncio.run(flow())


def test_worker_on_error_hook():
    from server import run_queue
    seen = []

    async def handler(job):
        raise RuntimeError("boom")

    async def on_error(job, err):
        seen.append(err)

    async def flow():
        run_queue.MAX_ATTEMPTS = 1
        job = await run_queue.enqueue(agent_id="a", actor="dev", payload={}, kind="run")
        task = asyncio.create_task(run_queue.worker_loop("t-w0", handler, on_error=on_error))
        for _ in range(60):
            await asyncio.sleep(0.1)
            j = await run_queue.get(job["id"])
            if j and j["status"] == "failed":
                break
        task.cancel()
        assert (await run_queue.get(job["id"]))["status"] == "failed"
        assert seen and "boom" in seen[0]
    asyncio.run(flow())


def test_bus_event_fires_event_trigger():
    from server import agent_store, systems_store, triggers
    fired = []

    async def executor(agent, contract, started_by, *, trigger=None, user_context="", **kw):
        fired.append((agent["id"], started_by, user_context))
        return {"saved": {"id": "run-x"}}

    async def flow():
        await systems_store.save("redmine", {"kind": "rest", "base_url": "http://x", "scope": []})
        graph = {"nodes": [{"id": "trg1", "kind": "trigger", "title": "по событию",
                            "trig": {"type": "event", "source": "redmine", "enabled": True, "spawn": {"autonomy_max": "A1"}}}]}
        a = await agent_store.save(name="Тест", audit_id="t-ev", version=1, graph=graph, autonomy_max="A1", family="management", role="pm")
        res = await triggers.on_bus_event("redmine", {"type": "issue.created", "payload": {"id": 7}}, executor)
        assert res and res[0]["status"] == "fired", res
        assert fired and "СОБЫТИЕ ИЗ ШИНЫ" in fired[0][2] and '"id": 7' in fired[0][2]
        # другая система — не стреляет
        assert await triggers.on_bus_event("bookstack", {"type": "x"}, executor) == []
    asyncio.run(flow())
