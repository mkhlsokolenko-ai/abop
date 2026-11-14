# -*- coding: utf-8 -*-
"""Шаг 8 «Отделов агентов»: динамическая автономия (ADR-085).

Три ограничения, зашитые в код:
1. Потолок контракта не перепрыгивается — ИКА не добавляет автономии выше.
2. Необратимое (egress=external) — всегда HITL, при любом ИКА.
3. Включение — один раз явно человеком; сужение — автоматическое и немедленное.
"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from unittest.mock import AsyncMock

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}
АНАЛИТ = {"sub": "u2", "name": "аналитик", "roles": ["analyst"], "level": "analyst", "department": "finance"}

# audit1c-extract имеет egress=external (реальные данные навыка) — необратимое, всегда HITL
ГРАФ = {"nodes": [
    {"id": "n1", "kind": "skill", "skill": "audit1c-extract", "hitl": True},  # egress=external
    {"id": "n2", "kind": "skill", "skill": "audit1c-checks"},
    {"id": "n3", "kind": "skill", "skill": "audit1c-explain", "hitl": True},
]}


def test_выключенная_без_изменений(monkeypatch):
    from server import autonomy as A
    monkeypatch.setattr(A, 'включена', AsyncMock(return_value=False))
    граф, мета = asyncio.run(A.подстроить_граф("finance", copy.deepcopy(ГРАФ), "админ"))
    assert мета["подстройка"] == "выключена"
    assert граф == ГРАФ


def test_повышенная_снимает_hitl_с_внутренних(monkeypatch):
    from server import autonomy as A, ika
    monkeypatch.setattr(A, 'включена', AsyncMock(return_value=True))
    monkeypatch.setattr(A.ika, 'ика', AsyncMock(return_value={'ика': 95, 'покрыто': ['ftfr']}))
    граф, мета = asyncio.run(A.подстроить_граф("finance", copy.deepcopy(ГРАФ), "админ"))
    assert мета["подстройка"] == "повышенная"
    extract = next(n for n in граф["nodes"] if n["skill"] == "audit1c-extract")
    assert extract.get("hitl") is True, "необратимое (egress=external) всегда HITL"
    explain = next(n for n in граф["nodes"] if n["skill"] == "audit1c-explain")
    assert not explain.get("hitl"), "внутренний шаг без HITL при повышенной"


def test_суженная_добавляет_hitl_на_каждый_шаг(monkeypatch):
    from server import autonomy as A, ika
    monkeypatch.setattr(A, 'включена', AsyncMock(return_value=True))
    monkeypatch.setattr(A.ika, 'ика', AsyncMock(return_value={'ика': 55}))
    граф, мета = asyncio.run(A.подстроить_граф("finance", copy.deepcopy(ГРАФ), "админ"))
    assert мета["подстройка"] == "суженная"
    for n in граф["nodes"]:
        if n.get("kind") == "skill":
            assert n.get("hitl") is True, f"{n['skill']}: суженная = hitl на каждом шаге"


def test_базовая_без_изменений(monkeypatch):
    from server import autonomy as A, ika
    monkeypatch.setattr(A, 'включена', AsyncMock(return_value=True))
    monkeypatch.setattr(A.ika, 'ика', AsyncMock(return_value={'ика': 78}))
    граф, мета = asyncio.run(A.подстроить_граф("finance", copy.deepcopy(ГРАФ), "админ"))
    assert мета["подстройка"] == "базовая"
    assert граф == ГРАФ


def test_оригинал_не_тронут(monkeypatch):
    from server import autonomy as A, ika
    monkeypatch.setattr(A, 'включена', AsyncMock(return_value=True))
    monkeypatch.setattr(A.ika, 'ика', AsyncMock(return_value={'ика': 95}))
    ориг = copy.deepcopy(ГРАФ)
    граф, _ = asyncio.run(A.подстроить_граф("finance", ориг, "админ"))
    assert ориг == ГРАФ, "runtime-копия: оригинал не изменяется"
    explain = next(n for n in граф["nodes"] if n["skill"] == "audit1c-explain")
    assert not explain.get("hitl"), "копия подстроена"


def test_не_покрытый_ика_базовая(monkeypatch):
    from server import autonomy as A, ika
    monkeypatch.setattr(A, 'включена', AsyncMock(return_value=True))
    monkeypatch.setattr(A.ika, 'ика', AsyncMock(return_value={'ика': None, 'почему_пусто': 'нет данных'}))
    граф, мета = asyncio.run(A.подстроить_граф("finance", copy.deepcopy(ГРАФ), "админ"))
    assert мета["подстройка"] == "ИКА не покрыт — базовая, без изменений"
    assert граф == ГРАФ, "не покрытый ИКА = без изменений (ноль бы означал «отдел плох»)"


def test_включение_и_чтение_настройки(monkeypatch):
    from server import families_store, autonomy as A
    card = {"id": "finance", "динамическая_автономия": False}

    async def _get(fid):
        return dict(card)

    async def _save(fid, spec, editor="dev", builtin=False):
        card.update(spec)
        return dict(card)

    monkeypatch.setattr(families_store, "get", _get)
    monkeypatch.setattr(families_store, "save", _save)
    итог = asyncio.run(A.включить("finance", True, "админ"))
    assert итог["динамическая_автономия"] is True
    assert asyncio.run(A.включена("finance")) is True


def test_ручки_чужой_отдел_403(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from server.routes import departments as D
    from server import autonomy as A, ika

    async def _никогда(*a, **kw):
        raise AssertionError("чужой отдел не должен был работать")

    monkeypatch.setattr(D.autonomy, "включена", _никогда)
    monkeypatch.setattr(D.ika, "ика", _никогда)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(D.autonomy_toggle("audit", {"включить": True}, АНАЛИТ))
    assert ex.value.status_code == 403
    with pytest.raises(HTTPException) as ex:
        asyncio.run(D.autonomy_status("audit", АНАЛИТ))
    assert ex.value.status_code == 403
