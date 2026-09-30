"""Этап 1: граф исполняется по волнам, выход навыка доезжает до следующего по контракту.

До этого волны считались, но прогон запускал все навыки одним gather: рёбра графа ни на что не влияли,
и передать результат одного навыка другому было нельзя.
"""
from __future__ import annotations

import asyncio
import json

from server import runner


def _contract(sid_from: str, path: str, fields: list[str]) -> dict:
    return {"inputs": {"required": [{"from": "skill", "skill": sid_from, "path": path, "fields": fields}]}}


def _run(agent, schemas, chat_fn, data=None):
    contract = {"audit_id": "t-w", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    return asyncio.run(runner.run_live(
        agent, contract, lambda sid: {"mode": "read", "egress": "internal", "cite": False},
        data_query=lambda e, **kw: (data or {}).get(e, []),
        skill_sources=lambda sid: [{"entity": "doc1c"}] if sid == "first" else [],
        load_body=lambda sid: "методика " + sid,
        chat_fn=chat_fn, skill_schemas=schemas, user_context="задача оператора"))


def test_waves_order_and_payload_pass_through():
    """Второй навык получает объявленный список первого, а не весь его результат."""
    seen: dict[str, str] = {}

    async def chat_fn(messages=None, **kw):
        prompt = messages[-1]["content"]
        sid = "second" if "методика second" in json.dumps(messages, ensure_ascii=False) else "first"
        seen[sid] = prompt
        if sid == "first":
            return {"text": json.dumps({"находки": [{"id": "A1", "класс": "A", "лишнее": "мусор"}],
                                        "итог": "есть"}, ensure_ascii=False),
                    "model": "local/test", "input_tokens": 1, "output_tokens": 1}
        return {"text": json.dumps({"выводы": ["по A1"], "итог": "готово"}, ensure_ascii=False),
                "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    agent = {"id": "ag-w", "name": "Волны", "family": "audit", "graph": {
        "nodes": [{"id": "n1", "kind": "skill", "skill": "first"},
                  {"id": "n2", "kind": "skill", "skill": "second"}],
        "edges": [{"from": "n1", "to": "n2"}]}}
    schemas = {
        "first": {"instruction": "найди", "template_id": "first",
                  "produces": {"path": "находки", "key": "id"}},
        "second": dict(_contract("first", "находки", ["id", "класс"]),
                       instruction="объясни", template_id="second"),
    }
    res = _run(agent, schemas, chat_fn, {"doc1c": [{"id": "d1", "тип": "Реализация"}]})

    assert res["run_metrics"].get("waves") == 2, "две волны по рёбрам графа"
    assert "second" in seen, "второй навык должен запуститься"
    body = seen["second"]
    assert "ВХОД ОТ НАВЫКА «first»" in body, "во входе должен быть блок от первого навыка"
    assert "A1" in body, "объявленное поле должно доехать"
    assert "мусор" not in body, "необъявленные поля не передаются"


def test_skill_skipped_when_upstream_missing():
    """Навык с непокрытым обязательным входом не идёт в модель, а честно помечается пропущенным."""
    calls: list[str] = []

    async def chat_fn(messages=None, **kw):
        calls.append("call")
        return {"text": json.dumps({"выводы": []}, ensure_ascii=False), "model": "local/test",
                "input_tokens": 1, "output_tokens": 1}

    agent = {"id": "ag-gap", "name": "Разрыв", "family": "audit", "graph": {
        "nodes": [{"id": "n2", "kind": "skill", "skill": "second"}], "edges": []}}
    schemas = {"second": dict(_contract("first", "находки", ["id"]),
                              instruction="объясни", template_id="second")}
    res = _run(agent, schemas, chat_fn)

    assert not calls, "модель не должна вызываться без входа"
    fnd = res.get("findings") or []
    assert fnd and fnd[0].get("skipped"), "навык помечен пропущенным"
    assert "не готов вход" in fnd[0]["text"], fnd[0]["text"]


def test_no_contract_keeps_old_behaviour():
    """Навыки без контракта работают как раньше: один уровень, оба запускаются."""
    calls: list[str] = []

    async def chat_fn(messages=None, **kw):
        calls.append("c")
        return {"text": json.dumps({"находки": [], "итог": "ок"}, ensure_ascii=False),
                "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    agent = {"id": "ag-plain", "name": "Без контракта", "family": "audit", "graph": {
        "nodes": [{"id": "n1", "kind": "skill", "skill": "first"},
                  {"id": "n2", "kind": "skill", "skill": "second"}], "edges": []}}
    res = _run(agent, {}, chat_fn, {"doc1c": [{"id": "d1"}]})
    assert len(calls) == 2, "оба навыка запускаются"
    assert res["run_metrics"].get("waves") == 1, "без рёбер — одна волна"
