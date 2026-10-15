# -*- coding: utf-8 -*-
"""Личное пространство человека: изоляция, провенанс и правило «не для расчётов».

Навык `bft-draft` объявляет вход «из контекста»: расшифровку интервью может принести только
человек, вставив её в чат. Значит «собери БФТ по интервью с заказчиками» физически не на чем
выполнить — и честное «нет данных» тут следствие отсутствия источника. Личное пространство
закрывает эту дыру, но вносит риск: заметка в одном блоке с нормами становится «корпоративной
истиной» и уезжает в документ заказчику. Поэтому тесты держат три вещи: чужое личное недоступно,
имя документа едет вместе с куском, и блок промпта сам говорит, что считать по нему нельзя.
"""
from __future__ import annotations

import asyncio
import sys

import pytest
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

from server import personal_kb, run_exec, skill_contract  # noqa: E402

ЗАМЕТКА = (
    "Интервью с главбухом холдинга, 14 августа.\n\n"
    "Боль: закрытие квартала занимает девять дней, из них четыре уходят на поиск документов "
    "без статьи движения денежных средств.\n\n"
    "Просьба: показывать сразу, какие документы задерживают закрытие, и кто их проводит."
)


def _дать(корутина):
    return asyncio.run(корутина)


@pytest.fixture(autouse=True)
def _без_сети(monkeypatch):
    """sLAVA в тестах не зовём: это чужой сервис, а проверяем мы поведение личного пространства.

    Отказ индексации — штатный путь стенда без sLAVA: документ остаётся доступен поиску по тексту.
    """
    async def _нет(*a, **k):
        raise RuntimeError("sLAVA недоступна (тест)")
    monkeypatch.setattr(personal_kb.slava, "ingest", _нет)
    monkeypatch.setattr(personal_kb.slava, "query", _нет)


def _чисто():
    personal_kb._MEM.clear()
    personal_kb._TEXT.clear()


def test_документ_ложится_и_находится_по_словам_запроса():
    """Без sLAVA на стенде поиск всё равно должен возвращать материал, а не «нет данных»."""
    _чисто()
    карточка = _дать(personal_kb.add("user-1", "Интервью с главбухом", ЗАМЕТКА, kind="интервью"))
    assert карточка["id"].startswith("pd-") and карточка["chars"] == len(ЗАМЕТКА)
    куски = _дать(personal_kb.search("user-1", "что задерживает закрытие квартала"))
    assert куски, "личный материал не найден"
    assert any("закрытие квартала" in к["текст"] for к in куски)
    # Имя документа едет с куском: иначе личное знание неотличимо от выдумки модели.
    assert all(к["документ"] == "Интервью с главбухом" for к in куски)


def test_чужое_личное_недоступно():
    """Это не вопрос ролей: смысл личного пространства — что его не читает никто другой."""
    _чисто()
    _дать(personal_kb.add("user-1", "Моё интервью", ЗАМЕТКА))
    assert _дать(personal_kb.documents("user-2")) == []
    assert _дать(personal_kb.search("user-2", "закрытие квартала")) == []
    # Коллекции разные и имя не раскрывает адрес владельца.
    к1, к2 = personal_kb.collection_for("user-1"), personal_kb.collection_for("user-2")
    assert к1 != к2 and "user-1" not in к1 and "@" not in к1


def test_пустое_и_огромное_не_принимаются():
    _чисто()
    for текст in ("", "   \n  "):
        try:
            _дать(personal_kb.add("user-1", "пусто", текст))
            raise AssertionError("пустой документ принят")
        except ValueError as ex:
            assert "нечего искать" in str(ex)
    try:
        _дать(personal_kb.add("user-1", "выгрузка", "х" * (personal_kb.ЛИМИТ_ДОКУМЕНТА + 1)))
        raise AssertionError("выгрузка принята как заметка")
    except ValueError as ex:
        assert "выгрузка" in str(ex)


def test_удаление_убирает_и_из_поиска():
    _чисто()
    карточка = _дать(personal_kb.add("user-1", "Черновик", ЗАМЕТКА))
    assert _дать(personal_kb.remove("user-1", карточка["id"])) is True
    assert _дать(personal_kb.documents("user-1")) == []
    assert _дать(personal_kb.search("user-1", "закрытие")) == []
    assert _дать(personal_kb.remove("user-1", карточка["id"])) is False


def test_блок_промпта_запрещает_считать_по_личному():
    """Главное правило живёт в самом блоке: модель читает его вместе с материалом."""
    блок = personal_kb.блок_для_промпта([{"текст": "боль: девять дней на закрытие",
                                          "документ": "Интервью с главбухом"}])
    assert "ЛИЧНОЕ ПРОСТРАНСТВО" in блок
    for слово in ("СФОРМУЛИРОВАТЬ", "СЧИТАТЬ", "СВЕРЯТЬ", "нельзя"):
        assert слово in блок, слово
    assert "[Интервью с главбухом]" in блок
    assert "НОРМЫ" not in блок, "личное не должно выглядеть как нормы"


def test_личное_даётся_только_объявившим_вход_навыкам():
    """Порядок лестницы задаёт навык: «составь БФТ» — можно, «сверь ОДДС» — нет."""
    _чисто()
    _дать(personal_kb.add("user-1", "Интервью", ЗАМЕТКА))
    схемы = {
        "bft-draft": {"inputs": {"optional": [{"from": "personal"}]}},
        "oddc-build": {"inputs": {"required": [{"from": "data", "entity": "oddc_report_line"}]}},
    }
    fn = run_exec._personal_fn("user-1", схемы)
    assert fn is not None
    assert _дать(fn("bft-draft", [], "боли заказчика")), "навык с объявленным входом не получил личное"
    assert _дать(fn("oddc-build", [], "сальдо 4400")) == [], "расчётный навык получил личные заметки"


def test_без_личности_и_без_объявлений_личное_не_подключается():
    assert run_exec._personal_fn("", {"bft-draft": {"inputs": {"optional": [{"from": "personal"}]}}}) is None
    assert run_exec._personal_fn("user-1", {"oddc-build": {"inputs": {"required": []}}}) is None


def test_personal_объявлен_источником_контракта():
    """Привилегия чтения личного объявляется в контракте, а не прячется в коде сервера."""
    assert "personal" in skill_contract.SOURCES
    пути = sorted((КОРЕНЬ / "skills").glob("*/template.json"))
    объявили = []
    for p in пути:
        import io, json
        d = json.load(io.open(p, encoding="utf-8"))
        вх = d.get("inputs") or {}
        for ветка in ("required", "optional"):
            if any(isinstance(i, dict) and i.get("from") == "personal" for i in вх.get(ветка) or []):
                объявили.append(p.parent.name)
    assert "bft-draft" in объявили, объявили
    # Расчётные навыки в личное не заглядывают — это и есть правило, а не настройка.
    for расчётный in ("oddc-build", "period_close_orchestration", "audit1c-rank"):
        assert расчётный not in объявили, расчётный


def test_в_промпте_личное_идёт_отдельным_блоком_до_норм():
    import io
    runner = io.open(КОРЕНЬ / "server" / "runner.py", encoding="utf-8").read()
    i = runner.index("_head = (uc_block")
    блок = runner[i:i + 400]
    assert "personal_block" in блок and блок.index("personal_block") < блок.index("know_block")
    # Личный материал — материал: навык не должен считать, что у него нет ничего.
    assert "or bool(personal_block)" in runner
