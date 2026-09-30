"""Лимиты прогона: настройка вместо константы в коде.

Параллелизм, длина ответа навыка, шаги инструментов и размер выборки задавались переменными окружения
сервера. Человек видел лимит ответа навыка в карточке только для чтения: изменить его было нельзя, и
откуда взялось число, тоже не было видно.

Лестница перекрытия: значение из кода → настройка среды → настройка навыка → узел этого агента.
"""
from __future__ import annotations

import asyncio
import json

from server import runner


def test_defaults_cover_every_field():
    d = runner.default_limits()
    assert set(d) == set(runner.LIMIT_FIELDS), "у каждого поля настройки есть значение по умолчанию"
    assert all(isinstance(v, int) and v >= 0 for v in d.values())


def test_setting_overrides_default():
    eff = runner.effective_limits({"concurrency": 5, "tool_steps": 0})
    assert eff["concurrency"] == 5
    assert eff["tool_steps"] == 0, "ноль шагов — осмысленное значение, а не «не задано»"


def test_out_of_range_value_is_clamped_not_swallowed():
    """Опечатка в поле не должна ломать прогоны: значение подрезается до границы."""
    eff = runner.effective_limits({"concurrency": 999, "max_tokens": 1})
    _, lo_c, hi_c = runner.LIMIT_FIELDS["concurrency"]
    _, lo_t, _ = runner.LIMIT_FIELDS["max_tokens"]
    assert eff["concurrency"] == hi_c and eff["max_tokens"] == lo_t


def test_garbage_and_empty_are_ignored():
    base = runner.default_limits()
    eff = runner.effective_limits({"concurrency": "", "retries": "два", "неизвестное": 5})
    assert eff["concurrency"] == base["concurrency"] and eff["retries"] == base["retries"]
    assert "неизвестное" not in eff


def test_free_form_never_gets_less_room_than_structured():
    eff = runner.effective_limits({"max_tokens": 4000, "max_tokens_free": 1000})
    assert eff["max_tokens_free"] >= eff["max_tokens"], "иначе нарратив обрывается на полуслове"


# ── Как лимиты действуют на прогон ───────────────────────────────────────────────────────────

