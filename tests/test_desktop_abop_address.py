# -*- coding: utf-8 -*-
"""Адрес ABOP — настройка в «Кабинете», а не переменная среды.

12.11.2026 владелец остался без ABOP: маршрут до прода перестал работать, сервер при этом был жив
(из Амстердама `/api/health` отвечал за 0,2 с). Переключить адрес было нечем — он читался ОДИН раз
при импорте сайдкара, и единственным выходом были `setx` и перезапуск приложения, то есть доступ к
машине человека.

Что здесь закреплено:

* **три источника и их сила**: переменная среды → настройка → умолчание. Среда сильнее файла
  намеренно: ею пользуются разработка и CI, и она не должна уступать сохранённому значению;
* **значение читается на каждый запрос**, а не на импорт: иначе правка в «Кабинете» требует
  перезапуска, чего человек не ожидает;
* **непроверенный адрес не сохраняется.** Иначе человек останется без связи и без понимания,
  почему: экран скажет «ABOP недоступен», а причина — опечатка, которую он сам же и внёс;
* **отказ называет адрес.** «ABOP недоступен» отправляет чинить сеть там, где сменился адрес.
"""
from __future__ import annotations

import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ / "desktop"))

ПАНЕЛЬ = io.open(КОРЕНЬ / "desktop" / "ui" / "modules" / "cabinet" / "panel.js",
                 encoding="utf-8").read()
КОНФИГ = io.open(КОРЕНЬ / "desktop" / "sidecar" / "config.py", encoding="utf-8").read()


@pytest.fixture()
def дом(tmp_path, monkeypatch):
    """Отдельный каталог настроек: тест не должен трогать настоящий ~/.abop-desktop."""
    from sidecar import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("ABOP_BASE_URL", raising=False)
    return tmp_path


