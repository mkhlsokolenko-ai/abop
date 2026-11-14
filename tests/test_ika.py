# -*- coding: utf-8 -*-
"""ИКА отдела — шаг 2 плана «Отделов агентов» (docs/OTDELY_AGENTOV.md §7).

Проверяются три вещи, на которых стоит доверие к числу: слагаемое без данных НЕ становится
нолём (ноль означал бы «отдел плох», а это другое утверждение); веса приходят данными
вертикали, и источник весов назван; чужой отдел не считается (ABAC семьи).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}
БАКС = {"sub": "u2", "name": "бухгалтер", "roles": ["analyst"], "level": "analyst", "department": "finance"}

ТЕПЕРЬ = dt.datetime.now(dt.timezone.utc).isoformat()


def _мир(monkeypatch):
    """Стенд: семья finance, два прогона, три задания (одно с повтором), четыре находки."""
    from server import agent_store, findings_registry, ika, run_queue, run_store
    from server import dataplane_records as dr

    агенты = [{"id": "fin.v1", "family": "finance"}, {"id": "fin.v2", "family": "finance"},
              {"id": "другой.v1", "family": "credit"}]
    прогоны = [{"id": "r1", "family": "finance", "created_at": ТЕПЕРЬ,
                "cost": {"rub": 1.5}, "seconds": 12.0},
               {"id": "r2", "family": "finance", "created_at": ТЕПЕРЬ,
                "cost": {"rub": 0.5}, "seconds": 8.0},
               {"id": "чужой", "family": "credit", "created_at": ТЕПЕРЬ}]
    тела = {"r1": {"skill_outputs": [{"skill": "a", "structured": {"x": 1}},
                                     {"skill": "b", "structured": None},
                                     {"skill": "c", "structured": []}]},
            "r2": {"skill_outputs": [{"skill": "a", "structured": {"y": 2}}]}}
    задания = [{"agent_id": "fin.v1", "attempts": 1, "created_at": ТЕПЕРЬ},
               {"agent_id": "fin.v2", "attempts": 2, "created_at": ТЕПЕРЬ},
               {"agent_id": "fin.v1", "attempts": 1, "created_at": ТЕПЕРЬ},
               {"agent_id": "fin.v1", "attempts": 0, "created_at": ТЕПЕРЬ},   # ещё не брали — не считается
               {"agent_id": "другой.v1", "attempts": 1, "created_at": ТЕПЕРЬ}]
    находки = [{"прогон": "r1", "состояние": "подтверждено"},
               {"прогон": "r1", "состояние": "исправлено"},
               {"прогон": "r1", "состояние": "отклонено"},
               {"прогон": "r2", "состояние": "подтверждено"},
               {"прогон": "чужой", "состояние": "подтверждено"}]   # чужой отдел — не считается

    async def _list_for(audit_id=None, limit=100, archived=False):
        return агенты

    async def _list_runs(agent_id=None, limit=100):
        return прогоны

    async def _get(rid):
        return тела.get(rid)

    async def _jobs(actor=None, limit=50):
        return задания

    async def _query(*a, **kw):
        raise RuntimeError("сущности нет")

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(run_store, "list_runs", _list_runs)
    monkeypatch.setattr(run_store, "get", _get)
    monkeypatch.setattr(run_queue, "list_jobs", _jobs)
    monkeypatch.setattr(findings_registry, "список", lambda *a, **kw: list(находки))
    monkeypatch.setattr(dr, "query", _query)


def test_слагаемые_считаются_из_известных_чисел(monkeypatch):
    from server import ika
    _мир(monkeypatch)
    rep = asyncio.run(ika.ика("finance", дней=30))
    с = rep["слагаемые"]
    # принятие: (2 подтверждено + 1 исправлено) / 4 решённых = 0.75
    assert с["принятие"]["значение"] == 0.75 and с["принятие"]["данных"] == 4
    # доведение: 1 исправлено / 3 подтверждено
    assert с["доведение"]["значение"] == 0.3333
    # ftfr: 2 задания из 3 без повторов
    assert с["ftfr"]["значение"] == 0.6667
    # формат: 2 из 4 выходов со структурой — None и пустой список это «нет структуры»
    assert с["формат"]["значение"] == 0.5
    # чужие данные не заехали
    assert rep["факты"]["прогонов"] == 2
    assert rep["факты"]["средняя_стоимость_руб"] == 1.0
    # веса по умолчанию, и это названо
    assert rep["веса"]["источник"] == "умолчания"
    assert rep["ика"] == 56.5   # 100·(3·0.75 + 3·0.3333 + 2·0.6667 + 1·0.5) / 9
    assert rep["не_покрыто"] == []


def test_пустой_отдел_не_ноль_а_не_покрыт(monkeypatch):
    from server import ika
    _мир(monkeypatch)
    rep = asyncio.run(ika.ика("architecture", дней=30))
    assert rep["ика"] is None
    assert rep["почему_пусто"]
    assert set(rep["не_покрыто"]) == {"принятие", "доведение", "ftfr", "формат"}


def test_веса_приходят_данными_вертикали(monkeypatch):
    from server import ika
    from server import dataplane_records as dr
    _мир(monkeypatch)

    async def _query(*a, **kw):
        return [{"фактор": "принятие", "вес": "5"}, {"фактор": "доведение", "вес": "2,5"},
                {"фактор": "опечатка", "вес": "9"},   # чужой фактор игнорируется
                {"фактор": "ftfr", "вес": "не число"}]  # неразобранный вес игнорируется

    monkeypatch.setattr(dr, "query", _query)
    rep = asyncio.run(ika.ика("finance", дней=30))
    assert rep["веса"]["источник"] == "данные вертикали"
    assert rep["веса"]["значения"]["принятие"] == 5.0
    assert rep["веса"]["значения"]["доведение"] == 2.5
    assert rep["веса"]["значения"]["ftfr"] == 2.0   # остался умолчанием


def test_ручка_чужой_отдел_не_считает(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from server.routes import families as F
    _мир(monkeypatch)

    async def _ика(fid, дней=30):
        raise AssertionError("чужой отдел не должен был считаться")

    monkeypatch.setattr(F.ika, "ика", _ика)
    with pytest.raises(HTTPException) as е:
        asyncio.run(F.family_ika("credit", 30, БАКС))
    assert е.value.status_code == 403


def test_ручка_свой_отдел_считает(monkeypatch):
    from server.routes import families as F
    _мир(monkeypatch)
    клеветка = {"семья": "finance", "ика": 59.3}

    async def _ика(fid, дней=30):
        assert fid == "finance"
        return клеветка

    monkeypatch.setattr(F.ika, "ика", _ика)
    ответ = asyncio.run(F.family_ika("finance", 30, БАКС))
    assert ответ == клеветка
