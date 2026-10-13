# -*- coding: utf-8 -*-
"""Вход в приложение: петлевой PKCE должен выживать ровно там, где он падал.

13.10 владелец прислал ссылку, на которой вход обрывался:
`http://127.0.0.1:64902/callback?state=…&code=…` — страница не открывалась. Разбор дал ДВЕ
независимые ошибки, и каждая по отдельности даёт ровно этот симптом:

1. **Адрес входа переживал попытку.** `auth.login` выставлял `login.last_url` — атрибут функции, —
   а маршрут сайдкара опрашивал его, чтобы открыть браузер. На второй попытке атрибут уже
   существовал, со старым портом, и опрос находил его мгновенно: браузер уходил на прошлый
   `redirect_uri`, которого уже никто не слушал.
2. **Петлевой сервер обрабатывал один запрос.** `handle_request()` — это ровно один заход: любое
   обращение браузера до кода (предварительное соединение, `/favicon.ico`, обновление страницы)
   съедало его, и настоящий `/callback` получал отказ соединения.

Проверяем ПОВЕДЕНИЕМ: поднимаем поддельный Keycloak на петле, ходим на `/callback` настоящим
HTTP-запросом. Текстовые проверки тут бесполезны — обе ошибки выглядели как совершенно разумный код.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))


@pytest.fixture
def вход(tmp_path, monkeypatch):
    """Модуль входа с поддельным Keycloak и своим файлом настроек."""
    from desktop.sidecar import auth, config

    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg.json")
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "KC", "https://keycloak.example/realms/r/protocol/openid-connect")
    monkeypatch.setattr(config, "CLIENT", "portal")
    # Обмен кода на токен не трогает сеть: проверяем петлю и адреса, а не Keycloak.
    обмены: list[dict] = []

    def _обмен(url, data):
        import urllib.parse as up

        поля = dict(up.parse_qsl(data.decode()))
        обмены.append(поля)
        полезное = {"preferred_username": "михаил"}
        тело = __import__("base64").urlsafe_b64encode(
            json.dumps(полезное).encode()).rstrip(b"=").decode()
        return {"access_token": "x." + тело + ".y", "refresh_token": "r", "expires_in": 300}

    monkeypatch.setattr(auth, "_post_form", _обмен)
    if hasattr(auth.login, "last_url"):
        delattr(auth.login, "last_url")
    return {"auth": auth, "обмены": обмены}


def _порт(url: str) -> int:
    import urllib.parse as up

    q = dict(up.parse_qsl(up.urlparse(url).query))
    return int(up.urlparse(q["redirect_uri"]).port)


КОД = "код-авторизации-1"


def _зайти(url: str, путь: str = "") -> int:
    """Настоящий HTTP-запрос на петлевой сервер. Возвращает код ответа."""
    import urllib.parse as up

    q = dict(up.parse_qsl(up.urlparse(url).query))
    # Значения кодируем процентами: кириллица в строке запроса — не ASCII, и `http.client`
    # откажется её отправлять. Падал бы тест, а не вход, но разбираться пришлось бы дважды.
    адрес = q["redirect_uri"] + (путь or "?code=" + up.quote(КОД) + "&state=" + up.quote(q["state"]))
    with urllib.request.urlopen(адрес, timeout=5) as r:
        return r.status


def test_вход_завершается_по_настоящему_запросу(вход):
    """Базовый случай: код пришёл на /callback — вход состоялся, обмен ушёл с тем же redirect_uri."""
    auth = вход["auth"]
    адрес: dict = {}
    итог: dict = {}

    поток = threading.Thread(
        target=lambda: итог.update(auth.login(timeout_s=10, on_url=lambda u: адрес.update(u=u))),
        daemon=True)
    поток.start()
    for _ in range(100):
        if адрес.get("u"):
            break
        time.sleep(0.02)
    assert адрес.get("u"), "адрес входа не отдан вызывающему"
    assert _зайти(адрес["u"]) == 200
    поток.join(timeout=12)
    assert итог.get("ok") is True, итог
    assert итог.get("user") == "михаил"
    обмен = вход["обмены"][0]
    assert обмен["code"] == КОД
    assert обмен["redirect_uri"].endswith("/callback")
    assert int(обмен["redirect_uri"].rsplit(":", 1)[1].split("/")[0]) == _порт(адрес["u"]), (
        "обмен ушёл с другим redirect_uri, чем был в запросе — Keycloak такой обмен отклонит")


def test_посторонний_запрос_не_съедает_петлю(вход):
    """Предварительное соединение браузера или /favicon.ico не должны ломать вход.

    Это ошибка №2: сервер обрабатывал ровно один запрос, и первый посторонний заход закрывал его —
    настоящий /callback получал отказ соединения.
    """
    auth = вход["auth"]
    адрес: dict = {}
    итог: dict = {}
    поток = threading.Thread(
        target=lambda: итог.update(auth.login(timeout_s=10, on_url=lambda u: адрес.update(u=u))),
        daemon=True)
    поток.start()
    for _ in range(100):
        if адрес.get("u"):
            break
        time.sleep(0.02)
    # посторонние заходы ДО кода
    for путь in ("?x=1", "?x=2"):
        try:
            _зайти(адрес["u"], путь)
        except urllib.error.URLError as ex:          # noqa: PERF203 — важно, какой именно сбой
            pytest.fail(f"петля закрылась на постороннем запросе: {ex}")
    assert _зайти(адрес["u"]) == 200, "настоящий /callback не дошёл"
    поток.join(timeout=12)
    assert итог.get("ok") is True, итог


def test_вторая_попытка_получает_свой_адрес(вход):
    """Ошибка №1: адрес не должен переживать попытку — иначе браузер уйдёт на мёртвый порт."""
    auth = вход["auth"]
    адреса: list[str] = []
    for _ in range(2):
        держатель: dict = {}
        поток = threading.Thread(
            target=lambda: держатель.update(
                auth.login(timeout_s=2, on_url=lambda u: адреса.append(u))),
            daemon=True)
        поток.start()
        поток.join(timeout=8)         # попытку не завершаем: интересен именно адрес
    assert len(адреса) == 2, f"адрес отдан не на каждую попытку: {len(адреса)}"
    assert _порт(адреса[0]) != _порт(адреса[1]), (
        "вторая попытка получила порт первой — браузер уйдёт туда, где уже никто не слушает")


def test_маршрут_открывает_браузер_адресом_попытки():
    """Маршрут обязан брать адрес из вызова, а не из пережившего попытку атрибута."""
    код = (КОРЕНЬ / "desktop" / "sidecar" / "app.py").read_text(encoding="utf-8")
    i = код.index("def do_login")
    тело = код[i:код.index("\n@app.", i)]
    # Комментарии выкидываем: объяснение «почему не last_url» в коде НУЖНО, и ловить его как
    # нарушение значит требовать молчания там, где следующий читатель снимет правку как лишнюю.
    живое = "\n".join(с for с in тело.splitlines() if not с.strip().startswith("#"))
    assert "on_url=" in живое, "адрес входа снова читается не из вызова"
    assert "last_url" not in живое, (
        "маршрут вернулся к атрибуту функции — он переживает попытку и даёт мёртвый порт")


def test_отказ_keycloak_виден_человеку(вход):
    """`error=access_denied` не должен выглядеть как успешный вход, после которого ничего не вышло."""
    auth = вход["auth"]
    адрес: dict = {}
    итог: dict = {}
    поток = threading.Thread(
        target=lambda: итог.update(auth.login(timeout_s=10, on_url=lambda u: адрес.update(u=u))),
        daemon=True)
    поток.start()
    for _ in range(100):
        if адрес.get("u"):
            break
        time.sleep(0.02)
    import urllib.parse as up

    _зайти(адрес["u"], "?error=access_denied&error_description=" + up.quote("Нет прав"))
    поток.join(timeout=12)
    assert итог.get("ok") is False
    assert "Нет прав" in итог.get("error", ""), итог


def test_недоступный_keycloak_объясняется_словами(вход):
    """Срок вышел, кода нет — человеку надо сказать, куда смотреть, а не «таймаут или отказ»."""
    auth = вход["auth"]
    итог = auth.login(timeout_s=1, on_url=lambda u: None)
    assert итог["ok"] is False
    assert "keycloak.example" in итог["error"], (
        f"в отказе нет адреса, который надо проверить: {итог['error']}")
    assert "VPN" in итог["error"] or "прокси" in итог["error"], (
        "не названа самая частая причина — сеть между приложением и Keycloak")
