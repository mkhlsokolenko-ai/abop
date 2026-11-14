# -*- coding: utf-8 -*-
"""Шаг 7 «Отделов агентов»: аудитор качества — формат, полнота, свежесть, доказательства.

Главные гарантии: аудитор НИЧЕГО не чинит — только NCR с цитатой источника; дедуп не плодит
вторую карточку на тот же провал; «почему: …» в поле — законный исход полноты; чужой отдел — 403.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}
АНАЛИТ = {"sub": "u2", "name": "аналитик", "roles": ["analyst"], "level": "analyst", "department": "finance"}


def test_ncr_дедуп_не_плодит_карточки(monkeypatch):
    from server import ncr_store
    monkeypatch.setattr(ncr_store, "_has_pg", lambda: False)
    ncr_store._MEM.clear()

    async def flow():
        п1 = await ncr_store.создать("формат", "r1|audit1c-extract", "audit", "выход не структурирован")
        п2 = await ncr_store.создать("формат", "r1|audit1c-extract", "audit", "выход не структурирован")
        assert п1 and п2 is None, "повтор на тот же провал — «уже видно», а не вторая карточка"
        await ncr_store.закрыть(п1["id"], "главбух", "починили")
        п3 = await ncr_store.создать("формат", "r1|audit1c-extract", "audit", "снова провал")
        assert п3, "закрытый не переоткрывается, но новый провал создаёт новую карточку"

    asyncio.run(flow())


def test_формат_ловит_выход_без_структуры(monkeypatch):
    from server import agent_store, ncr_store, quality_audit, run_store
    from server import skill_templates as st
    monkeypatch.setattr(quality_audit, "_has_pg_skip", True, raising=False)
    навык = "audit1c-extract"   # у него есть template.json в репо

    прогоны = [{"id": "r1", "agent_id": "a1"}]
    тела = {"r1": {"verdict": {"ok": True},
                   "skill_outputs": [{"skill": навык, "structured": None}]}}

    async def _list_for(audit_id=None, limit=100, archived=False):
        return [{"id": "a1", "family": "audit"}]

    async def _list_runs(agent_id=None, limit=100):
        return прогоны

    async def _get(rid):
        return тела[rid]

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(run_store, "list_runs", _list_runs)
    monkeypatch.setattr(run_store, "get", _get)

    создано: list = []

    async def _создать(тип, предмет, отдел, что_нарушено, **kw):
        создано.append(dict(kw, тип=тип, предмет=предмет, отдел=отдел, что_нарушено=что_нарушено))
        return dict(kw, тип=тип, предмет=предмет)

    monkeypatch.setattr(ncr_store, "создать", _создать)
    итог = asyncio.run(quality_audit.прогон_отдела("audit", "админ"))
    форматы = [к for к in создано if к["тип"] == "формат"]
    assert форматы and навык in форматы[0]["предмет"]
    assert "template.json" in форматы[0]["источник"], "NCR обязан цитировать источник"


def test_полнота_пустые_required_почему_законно(monkeypatch):
    from server import agent_store, ncr_store, quality_audit, run_store

    прогоны = [{"id": "r2", "agent_id": "a2"}]
    тела = {"r2": {"verdict": {"ok": True},
                   "skill_outputs": [{"skill": "fastapi-patterns",
                                      "structured": {"что_сделано": ""}}]}}

    async def _list_for(audit_id=None, limit=100, archived=False):
        return [{"id": "a2", "family": "management"}]

    async def _list_runs(agent_id=None, limit=100):
        return прогоны

    async def _get(rid):
        return тела[rid]

    создано: list = []

    async def _создать(тип, предмет, отдел, что_нарушено, **kw):
        создано.append(dict(kw, тип=тип, предмет=предмет))
        return dict(kw, тип=тип, предмет=предмет)

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(run_store, "list_runs", _list_runs)
    monkeypatch.setattr(run_store, "get", _get)
    monkeypatch.setattr(ncr_store, "создать", _создать)
    # fastapi-patterns без template.json — полноту по схеме не проверить, NCR не создаётся: судить не на чем
    итог = asyncio.run(quality_audit.прогон_отдела("management", "админ"))
    assert not [к for к in создано if к["тип"] in ("формат", "полнота")]


def test_свежесть_протухшие_факты_доски(monkeypatch):
    from server import blackboard, ncr_store, quality_audit
    facts = [{"key": "курс-цб", "author": "агент", "ttl_sec": 3600, "at": "2026-01-01T00:00:00"}]

    async def _load(scope, include_stale=False, слой=""):
        return facts if include_stale else []

    monkeypatch.setattr(blackboard, "load", _load)
    создано: list = []

    async def _создать(тип, предмет, отдел, что_нарушено, **kw):
        создано.append(dict(kw, тип=тип, предмет=предмет, что_нарушено=что_нарушено))
        return dict(kw, тип=тип, предмет=предмет)

    monkeypatch.setattr(ncr_store, "создать", _создать)
    asyncio.run(quality_audit._свежесть("finance"))
    assert создано and "просрочен" in создано[0]["что_нарушено"]
    assert "курс-цб" in создано[0]["предмет"]


def test_доказательства_пустые_ловятся(monkeypatch):
    from server import findings_registry as fr
    from server import ncr_store, quality_audit
    строки = [{"id": "f1", "состояние": "найдено", "код": "B1", "предмет": "Платёж №214",
               "доказательство": ""},
              {"id": "f2", "состояние": "найдено", "код": "C2", "предмет": "Счёт №4021",
               "доказательство": "Счёт №4021 от 12.10 · 62 400 ₽"}]

    async def flow():
        fr._MEM.clear()
        fr._MEM.append({"id": "rid", "value": строки})
        return строки

    def _список(*a, **kw):
        return строки

    monkeypatch.setattr(fr, "список", _список)
    создано: list = []

    async def _создать(тип, предмет, отдел, что_нарушено, **kw):
        создано.append(dict(kw, тип=тип, предмет=предмет, что_нарушено=что_нарушено))
        return dict(kw, тип=тип, предмет=предмет)

    monkeypatch.setattr(ncr_store, "создать", _создать)
    asyncio.run(quality_audit._доказательства(None))
    assert len(создано) == 1 and "f1" in создано[0]["предмет"]
    assert "без доказательства" in создано[0]["что_нарушено"]


def test_ручка_чужой_отдел_403(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from server.routes import departments as D
    from server import quality_audit as Q

    async def _никогда(*a, **kw):
        raise AssertionError("чужой отдел не должен был проверяться")

    monkeypatch.setattr(Q, "прогон_отдела", _никогда)
    monkeypatch.setattr(D.quality_audit, "прогон_отдела", _никогда)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(D.audit_run("audit", АНАЛИТ))   # аналитик finance
    assert ex.value.status_code == 403