def _run(chat_fn, *, limits=None, nodes=None, schemas=None, tool_loop=None):
    agent = {"id": "ag-l", "name": "Лимиты", "family": "audit",
             "graph": {"nodes": nodes or [{"id": "n1", "kind": "skill", "skill": "s1"}], "edges": []}}
    contract = {"audit_id": "t-l", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    return asyncio.run(runner.run_live(
        agent, contract, lambda sid: {"mode": "read", "egress": "internal", "cite": False},
        data_query=lambda e, **kw: [{"id": "d1", "тип": "Реализация"}],
        skill_sources=lambda sid: [{"entity": "doc1c"}],
        load_body=lambda sid: "методика " + sid,
        chat_fn=chat_fn, skill_schemas=schemas or {}, user_context="задача",
        tool_loop=tool_loop, limits=limits))


def test_applied_limits_are_visible_in_metrics():
    """«Почему ответ обрезан» должно читаться в результате, а не в исходниках."""
    async def chat(messages=None, **kw):
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    res = _run(chat, limits={"max_tokens": 900})
    lim = (res.get("run_metrics") or {}).get("limits") or {}
    assert lim.get("max_tokens") == 900 and lim.get("source") == "настройка"
    assert (_run(chat).get("run_metrics") or {})["limits"]["source"] == "по умолчанию"


def test_answer_limit_reaches_the_model_call():
    seen = {}

    async def chat(messages=None, **kw):
        seen["max_tokens"] = kw.get("max_tokens")
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    _run(chat, limits={"max_tokens": 777})
    assert seen["max_tokens"] == 777


def test_node_limit_wins_over_setting():
    """Узел агента — самый точный уровень: его значение перекрывает общую настройку."""
    seen = {}

    async def chat(messages=None, **kw):
        seen["max_tokens"] = kw.get("max_tokens")
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    _run(chat, limits={"max_tokens": 777},
         nodes=[{"id": "n1", "kind": "skill", "skill": "s1", "max_tokens": 1500}])
    assert seen["max_tokens"] == 1500


def test_tool_steps_ladder_and_honest_zero():
    """Шаги инструментов: среда → навык → узел. Ноль означает «не ходить», а не «взять сверху»."""
    calls = []

    async def fake_tool_loop(sid, head, chat_fn, **kw):
        calls.append(kw.get("steps"))
        return "", []

    async def chat(messages=None, **kw):
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    _run(chat, limits={"tool_steps": 3}, tool_loop=fake_tool_loop)
    assert calls[-1] == 3, "значение среды"

    _run(chat, limits={"tool_steps": 3}, tool_loop=fake_tool_loop,
         schemas={"s1": {"tool_steps": 1}})
    assert calls[-1] == 1, "настройка навыка перекрывает среду"

    _run(chat, limits={"tool_steps": 3}, tool_loop=fake_tool_loop,
         schemas={"s1": {"tool_steps": 1}},
         nodes=[{"id": "n1", "kind": "skill", "skill": "s1", "tool_steps": 2}])
    assert calls[-1] == 2, "узел агента точнее навыка"

    _run(chat, limits={"tool_steps": 3}, tool_loop=fake_tool_loop,
         schemas={"s1": {"tool_steps": 0}})
    assert calls[-1] == 0, "ноль не проваливается на уровень выше"


def test_sample_size_limit_applies_to_the_prompt():
    """Сколько записей уходит модели — настройка, а не константа: на больших наборах это цена прогона."""
    seen = {}

    async def chat(messages=None, **kw):
        seen["body"] = "".join(str(m.get("content")) for m in (messages or []))
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    agent = {"id": "ag-l2", "name": "Выборка", "family": "audit",
             "graph": {"nodes": [{"id": "n1", "kind": "skill", "skill": "s1"}], "edges": []}}
    contract = {"audit_id": "t-l", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    rows = [{"id": f"d{i}", "тип": "Реализация"} for i in range(40)]
    asyncio.run(runner.run_live(
        agent, contract, lambda sid: {"mode": "read", "egress": "internal", "cite": False},
        data_query=lambda e, **kw: list(rows),
        skill_sources=lambda sid: [{"entity": "doc1c"}],
        load_body=lambda sid: "методика", chat_fn=chat, user_context="задача",
        limits={"full_rows": 10, "sample": 2}))
    body = seen["body"]
    assert '"всего": 40' in body, "полный объём выборки модель видит числом"
    assert '"сэмпл"' in body and '"записи"' not in body, "сорок записей больше порога — идёт сэмпл"
    assert body.count('"id": "d') == 2, "в промпт ушло ровно столько записей, сколько задано настройкой"


def test_empty_limits_behave_like_no_setting():
    async def chat(messages=None, **kw):
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    a = (_run(chat).get("run_metrics") or {})["limits"]
    b = (_run(chat, limits={}).get("run_metrics") or {})["limits"]
    assert {k: v for k, v in a.items() if k != "source"} == {k: v for k, v in b.items() if k != "source"}


def test_limits_survive_json_round_trip():
    """Лимиты едут в задание через очередь: несериализуемое значение потеряло бы прогон."""
    eff = runner.effective_limits({"concurrency": 4})
    assert json.loads(json.dumps(eff)) == eff


def test_zero_tool_steps_survives_the_store():
    """Ноль шагов должен сохраняться как ноль: «or» превращал его в пусто и терял настройку."""
    from server import schema_store

    async def flow():
        card = await schema_store.save("t-zero", {"name": "тест", "json_schema": {},
                                                  "instruction": "и", "tool_steps": 0})
        assert card["tool_steps"] == 0, "ноль — это настройка «в инструменты не ходить»"
        back = await schema_store.save("t-zero", {"name": "тест", "json_schema": {},
                                                  "instruction": "и", "tool_steps": ""})
        assert back["tool_steps"] is None, "пусто — вернуть к значению среды"
    asyncio.run(flow())
