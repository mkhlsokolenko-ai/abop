# -*- coding: utf-8 -*-
"""Дозапрос карточки в http-адаптере: список REST API не отдаёт содержимое.

BookStack `/api/pages` возвращает имя страницы без `html`, Redmine `/issues` — без `description`.
Рецепт `contracts` из-за этого клал в сущность `document` только id/kind/title: навык «читает
документ», в котором нет ни одного символа текста. Mailpit-адаптер ровно эту проблему уже решал
дозапросом тела письма — здесь приём обобщён на REST.
"""
from __future__ import annotations

import io as _io
import json
import os
import sys
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from cli import ape  # noqa: E402


class _Ответ(_io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _сеть(monkeypatch, карта: dict, журнал: list):
    """Подменяет сеть картой URL→JSON. Журнал фиксирует порядок запросов."""
    def fake(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        журнал.append(url)
        if url not in карта:
            raise urllib.error.HTTPError(url, 404, "нет", {}, None)
        return _Ответ(json.dumps(карта[url]).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_без_detail_строки_как_были(monkeypatch):
    журнал = []
    _сеть(monkeypatch, {"http://wiki.test/api/pages": {"data": [{"id": 1, "name": "Договор"}]}}, журнал)
    rows = ape._adapter_http({"url": "http://wiki.test/api/pages", "root": "data"})
    assert rows == [{"id": 1, "name": "Договор"}]
    assert len(журнал) == 1, "лишних запросов быть не должно"


def test_detail_подмешивает_текст_карточки(monkeypatch):
    журнал = []
    _сеть(monkeypatch, {
        "http://wiki.test/api/pages": {"data": [{"id": 1, "name": "Договор"}, {"id": 2, "name": "Акт"}]},
        "http://wiki.test/api/pages/1": {"id": 1, "html": "<p>Предмет договора</p>", "lish": "мусор"},
        "http://wiki.test/api/pages/2": {"id": 2, "html": "<p>Работы приняты</p>"},
    }, журнал)
    rows = ape._adapter_http({"url": "http://wiki.test/api/pages", "root": "data",
                              "detail": {"url": "http://wiki.test/api/pages/{id}", "fields": ["html"]}})
    assert [r["html"] for r in rows] == ["<p>Предмет договора</p>", "<p>Работы приняты</p>"]
    assert "lish" not in rows[0], "подмешиваем только объявленные поля"
    assert len(журнал) == 3


def test_недоступная_карточка_не_валит_рецепт(monkeypatch):
    журнал = []
    _сеть(monkeypatch, {
        "http://wiki.test/api/pages": {"data": [{"id": 1, "name": "Есть"}, {"id": 9, "name": "Нет"}]},
        "http://wiki.test/api/pages/1": {"id": 1, "html": "<p>текст</p>"},
    }, журнал)
    rows = ape._adapter_http({"url": "http://wiki.test/api/pages", "root": "data",
                              "detail": {"url": "http://wiki.test/api/pages/{id}", "fields": ["html"]}})
    assert len(rows) == 2 and rows[0].get("html") and "html" not in rows[1]


def test_предел_дозапросов(monkeypatch):
    журнал = []
    карта = {"http://wiki.test/api/pages": {"data": [{"id": i} for i in range(5)]}}
    карта.update({f"http://wiki.test/api/pages/{i}": {"html": f"<p>{i}</p>"} for i in range(5)})
    _сеть(monkeypatch, карта, журнал)
    rows = ape._adapter_http({"url": "http://wiki.test/api/pages", "root": "data",
                              "detail": {"url": "http://wiki.test/api/pages/{id}",
                                         "fields": ["html"], "limit": 2}})
    assert len(rows) == 5
    assert sum(1 for r in rows if "html" in r) == 2, "предел обязан ограничивать число дозапросов"


def test_плоские_поля_из_UI(monkeypatch):
    журнал = []
    _сеть(monkeypatch, {
        "http://wiki.test/api/pages": {"data": [{"id": 1}]},
        "http://wiki.test/api/pages/1": {"html": "<p>из UI</p>", "markdown": "из UI"},
    }, журнал)
    rows = ape._adapter_http({"url": "http://wiki.test/api/pages", "root": "data",
                              "detail_url": "http://wiki.test/api/pages/{id}",
                              "detail_fields": "html, markdown"})
    assert rows[0]["markdown"] == "из UI"


def test_приведение_html_в_текст():
    assert ape._cast("<p>Предмет&nbsp;договора</p>", "text") == "Предмет договора"
    assert ape._cast("<style>p{}</style><p>Текст</p>", "text") == "Текст"
    assert ape._cast("  уже текст  ", "plain") == "уже текст"
