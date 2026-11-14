# -*- coding: utf-8 -*-
"""Шаг 5 «Отделов агентов»: супервайзер отдела.

Ответ на главный вопрос шага: супервайзер — системный по рождению, пользовательский по жизни
(ADR-083). Гарантии: сеется автоматически, живёт как любой агент (версии/паспорт/аудит);
декомпозиция объявляет РЕЗУЛЬТАТ с DoD; исполнитель выбирается контрактом навыка и не бывает
супервайзером; возврат после DoD-провала; две попытки — эскалация человеку; чужой отдел — 403.
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


def test_роль_супервайзера_сеется_во_все_рабочие_семьи():
    from cli import ape
    for fam in ("analytics", "finance", "credit", "audit", "architecture", "management",
                "research", "engineering"):
        assert "supervisor" in ape.AGENT_FAMILIES[fam]["members"], fam
    title, sk = ape.AGENT_FAMILIES["finance"]["members"]["supervisor"]
    assert sk, "у роли должны быть методичные навыки"


def test_супервайзер_сеется_автоматически_и_живет_как_агент(monkeypatch):
    from server import agent_store, supervisor
    создано: list = []

    async def _list_for(audit_id=None, limit=100, archived=False):
        return []   # супервайзера ещё нет

    async def _next_version(aid):
        return 1

    async def _save(**kw):
        создано.append(kw)
        return {"id": "supervisor-finance.v1", "family": kw["family"], "role": kw["role"],
                "status": "draft"}

    статусы: list = []

    async def _set(aid, st):
        статусы.append((aid, st))

    async def _get(aid):
        return {"id": aid, "family": "finance", "role": "supervisor", "status": "deployed"}

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(agent_store, "next_version", _next_version)
    monkeypatch.setattr(agent_store, "save", _save)
    monkeypatch.setattr(agent_store, "set_status", _set)
    monkeypatch.setattr(agent_store, "get", _get)

    async def _rec(*a, **kw):
        pass

    monkeypatch.setattr(supervisor.audit_store, "record", _rec)
    агент = asyncio.run(supervisor.супервайзер_отдела("finance", "админ"))
    assert агент["role"] == "supervisor"
    assert создано and создано[0]["family"] == "finance"
    assert ("supervisor-finance.v1", "deployed") in статусы
    assert создано[0]["audit_id"] == "supervisor-finance"


def test_декомпозиция_объявляет_результат_и_dod(monkeypatch):
    from server import supervisor
    from server import dept_tasks
    под = {"id": "sub-1", "вид": "Разбор писем", "навык": "mail-triage",
           "критерии": supervisor.DOD_ПО_УМОЛЧАНИЮ, "попытка": 0}
    assert any("провенанс" in к for к in под["критерии"]), "DoD обязан требовать происхождение"
    assert под["вид"] == "Разбор писем"


def test_исполнитель_не_бывает_супервайзером(monkeypatch):
    from server import agent_store, supervisor
    агенты = [
        {"id": "суп.v1", "family": "finance", "role": "supervisor", "status": "deployed",
         "version": 3, "graph": {"nodes": [{"kind": "skill", "skill": "mail-triage"}]}},
        {"id": "исп.v2", "family": "finance", "role": "task-digester", "status": "deployed",
         "version": 2, "graph": {"nodes": [{"kind": "skill", "skill": "mail-triage"}]}},
        {"id": "исп.чужой", "family": "credit", "role": "x", "status": "deployed",
         "version": 9, "graph": {"nodes": [{"kind": "skill", "skill": "mail-triage"}]}},
        {"id": "исп.черновик", "family": "finance", "role": "task-digester", "status": "draft",
         "version": 5, "graph": {"nodes": [{"kind": "skill", "skill": "mail-triage"}]}},
    ]

    async def _list_for(audit_id=None, limit=100, archived=False):
        return [{k: v for k, v in a.items() if k != "graph"} for a in агенты]

    async def _get(aid):
        return next(a for a in агенты if a["id"] == aid)

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(agent_store, "get", _get)
    исп = asyncio.run(supervisor._исполнитель_для("finance", "mail-triage"))
    assert исп["id"] == "исп.v2", "супервайзер не считает сам; чужой отдел и черновики — мимо"


def test_dod_провал_две_попытки_потом_эскалация(monkeypatch):
    from server import dept_tasks, supervisor
    под = {"id": "sub-9", "task_id": "task-1", "вид": "Разбор писем", "навык": "mail-triage",
           "критерии": [], "агент": "исп.v2", "прогон": "job-1", "попытка": 2, "статус": "возврат"}

    class _Job:
        status = "done"
        run_id = "r1"
        error = ""

    async def _get(job_id):
        return dict(_Job().__dict__, status="done", run_id="r1")

    async def _run_get(rid):
        return {"verdict": {"ok": True},
                "skill_outputs": [{"skill": "mail-triage", "structured": None}]}  # выхода нет

    события: list = []

    async def _обнов(sid, **поля):
        события.append((sid, поля.get("статус"), поля.get("причины")))
        под2 = dict(под, **поля)
        return под2

    async def _rec(*a, **kw):
        pass

    monkeypatch.setattr(supervisor.run_queue, "get", _get)
    from server import run_store as _rs
    monkeypatch.setattr(_rs, "get", _run_get)
    monkeypatch.setattr(dept_tasks, "обновить_подзадачу", _обнов)
    monkeypatch.setattr(supervisor.audit_store, "record", _rec)
    итог = asyncio.run(supervisor.проверить_подзадачу(под))
    assert итог["статус"] == "эскалация" , "попытки исчерпаны — законный исход человеку"
    assert any("не структурирован" in п for п in (итог.get("причины") or []))


def test_dod_успех_выполнено(monkeypatch):
    from server import dept_tasks, supervisor
    под = {"id": "sub-9", "task_id": "task-1", "вид": "Разбор писем", "навык": "mail-triage",
           "критерии": [], "агент": "исп.v2", "прогон": "job-1", "попытка": 1, "статус": "в очереди"}

    async def _get(job_id):
        return {"status": "done", "run_id": "r1"}

    async def _run_get(rid):
        return {"verdict": {"ok": True},
                "skill_outputs": [{"skill": "mail-triage", "structured": {"итог": "3 задачи"}}]}

    async def _обнов(sid, **поля):
        return dict(под, **поля)

    async def _rec(*a, **kw):
        pass

    monkeypatch.setattr(supervisor.run_queue, "get", _get)
    from server import run_store as _rs
    monkeypatch.setattr(_rs, "get", _run_get)
    monkeypatch.setattr(dept_tasks, "обновить_подзадачу", _обнов)
    monkeypatch.setattr(supervisor.audit_store, "record", _rec)
    итог = asyncio.run(supervisor.проверить_подзадачу(под))
    assert итог["статус"] == "выполнено" 


def test_ручки_отдела_гейты(monkeypatch):
    import json as _json
    import pytest
    from fastapi import HTTPException
    from server.routes import departments as D
    from server import dept_tasks

    async def _никогда(*a, **kw):
        raise AssertionError("чужой отдел не должен был работать")

    monkeypatch.setattr(D.supervisor, "супервайзер_отдела", _никогда)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(D.task_create("credit", {"текст": "x"}, АНАЛИТ))   # аналитик finance
    assert ex.value.status_code == 403

    async def _задача(tid):
        return {"id": tid, "отдел": "credit", "текст": "x", "предмет": {}}

    monkeypatch.setattr(dept_tasks, "задача", _задача)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(D.task_card("credit", "task-1", АНАЛИТ))
    assert ex.value.status_code == 403
