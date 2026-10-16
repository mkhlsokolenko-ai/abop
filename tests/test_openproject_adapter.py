# -*- coding: utf-8 -*-
"""Дорожная карта берётся из проектного сервера, а соответствие полей объявляется рецептом.

Владелец: «чтобы данные графиков брались из реальных проектов, надо ставить на наш сервер аналог
Microsoft Project». Поставлен OpenProject; адаптер знает ФОРМАТ его ответа (HAL+JSON) и больше
ничего: какое пользовательское поле заказчика означает стадию, критичность, дату входа на стадию и
число возвратов — говорит рецепт. У другого заказчика поля называются иначе, и это не повод
править релиз.

Зависимости берутся из связей `follows`: «2 follows 1» значит «пункт 2 ждёт пункт 1» — ровно то,
что движок смещения понимает под «ждёт».
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

import ape  # noqa: E402

ЗАДАЧИ = {
    "_embedded": {"elements": [
        {"id": 101, "subject": "Модуль расчёта тарифов", "startDate": "2026-08-03",
         "dueDate": "2026-10-16", "percentageDone": 90,
         "customField1": {"value": "Вывод в пром"}, "customField2": True,
         "customField3": "2026-10-12", "customField4": 0,
         "_links": {"status": {"title": "In progress"}, "assignee": {"title": "Соколов Д."}}},
        {"id": 102, "subject": "Интеграция с биллингом", "startDate": "2026-08-17",
         "dueDate": "2026-10-09", "percentageDone": 70,
         "customField1": {"value": "Сквозное тестирование (E2E)"}, "customField2": True,
         "customField3": "2026-09-28", "customField4": 0,
         "_links": {"status": {"title": "In progress"}, "assignee": {"title": "Орлова М."}}},
    ]},
}
СВЯЗИ = {
    "_embedded": {"elements": [
        {"_links": {"from": {"href": "/api/v3/work_packages/102"},
                    "to": {"href": "/api/v3/work_packages/101"}}, "type": "follows"},
    ]},
}

ИСТОЧНИК = {
    "base": "http://проектный:8096", "проект": "prj-2451", "токен": "ключ",
    "поля": {"стадия": "customField1.value", "критично_для_ввода": "customField2",
             "дата_входа": "customField3", "возвратов": "customField4"},
}


@pytest.fixture
def сервер(monkeypatch):
    """Подменный OpenProject: отвечает задачами и связями, запоминает заголовки запроса."""
    запросы = []

    class Ответ:
        def __init__(self, тело):
            self._тело = json.dumps(тело).encode("utf-8")

        def read(self):
            return self._тело

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _открыть(запрос, timeout=0):
        запросы.append(запрос)
        адрес = запрос.full_url if hasattr(запрос, "full_url") else str(запрос)
        return Ответ(СВЯЗИ if "/relations" in адрес else ЗАДАЧИ)

    monkeypatch.setattr(ape.urllib.request, "urlopen", _открыть)
    monkeypatch.setattr(ape, "_guard_url", lambda u: u)
    return запросы


def test_задачи_превращаются_в_пункты_дорожной_карты(сервер):
    строки = ape._adapter_openproject(dict(ИСТОЧНИК))
    assert len(строки) == 2
    первый = строки[0]
    assert первый["пункт"] == "101" and первый["название"] == "Модуль расчёта тарифов"
    assert первый["план_старт"] == "2026-08-03" and первый["план_финиш"] == "2026-10-16"
    assert первый["ответственный"] == "Соколов Д." and первый["статус_задачи"] == "In progress"
    assert первый["задача"].endswith("/work_packages/101"), "ссылка на первоисточник потеряна"


def test_соответствие_полей_объявляет_рецепт(сервер):
    """У заказчика свои пользовательские поля: их номера не должны жить в коде."""
    строки = {с["пункт"]: с for с in ape._adapter_openproject(dict(ИСТОЧНИК))}
    assert строки["101"]["стадия"] == "Вывод в пром"
    assert строки["101"]["критично_для_ввода"] == "да", "булево поле должно читаться как «да/нет»"
    assert строки["101"]["дата_входа"] == "2026-10-12"
    assert строки["101"]["возвратов"] == "0"
    # Ни одного customField в тексте адаптера: номера приходят рецептом.
    тело = io.open(КОРЕНЬ / "cli" / "ape.py", encoding="utf-8").read()
    i = тело.index("def _adapter_openproject(")
    assert "customField" not in тело[i:тело.index("register_adapter(\"openproject\"")], (
        "номер пользовательского поля зашит в адаптере")


def test_связь_follows_читается_как_ждёт(сервер):
    строки = {с["пункт"]: с for с in ape._adapter_openproject(dict(ИСТОЧНИК))}
    assert строки["102"]["зависит_от"] == "101", "зависимость из проектного сервера не доехала"
    assert строки["101"]["зависит_от"] == ""


def test_без_токена_и_адреса_отказ_объясняет_себя(monkeypatch):
    monkeypatch.delenv("OPENPROJECT_TOKEN", raising=False)
    with pytest.raises(ValueError) as e1:
        ape._adapter_openproject({"проект": "prj-2451", "токен": "к"})
    assert "адрес" in str(e1.value)
    with pytest.raises(ValueError) as e2:
        ape._adapter_openproject({"base": "http://x", "проект": "prj-2451", "токен_env": "OPENPROJECT_TOKEN"})
    assert "ключ" in str(e2.value), "молчаливый отказ без объяснения недопустим"


def test_ключ_можно_держать_файлом(сервер, tmp_path, monkeypatch):
    """Файл надёжнее переменной среды и рецепта: его нет ни в данных, ни в логах."""
    monkeypatch.delenv("OPENPROJECT_TOKEN", raising=False)
    ф = tmp_path / "op_token"
    ф.write_text("ключ-из-файла" + chr(10), encoding="utf-8")
    источник = {k: v for k, v in ИСТОЧНИК.items() if k != "токен"}
    источник["токен_файл"] = str(ф)
    строки = ape._adapter_openproject(источник)
    assert len(строки) == 2, "ключ из файла не подхвачен"


def test_токен_можно_держать_в_среде(сервер, monkeypatch):
    monkeypatch.setenv("OPENPROJECT_TOKEN", "из-среды")
    источник = {k: v for k, v in ИСТОЧНИК.items() if k != "токен"}
    источник["токен_env"] = "OPENPROJECT_TOKEN"
    строки = ape._adapter_openproject(источник)
    assert len(строки) == 2, "ключ из переменной среды не подхвачен"


def test_отказ_проектного_сервера_называет_причину(monkeypatch):
    """403 и 401 — разные беды: «ключ не тот» и «ключ не от того человека»."""
    def отказ(код):
        def _открыть(запрос, timeout=0):
            raise ape.urllib.error.HTTPError(
                "http://проектный:8096", код, "Forbidden", {}, None)
        return _открыть

    monkeypatch.setattr(ape, "_guard_url", lambda u: u)
    monkeypatch.setattr(ape.urllib.request, "urlopen", отказ(403))
    with pytest.raises(ValueError) as e403:
        ape._adapter_openproject(dict(ИСТОЧНИК))
    assert "прав" in str(e403.value) and "prj-2451" in str(e403.value), (
        "по тексту ошибки должно быть видно, что ключ рабочий, а прав на проект нет")

    monkeypatch.setattr(ape.urllib.request, "urlopen", отказ(401))
    with pytest.raises(ValueError) as e401:
        ape._adapter_openproject(dict(ИСТОЧНИК))
    assert "не принял ключ" in str(e401.value)
