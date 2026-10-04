# -*- coding: utf-8 -*-
"""Премиум-слой десктопа и совпадение формы с вебом.

Владелец: «более красивое и современное… полное совпадение по цветам и формам с вебом, но надо прям
премиум». Премиум здесь — не украшение, а последовательность: одна шкала форм, один источник света
на стекле, один вид маршрута в вебе и десктопе. Разъезжается это молча, поэтому правила закреплены:

1. шкала форм и кромка света объявлены темой, а не расставлены по месту в разметке;
2. кромка/градиент поверхности есть в ОБЕИХ темах — на белом фоне блик в 7% не виден, и поверхность
   в светлой теме выглядела бы плоской;
3. раздел в рейле — строка маршрута (плашка знака, подпись, полоса слева), как в навигации веба;
4. свёрнутый рейл переживает перезапуск: это выбор человека, а не состояние по умолчанию;
5. знак ABOP в десктопе — тот же, что в вебе: тёмная плашка и градиентное кольцо.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME = (ROOT / "desktop" / "ui" / "core" / "theme.css").read_text(encoding="utf-8")
APP = (ROOT / "desktop" / "ui" / "core" / "app.js").read_text(encoding="utf-8")
APE = (ROOT / "desktop" / "ui" / "core" / "ape.js").read_text(encoding="utf-8")
SHELL = (ROOT / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
WEB = (ROOT / "webapp" / "src" / "template.html").read_text(encoding="utf-8")


def test_шкала_форм_объявлена_темой():
    for t in ("--r-xs", "--r-sm", "--r-md", "--r-lg", "--r-xl", "--r-pill"):
        assert t + ":" in THEME, f"в теме нет ступени формы {t}"
    for cls in (".btn", ".dcard", ".ape-modal", ".ape-card"):
        block = THEME.split(cls + " {")[1].split("}")[0]
        assert "var(--r-" in block, f"{cls} держит радиус литералом, а не ступенью шкалы"


def test_кромка_света_есть_в_обеих_темах():
    """Один и тот же токен в тёмной и светлой: иначе половина поверхностей теряет объём."""
    for t in ("--edge:", "--panel-grad:", "--shadow-3:", "--sel:"):
        assert THEME.count(t) >= 2, f"{t} объявлен только в одной теме"


def test_раздел_рейла_повторяет_маршрут_веба():
    nav = THEME.split(".navitem {")[1].split("}")[0]
    assert "border-left" in nav and "border-radius: 0 var(--r-md)" in nav, \
        "у раздела нет полосы слева и скругления только справа — это форма навигации веба"
    on = THEME.split(".navitem.on {")[1].split("}")[0]
    assert "accent" in on, "текущий раздел не выделен акцентом"
    assert 'class="navitem' in APP, "рейл не использует общий рецепт"
    # в вебе та же форма: полоса слева и скругление справа
    assert "border-left: 3px solid" in WEB and "border-radius: 0 10px 10px 0" in WEB, \
        "форма навигации веба изменилась — десктоп нужно привести к ней заново"


def test_свёрнутый_рейл_запоминается():
    assert '"ape_rail_narrow"' in APP, "состояние рейла не хранится"
    assert "rail-narrow" in THEME and "rail-narrow" in APP, "нет узкого вида рейла"
    toggle = APP.split("function applyRail()")[1].split("function toggleRail()")[0]
    assert "aria-expanded" in toggle, "переключатель не объявляет состояние диктору"
    assert 'id="railToggle"' in SHELL, "в шапке нет переключателя меню"


def test_знак_совпадает_с_вебом():
    logo = APE.split("export function apeLogo")[1][:2200]
    assert 'rx="20"' in logo and "stroke-width=" in logo, "у знака нет плашки с кольцом, как в вебе"
    assert "#34d399" in logo and "#8b5cf6" in logo, "кольцо знака потеряло градиент веба"


def test_учётная_запись_не_занимает_шапку():
    """Имя, почта и «Выйти» в строку съедали треть шапки; теперь плашка с инициалами и диалог."""
    box = APP.split("function renderAuthBox(me)")[1].split("} else if (me.busy)")[0]
    assert 'id="meBtn"' in box, "нет компактной плашки учётной записи"
    assert "logoutBtn" in APP and "Выйти из ABOP" in APP, "выход пропал вовсе"
    assert re.search(r"ini\b", box), "в плашке нет инициалов"
