# -*- coding: utf-8 -*-
"""Цепочку, собранную из навыков, человек может поправить до запуска.

05.10 владелец: «на очередности агентов нет возможности изменить каждый шаг на нужный навык через
иконку карандаша». Подбор ошибается — и тогда единственным выходом была отмена всей сборки, хотя
неверен обычно один шаг. Теперь у шага есть карандаш (заменить навык), крестик (убрать шаг) и
кнопка «＋ навык» (добавить в конец).

Отдельное правило: исправленную человеком цепочку пересчёт подбора НЕ трогает. Перед запуском
карточка пересчитывается (чтобы не исполнять устаревший план) — и этот пересчёт молча отменил бы
ручную правку, что выглядело бы как потеря данных.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
CARD = CHAT.split("function assembleHTML(a)")[1].split("async function buildAndRunPlan")[0]


def test_у_шага_есть_карандаш_и_удаление():
    assert "asmEdit" in CARD, "нет карандаша на шаге"
    assert "asmDrop" in CARD, "шаг нельзя убрать"
    assert "Заменить навык этого шага" in CARD, "подпись действия не говорит, что оно делает"


def test_шаг_нельзя_убрать_последним():
    """Пустая цепочка — не план: крестик появляется только когда шагов больше одного."""
    assert '(a.steps || []).length > 1 ?' in CARD, "крестик показывается даже у единственного шага"


def test_навык_можно_добавить():
    assert "asmAdd" in CARD and "＋ навык" in CARD, "добавить шаг нечем"


def test_правка_сохраняется_в_историю():
    wire = CHAT.split('querySelectorAll(".asmEdit")')[1].split('querySelectorAll(".asmDrop")')[0]
    assert "asmSave" in wire, "правка не сохраняется"
    save = CHAT.split("async function asmSave(a, i)")[1].split("$(\"col\").querySelectorAll(\".asmEdit\")")[0]
    assert "/messages/" in save and "PATCH" in save, "правка не доезжает до истории чата"
    assert "a.manual = true" in save, "правка не помечается как ручная"


def test_ручная_правка_переживает_пересчёт():
    build = CHAT.split("async function buildAndRunPlan(a)")[1][:900]
    assert "a.manual ? null : await decide(" in build, \
        "пересчёт перед запуском снова затирает ручную правку"


def test_карточка_говорит_что_цепочку_правили():
    assert "цепочка исправлена вручную" in CARD, "по карточке не видно, что план правил человек"
    assert "готового агента нет — соберём из навыков" in CARD, "пропало объяснение, почему собираем"
