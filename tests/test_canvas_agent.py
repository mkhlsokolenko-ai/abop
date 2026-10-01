"""Агент, собранный на канве, доезжает до сервера целиком.

Случай с демо 01.10: человек построил агента из задачи, добавил блок подтверждения и блок вывода,
нажал запуск — «исполняется», а подтверждение не спрашивается. На стенде этого агента не оказалось
вовсе: сохранить граф без контракта LUDA было некуда, авторинг строил граф сам из списка навыков и
выбрасывал всё, кроме них, — вывод и подтверждение до прогона не доезжали. А заявку HITL создаёт
именно узел вывода.

Здесь проверяется серверная половина: граф с канвы сохраняется как есть, но правила остаются за
средой.
"""
from __future__ import annotations

from server import web_api

ENV_MAX = "A1"
SKILLS = [
    {"id": "mail-triage", "safety": {"mode": "read", "egress": "internal"}},
    {"id": "to-tickets", "safety": {"mode": "action", "egress": "external"}},
]
CANVAS = {
    "nodes": [
        {"id": "src", "kind": "source", "title": "Почта", "entity": "email"},
        {"id": "s1", "kind": "skill", "skill": "mail-triage", "title": "mail-triage"},
        {"id": "s2", "kind": "skill", "skill": "to-tickets", "title": "to-tickets", "autonomy": "A4"},
        {"id": "ask", "kind": "hitl", "title": "Подтверждение оператора"},
        {"id": "out", "kind": "out", "title": "Письмо руководителю", "out": {"channel": "email", "hitl": True}},
    ],
    "edges": [
        {"from": "src", "to": "s1"}, {"from": "s1", "to": "s2"},
        {"from": "s2", "to": "ask"}, {"from": "ask", "to": "out"},
        {"from": "s2", "to": "призрак"},
    ],
}


def built() -> dict:
    return web_api._graph_from_canvas(CANVAS, SKILLS, ENV_MAX, {})


def test_all_kinds_survive_not_only_skills():
    """Источник, подтверждение и вывод остаются: ими агент и отличается от списка навыков."""
    kinds = [n["kind"] for n in built()["nodes"]]
    assert kinds == ["source", "skill", "skill", "hitl", "out"], kinds


def test_out_node_keeps_its_delivery_and_gate():
    """Узел вывода довозит канал и подтверждение — заявку HITL создаёт именно он."""
    out = [n for n in built()["nodes"] if n["kind"] == "out"][0]
    assert out["out"]["channel"] == "email"
    assert out["out"]["hitl"] is True, "без этого прогон отправит результат молча"


def test_autonomy_is_capped_by_envelope_whatever_is_drawn():
    """A4 на узле не даёт A4 агенту: потолок считает среда по навыкам, а не человек на канве."""
    assert all(n.get("autonomy") == ENV_MAX for n in built()["nodes"] if n["kind"] == "skill")


def test_external_action_is_always_under_confirmation():
    """ADR-014: действие наружу под подтверждением, даже если на канве галку не ставили."""
    by = {n["id"]: n for n in built()["nodes"]}
    assert by["s2"]["hitl"] is True
    assert not by["s1"].get("hitl"), "читающий навык подтверждения не требует"


def test_edges_to_missing_nodes_are_dropped():
    """Ребро в несуществующий узел ломает расчёт волн — его отбрасываем."""
    ids = {n["id"] for n in built()["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in built()["edges"])
    assert len(built()["edges"]) == 4


def test_unknown_skill_is_not_saved():
    """Навык, которого нет в каталоге, в агента не попадает: исполнять его нечем."""
    g = web_api._graph_from_canvas(
        {"nodes": [{"id": "x", "kind": "skill", "skill": "нет-такого"}], "edges": []}, SKILLS, ENV_MAX, {})
    assert g["nodes"] == []


def test_run_id_exists_before_the_run_is_saved():
    """Заявку на подтверждение создаёт доставка — внутри прогона, до его записи.

    Пока идентификатор выдавался только при сохранении, каждая заявка уходила в очередь с пустой
    ссылкой на прогон: видно, что чего-то ждут, но непонятно, чего именно. Теперь идентификатор
    выдаётся заранее, и запись его уважает, а не присваивает свой.
    """
    import asyncio

    from server import run_store

    rid = asyncio.run(run_store.new_id("agent-x"))
    assert rid.startswith("run-agent-x-")

    saved = asyncio.run(run_store.save({"agent_id": "agent-x", "id": rid, "verdict": {"ok": True}}))
    assert saved["id"] == rid, "запись обязана уважать заранее выданный идентификатор"

    auto = asyncio.run(run_store.save({"agent_id": "agent-x", "verdict": {"ok": True}}))
    assert auto["id"] != rid and auto["id"].startswith("run-agent-x-"), "без заранее выданного — свой"


def test_autosave_overwrites_the_draft_instead_of_minting_versions():
    """ADR-024: автосейв канвы перезаписывает черновик.

    Канва сохраняет агента через секунду после каждого движения мышью. Пока сохранение брало
    next_version, на стенде выросло двадцать версий одного «Финаналитика» — список агентов
    превращался в историю правок, а найти в нём рабочего агента было нельзя.
    """
    import asyncio

    from server import agent_store

    aid = "authored-test-черновик"
    first = asyncio.run(agent_store.save_draft(name="Черновик", audit_id=aid, graph={"nodes": []},
                                               autonomy_max="A1", source="authored"))
    again = asyncio.run(agent_store.save_draft(name="Черновик", audit_id=aid,
                                               graph={"nodes": [{"id": "n1"}]},
                                               autonomy_max="A1", source="authored"))
    assert again["id"] == first["id"], "второе сохранение обязано лечь в тот же черновик"
    assert again["version"] == first["version"]
    assert (again.get("graph") or {}).get("nodes"), "но содержимое — новое"


def test_tested_version_is_not_overwritten():
    """Проверенную версию автосейв не трогает: иначе правка затёрла бы то, что уже приняли."""
    import asyncio

    from server import agent_store

    aid = "authored-test-проверенный"
    v1 = asyncio.run(agent_store.save_draft(name="Агент", audit_id=aid, graph={"nodes": []},
                                            autonomy_max="A1", source="authored"))
    asyncio.run(agent_store.set_status(v1["id"], "tested"))
    v2 = asyncio.run(agent_store.save_draft(name="Агент", audit_id=aid, graph={"nodes": [{"id": "x"}]},
                                            autonomy_max="A1", source="authored"))
    assert v2["id"] != v1["id"] and v2["version"] == v1["version"] + 1
