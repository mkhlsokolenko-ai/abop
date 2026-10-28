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
    """Знаки берутся из спрайта ядра (28.10), а не из символов шрифта.

    Было «▶ ✎ ↺ ⏹ ✕» — разной толщины и размера, зависят от системного шрифта и рядом с
    остальными экранами выглядят дёшево. Набор действий остался тем же, сменился носитель.
    """
    i = PANEL.index("const actions = (a) =>")
    row = PANEL[i:i + 1200]
    for имя, what in (("play", "запуск"), ("pencil", "настройка"), ("clock", "версии"),
                      ("exit", "архив"), ("trash", "удаление")):
        assert f'"{имя}"' in row, f"нет знака для «{what}»"
    assert "Обновить список агентов" in PANEL and 'icon("refresh")' in PANEL, (
        "кнопка обновления осталась подписанной или рисует свой символ")


def test_у_каждого_знака_есть_подпись_для_человека():
    """Знак без подсказки — ребус: title и aria-label обязательны."""
    i = PANEL.index("const ico = (cls, имя, title, a) =>")
    body = PANEL[i:i + 400]
    assert 'title="${esc(title)}"' in body and "aria-label=" in body
    assert "icon(имя)" in body, "значок рисуется не спрайтом ядра — панель завела свой символ"


def test_разрушающее_отделено_от_запуска():
    i = PANEL.index("const actions = (a) =>")
    row = PANEL[i:i + 1200]
    assert row.index('"play"') < row.index('flex:1') < row.index('"trash"'), \
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


# ── паспорт агента (21.10): карточка показывала три строки и никуда не вела ───────────────────────

def test_имя_агента_открывает_паспорт():
    """Клик по имени — единственный способ узнать, чем агент питается и к чему привязан.

    В вебе имя агента кликабельно с самого начала, в десктопе карточка не вела никуда: семья,
    автономия, версия — и всё. За зависимостями и расписаниями шли в браузер.
    """
    assert 'class="pass"' in PANEL, "имя агента не кнопка — паспорт нечем открыть"
    assert "showPassport(b.dataset.id, b.dataset.name)" in PANEL
    i = PANEL.index('class="pass"')
    head = PANEL[i:i + 500]
    assert "aria-label=" in head and "title=" in head, "кнопка-имя без подписи для подсказки и диктора"
    assert "<button type=\"button\" class=\"pass\"" in PANEL, "имя должно быть кнопкой, а не div с onclick"


def test_паспорт_не_считает_ничего_сам():
    """Считает сервер: второй расчёт на клиенте разошёлся бы с первым."""
    i = PANEL.index("async function showPassport")
    body = PANEL[i:i + 700]
    assert '/passport/' in body, "паспорт должен приходить с сервера"
    for запрет in ("Math.", "reduce(", "/ 60"):
        assert запрет not in body, f"в паспорте появился свой расчёт ({запрет})"


def test_паспорт_показывает_все_четыре_раздела():
    """Ради них всё и затевалось: без любого из них карточка снова станет справкой ни о чём."""
    i = PANEL.index("function passportHTML")
    body = PANEL[i:PANEL.index("async function showPassport")]
    for раздел in ("навыки агента (из его графа)", "зависимости по данным", "привязки", "оценка"):
        assert раздел in body, f"в паспорте нет раздела «{раздел}»"
    assert "нет рецепта — создать" in body and "зап." in body, \
        "зависимость без рецепта и без числа записей читается как «всё в порядке»"
    assert "запускается только вручную" in body, "пустые привязки обязаны объяснять себя"
    assert "ещё не запускался" in body, "пустая оценка обязана объяснять себя"
