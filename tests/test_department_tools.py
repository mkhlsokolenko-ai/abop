# -*- coding: utf-8 -*-
"""Тулы отделов: реестр как данные, отдел-скоуп, эфемерное исполнение (решение владельца
14.11.2026). Главные гарантии: чужой тул для агента НЕ СУЩЕСТВУЕТ (ни в подсказке, ни в
исполнении — конверт отдела не расширяется изнутри); docker-тул поднимается на время вызова
и умирает после; отказ среды назван по-отдельности от отказа тулa.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

РЕЕСТР = [
    {"id": "pdf-разбор", "название": "Разбор PDF", "отдел": "audit", "тип": "docker",
     "образ": "registry:5000/abop/pdf-inspector:1.2", "команда": ["python", "inspect.py"],
     "таймаут": 30, "память": "256m", "цпу": "0.5", "описание": "классифицирует PDF и вынимает текст"},
    {"id": "курсы-цб", "название": "Курсы ЦБ", "отдел": "общий", "тип": "docker",
     "образ": "registry:5000/abop/cbr-rates:1.0", "сеть": True, "описание": "курсы ЦБ на дату"},
]


def _реестр(monkeypatch, строки=None):
    from server import dataplane_records as dr
    monkeypatch.setattr(dr, "query", lambda *a, **kw: list(РЕЕСТР if строки is None else строки))


def test_свой_и_общий_видны_чужой_нет(monkeypatch):
    from server import department_tools as dt
    _реестр(monkeypatch)
    имена_аудита = [t["id"] for t in dt.тулы_для("audit")]
    assert "pdf-разбор" in имена_аудита and "курсы-цб" in имена_аудита
    имена_менеджмента = [t["id"] for t in dt.тулы_для("management")]
    assert "pdf-разбор" not in имена_менеджмента, "чужой тул не должен существовать для другого отдела"
    assert "курсы-цб" in имена_менеджмента


def test_найти_чужой_тул_есть_несуществующий(monkeypatch):
    from server import department_tools as dt
    _реестр(monkeypatch)
    assert dt.найти("pdf-разбор", "audit") is not None
    assert dt.найти("pdf-разбор", "management") is None, "чужой = несуществующий, неразличимо и намеренно"


def test_подсказка_показывает_только_свои(monkeypatch):
    from server import skill_tools
    _реестр(monkeypatch)
    блок = skill_tools.prompt_block("audit1c-extract", family="audit")
    assert "pdf-разбор" in блок and "курсы-цб" in блок
    блок_другого = skill_tools.prompt_block("mail-triage", family="management")
    assert "pdf-разбор" not in блок_другого and "курсы-цб" in блок_другого


def test_исполнение_эфемерное_без_сети_и_с_лимитами(monkeypatch):
    from server import department_tools as dt
    _реестр(monkeypatch)
    вызовы: list = []

    class _Пр:
        returncode = 0
        stdout = json.dumps({"страниц": 3, "тип": "УПД"}).encode()
        stderr = b""

    def _run(cmd, **kw):
        вызовы.append((cmd, kw))
        return _Пр()

    monkeypatch.setattr(subprocess, "run", _run)
    тул = dt.найти("pdf-разбор", "audit")
    out = dt.вызвать_эфемерно(тул, {"путь": "/tmp/x.pdf"})
    assert out == {"страниц": 3, "тип": "УПД"}
    cmd, kw = вызовы[0]
    assert cmd[0:4] == ["docker", "run", "--rm", "--interactive"]
    assert "--network" in cmd and "none" in cmd
    assert "--memory" in cmd and "256m" in cmd
    assert kw.get("timeout") == 30
    assert kw.get("input")   # аргументы — JSON в stdin, не в argv/env


def test_тул_с_сетью_получает_мост(monkeypatch):
    from server import department_tools as dt
    _реестр(monkeypatch)
    вызовы: list = []

    class _Пр:
        returncode = 0
        stdout = b"{}"
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (вызовы.append(cmd) or _Пр()))
    dt.вызвать_эфемерно(dt.найти("курсы-цб", "finance"), {"дата": "2026-11-14"})
    assert "bridge" in вызовы[0], "сеть: true в реестре — осознанное исключение, видимое в команде"


def test_таймаут_убивает_вызов_с_внятным_отказом(monkeypatch):
    from server import department_tools as dt
    _реестр(monkeypatch)

    def _run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(subprocess, "run", _run)
    try:
        dt.вызвать_эфемерно(dt.найти("pdf-разбор", "audit"), {})
        raise AssertionError("ожидали отказ по таймауту")
    except RuntimeError as ex:
        assert "30 с" in str(ex) and "убит" in str(ex)


def test_docker_недоступен_отказ_среды(monkeypatch):
    from server import department_tools as dt
    _реестр(monkeypatch)

    def _run(cmd, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", _run)
    try:
        dt.вызвать_эфемерно(dt.найти("pdf-разбор", "audit"), {})
        raise AssertionError("ожидали отказ среды")
    except RuntimeError as ex:
        assert "docker недоступен" in str(ex)


def test_run_tool_чужой_тул_не_исполняется(monkeypatch):
    import asyncio
    from server import skill_tools
    from server import department_tools as dt
    _реестр(monkeypatch)
    звали: list = []

    def _run(cmd, **kw):
        звали.append(cmd)
        raise AssertionError("эфемерный вызов не должен был случиться")

    monkeypatch.setattr(subprocess, "run", _run)
    ответ = asyncio.run(skill_tools.run_tool("mail-triage", "pdf-разбор", {}, family="management"))
    assert "не разрешён" in ответ and not звали

    class _Пр:
        returncode = 0
        stdout = json.dumps({"страниц": 3, "тип": "УПД"}).encode()
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Пр())
    свой = asyncio.run(skill_tools.run_tool("audit1c-extract", "pdf-разбор", {}, family="audit"))
    assert "страниц" in свой and "УПД" in свой
