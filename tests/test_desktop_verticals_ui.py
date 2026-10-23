# -*- coding: utf-8 -*-
"""Экран «Вертикали», счётчик работы в рейле и рабочее место бухгалтера.

Три правки интерфейса, и у каждой своя причина не быть красивой кнопкой:

* **«Вертикали» в приложении.** Внедренец ставил вертикаль ручкой API — то есть открывал
  терминал у заказчика. Экран показывает то же, что видит платформа, её же словами: годен ли
  пакет, чего не хватает в системе, что изменилось при установке и какова точка отсчёта;
* **счётчик в рейле.** Счётчик был только у подтверждений, и половина работы оставалась
  невидимой: находку расчёта и поле первички человек находил, только если сам заходил в раздел;
* **экран «Сверка» как рабочее место.** Бухгалтер открывает его, чтобы понять, за что браться, а
  не чтобы изучить устройство конвейера — поэтому сверху три числа без терминов, ниже акты
  сверки с контрагентами и ответ на «вы точно читаете мои документы правильно».
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "desktop"))

ОБОЛОЧКА = (КОРЕНЬ / "desktop" / "ui" / "core" / "app.js").read_text("utf-8")
ЭКРАН = (КОРЕНЬ / "desktop" / "ui" / "modules" / "verticals" / "panel.js").read_text("utf-8")
СВЕРКА = (КОРЕНЬ / "verticals" / "finance" / "panels" / "sverka" / "panel.js").read_text("utf-8")


# ── Экран «Вертикали» ────────────────────────────────────────────────────────────────────────

def test_модуль_вертикалей_объявлен_и_подхватится_в_сборке():
    from sidecar import registry
    from sidecar.modules.verticals import module as м
    assert м.MANIFEST["id"] == "verticals" and м.MANIFEST["ui"] == "verticals"
    пути = {getattr(р, "path", "") for р in м.router.routes}
    assert {"/list", "/{vid}/install", "/{vid}/zero-scan", "/{vid}/baseline",
            "/{vid}/activate"} <= пути
    assert "verticals" in registry._names(), (
        "в собранном .exe авто-обход пакетов может вернуть пусто — нужен явный список")


def test_клиент_сайдкара_зовёт_настоящие_ручки_платформы(monkeypatch):
    from sidecar import abop_client
    вызовы = []

    def запрос(метод, путь, тело=None, timeout=0):
        вызовы.append((метод, путь))
        return {"вертикали": []}

    monkeypatch.setattr(abop_client, "_req", запрос)
    abop_client.vertical_list()
    abop_client.vertical_install("finance")
    abop_client.vertical_zero_scan("finance")
    abop_client.vertical_baseline("finance")
    assert вызовы == [
        ("GET", "/api/verticals"),
        ("POST", "/api/verticals/finance/install"),
        ("POST", "/api/verticals/finance/zero-scan"),
        ("GET", "/api/verticals/finance/baseline"),
    ]


def test_отказ_платформы_доносится_кодом_а_не_общими_словами(monkeypatch):
    from sidecar import abop_client
    from sidecar.modules.verticals import module as м

    class Отказ(Exception):
        code = 422

    def запрос(*a, **k):
        raise Отказ("пакет не прошёл сверку: манифест обещает данные, файла нет")

    monkeypatch.setattr(abop_client, "_req", запрос)
    import pytest
    with pytest.raises(Exception) as отказ:
        м.install("finance")
    assert getattr(отказ.value, "status_code", 0) == 422
    assert "не прошёл сверку" in str(getattr(отказ.value, "detail", ""))


def test_негодный_пакет_не_предлагает_кнопку_установки():
    """Сначала человеку сказано, чего не хватает, и только потом — что можно сделать."""
    assert "чегоНет" in ЭКРАН and "пакет не сходится с манифестом" in ЭКРАН
    assert "const годен = !!в[\"годен\"] && !нет;" in ЭКРАН
    assert "if (годен) {" in ЭКРАН, "кнопка установки появляется только у годного пакета"
    assert "Пакет не привозит чужой код" in ЭКРАН


def test_экран_объясняет_нулевой_прогон_и_широкий_режим():
    assert "Нулевой прогон" in ЭКРАН and "точка отсчёта" in ЭКРАН.lower()
    assert "отличить «мы нашли и починили»" in ЭКРАН
    assert "Вернуться в широкий режим" in ЭКРАН
    assert "широкий режим" in ЭКРАН.lower()
    # Отчёт установки показывается пунктами со словами, а не «готово».
    assert "поставлен / обновлён / пропущен" in ЭКРАН


def test_раздел_виден_руководителю_и_при_любой_вертикали():
    assert "verticals: \"manager\"" in ОБОЛОЧКА, "ставит вертикаль тот, кто меняет установку"
    assert '"verticals",' in ОБОЛОЧКА and "RAIL_ORDER" in ОБОЛОЧКА
    строка = next(с for с in ОБОЛОЧКА.splitlines() if "РАЗДЕЛЫ_ВСЕГДА" in с and "new Set" in с)
    assert "verticals" in строка, (
        "иначе человек, поставивший узкое представление, лишился бы способа его сменить")


# ── Счётчик работы в рейле ───────────────────────────────────────────────────────────────────

def test_счётчик_работы_отдельно_от_подтверждений():
    assert "setРаботаCount" in ОБОЛОЧКА
    assert "работаCount = { находки: 0, поля: 0 }" in ОБОЛОЧКА
    assert "m.id === \"decisions\" && работа" in ОБОЛОЧКА, "счётчик висит на «Решениях»"
    assert "m.id === \"chat\" && hitlCount" in ОБОЛОЧКА, "подтверждения остались у чата"


def test_подсказка_расшифровывает_число():
    """Одно «7» на кнопке не говорит, что ждёт человека, а вопросы разные."""
    assert "находок:" in ОБОЛОЧКА and "полей первички:" in ОБОЛОЧКА
    assert "title=\"${esc(изЧего)}\"" in ОБОЛОЧКА
    assert "aria = `${label}, ${изЧего}`" in ОБОЛОЧКА


def test_счётчик_опрашивается_ядром_и_терпит_старый_сайдкар():
    assert "/api/modules/decisions/findings" in ОБОЛОЧКА
    assert "/api/modules/decisions/fields" in ОБОЛОЧКА
    assert "сайдкар старее конвейера первички" in ОБОЛОЧКА, (
        "ручки полей может не быть — это не повод ронять рейл")
    assert "setInterval(pollРабота" in ОБОЛОЧКА


# ── Рабочее место бухгалтера ─────────────────────────────────────────────────────────────────

def test_сверху_что_делать_без_терминов():
    assert "чтоДелать" in СВЕРКА
    for слово in ("полей проверить в документах", "расхождений ждут вашего слова",
                  "актов расходятся с нашими данными"):
        assert слово in СВЕРКА, слово
    for термин in ("рецепт", "сущность", "адаптер"):
        блок = СВЕРКА[СВЕРКА.index("const чтоДелать"):СВЕРКА.index("const актыСвои")]
        assert термин not in блок.lower(), f"в «что делать сейчас» просочился термин «{термин}»"


def test_акты_сверки_видны_по_контрагентам():
    assert "разделАктов" in СВЕРКА and "recon_line" in СВЕРКА
    for подпись in ("сошлось", "нет у нас", "нет у них", "суммы разошлись", "разные периоды",
                    "решает человек"):
        assert подпись in СВЕРКА, подпись
    assert "предъявляют ему же" in СВЕРКА
    assert "Расхождений нет: все строки акта нашли себе наш документ." in СВЕРКА


def test_откуда_данные_и_за_что_платим():
    assert "разделДанных" in СВЕРКА and "/api/pervichka/quality" in СВЕРКА
    assert "из ЭДО (готовые поля)" in СВЕРКА and "распознаны со скана" in СВЕРКА
    assert "не взято из брошюры поставщика" in СВЕРКА
    assert "Качество на ваших документах" in СВЕРКА


def test_экран_вертикали_только_читает_данные_платформы():
    """Панель приезжает пакетом: право писать из неё означало бы чужой код под правами людей."""
    from sidecar import abop_client
    пути = [с.split('"')[1] for с in СВЕРКА.split("const ")[1:] if '"/api/panel-data/' in с]
    assert len(пути) >= 4, пути
    for п in пути:
        цель = п.replace("/api/panel-data", "").split("?")[0]
        assert any(цель.startswith(пр) for пр in abop_client.ПАНЕЛЯМ_РАЗРЕШЕНО), цель
    assert "POST" not in СВЕРКА


def test_версия_десктопа_поднята_за_правку_сайдкара():
    """Правка сайдкара доезжает до человека только переустановкой — версия обязана вырасти."""
    пакет = json.loads((КОРЕНЬ / "desktop" / "package.json").read_text("utf-8"))
    конфиг = (КОРЕНЬ / "desktop" / "sidecar" / "config.py").read_text("utf-8")
    assert f'"{пакет["version"]}"' in конфиг, "версия в package.json и в сайдкаре должна совпадать"
    дисциплина = (КОРЕНЬ / "docs" / "RELEASE_DISCIPLINE.md").read_text("utf-8")
    assert пакет["version"] in дисциплина, "релиз без записи в дисциплину — релиз без причины"
