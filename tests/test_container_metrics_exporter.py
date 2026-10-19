# -*- coding: utf-8 -*-
"""Экспортер метрик контейнеров читает cgroup честно и не падает, когда что-то недоступно.

19.10.2026 майнер в контейнере прожил трое суток незамеченным: метрик по контейнерам не было.
Штатный cAdvisor на этом хосте отдаёт только systemd-юниты, поэтому расход читается прямо из
cgroup v2. Тест проверяет разбор на подставном дереве каталогов — настоящий `/sys/fs/cgroup`
в тестах недоступен, а ошибиться в парсинге легко: `cpu.stat` отдаёт микросекунды, а не секунды.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]


def _модуль(monkeypatch, корень_cgroup: Path, порт: str = "9102"):
    monkeypatch.setenv("CGROUP_ROOT", str(корень_cgroup))
    monkeypatch.setenv("PORT", порт)
    monkeypatch.setattr(sys, "argv", ["container_metrics_exporter.py"])
    спец = importlib.util.spec_from_file_location(
        "экспортер_контейнеров", КОРЕНЬ / "ops" / "container_metrics_exporter.py")
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    monkeypatch.setattr(м, "имена_контейнеров", lambda: {})
    return м


def _контейнер(корень: Path, ид: str, микросекунды: int, память: int) -> None:
    путь = корень / "system.slice" / ("docker-%s.scope" % ид)
    путь.mkdir(parents=True)
    (путь / "cpu.stat").write_text(
        "usage_usec %d\nuser_usec %d\nsystem_usec 10\n" % (микросекунды, микросекунды - 10),
        encoding="utf-8")
    (путь / "memory.current").write_text("%d\n" % память, encoding="utf-8")


def test_расход_читается_из_cgroup(tmp_path, monkeypatch):
    _контейнер(tmp_path, "a" * 64, 7_500_000, 1048576)
    м = _модуль(monkeypatch, tmp_path)
    собрано = м.собрать()
    assert len(собрано) == 1
    ид, имя, секунды, память = собрано[0]
    assert ид == "a" * 12
    assert abs(секунды - 7.5) < 0.001, "микросекунды cpu.stat обязаны стать секундами"
    assert память == 1048576
    assert имя == "a" * 12, "без доступа к Docker подписью служит идентификатор"


def test_имя_контейнера_подставляется_когда_docker_доступен(tmp_path, monkeypatch):
    _контейнер(tmp_path, "b" * 64, 1_000_000, 2048)
    м = _модуль(monkeypatch, tmp_path)
    monkeypatch.setattr(м, "имена_контейнеров", lambda: {"b" * 64: "svod-targets-gitea-1"})
    assert м.собрать()[0][1] == "svod-targets-gitea-1"


def test_посторонние_cgroup_не_попадают(tmp_path, monkeypatch):
    """В system.slice живут и обычные юниты — их расход к контейнерам не относится."""
    _контейнер(tmp_path, "c" * 64, 2_000_000, 512)
    юнит = tmp_path / "system.slice" / "cron.service"
    юнит.mkdir(parents=True)
    (юнит / "cpu.stat").write_text("usage_usec 999999999\n", encoding="utf-8")
    м = _модуль(monkeypatch, tmp_path)
    имена = {с[1] for с in м.собрать()}
    assert имена == {"c" * 12}


def test_недоступный_cgroup_не_роняет_экспортер(tmp_path, monkeypatch):
    """Нет каталога или файла — отдаём пусто, а не трассировку: иначе молчит весь мониторинг."""
    м = _модуль(monkeypatch, tmp_path / "нет-такого")
    assert м.собрать() == []
    текст = м.экспорт()
    assert "abop_containers_observed 0" in текст


def test_формат_пригоден_для_prometheus(tmp_path, monkeypatch):
    _контейнер(tmp_path, "d" * 64, 3_000_000, 4096)
    м = _модуль(monkeypatch, tmp_path)
    текст = м.экспорт()
    assert "# TYPE abop_container_cpu_seconds_total counter" in текст
    assert "# TYPE abop_container_memory_bytes gauge" in текст
    строка = [с for с in текст.splitlines() if с.startswith("abop_container_cpu_seconds_total{")][0]
    assert 'id="' in строка and 'name="' in строка and строка.endswith("3.000000")
    # Каждая строка метрики — «имя{метки} значение», иначе Prometheus отбросит весь скрейп.
    for с in текст.splitlines():
        if с and not с.startswith("#"):
            assert len(с.rsplit(" ", 1)) == 2 and с.rsplit(" ", 1)[1].replace(".", "").isdigit()
