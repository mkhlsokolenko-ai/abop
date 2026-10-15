# -*- coding: utf-8 -*-
"""Фильтр выборки Data Plane: диапазон работает, непонятное условие не притворяется «нет данных».

Поймано на живом прогоне ОДДС: агент трижды запросил движения за период условием
`{"дата": {"$gte": ..., "$lt": ...}}`, трижды получил пустой список и собрал пустой отчёт.
Данные при этом лежали на месте — просто словарь-условие сравнивался строкой и не совпадал ни с
чем. Для агента (и для человека, читающего журнал) это неотличимо от честного «данных нет», и
именно поэтому молчаливый ноль здесь опаснее ошибки.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))
os.environ["ABOP_DEV_AUTH"] = "1"

from cli import ape  # noqa: E402

ЗАПИСЬ = {"id": "БВ-001", "дата": "2026-07-03", "счёт": "51", "сумма": 12000000,
          "статья_ддс": "Оплата от покупателей", "признак": ""}


def test_равенство_как_раньше():
    assert ape.фильтр_проходит(ЗАПИСЬ, "счёт", "51")
    assert ape.фильтр_проходит(ЗАПИСЬ, "счёт", 51), "число и строка сравниваются как значения"
    assert not ape.фильтр_проходит(ЗАПИСЬ, "счёт", "50")
    assert not ape.фильтр_проходит(ЗАПИСЬ, "признак", "депозит")


def test_диапазон_дат_работает():
    assert ape.фильтр_проходит(ЗАПИСЬ, "дата", {">=": "2026-07-01", "<=": "2026-09-30"})
    assert not ape.фильтр_проходит(ЗАПИСЬ, "дата", {">=": "2026-08-01"})
    # тот самый вид условия, на котором прогон вернул пусто
    assert ape.фильтр_проходит(ЗАПИСЬ, "дата", {"$gte": "2026-01-01", "$lt": "2027-01-01"})


def test_числовое_сравнение_числами_а_не_строками():
    assert ape.фильтр_проходит(ЗАПИСЬ, "сумма", {">": 1000000})
    assert ape.фильтр_проходит({"сумма": 9}, "сумма", {"<": 100}), "«9» < «100» только как числа"


def test_перечень_и_вхождение():
    assert ape.фильтр_проходит(ЗАПИСЬ, "счёт", ["50", "51", "52"])
    assert not ape.фильтр_проходит(ЗАПИСЬ, "счёт", ["50", "55"])
    assert ape.фильтр_проходит(ЗАПИСЬ, "статья_ддс", {"contains": "покупател"})


def test_непонятный_оператор_говорит_о_себе():
    """Молчаливый ноль здесь — ложь: агент решит, что данных нет, и построит пустой документ."""
    with pytest.raises(ValueError) as сбой:
        ape.фильтр_проходит(ЗАПИСЬ, "дата", {"$regex": "2026-.*"})
    текст = str(сбой.value)
    assert "regex" in текст and "дата" in текст
    assert ">=" in текст, "в сообщении должен быть пример поддержанного условия"


def test_выборка_целиком_через_data_query(tmp_path, monkeypatch):
    """Сквозь `data_query` по файловому пути: тот же фильтр, тот же результат."""
    monkeypatch.setenv("APE_HOME", str(tmp_path))
    ape.set_record_store(None)
    записи = [ЗАПИСЬ, {**ЗАПИСЬ, "id": "БВ-002", "дата": "2026-10-15", "счёт": "50"}]
    путь = ape._data_path("cash_movement")
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    import json
    with open(путь, "w", encoding="utf-8") as f:
        for r in записи:
            f.write(json.dumps({**r, "provenance": {"fetched_at": 1}}, ensure_ascii=False) + "\n")
    try:
        assert len(ape.data_query("cash_movement", None, None, 50)) == 2
        за_квартал = ape.data_query("cash_movement", {"дата": {">=": "2026-07-01", "<=": "2026-09-30"}}, None, 50)
        assert [r["id"] for r in за_квартал] == ["БВ-001"]
    finally:
        try:
            os.remove(путь)
        except OSError:
            pass


def test_смещение_позволяет_дочитать_выборку(tmp_path, monkeypatch):
    """Без offset обрезанную по размеру выдачу нельзя продолжить: агент считал по 15 записям из 26."""
    monkeypatch.setenv("APE_HOME", str(tmp_path))
    ape.set_record_store(None)
    import json
    путь = ape._data_path("cash_movement")
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    with open(путь, "w", encoding="utf-8") as f:
        for i in range(26):
            f.write(json.dumps({"id": f"БВ-{i:03d}", "сумма": i,
                                "provenance": {"fetched_at": 1}}, ensure_ascii=False) + "\n")
    try:
        первая = ape.data_query("cash_movement", None, None, 15)
        вторая = ape.data_query("cash_movement", None, None, 15, False, 15)
        assert len(первая) == 15 and len(вторая) == 11
        все = {r["id"] for r in первая} | {r["id"] for r in вторая}
        assert len(все) == 26, "две страницы вместе дают всю выборку без пересечений"
    finally:
        try:
            os.remove(путь)
        except OSError:
            pass


def test_правка_карты_рецепта_применяется():
    """`map_rows` — редактируемая форма карты; если пришли обе, ведёт она.

    Раньше при наличии `map` строки игнорировались: методолог добавлял графу, рецепт продолжал
    грузить по-старому, и поле в сущности не появлялось — молча, без единой ошибки.
    """
    было = {"recipe": "x", "entity": "transaction",
            "source": {"kind": "json", "path": "/tmp/x.json"},
            "map": {"id": {"col": "id"}},
            "map_rows": [{"field": "id", "spec": "col:id"},
                         {"field": "роль", "spec": "col:роль"}]}
    стало = ape.normalize_recipe("x", было)
    assert "роль" in стало["map"], "добавленная графа обязана попасть в карту"
