# -*- coding: utf-8 -*-
"""Эмбеддер можно держать у себя, а не в облаке — и смена модели не врёт числами.

**Повод (13.11.2026).** Владелец, увидев в замере прода `ROUTEAI_EMBED_MODEL=google/gemini-embedding-2`:
«эмбеддинги надо поднять у нас, тем более рядом есть слава». Подбор исполнителя зовёт `embed` на
каждом запросе чата — то есть внешний сервис стоит в горячем пути: его задержка становится нашей,
а его недоступность — нашей. У реранкера выбор бэкенда был с самого начала (`RERANKER_BACKEND`),
у эмбеддера — нет, он был прибит к RouteAI жёстко.

**Вторая находка, опаснее первой.** `vectors.cosine` считал близость через `zip`, который при
разной длине молча обрезает длинный вектор до короткого и возвращает правдоподобное число. Векторы
разных моделей несопоставимы по построению, а часть их лежит в базе (`reglament.embedding`, 7
записей на проде от облачной модели). Первая же смена эмбеддера дала бы «похожесть», посчитанную
по обрубку, — без ошибки, без признака, без следа в журнале.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from server.vectors import cosine  # noqa: E402


def test_векторы_разной_длины_не_сравниваются():
    """Главное: не «примерно похожи», а «сравнивать нечем»."""
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
    # Тот же вектор, но из модели с другой размерностью — результат обязан быть нулём, а не 1.0,
    # которую дал бы zip, обрезав хвост.
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.5, 0.5]) == 0.0
    assert cosine([1.0, 0.0, 0.0, 0.5, 0.5], [1.0, 0.0, 0.0]) == 0.0
    # Прежнее поведение для пустых — без изменений.
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [0.0, 0.0]) == 0.0


@pytest.mark.parametrize("бэкенд,ожидаем_облако", [("routeai", True), ("local", False),
                                                   ("", True), ("LOCAL", False)])
def test_бэкенд_эмбеддера_выбирается_настройкой(бэкенд, ожидаем_облако, monkeypatch):
    """Проверяем, КУДА уходит запрос, а не наличие ветки в коде."""
    import asyncio

    from server import clients
    from server.config import settings

    записано: dict = {}

    class _Ответ:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"data": [{"embedding": [0.1, 0.2]}]}

    class _Клиент:
        def __init__(self, *a, **kw):
            записано["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            записано["url"] = url
            записано["headers"] = headers or {}
            записано["json"] = json or {}
            return _Ответ()

    # `settings` — замороженный dataclass: подменяем не поля, а сам объект в модуле клиента.
    import dataclasses
    свои = dataclasses.replace(settings, embed_backend=бэкенд,
                               embed_base_url="http://127.0.0.1:8085/v1",
                               embed_model_local="intfloat/multilingual-e5-base")
    monkeypatch.setattr(clients.httpx, "AsyncClient", _Клиент)
    monkeypatch.setattr(clients, "settings", свои)

    итог = asyncio.run(clients.embed(["привет"]))
    assert итог == [[0.1, 0.2]]

    if ожидаем_облако:
        assert свои.routeai_base_url in записано["url"], записано["url"]
        assert "Authorization" in записано["headers"], "в облако без ключа не ходят"
        assert записано["json"]["model"] == свои.embed_model
    else:
        assert записано["url"] == "http://127.0.0.1:8085/v1/embeddings", записано["url"]
        assert not записано["headers"], "свой сервис ключа не требует — лишний заголовок не шлём"
        assert записано["json"]["model"] == "intfloat/multilingual-e5-base"


def test_адрес_склеивается_без_двойного_слэша(monkeypatch):
    """`EMBED_BASE_URL` люди пишут и со слэшем на конце, и без."""
    import asyncio

    from server import clients
    from server.config import settings

    записано: dict = {}

    class _Ответ:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"data": []}

    class _Клиент:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            записано["url"] = url
            return _Ответ()

    import dataclasses
    monkeypatch.setattr(clients.httpx, "AsyncClient", _Клиент)
    monkeypatch.setattr(clients, "settings",
                        dataclasses.replace(settings, embed_backend="local",
                                            embed_base_url="http://127.0.0.1:8085/v1/"))
    asyncio.run(clients.embed(["x"]))
    assert записано["url"] == "http://127.0.0.1:8085/v1/embeddings"
