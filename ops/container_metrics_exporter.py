# -*- coding: utf-8 -*-
"""Метрики контейнеров для Prometheus: процессор и память по каждому контейнеру.

Зачем своё. 19.10.2026 в контейнере демо-сервиса трое суток работал майнер, и мониторинг его не
видел — метрик по контейнерам не было вовсе. Штатный cAdvisor на этом хосте показывает только
systemd-юниты: cgroup-и контейнеров (`/sys/fs/cgroup/system.slice/docker-*.scope`) он видит, но в
метрики не отдаёт. Разбираться в его внутренностях дороже, чем прочитать те же файлы самому:
cgroup v2 отдаёт расход процессора и памяти обычным текстом.

Зависимостей нет: стандартная библиотека. Имя контейнера берётся из Docker по сокету, и если
сокет недоступен — метрики всё равно идут, просто с идентификатором вместо имени.

Запуск: python container_metrics_exporter.py [порт]   (по умолчанию 9102)
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

КОРЕНЬ_CGROUP = os.environ.get("CGROUP_ROOT", "/sys/fs/cgroup")
СОКЕТ_DOCKER = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
ПОРТ = int(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PORT", "9102"))
# Контейнер в cgroup v2 с systemd-драйвером: system.slice/docker-<64 hex>.scope
ИМЯ_CGROUP = re.compile(r"^docker-([0-9a-f]{12,64})\.scope$")


class СокетHTTP(http.client.HTTPConnection):
    """HTTP поверх unix-сокета: Docker слушает именно так."""

    def __init__(self, путь: str):
        super().__init__("localhost")
        self._путь = путь

    def connect(self):
        с = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        с.settimeout(5)
        с.connect(self._путь)
        self.sock = с


def имена_контейнеров() -> dict:
    """id → имя. Docker недоступен — вернём пусто: метрики важнее подписи."""
    try:
        к = СокетHTTP(СОКЕТ_DOCKER)
        к.request("GET", "/containers/json?all=0")
        ответ = json.loads(к.getresponse().read().decode("utf-8"))
        к.close()
    except Exception:  # noqa: BLE001 — экспортер не должен падать из-за соседа
        return {}
    out = {}
    for к_ in ответ:
        имя = (к_.get("Names") or ["/?"])[0].lstrip("/")
        out[str(к_.get("Id") or "")] = имя
    return out


def _число(путь: str, ключ: str = "") -> int:
    """Значение из файла cgroup: либо всё содержимое, либо поле `ключ` из `cpu.stat`."""
    try:
        with open(путь, encoding="utf-8") as ф:
            текст = ф.read()
    except OSError:
        return -1
    if not ключ:
        текст = текст.strip()
        return int(текст) if текст.isdigit() else -1
    for строка in текст.splitlines():
        части = строка.split()
        if len(части) == 2 and части[0] == ключ and части[1].isdigit():
            return int(части[1])
    return -1


def собрать() -> list:
    """Расход по каждому контейнеру: (идентификатор, имя, секунды процессора, байты памяти)."""
    каталог = os.path.join(КОРЕНЬ_CGROUP, "system.slice")
    имена = имена_контейнеров()
    строки = []
    try:
        записи = sorted(os.listdir(каталог))
    except OSError:
        return строки
    for запись in записи:
        совпало = ИМЯ_CGROUP.match(запись)
        if not совпало:
            continue
        ид = совпало.group(1)
        путь = os.path.join(каталог, запись)
        микросекунды = _число(os.path.join(путь, "cpu.stat"), "usage_usec")
        память = _число(os.path.join(путь, "memory.current"))
        if микросекунды < 0:
            continue
        имя = next((и for к, и in имена.items() if к.startswith(ид) or ид.startswith(к)), ид[:12])
        строки.append((ид[:12], имя, микросекунды / 1_000_000.0, max(0, память)))
    return строки


def экспорт() -> str:
    части = [
        "# HELP abop_container_cpu_seconds_total Процессорное время контейнера, секунды",
        "# TYPE abop_container_cpu_seconds_total counter",
    ]
    данные = собрать()
    for ид, имя, секунды, _память in данные:
        части.append('abop_container_cpu_seconds_total{id="%s",name="%s"} %.6f' % (ид, имя, секунды))
    части += [
        "# HELP abop_container_memory_bytes Память контейнера, байты",
        "# TYPE abop_container_memory_bytes gauge",
    ]
    for ид, имя, _секунды, память in данные:
        части.append('abop_container_memory_bytes{id="%s",name="%s"} %d' % (ид, имя, память))
    части += [
        "# HELP abop_containers_observed Сколько контейнеров удалось прочитать",
        "# TYPE abop_containers_observed gauge",
        "abop_containers_observed %d" % len(данные),
        "",
    ]
    return "\n".join(части)


class Обработчик(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        тело = экспорт().encode("utf-8") if self.path.startswith("/metrics") else b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(тело)))
        self.end_headers()
        self.wfile.write(тело)

    def log_message(self, *_а):  # тишина: каждый скрейп раз в 15 секунд — это шум
        return


if __name__ == "__main__":
    print("экспортер метрик контейнеров на :%d, cgroup=%s" % (ПОРТ, КОРЕНЬ_CGROUP), flush=True)
    HTTPServer(("0.0.0.0", ПОРТ), Обработчик).serve_forever()
