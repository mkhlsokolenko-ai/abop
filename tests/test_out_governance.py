"""Узел вывода под governance: снять человека можно, но тогда сузить канал данных.

ADR-014 требовал подтверждения у навыка-действия, а отправляет результат узел ДОСТАВКИ — его никто
не проверял. На нём две галочки, «подтверждение» и «реальная отправка»: снял первую, включил вторую —
письмо уходит наружу само.

Полагаться на то, что человек будет щёлкать подтверждения, нельзя: он устанет и отключит их, и рычаг
для этого система даёт сама. Поэтому рычаги связаны обратно — безнадзорная отправка допустима, только
если право дано контрактом (A3+) и действующий шаг не читает данные напрямую.
"""
from __future__ import annotations

from server import assembly

SAFE = {"reader": {"mode": "read", "egress": "internal"},
        "actor": {"mode": "action", "egress": "external"}}


def safety_of(sid):
    return SAFE.get(sid, {"mode": "read", "egress": "internal"})


def graph(autonomy="A1", hitl_out=False, actor_reads=True, channel="email"):
    return {"nodes": [{"id": "s1", "kind": "skill", "skill": "actor", "autonomy": autonomy, "hitl": True},
                      {"id": "o1", "kind": "out", "title": "Письмо",
                       "out": {"channel": channel, "hitl": hitl_out, "run": "true"}}],
            "edges": [{"from": "s1", "to": "o1"}]}


def check(g, ceiling="A4", reads=None):
    return assembly.check_graph(g, {"autonomy_ceiling": ceiling}, safety_of, reads_data=reads)


def test_external_output_without_confirmation_is_refused_at_low_autonomy():
    """Главный случай: две галочки на узле вывода снимали человека, и никто не возражал."""
    errs = check(graph(autonomy="A1", hitl_out=False), reads=set())["errors"]
    assert any("без подтверждения требует автономии A3" in e for e in errs), errs


def test_confirmation_makes_it_legal_at_any_autonomy():
    """С подтверждением человек в контуре — ограничения на данные не нужны."""
    assert check(graph(autonomy="A1", hitl_out=True), reads={"actor"})["errors"] == []


def test_high_autonomy_still_needs_quarantine():
    """Право действовать без человека не отменяет требования к каналу данных: оно его и вводит."""
    errs = check(graph(autonomy="A3", hitl_out=False), reads={"actor"})["errors"]
    assert any("не должен читать данные напрямую" in e for e in errs), errs
    assert any("actor" in e for e in errs), "ошибка обязана назвать виновный навык"


def test_high_autonomy_with_quarantine_is_allowed():
    """Перестроил граф через читающий навык — безнадзорная отправка разрешена."""
    assert check(graph(autonomy="A3", hitl_out=False), reads={"reader"})["errors"] == []


def test_unknown_contracts_mean_quarantine_is_not_proven():
    """Нечем доказать карантин — значит его нет. Умолчание в пользу человека, а не автоматики."""
    errs = check(graph(autonomy="A3", hitl_out=False), reads=None)["errors"]
    assert any("не должен читать данные напрямую" in e for e in errs), errs


def test_local_channels_are_free():
    """Файл и PDF никуда не уезжают — гейтить нечего."""
    for ch in ("pdf", "file", "chat"):
        assert check(graph(autonomy="A1", hitl_out=False, channel=ch), reads=set())["errors"] == [], ch


def test_confirmed_output_counts_toward_hitl():
    """Подтверждение на выводе — это точка HITL, и она должна считаться как точка."""
    assert check(graph(autonomy="A1", hitl_out=True), reads=set())["hitl_count"] >= 2


def test_rule_does_not_touch_graphs_without_output():
    """Агент без доставки правилом не затронут: отправлять нечего."""
    g = {"nodes": [{"id": "s1", "kind": "skill", "skill": "actor", "autonomy": "A1", "hitl": True}], "edges": []}
    assert check(g, reads={"actor"})["errors"] == []
