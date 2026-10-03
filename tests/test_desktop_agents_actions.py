"""Действия над агентом — знаками, а не подписями.

03.10: в «Моих агентах» съехала вёрстка. Причина — три подписанные кнопки («✎ Настроить»,
«↺ Версии», «▶ Запустить») в карточке шириной 280 px: они не влезали и переносились, ломая ряд.
Подписи ушли в title и aria-label — подсказка и чтение с экрана остались, место освободилось.

Второе правило — про промах мышью: запуск слева, разрушающее справа, между ними распорка.
"""
from __future__ import annotations

import re
from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "desktop" / "ui" / "modules" / "agents" / "panel.js").read_text(encoding="utf-8")
THEME = (Path(__file__).resolve().parents[1] / "desktop" / "ui" / "core" / "theme.css").read_text(encoding="utf-8")


def test_в_карточке_нет_подписанных_кнопок():
    i = PANEL.index("const actions = (a) =>")
    row = PANEL[i:PANEL.index("`;", PANEL.index("</div>`", i))]
    for label in ("Настроить<", "Запустить<", "Версии<", "✎ Настроить", "▶ Запустить", "↺ Версии"):
        assert label not in row, f"в ряду действий осталась подпись «{label}»"
    assert '<button class="btn' not in row, "в ряду действий осталась обычная кнопка с текстом"


def test_полный_набор_знаков_на_месте():
    i = PANEL.index("const actions = (a) =>")
    row = PANEL[i:i + 1200]
    for glyph, what in (("▶", "запуск"), ("✎", "настройка"), ("↺", "версии"), ("⏹", "архив"), ("✕", "удаление")):
        assert glyph in row, f"нет знака для «{what}»"
    assert "Обновить список агентов" in PANEL and "↻" in PANEL, "кнопка обновления осталась подписанной"


def test_у_каждого_знака_есть_подпись_для_человека():
    """Знак без подсказки — ребус: title и aria-label обязательны."""
    i = PANEL.index("const ico = (cls, glyph, title, a) =>")
    body = PANEL[i:i + 400]
    assert 'title="${esc(title)}"' in body and "aria-label=" in body


def test_разрушающее_отделено_от_запуска():
    i = PANEL.index("const actions = (a) =>")
    row = PANEL[i:i + 1200]
    assert row.index('"▶"') < row.index('flex:1') < row.index('"✕"'), \
        "удаление стоит рядом с запуском — промах мышью будет стоить агента"


def test_архив_и_удаление_разные_кнопки():
    """Прежде обе жили под «✕» в одном диалоге: нажимал «удалить», а выбирал из двух действий."""
    assert "async function archiveAgent" in PANEL and "async function deleteAgent" in PANEL
    assert "В архив" in PANEL and "Удалить навсегда" in PANEL
    assert "agArch" not in PANEL, "остался прежний диалог выбора между архивом и удалением"
    for fn in ("archiveAgent", "deleteAgent"):
        i = PANEL.index("async function " + fn)
        assert "confirmDialog" in PANEL[i:i + 600], f"{fn}: действие без подтверждения"


def test_знак_запуска_выделен_акцентом():
    """Главное действие должно читаться главным и без подписи."""
    assert re.search(r"\.ico\.go \{[^}]*accent", THEME), "нет акцентного вида у знака запуска"
    assert '"go run"' in PANEL
