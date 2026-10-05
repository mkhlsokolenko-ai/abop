# -*- coding: utf-8 -*-
"""Готовые части прогона переживают таймаут попытки.

05.10 владелец: «для длинных и многошаговых агентов вылетаем по таймауту, надо кешировать между —
отдал одну часть → ушло в кэш, и так до конца, затем вычитываем из кеша и сводим в отчёт, после
инвалидация кеша».

До этого попытка задания жила `ABOP_RUN_TIMEOUT` (600 с) и по таймауту задание ПАДАЛО: пять уже
отработавших навыков и оплаченные ими токены пропадали вместе с ней. Теперь каждая готовая часть
сразу ложится в кэш задания, следующая попытка берёт её оттуда и считает только недостающее, а после
сведения отчёта кэш гасится — он живёт ровно столько, сколько идёт прогон.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from server import run_parts_store, runner


def _agent(*skills):
    return {"id": "ag-parts", "name": "Длинный", "family": "audit", "version": 3, "graph": {
        "nodes": [{"id": f"n{i}", "kind": "skill", "skill": s} for i, s in enumerate(skills, 1)],
        "edges": []}}


def _run(agent, chat_fn, parts=None):
    contract = {"audit_id": "t-parts", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    return asyncio.run(runner.run_live(
        agent, contract, lambda sid: {"mode": "read", "egress": "internal", "cite": False},
        data_query=lambda e, **kw: [],
        skill_sources=lambda sid: [],
        load_body=lambda sid: "методика " + sid,
        chat_fn=chat_fn, skill_schemas={s: {"instruction": "сделай", "template_id": s}
                                        for s in ("alpha", "beta", "gamma")},
        user_context="длинная задача", parts=parts))


def _answer(sid):
    return {"text": json.dumps({"находки": [{"наблюдение": "итог " + sid}], "итог": sid},
                               ensure_ascii=False),
            "model": "local/test", "input_tokens": 10, "output_tokens": 5}


# ── хранилище частей ──
def test_часть_кладётся_и_читается():
    async def go():
        await run_parts_store.drop("job-1")
        assert await run_parts_store.save("job-1", "alpha", {"skill": "alpha", "text": "готово"}, rev="r1")
        got = await run_parts_store.load("job-1", rev="r1")
        assert got["alpha"]["text"] == "готово"
        assert await run_parts_store.drop("job-1") >= 1
        assert await run_parts_store.load("job-1", rev="r1") == {}
    asyncio.run(go())


def test_части_от_другой_версии_агента_не_берутся():
    """Агента правили между попытками — значит методика другая, и старый результат уже не про неё."""
    async def go():
        await run_parts_store.drop("job-2")
        await run_parts_store.save("job-2", "alpha", {"skill": "alpha"}, rev="старая")
        assert await run_parts_store.load("job-2", rev="новая") == {}
        await run_parts_store.drop("job-2")
    asyncio.run(go())


def test_отпечаток_меняется_вместе_с_графом():
    a = _agent("alpha", "beta")
    b = _agent("alpha", "beta")
    assert run_parts_store.rev_of(a) == run_parts_store.rev_of(b), "одинаковые агенты — один отпечаток"
    c = _agent("alpha", "gamma")
    assert run_parts_store.rev_of(a) != run_parts_store.rev_of(c), "правка графа должна обесценить части"


def test_огромная_часть_не_кладётся():
    """Кэш страхует попытку, а не хранит всё подряд: гигантский ответ пересчитаем."""
    async def go():
        big = {"skill": "alpha", "text": "ы" * (run_parts_store.MAX_PART_BYTES)}
        assert await run_parts_store.save("job-3", "alpha", big) is False
        assert await run_parts_store.load("job-3") == {}
    asyncio.run(go())


# ── рантайм ──
def test_готовая_часть_не_считается_заново():
    """Главное: навык из кэша НЕ зовёт модель, но его результат попадает в прогон."""
    called: list[str] = []

    async def chat_fn(messages=None, **kw):
        body = json.dumps(messages, ensure_ascii=False)
        sid = next(s for s in ("alpha", "beta", "gamma") if "методика " + s in body)
        called.append(sid)
        return _answer(sid)

    ready = {"alpha": {"skill": "alpha", "entities": [], "model": "local/test", "text": "итог alpha (из кэша)",
                       "structured": {"итог": "alpha"}, "input_tokens": 0, "output_tokens": 0}}
    saved: dict[str, dict] = {}

    async def save(sid, res):
        saved[sid] = res

    res = _run(_agent("alpha", "beta"), chat_fn, parts={"done": ready, "save": save})

    assert called == ["beta"], f"модель звали не только для недостающего навыка: {called}"
    assert saved and "beta" in saved, "новая часть не отложена в кэш"
    texts = " ".join(str(b.get("text") or "") for b in (res.get("board") or []))
    assert "из кэша" in texts, "готовая часть не попала в прогон — отчёт соберётся неполным"


def test_без_кэша_ничего_не_меняется():
    """Прогон без кэша работает как раньше: кэш — страховка, а не условие работы."""
    called: list[str] = []

    async def chat_fn(messages=None, **kw):
        body = json.dumps(messages, ensure_ascii=False)
        sid = next(s for s in ("alpha", "beta", "gamma") if "методика " + s in body)
        called.append(sid)
        return _answer(sid)

    _run(_agent("alpha", "beta"), chat_fn)
    assert sorted(called) == ["alpha", "beta"]


def test_сбой_записи_в_кэш_не_валит_прогон():
    async def chat_fn(messages=None, **kw):
        return _answer("alpha")

    async def save(sid, res):
        raise RuntimeError("база недоступна")

    res = _run(_agent("alpha"), chat_fn, parts={"done": {}, "save": save})
    assert res.get("board") is not None, "сбой кэша не должен ронять прогон"


# ── очередь ──
def test_таймаут_возвращает_задание_в_очередь():
    """Пока попытки не исчерпаны, таймаут — повод продолжить с готовых частей, а не упасть."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "server" / "run_queue.py").read_text(encoding="utf-8")
    i = src.index("except asyncio.TimeoutError:")
    body = src[i:i + 1100]
    assert "requeue=rq" in body, "таймаут снова роняет задание вместо возврата в очередь"
    assert "MAX_ATTEMPTS" in body, "нет предела попыток — задание может крутиться вечно"
    assert "on_error and not rq" in body, "человеку сообщают об ошибке даже когда работа продолжится"
