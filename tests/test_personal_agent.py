"""Личный агент: чем он ограничен, если контракта у него нет.

У системного агента конверт объявлен контрактом LUDA. Личный агент (семья + навыки, без контракта)
получает конверт ВЫВЕДЕННЫЙ — по строжайшему из навыков. Проверка 01.10 показала, что этот механизм
работал, но две вещи рядом с ним — нет: покрытие входов в авторинге не считалось вовсе, а граф
собирался без рёбер, из-за чего навыки шли одной волной и объявленный вход от предыдущего навыка не
мог быть выполнен в принципе.
"""
from __future__ import annotations

import asyncio

from cli import ape
from server import runner, web_api


def test_envelope_is_derived_from_skills_not_declared():
    """Конверт личного агента — строжайший по совокупности навыков."""
    only_read = ape._envelope_of(["audit1c-checks"])
    assert only_read["mode"] == "read"
    assert ape._autonomy_for(only_read) == "A2"

    # «написать письмо» — это write/internal: письмо только готовится, уходит оно OUT-узлом под
    # подтверждением. Потолок понижает именно действие наружу или выход во внешнюю сеть.
    drafting = ape._envelope_of(["client-letter"])
    assert drafting["mode"] == "write" and ape._autonomy_for(drafting) == "A2"

    acting = ape._envelope_of(["to-tickets"])             # заводит задачи в трекере
    assert acting["mode"] == "action"
    assert ape._autonomy_for(acting) == "A1", "действие наружу обязано требовать подтверждения"


def test_personal_agent_never_exceeds_a2():
    """A3/A4 личному агенту не выдаются: такую автономию даёт только контракт."""
    for skills in (["audit1c-checks"], ["mail-triage", "daily-plan"], ["client-letter"]):
        env = ape._envelope_of([s for s in skills if s in ape.SKILLS])
        assert ape._autonomy_for(env) in ("A1", "A2")


def test_envelope_reason_is_said_in_words():
    """Правило объясняется человеку, иначе выглядит произволом системы."""
    assert "A1" in web_api._envelope_reason({"mode": "action", "egress": "internal"})
    assert "подтверждения" in web_api._envelope_reason({"mode": "action", "egress": "internal"})
    assert "A1" in web_api._envelope_reason({"mode": "read", "egress": "external"})
    assert "A2" in web_api._envelope_reason({"mode": "read", "egress": "internal"})


def test_authored_graph_is_a_chain_not_a_heap():
    """Навыки связаны рёбрами: без них все идут одной волной и не видят друг друга."""
    nodes = [{"id": "a", "kind": "skill", "skill": "a"},
             {"id": "b", "kind": "skill", "skill": "b"},
             {"id": "c", "kind": "skill", "skill": "c"}]
    edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"]} for i in range(len(nodes) - 1)]
    waves = runner._waves(nodes, edges)
    assert [len(w) for w in waves] == [1, 1, 1], "цепочка должна идти по шагам, а не одной волной"

    one_wave = runner._waves(nodes, [])
    assert len(one_wave) == 1 and len(one_wave[0]) == 3, \
        "без рёбер раскладка кладёт всё в одну волну — ровно та ошибка, что была в авторинге"


def test_supplier_goes_before_consumer():
    """Поставщик объявленного входа поднимается выше приёмника, даже если кликнули наоборот."""
    from server import schema_store
    schema_store._MEM["объяснение"] = {"id": "объяснение", "inputs": {"required": [
        {"from": "skill", "skill": "проверки", "path": "находки"}]}}
    schema_store._MEM["проверки"] = {"id": "проверки", "produces": {"path": "находки", "key": "id"}}
    nodes = [{"id": "объяснение", "kind": "skill", "skill": "объяснение"},
             {"id": "проверки", "kind": "skill", "skill": "проверки"}]
    ordered = asyncio.run(web_api._order_by_contract(nodes))
    ids = [n["skill"] for n in ordered]
    assert ids.index("проверки") < ids.index("объяснение"), ids
