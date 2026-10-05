# -*- coding: utf-8 -*-
"""Одно решение — одна карточка: «запустить готового» и «собрать из навыков» перестали быть
двумя несвязанными механизмами.

05.10 владелец: «кажется, у нас 2 механизма подбора, которые конфликтуют при вызове, нам надо
унифицировать их через наш сервис — оркестратор… оба работают, но логично не сшиты в один механизм,
это прямо UX-дыра». Так и было: кнопка «задача многошаговая, собрать цепочку» уходила в подбор
цепочки ИЗ АГЕНТОВ (`/pipelines/suggest`) — он не знал ни про навыки, ни про решение оркестратора, и
шаги в нём можно было менять только на других агентов. Отсюда и «изменить цепочку можно, только если
нужный агент есть».

Теперь любой переход идёт через решение оркестратора, и в каждой карточке видна вторая дорога.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")


def test_карточка_агента_ведёт_в_сборку_а_не_в_старый_подбор():
    card = CHAT.split("function decisionHTML(dc)")[1].split("function _scheduleGuard")[0]
    assert "dcBuild" in card, "с карточки агента нельзя перейти к сборке из навыков"
    assert "dcChain" not in card, "кнопка всё ещё зовёт старый подбор цепочки из агентов"
    wire = CHAT.split('querySelectorAll(".dcBuild")')[1].split("});")[0]
    assert "alt_skills" in wire and "assembleCard" in wire, \
        "переход собран не из решения оркестратора"
    assert "suggestChain(" not in wire, "переход снова уходит во второй механизм"


def test_карточка_сборки_ведёт_к_готовому_агенту():
    card = CHAT.split("function assembleHTML(a)")[1].split("async function buildAndRunPlan")[0]
    assert "asmAgent" in card and "alt_agent" in card, \
        "из сборки нельзя запустить близкого готового агента"


def test_обе_возможности_доезжают_из_решения():
    assert "alt_skills: (d && d.alt_skills)" in CHAT, "вторая возможность теряется при создании карточки"
    assert "alt_agent: d.alt_agent || {}" in CHAT, "близкий агент не доезжает до карточки сборки"
    assert "gaps: (d && d.gaps)" in CHAT, "пробелы агента не доезжают до карточки"


def test_пробелы_агента_видны_человеку():
    card = CHAT.split("function decisionHTML(dc)")[1].split("function _scheduleGuard")[0]
    assert "Из плана он не делает" in card, "человек не видит, чего выбранный агент не умеет"


def test_кандидаты_в_уточнении_кликабельны():
    card = CHAT.split("function clarifyHTML(c)")[1].split("function decisionHTML")[0]
    assert "clPick" in card, "кандидаты подбора остались текстом — выбрать их нельзя"
    wire = CHAT.split('querySelectorAll(".clPick")')[1].split("});")[0]
    assert "assembleCard" in wire, "выбор кандидата не ведёт к сборке"


def test_старый_подбор_цепочки_остался_только_для_инструмента_цепочек():
    """`/pipelines/suggest` сам по себе не вреден — вреден был вызов его из решения по задаче."""
    assert "/pipelines/suggest" in CHAT, "инструмент «Цепочки» потерял свой подбор"
    i = CHAT.index("async function suggestChain")
    callers = [ln for ln in CHAT.splitlines() if "suggestChain(" in ln and "async function" not in ln]
    assert all("chain" in ln.lower() or "pipe" in ln.lower() for ln in callers), \
        f"подбор цепочки из агентов снова зовут из решения по задаче: {callers}"
