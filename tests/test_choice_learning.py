# -*- coding: utf-8 -*-
"""Подбор учится на решениях человека — но не замыкается на них.

Владелец 05.10: «нет обучения на исходе. Мы не используем историю прогонов: какой подбор человек
принял, какой отменил, что он правил руками». Данные были — нажатия «собрать и запустить», «не
запускать», правка шага карандашом, — но нигде не сходились.

Правило, которое здесь закреплено: прошлый выбор решает НИЧЬЮ, но не назначает исполнителя. Прибавка
маленькая, с потолком ниже планки уверенного подбора; отмена весит больше согласия (её делают
осознанно, а соглашаются часто по инерции); предпочтения персональные.
"""
from __future__ import annotations

import asyncio

from server import choice_store, planner


def _hist(rows):
    return choice_store._score(rows)


def test_взятое_в_плюс_отменённое_в_минус():
    w = _hist([{"taken": ["idea-scorer"], "offered": ["idea-scorer"], "outcome": "accepted"},
               {"taken": ["adr-writer"], "offered": ["adr-writer"], "outcome": "cancelled"}])
    assert w["idea-scorer"] > 0, "принятый навык должен усиливаться"
    assert w["adr-writer"] < 0, "отменённый — ослабляться"
    assert abs(w["adr-writer"]) > abs(w["idea-scorer"]), "отмена — более сильный сигнал, чем согласие"


def test_ручная_правка_весит_больше_простого_согласия():
    acc = _hist([{"taken": ["a"], "offered": ["a"], "outcome": "accepted"}])["a"]
    edi = _hist([{"taken": ["a"], "offered": ["a"], "outcome": "edited"}])["a"]
    assert edi > acc, "правка руками — осознанный выбор, он должен весить больше"


def test_непринятое_предложение_тоже_решение():
    w = _hist([{"offered": ["a", "b"], "taken": ["a"], "outcome": "accepted"}])
    assert w["a"] > 0 and w["b"] < 0, "навык предложили, а человек его не взял — это сигнал"


def test_свежее_решение_весит_больше_старого():
    старое = _hist([{"taken": ["a"], "offered": ["a"], "outcome": "accepted"}] * 1 +
                   [{"taken": ["b"], "offered": ["b"], "outcome": "accepted"}] * 1)
    assert старое["b"] > старое["a"], "последнее решение должно весить больше первого"


def test_прибавка_ограничена_потолком():
    async def go():
        for _ in range(40):
            await choice_store.record("u1", "задача", ["x"], ["x"], "accepted")
        p = await choice_store.prefer("u1")
        assert p["x"] <= choice_store.PREFER_CAP, "прибавка пробила потолок"
        assert choice_store.PREFER_CAP < 0.30, "потолок обязан быть ниже планки уверенного подбора"
    asyncio.run(go())


def test_предпочтения_персональные():
    async def go():
        await choice_store.record("анна", "задача", ["a"], ["a"], "accepted")
        await choice_store.record("борис", "задача", ["b"], ["b"], "accepted")
        assert "a" in (await choice_store.prefer("анна"))
        assert "a" not in (await choice_store.prefer("борис")), "предпочтения утекли к другому человеку"
    asyncio.run(go())


def test_предпочтение_решает_ничью_но_не_перебивает_подбор():
    """Два почти одинаковых навыка: прибавка выводит вперёд тот, что человек уже выбирал. И она же
    НЕ должна перебить навык, который подходит по существу заметно лучше."""
    cat = {"разбор-альфы": {"title": "Разбор альфы", "short": "разбирает альфу подробно",
                            "body": "разбери альфу: расхождения, причины, выводы"},
           "разбор-беты": {"title": "Разбор беты", "short": "разбирает альфу подробно",
                           "body": "разбери альфу: расхождения, причины, выводы"},
           "сводка-чисел": {"title": "Сводка чисел", "short": "считает суммы",
                            "body": "посчитай суммы и остатки по договорам"}}
    задача = "разбери альфу: расхождения, причины и выводы"
    ровно = planner.plan(задача, cat, entities=set(), slots=set(), max_steps=1)
    assert ровно["steps"], "подбор ничего не выбрал — проверять нечего"
    с_пред = planner.plan(задача, cat, entities=set(), slots=set(), max_steps=1,
                          prefer={"разбор-беты": choice_store.PREFER_CAP})
    assert с_пред["steps"][0]["skill"] == "разбор-беты", "предпочтение не решило ничью"
    assert с_пред["steps"][0].get("prefer"), "прибавка не доезжает до трассы шага"
    чужое = planner.plan(задача, cat, entities=set(), slots=set(), max_steps=1,
                         prefer={"сводка-чисел": choice_store.PREFER_CAP})
    assert чужое["steps"][0]["skill"] != "сводка-чисел",         "предпочтение перебило подбор по существу — так система замкнётся на привычке"


def test_десктоп_сообщает_исход_подбора():
    """Проверка кейса 06.10 показала разрыв: сервер учится, а десктоп ему ничего не говорит —
    сборка уходила без задачи и без признака ручной правки, отказ не доезжал вовсе. Тогда журнал
    решений заполняется пустыми задачами и никогда не видит правок, ради которых и затевался."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    chat = (root / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
    side = (root / "desktop" / "sidecar" / "modules" / "chat" / "module.py").read_text(encoding="utf-8")
    cli = (root / "desktop" / "sidecar" / "abop_client.py").read_text(encoding="utf-8")

    build = chat.split("async function buildAndRunPlan(a)")[1][:1800]
    assert "manual: !!a.manual" in build, "признак ручной правки не доезжает до сервера"
    assert "offered:" in build and "task: a.task" in build, "задача и предложенное не доезжают"

    cancel = chat.split("async function cancelCard(i, what)")[1][:900]
    assert '"/plan/feedback"' in cancel and '"cancelled"' in cancel, "отказ не сообщается серверу"

    assert "class PlanFeedbackIn" in side and '@router.post("/plan/feedback")' in side, \
        "в сайдкаре нет ручки исхода"
    assert "def plan_feedback(" in cli and "/api/plan/feedback" in cli, "клиент не умеет слать исход"