class _Заглушка(BaseHTTPRequestHandler):
    """Минимальный «ABOP»: отвечает на /api/health так же, как настоящий."""

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        тело = json.dumps({"ok": True}).encode() if self.path == "/api/health" else b"{}"
        self.send_response(200 if self.path == "/api/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(тело)))
        self.end_headers()
        self.wfile.write(тело)

    def log_message(self, *a):  # тишина в выводе теста
        pass


@pytest.fixture()
def похожий_на_abop():
    сервер = HTTPServer(("127.0.0.1", 0), _Заглушка)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % сервер.server_port
    сервер.shutdown()


def test_порядок_источников(дом, monkeypatch):
    from sidecar import config
    assert config.источник_адреса() == "умолчание"
    assert config.адрес_abop() == config.ABOP_ПО_УМОЛЧАНИЮ

    (дом / "config.json").write_text(json.dumps({"abop_base_url": "http://из-файла:8091"}),
                                     encoding="utf-8")
    assert config.адрес_abop() == "http://из-файла:8091"
    assert config.источник_адреса() == "настройка"

    monkeypatch.setenv("ABOP_BASE_URL", "http://из-среды:8091/")
    assert config.адрес_abop() == "http://из-среды:8091", "среда обязана быть сильнее файла"
    assert config.источник_адреса() == "переменная среды"


def test_значение_читается_на_каждый_запрос(дом):
    """Прежде адрес был константой: смена требовала перезапуска приложения."""
    from sidecar import config
    первый = config.ABOP
    (дом / "config.json").write_text(json.dumps({"abop_base_url": "http://другой:8091"}),
                                     encoding="utf-8")
    assert config.ABOP == "http://другой:8091" != первый, (
        "config.ABOP вернул снимок на момент импорта — правка в «Кабинете» не подействует")


def test_непроверенный_адрес_не_сохраняется(дом):
    from sidecar import config
    from sidecar.modules.cabinet import module as m
    было = config.адрес_abop()
    для_проверки = ["", "ftp://х", "http://127.0.0.1:9/нет-такого",
                    "http://кириллица.рф:8091", "127.0.0.1:8091"]
    for адрес in для_проверки:
        о = m.abop_address_save(m.АдресIn(адрес=адрес))
        assert о["ok"] is False, адрес
        assert о["error"], адрес
    assert config.адрес_abop() == было, "адрес изменился после отказа"
    assert not (дом / "config.json").exists() or "abop_base_url" not in json.loads(
        (дом / "config.json").read_text(encoding="utf-8"))


def test_отказ_называет_адрес(дом):
    """«ABOP недоступен» отправляет чинить сеть там, где сменился адрес."""
    from sidecar.modules.cabinet import module as m
    о = m.abop_address_save(m.АдресIn(адрес="http://127.0.0.1:9/no-such"))
    assert "127.0.0.1:9" in о["error"], о


def test_не_abop_по_адресу_тоже_отказ(дом, monkeypatch):
    """По адресу может ответить что угодно. Подменять установку на случайный сервер нельзя."""
    from sidecar.modules.cabinet import module as m

    class ЧужойСервер(_Заглушка):
        def do_GET(self):  # noqa: N802
            тело = b'{"nginx": "hello"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(тело)))
            self.end_headers()
            self.wfile.write(тело)

    сервер = HTTPServer(("127.0.0.1", 0), ЧужойСервер)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    try:
        о = m.abop_address_save(m.АдресIn(адрес="http://127.0.0.1:%d" % сервер.server_port))
        assert о["ok"] is False and "не ABOP" in о["error"], о
    finally:
        сервер.shutdown()


def test_проверенный_адрес_сохраняется_и_действует(дом, похожий_на_abop):
    from sidecar import config
    from sidecar.modules.cabinet import module as m
    о = m.abop_address_save(m.АдресIn(адрес=похожий_на_abop + "/"))
    assert о["ok"] is True, о
    assert о["адрес"] == похожий_на_abop, о
    assert о["источник"] == "настройка"
    assert "перезапускать приложение не нужно" in о["дальше"]
    # Действует сразу — без перезапуска процесса.
    assert config.ABOP == похожий_на_abop
    assert json.loads((дом / "config.json").read_text(encoding="utf-8"))["abop_base_url"] \
        == похожий_на_abop


def test_среда_перекрывает_настройку_и_экран_об_этом_говорит(дом, monkeypatch):
    from sidecar import config
    from sidecar.modules.cabinet import module as m
    (дом / "config.json").write_text(json.dumps({"abop_base_url": "http://из-файла:8091"}),
                                     encoding="utf-8")
    assert m.abop_address()["перекрыт_средой"] is False
    monkeypatch.setenv("ABOP_BASE_URL", "http://из-среды:8091")
    о = m.abop_address()
    assert о["перекрыт_средой"] is True and о["источник"] == "переменная среды"
    # Экран обязан выключить поле и сказать, почему: иначе оно выглядит сломанным.
    assert 'перекрыт_средой"]) ? "disabled" : ""' in ПАНЕЛЬ
    assert "сильнее настройки, поэтому поле выключено" in ПАНЕЛЬ


def test_экран_показывает_адрес_abop_а_не_сайдкара():
    """Строка «Сервер ABOP» показывала `ctx.base` — адрес локального сайдкара (127.0.0.1:8799).
    Человек видел «есть связь» там, где связь с ABOP как раз и потерялась."""
    assert 'строка("Сервер ABOP", String(ctx.base' not in ПАНЕЛЬ
    assert 'адресABOP && адресABOP["адрес"]' in ПАНЕЛЬ
    assert 'api(C + "/abop")' in ПАНЕЛЬ, "панель не спрашивает адрес и его источник"


def test_в_конфиге_нет_константы_адреса():
    """Константа вернула бы снимок на момент импорта. Имя `ABOP` отдаёт `__getattr__`."""
    assert "\nABOP = " not in КОНФИГ
    assert "def __getattr__" in КОНФИГ and "def адрес_abop" in КОНФИГ
