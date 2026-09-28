# -*- coding: utf-8 -*-
"""Тесты сайдкара десктопа (CI): честная деградация при недоступном ABOP.

Локальный сайдкар обязан оставаться живым, когда сервер недоступен, и честно сообщать об этом:
приложение работает (история чата, настройки), а запуск агентов — нет. Клиент рисует это индикатором
в шапке, а не пустыми панелями.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

DESKTOP = pathlib.Path(__file__).resolve().parents[1] / "desktop"
sys.path.insert(0, str(DESKTOP))


@pytest.fixture()
def sidecar():
    sc = pytest.importorskip("sidecar.app", reason="сайдкар десктопа недоступен")
    from sidecar import abop_client
    orig = abop_client.health
    yield sc, abop_client
    abop_client.health = orig
    sc._ABOP_PING.update({"at": 0.0, "ok": False, "error": ""})


def test_health_reports_abop_outage_honestly(sidecar):
    sc, abop_client = sidecar
    from fastapi.testclient import TestClient

    def boom():
        raise abop_client.AbopError(0, "ConnectionRefused: сервер недоступен")

    abop_client.health = boom
    sc._ABOP_PING.update({"at": 0.0})
    c = TestClient(sc.app)
    h = c.get("/api/health").json()
    assert h["ok"] is True                      # приложение живо — локальные функции доступны
    assert h["abop"] is False and h["abop_error"]   # но сервер недоступен, и причина названа
    assert h["abop_url"]                        # UI показывает, куда именно нет связи

    abop_client.health = lambda: {"ok": True}
    sc._ABOP_PING.update({"at": 0.0})           # TTL кэша истёк
    h2 = c.get("/api/health").json()
    assert h2["abop"] is True and not h2["abop_error"]


def test_health_caches_ping(sidecar):
    """Пинг сервера кэшируется: UI опрашивает состояние часто, сервер не должен получать шквал запросов."""
    sc, abop_client = sidecar
    from fastapi.testclient import TestClient

    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {"ok": True}

    abop_client.health = counted
    sc._ABOP_PING.update({"at": 0.0})
    c = TestClient(sc.app)
    for _ in range(5):
        assert c.get("/api/health").json()["abop"] is True
    assert calls["n"] == 1
