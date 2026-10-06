# -*- coding: utf-8 -*-
"""Кириллические идентификаторы в адресах: кнопка «на доску» падала там, где сеть была ни при чём.

06.10 владелец нажал «🧷 На доску» и получил «ABOP недоступен — проверьте сеть и повторите
(UnicodeEncodeError: 'ascii' codec can't encode characters in position 35-40)». Сеть была в порядке:
у агента «Анализ идеи» идентификатор кириллический (`authored-…-анализ-идеи.v1`), прогон наследует
его, а urllib отказывается открывать адрес с не-ASCII символами.

Экранирование перенесено в одно место — в `_req`, через который идут ВСЕ вызовы. Иначе та же ошибка
возвращается при каждом новом маршруте, где об экранировании забыли, и выглядит как сбой сети.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

DESKTOP = pathlib.Path(__file__).resolve().parents[1] / "desktop"
sys.path.insert(0, str(DESKTOP))

RID = "run-authored-77fa87-анализ-идеи.v1-8770ca"


@pytest.fixture()
def клиент():
    pytest.importorskip("sidecar.abop_client", reason="сайдкар десктопа недоступен")
    from sidecar import abop_client, auth
    saved = auth.token
    auth.token = lambda: "jwt"
    yield abop_client
    auth.token = saved


def test_кириллический_прогон_не_роняет_запрос(клиент, monkeypatch):
    """Главное: адрес собирается, а не падает на кодировке. Что ответит сервер — дело сервера."""
    видели = {}

    class _R:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=0):
        видели["url"] = req.full_url
        видели["body"] = req.data
        return _R()

    monkeypatch.setattr(клиент.urllib.request, "urlopen", _open)
    r = клиент.board_from_run(RID, "user", "", "проверка")
    assert r.get("ok") is True
    assert "анализ" not in видели["url"], "кириллица осталась в адресе — urllib на нём падает"
    assert "%D0%B0" in видели["url"], f"адрес не экранирован: {видели['url']}"
    assert видели["url"].isascii(), "в адресе остались не-ASCII символы"
    assert "/to-board" in видели["url"], "маршрут выноса на доску потерялся при экранировании"


def test_уже_экранированное_не_экранируется_дважды(клиент, monkeypatch):
    """Часть вызовов экранирует путь сама (задания очереди). Двойное экранирование превратило бы
    `%D0%90` в `%25D0%2590`, и сервер перестал бы находить объект."""
    видели = {}

    class _R:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(клиент.urllib.request, "urlopen",
                        lambda req, timeout=0: (видели.update(url=req.full_url), _R())[1])
    клиент.run_job("job-ждём-результат")
    assert "%25" not in видели["url"], f"двойное экранирование: {видели['url']}"


def test_экранирование_живёт_в_одном_месте():
    """Если его опять размазать по вызовам, забытый маршрут снова будет выглядеть сбоем сети."""
    src = (DESKTOP / "sidecar" / "abop_client.py").read_text(encoding="utf-8")
    i = src.index("def _req(")
    тело = src[i:i + 1400]
    assert "urllib.parse.quote(path" in тело, "адрес больше не экранируется в общей точке"
    assert "safe=" in тело, "без safe теряются разделители запроса и уже экранированные куски"
