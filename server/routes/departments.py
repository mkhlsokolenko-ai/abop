# -*- coding: utf-8 -*-
"""Задачи отделу: человек отдаёт задачу, супервайзер раскладывает на результаты.

Шаг 5 «Отделов агентов» (docs/OTDELY_AGENTOV.md §4; ADR-083). Подзадача объявляет РЕЗУЛЬТАТ
(вид артефакта + критерии готовности), а не шаги; исполнителя выбирает контрактом навыка;
не вышло за две попытки — эскалация человеку. Супервайзер отдела — агент, рождённый системой
из ростера и живущий как любой агент: его видно во Флоте, паспорт, аудит, Пересмотр.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import audit_store, dept_tasks, supervisor
from ..deps import can_see_family, require_level, user

router = APIRouter()


@router.post("/api/departments/{fid}/tasks")
async def task_create(fid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Отдать задачу отделу: супервайзер разложит её на подзадачи-результаты и поставит в очередь.

    Декомпозиция v1 детерминированная (план по контрактам навыков отдела). Отказ планировщика —
    честный вопрос человеку за предметами, а не угадывание. Супервайзер отдела сеется системой,
    если его ещё нет; настраивается после как любой агент.
    """
    require_level(u, "manager")
    if not can_see_family(u, fid):
        raise HTTPException(403, "отдел другого отдела")
    текст = str((body or {}).get("текст") or "").strip()
    if not текст:
        raise HTTPException(422, "нужен текст задачи")
    предмет = {str(k): str(v) for k, v in ((body or {}).get("предмет") or {}).items() if str(v or "").strip()}
    actor = u.get("name") or u.get("sub") or "dev"
    суп = await supervisor.супервайзер_отдела(fid, actor)
    задача = await dept_tasks.создать_задачу(fid, текст, предмет, actor)
    разбор = await supervisor.декомпозировать(fid, текст, предмет)
    подзадачи: list = []
    if разбор.get("ок"):
        for п in разбор["подзадачи"]:
            под = await dept_tasks.создать_подзадачу(задача["id"], п["вид"], п["критерии"], п["навык"])
            запуск = await supervisor.запустить_подзадачу(fid, задача["id"], под, предмет, actor)
            подзадачи.append(dict(под, **запуск))
    else:
        # отказ декомпозиции — задача остаётся «в работе» без подзадач: человек уточняет предметы
        pass
    await audit_store.record(actor, "dept.task.create", задача["id"],
                             {"отдел": fid, "подзадач": len(подзадачи),
                              "почему": "" if разбор.get("ок") else разбор.get("почему")})
    return {"задача": задача, "супервайзер": суп["id"], "подзадачи": подзадачи,
            "почему_пусто": "" if разбор.get("ок") else разбор.get("почему")}


@router.get("/api/departments/{fid}/tasks")
async def tasks_list(fid: str, u: dict = Depends(user)) -> dict:
    """Задачи отдела: последняя сверху, с числом подзадач по статусам."""
    if not can_see_family(u, fid):
        raise HTTPException(403, "отдел другого отдела")
    задачи = []
    for t in await dept_tasks.задачи_отдела(fid):
        под = await dept_tasks.подзадачи(t["id"])
        по_статусу: dict = {}
        for s in под:
            по_статусу[s["статус"]] = по_статусу.get(s["статус"], 0) + 1
        задачи.append(dict(t, подзадач=len(под), по_статусу=по_статусу))
    return {"задачи": задачи, "отдел": fid}


@router.get("/api/departments/{fid}/tasks/{tid}")
async def task_card(fid: str, tid: str, u: dict = Depends(user)) -> dict:
    """Карточка задачи с подзадачами. Чтение лениво проверяет DoD по завершённым прогонам:
    выполнено / возврат на доработку / эскалация человеку — по машинным критериям."""
    if not can_see_family(u, fid):
        raise HTTPException(403, "отдел другого отдела")
    t = await dept_tasks.задача(tid)
    if not t or t["отдел"] != fid:
        raise HTTPException(404, "задачи нет или она другого отдела")
    под = []
    for s in await dept_tasks.подзадачи(tid):
        if s["статус"] in ("в очереди", "возврат") and s.get("прогон"):
            s = await supervisor.проверить_подзадачу(s)
        под.append(s)
    return {"задача": t, "подзадачи": под}


@router.post("/api/departments/{fid}/tasks/{tid}/subtasks/{sid}/retry")
async def subtask_retry(fid: str, tid: str, sid: str, u: dict = Depends(user)) -> dict:
    """Человек решил: доработать. Возврат/эскалацию — заново в очередь, если попытки остались;
    исчерпаны — честный отказ: путь один, руками или пересмотр исполнителя."""
    require_level(u, "manager")
    if not can_see_family(u, fid):
        raise HTTPException(403, "отдел другого отдела")
    t = await dept_tasks.задача(tid)
    if not t or t["отдел"] != fid:
        raise HTTPException(404, "задачи нет или она другого отдела")
    подз = await dept_tasks.подзадачи(tid)
    под = next((s for s in подз if s["id"] == sid), None)
    if not под:
        raise HTTPException(404, "подзадачи нет в этой задаче")
    итог = await supervisor.возобновить(под, fid, t.get("предмет") or {},
                                        u.get("name") or u.get("sub") or "dev")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "dept.subtask.retry", sid,
                             {"статус": итог.get("статус")})
    return итог
