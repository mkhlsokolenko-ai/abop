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
    from sidecar import abop_client, auth
    orig, orig_token = abop_client.health, auth.token
    # Вход в ABOP тесту не нужен, а настоящий `auth.token()` при просроченном токене уходит в сеть за
    # обновлением: на машине разработчика, где десктопом пользовались, каждый запрос тогда занимает
    # секунды — и проверка кэша пинга начинает зависеть от сети, а не от кэша.
    auth.token = lambda: None
    yield sc, abop_client
    abop_client.health, auth.token = orig, orig_token
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
    assert calls["n"] == 1, "пять опросов состояния — один запрос к серверу"

    # Истёк TTL — спрашиваем заново: кэш обязан протухать, иначе UI не заметит, что связь вернулась.
    sc._ABOP_PING.update({"at": 0.0})
    assert c.get("/api/health").json()["abop"] is True
    assert calls["n"] == 2


def test_sidecar_proxies_departments_endpoints(sidecar):
    """Шаг 3 «Отделов»: карточка отдела (ИКА + аудит назначения) и срез «Решений» по отделу
    проходят через сайдкар тем же тонким прокси. Панель чисел не считает — только показывает."""
    sc, abop_client = sidecar
    from fastapi.testclient import TestClient

    sent = {}

    def _ика(fid, дней=30):
        sent["ika"] = (fid, дней)
        return {"семья": fid, "ика": 74.4}

    def _аудит():
        sent["audit"] = True
        return {"строки": [{"id": "x.v1", "family": "audit", "серьёзность": "crit"}]}

    def _находки(project="", process="", organisation="", отдел=""):
        sent["findings"] = отдел
        return {"findings": [], "сводка": {"всего": 0}}

    abop_client.family_ika = _ика
    abop_client.assignment_audit = _аудит
    abop_client.findings_open = _находки

    # Модули монтируются на startup-событии: без контекст-менеджера ручки модулей отсутствуют.
    with TestClient(sc.app) as c:
        р1 = c.get("/api/modules/agents/families/ika?family=finance&дней=30").json()
        assert р1["ика"] == 74.4 and sent["ika"] == ("finance", 30)
        р2 = c.get("/api/modules/agents/assignment-audit").json()
        assert р2["строки"][0]["серьёзность"] == "crit"
        р3 = c.get("/api/modules/decisions/findings?отдел=finance").json()
        assert sent["findings"] == "finance"
        assert "findings" in р3
