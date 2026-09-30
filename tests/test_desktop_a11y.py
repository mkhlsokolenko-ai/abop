"""Доступность интерфейса десктопа: то, что легко сломать обратно одной правкой вёрстки.

Разрывы из UX-аудита 29.09: холст графа и карта операций не работали с клавиатуры совсем, закрытая
шторка чата оставалась в обходе Tab, цвета в двух разделах были заданы литералами (в светлой теме
они теряли контраст), а на копирование система отвечала то надписью на кнопке, то уведомлением.
Каждая проверка ниже сторожит один такой разрыв.
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "desktop" / "ui"
GRAPH = (UI / "modules" / "graphlens" / "panel.js").read_text(encoding="utf-8")
OPS = (UI / "modules" / "opslens" / "panel.js").read_text(encoding="utf-8")
CHAT = (UI / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
APP = (UI / "core" / "app.js").read_text(encoding="utf-8")
THEME = (UI / "core" / "theme.css").read_text(encoding="utf-8")

# Литерал цвета в разметке модуля: в светлой теме такой цвет остаётся тёмным и теряет контраст.
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def test_graph_nodes_are_reachable_by_keyboard():
    """Узел холста — фокусируемый объект со стрелками, связью и удалением."""
    assert 'tabindex="0"' in GRAPH and 'role="button"' in GRAPH
    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"):
        assert key in GRAPH, f"стрелка {key} не двигает узел"
    assert "Delete" in GRAPH and "Escape" in GRAPH
    assert 'role="application"' in GRAPH, "холсту нужна роль и подпись с раскладкой клавиш"


def test_graph_nodes_are_announced():
    """Диктор читает род узла и его связи, а не одно название."""
    assert "function nodeAria" in GRAPH
    assert "aria-label" in GRAPH and 'aria-live="polite"' in GRAPH
    assert ".sr-only" in THEME, "нужен класс для текста только для диктора"


def test_ops_zones_are_buttons_with_state_in_words():
    """Участок карты — кнопка (Tab и Enter), а статус назван словом, не только цветом."""
    assert 'class="zone"' in OPS and '<button type="button" class="zone"' in OPS
    assert "STATE_WORD" in OPS and "требует внимания" in OPS
    assert 'aria-pressed' in OPS


def test_map_colors_come_from_theme_not_literals():
    """Цвета родов узлов — токены: литералы жили только в тёмной теме."""
    for name, src in (("graphlens", GRAPH), ("opslens", OPS)):
        assert not HEX.search(src), f"{name}: цвет задан литералом вместо токена темы"
    for token in ("--node-trigger", "--node-skill", "--node-agent", "--node-output", "--node-sel"):
        assert THEME.count(token) >= 2, f"{token} должен быть объявлен и в тёмной, и в светлой теме"
    assert "--node-trigger" in GRAPH and "--node-sel" in OPS, "модули должны брать цвет из токенов"


def test_closed_chat_drawer_is_out_of_tab_order():
    """Закрытая шторка не ловит Tab и не читается диктором, фокус возвращается открывшему."""
    assert "inert" in CHAT and 'aria-hidden="true"' in CHAT
    assert "drawerOpener" in CHAT, "фокус должен возвращаться тому, кто открыл шторку"
    assert '$("drawer").style.transform = "translateX(0)"' not in CHAT, \
        "шторка открывается только через showDrawer — иначе inert останется включённым"


def test_enter_confirms_in_every_dialog():
    """Enter подтверждает в любой модалке: раньше это зависело от автора конкретного окна."""
    i = APP.index("export function modal(")
    body = APP[i:i + 2600]
    assert 'e.key !== "Enter"' in body and "ok.click()" in body
    assert 'tag === "TEXTAREA"' in body, "в многострочном поле Enter обязан остаться переносом строки"


def test_copy_answers_the_same_way_everywhere():
    """Одно действие — один отклик; отказ буфера виден, а не спрятан за «скопировано»."""
    assert "export async function copyText" in APP
    assert "copy: copyText" in APP
    others = [p for p in (UI / "modules").rglob("panel.js")]
    for p in others:
        src = p.read_text(encoding="utf-8")
        assert "navigator.clipboard" not in src, f"{p.name}: копирование в обход общего ctx.copy"
