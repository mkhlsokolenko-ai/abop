# -*- coding: utf-8 -*-
"""На карточке прогона видно, чем считали и во сколько обошлось.

05.10 владелец спросил: «проверь в десктопе, что прогон идёт на local». По карточке ответить было
нечем — она показывала находки и токены, но не модель. А разница существенная: `local/...` означает
свой бокс, 0 ₽ и данные, не уходившие к облачному провайдеру; любой другой префикс означает, что
каскад молча ушёл на роутер — это и деньги, и другой адресат данных.

Сервер эти числа считает всегда (`run_metrics.cost.by_model`), терялись они по дороге в карточку.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDE = (ROOT / "desktop" / "sidecar" / "modules" / "chat" / "module.py").read_text(encoding="utf-8")
CHAT = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")


def test_сводка_прогона_несёт_модель_и_стоимость():
    block = SIDE.split("def _run_summary(")[1].split("def _persist_run(")[0]
    assert '"model":' in block and '"cost_rub":' in block, "сводка прогона молчит о модели и цене"
    assert "by_model" in block, "модель берётся не из разбивки сервера по моделям"


def test_карточка_различает_свой_бокс_и_облако():
    card = CHAT.split("function runCard(s)")[1].split("function pipelineHTML")[0]
    assert 's.model' in card, "карточка не показывает модель"
    assert 'startsWith("local/")' in card, "свой бокс не отличается от облачного роутера"
    assert "var(--ok-ink)" in card and "var(--warn-ink)" in card, "различие не видно цветом"


def test_цена_показывается_когда_известна():
    card = CHAT.split("function runCard(s)")[1].split("function pipelineHTML")[0]
    assert "s.cost_rub != null" in card, "нулевая стоимость своего бокса должна показываться, а не прятаться"
