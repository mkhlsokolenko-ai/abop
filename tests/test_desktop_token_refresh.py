# -*- coding: utf-8 -*-
"""Обновление токена десктопа: один запрос в полёте, мёртвая сессия видна, офлайн не долбит.

21.10.2026 владелец сказал: «при подключении с VPN токен от сервера не приходит, видимо
блокируется по гео». Гео-блокировки не нашлось ни в одном слое (брандмауэры обоих серверов,
Caddy, код API — нигде нет логики по адресу клиента). Keycloak стенда говорил другое:

    type="REFRESH_TOKEN_ERROR" error="invalid_token" reason="Token is not active"
    grant_type="refresh_token" clientId="portal"

2524 таких отказа за шесть часов и ни одного удачного обновления. В логе они идут парами в 1–9 мс
друг от друга: обновление звал каждый вызывающий сам, и при ротации refresh-токена второй
предъявлял уже погашенный. На медленной связи окно гонки шире — отсюда и «с VPN не работает».

Проверяем поведением, а не текстом: поддельный Keycloak считает запросы.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))


@pytest.fixture
def вход(tmp_path, monkeypatch):
    from desktop.sidecar import auth, config

    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg.json")
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "KC", "https://keycloak.example/realms/r/protocol/openid-connect")
    monkeypatch.setattr(config, "CLIENT", "portal")
    auth.save_cfg({"access_token": "старый", "refresh_token": "рт-1", "expires_at": 0})
    return auth


def _ответ(auth, monkeypatch, счёт: list, задержка: float = 0.0, токен: str = "новый"):
    def поддельный(url, data):
        счёт.append(url)
        if задержка:
            time.sleep(задержка)
        return {"access_token": токен, "refresh_token": "рт-2", "expires_in": 300}
    monkeypatch.setattr(auth, "_post_form", поддельный)


def _отказ(auth, monkeypatch, счёт: list, код: int, ошибка: str):
    def поддельный(url, data):
        счёт.append(url)
        тело = json.dumps({"error": ошибка, "error_description": "подробности"}).encode()
        raise urllib.error.HTTPError(url, код, "err", {}, __import__("io").BytesIO(тело))
    monkeypatch.setattr(auth, "_post_form", поддельный)


def test_параллельные_обновления_идут_одним_запросом(вход, monkeypatch):
    """Шесть потоков просят токен одновременно — Keycloak должен увидеть ОДИН запрос.

    Это и есть исходная причина: второй запрос с тем же refresh-токеном получает
    «Token is not active», и сохранённый токен становится мёртвым.
    """
    счёт: list = []
    _ответ(вход, monkeypatch, счёт, задержка=0.05)
    итоги: list = []
    потоки = [threading.Thread(target=lambda: итоги.append(вход.token())) for _ in range(6)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=10)
    assert len(счёт) == 1, "обновление должно быть одно в полёте, а было %d" % len(счёт)
    assert итоги and all(т == "новый" for т in итоги), итоги


def test_окончательный_отказ_просит_войти_и_перестаёт_стучать(вход, monkeypatch):
    """«invalid_grant» — это конец сессии: токены стереть, причину показать, не повторять.

    Прежде сайдкар возвращал None и шёл за токеном снова при каждом запросе интерфейса: 2524
    обращения за шесть часов, в которых тонула причина, и ни слова человеку.
    """
    счёт: list = []
    _отказ(вход, monkeypatch, счёт, 400, "invalid_grant")
    assert вход.token() is None
    состояние = вход.состояние()
    assert состояние["нужен_вход"] is True
    assert "Войдите заново" in состояние["почему"] and "срок сессии" in состояние["почему"]
    assert not вход.load_cfg().get("refresh_token"), "мёртвый токен хранить нельзя"
    for _ in range(5):
        assert вход.token() is None
    assert len(счёт) == 1, "после окончательного отказа Keycloak больше не трогаем"


def test_нет_связи_сессию_не_убивает_но_паузу_держит(вход, monkeypatch):
    """Офлайн — состояние, а не поломка: refresh-токен остаётся, но долбить сервер нельзя."""
    счёт: list = []

    def падает(url, data):
        счёт.append(url)
        raise urllib.error.URLError("нет связи")
    monkeypatch.setattr(вход, "_post_form", падает)

    assert вход.token() is None
    assert вход.load_cfg().get("refresh_token") == "рт-1", "офлайн не причина терять сессию"
    состояние = вход.состояние()
    assert состояние["нужен_вход"] is False, "просить вход при офлайне — ложь"
    assert "Нет связи" in состояние["почему"]
    for _ in range(4):
        вход.token()
    assert len(счёт) == 1, "в паузе после сбоя повторов быть не должно"

    # Пауза кончилась — пробуем снова, и связь вернулась.
    cfg = вход.load_cfg()
    cfg["пауза_до"] = time.time() - 1
    вход.save_cfg(cfg)
    _ответ(вход, monkeypatch, счёт)
    assert вход.token() == "новый"
    assert вход.состояние()["почему"] == "", "удача снимает пометку о сбое"


def test_удачный_вход_снимает_просьбу_войти(вход, monkeypatch):
    """Вошли — значит, предупреждение должно исчезнуть, а не висеть у вошедшего."""
    счёт: list = []
    _отказ(вход, monkeypatch, счёт, 400, "invalid_grant")
    вход.token()
    assert вход.состояние()["нужен_вход"] is True
    вход._store_tokens({"access_token": "а", "refresh_token": "р", "expires_in": 300})
    assert вход.состояние() == {"нужен_вход": False, "почему": "", "ждём_до": 0.0,
                                "запомнили": False}


def test_сайдкар_отдаёт_причину_в_api_auth_me(вход, monkeypatch):
    """Интерфейс не умеет догадываться: причину он должен получить ответом, а не из логов."""
    счёт: list = []
    _отказ(вход, monkeypatch, счёт, 400, "invalid_grant")
    from desktop.sidecar import app as sidecar_app

    monkeypatch.setattr(sidecar_app, "auth", вход)
    ответ = sidecar_app.me()
    assert ответ["authed"] is False and ответ["нужен_вход"] is True
    assert "Войдите заново" in ответ["почему"]


def test_запомнить_меня_просит_offline_сессию(вход, monkeypatch, tmp_path):
    """Обычная сессия живёт сутки. «Запомнить» — это offline-область, а не галочка в интерфейсе."""
    адреса: list = []
    monkeypatch.setattr(вход, "_post_form", lambda url, data: {
        "access_token": "а", "refresh_token": "р", "expires_in": 300,
        "scope": "openid offline_access"})

    class _Поток:
        """Вход блокирует до редиректа; здесь нас интересует только собранный адрес."""

        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(вход.threading, "Thread", _Поток)
    вход.login(on_url=адреса.append, timeout_s=0, запомнить=True)
    assert адреса and "offline_access" in адреса[0], адреса
    адреса.clear()
    вход.login(on_url=адреса.append, timeout_s=0, запомнить=False)
    assert адреса and "offline_access" not in адреса[0], адреса


def test_запомнили_только_если_сервер_выдал_область(вход, monkeypatch):
    """Обещать «запомнили», когда Keycloak offline-область не дал, — ложь на сутки."""
    держатель = {"scope": "openid"}
    monkeypatch.setattr(вход, "_post_form", lambda url, data: {
        "access_token": "а.б.в", "refresh_token": "р", "expires_in": 300,
        "scope": держатель["scope"]})
    вход._store_tokens({"access_token": "а", "refresh_token": "р", "expires_in": 300})
    # Эмулируем хвост login: что записано — то, что ВЫДАЛ сервер.
    cfg = вход.load_cfg()
    cfg["запомнили"] = "offline" in держатель["scope"]
    вход.save_cfg(cfg)
    assert вход.состояние()["запомнили"] is False


def test_выход_отзывает_сессию_на_сервере(вход, monkeypatch):
    """«Вышел» не должно означать «забыл локально»: запомненный токен — действующий ключ."""
    звонки: list = []

    def поддельный(url, data):
        звонки.append((url, data.decode()))
        return {}
    monkeypatch.setattr(вход, "_post_form", поддельный)
    итог = вход.logout()
    assert итог["отозван"] is True
    assert звонки and звонки[0][0].endswith("/logout"), звонки
    assert "refresh_token=%D1%80%D1%82-1" in звонки[0][1] or "refresh_token=рт-1" in звонки[0][1]
    assert not вход.load_cfg().get("refresh_token")


def test_выход_без_связи_говорит_что_на_сервере_не_закрыл(вход, monkeypatch):
    """Офлайн: локально вышли, на сервере сессия жива — и человек должен это узнать."""
    def падает(url, data):
        raise urllib.error.URLError("нет связи")
    monkeypatch.setattr(вход, "_post_form", падает)
    итог = вход.logout()
    assert итог["отозван"] is False and "нет связи" in итог["почему"]
    assert not вход.load_cfg().get("access_token"), "локально выйти обязаны всё равно"
