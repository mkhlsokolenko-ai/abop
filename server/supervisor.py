# -*- coding: utf-8 -*-
"""Супервайзер отдела (шаг 5 «Отделов агентов», docs/OTDELY_AGENTOV.md §4; ADR-083).

ЧЬЯ СУЩНОСТЬ — главный вопрос шага, и ответ здесь: **системный по рождению, пользовательский
по жизни**. Роль «Супервайзер отдела» сеется в ростер каждой рабочей семьи из кода; агент
создаётся платформой автоматически, когда отделу отдают первую задачу. Дальше он живёт как
ЛЮБОЙ агент: версии, Пересмотр, паспорт, аудит назначения (шаг 1 проверит «супервайзер без
навыка декомпозиции» ровно как v11), ИКА. Настроить его — может методолог/владелец; системное —
только рождение и несменяемая роль. Чисто системный оркестратор против архитектуры (не виден во
Флоте, не настраивается, не проверяется аудитом); чисто пользовательский делает отдел безнадзорным,
пока его не соберут.

ДЕКОМПОЗИЦИЯ v1 — детерминированная, кодом: план по контрактам навыков отдела (planner.plan)
рассыпается на подзадачи, где ВИД АРТЕФАКТА = produces навыка, а критерии готовности (DoD) —
стандартный набор §4.3 плана. LLM-свобода декомпозиции — рост v2 супервайзера-агента; свободой
пути уже владеет исполнитель. Это одобрено планом: «механика сопоставления написана в
планировщике — применить её к подзадаче в рантайме».

DoD — машинные критерии: прогон завершился ok, выход навыка на доске прогона структурирован,
провенанс несёт прогон. Не вышло за ДВЕ попытки — эскалация человеку (HITL), и это законный
исход, а не сбой.
"""
from __future__ import annotations

import asyncio

from . import agent_store, audit_store, blackboard, dept_tasks, planner, run_bus, run_queue
from .config import settings
from .observability import current_trace_id

# DoD по умолчанию (§4.3 плана): то, что машинно проверяемо в v1.
DOD_ПО_УМОЛЧАНИЮ = [
    "прогон завершился с вердиктом ok",
    "выход навыка структурирован (прошёл схему)",
    "артефакт на доске прогона несёт провенанс (run_id)",
]


async def супервайзер_отдела(отдел: str, actor: str = "система") -> dict:
    """Развёрнутый супервайзер отдела. Нет — создаётся из ростера семьи и сразу deployed.

    Создание пишется в журнал: у каждого супервайзера есть происхождение «рождён системой»,
    и после него он обычный агент — его можно пересматривать, откатывать, аудировать.
    """
    живые = await agent_store.list_for(None, limit=500)
    for a in живые:
        if a.get("family") == отдел and a.get("role") == "supervisor" and a.get("status") == "deployed":
            return a
    from cli import ape
    spec = ape.build_agent_spec(отдел, "supervisor")
    навыки = [s["id"] for s in spec["skills"]]
    граф = {"nodes": [{"id": "n-" + s, "kind": "skill", "skill": s} for s in навыки], "edges": []}
    новая = await agent_store.save(
        name="Супервайзер отдела · " + (spec.get("family_title") or отдел),
        audit_id=f"supervisor-{отдел}", version=await agent_store.next_version(f"supervisor-{отдел}"),
        graph=граф, autonomy_max="A1", created_by=actor,
        family=отдел, role="supervisor", source="authored",
        verification={"verified": True, "mode": "system-seed",
                      "почему": "рождён системой из ростера семьи; живёт как любой агент"})
    await agent_store.set_status(новая["id"], "deployed")
    await audit_store.record(actor, "supervisor.seed", новая["id"], {"отдел": отдел})
    return await agent_store.get(новая["id"]) or новая


async def _агенты_исполнители(отдел: str) -> list[dict]:
    """Развёрнутые агенты отдела БЕЗ супервайзера: он не считает сам (§3 плана).

    list_for отдаёт КРАТКИЕ записи без графа — а декомпозиция и выбор исполнителя смотрят в
    навыки графа. Загружаем полные записи: краткая здесь — пустой каталог и ложная эскалация
    (живой цикл 15.11: «исполнителя под такую задачу в каталоге нет» при живых сверщиках).
    """
    живые = await agent_store.list_for(None, limit=500)
    ид = [a["id"] for a in живые
          if a.get("family") == отдел and a.get("role") != "supervisor"
          and a.get("status") == "deployed"]
    полные = []
    for i in ид:
        полный = await agent_store.get(i)
        if полный and полный.get("status") == "deployed":
            полные.append(полный)
    return полные


def _навыки_агента(a: dict) -> set[str]:
    return {str(n.get("skill") or "") for n in ((a.get("graph") or {}).get("nodes") or [])
            if isinstance(n, dict) and n.get("kind") == "skill" and n.get("skill")}


async def декомпозировать(отдел: str, задача: str, предмет: dict) -> dict:
    """Разложить задачу человека на подзадачи-результаты.

    v1 — детерминированно: план по контрактам навыков ОТДЕЛА (конверт: нет навыка в отделе —
    нет подзадачи). Каждый шаг плана даёт вид артефакта (produces навыка) и критерии DoD.
    Планировщик может отказаться (need_more) — это честный возврат человеку за предметами,
    а не угадывание.
    """
    # Каталог декомпозиции — навыки ЖИВЫХ исполнителей отдела, не весь ростер семьи.
    # Иначе планировщик выберет навык, который отдел декларирует, но в проде не держит:
    # живой цикл 15.11 поймал ровно это — «сверка расхождений» превратилась в budget-forecast,
    # исполнителя нет, эскалация (машина сработала верно, план — нет).
    навыки_отдела = sorted({s for a in await _агенты_исполнители(отдел) for s in _навыки_агента(a)})
    каталог = {}
    for sid in навыки_отдела:
        if sid not in ape.SKILLS:
            continue
        каталог[sid] = {"title": ape.SKILLS[sid][0], "short": ape.SKILLS[sid][1],
                        "body": ape.load_skill_body(sid) or "", "inputs": {},
                        "produces": None, "families": [], "mode": "read"}
    план = planner.plan(задача, каталог, entities=set(), slots=set(предмет or {}), max_steps=4)
    if not план.get("ok"):
        return {"ок": False, "почему": план.get("note") or "не хватает предметов — уточните у человека"}
    шаги = [s for s in (план.get("steps") or []) if s.get("skill")]
    if not шаги:
        return {"ок": False, "почему": "планировщик не нашёл ни одного навыка отдела под задачу"}
    подзадачи = []
    for s in шаги:
        produces = ""
        m = каталог.get(s["skill"]) or {}
        подзадачи.append({"вид": produces or m.get("title") or s["skill"],
                          "навык": s["skill"], "критерии": list(DOD_ПО_УМОЛЧАНИЮ)})
    return {"ок": True, "подзадачи": подзадачи}


async def _исполнитель_для(отдел: str, навык: str) -> dict | None:
    """Исполнитель подзадачи: агент отдела, чей граф несёт нужный навык; при нескольких —
    последняя версия. Никто не умеет — None, и это эскалация человеку, а не угадывание."""
    годные = [a for a in await _агенты_исполнители(отдел) if навык in _навыки_агента(a)]
    if not годные:
        return None
    годные.sort(key=lambda a: a.get("version") or 0, reverse=True)
    return годные[0]


async def запустить_подзадачу(отдел: str, task_id: str, под: dict, предмет: dict,
                              actor: str) -> dict:
    """Поставить подзадачу в очередь: исполнитель по навыку, предмет — структурой (data_scope)."""
    исп = await _исполнитель_для(отдел, под["навык"])
    if not исп:
        await dept_tasks.обновить_подзадачу(под["id"], статус="эскалация",
                                            причины=["в отделе нет агента с навыком «%s»" % под["навык"]])
        return {"статус": "эскалация", "почему": "нет исполнителя с навыком"}
    job = await run_queue.enqueue(agent_id=исп["id"], actor=actor,
                                 payload={"contract_audit_id": исп.get("contract_audit_id") or "",
                                          "use_cache": True, "user_context": "", "deliver_filter": "",
                                          "data_scope": {str(k): str(v) for k, v in (предмет or {}).items()} or None,
                                          "budget": None, "limits": None,
                                          "actor_sub": actor, "channel": "",
                                          "trace_id": current_trace_id()},
                                 priority=5)
    await run_bus.bus().publish_request(job, current_trace_id())
    await dept_tasks.обновить_подзадачу(под["id"], агент=исп["id"], прогон=job["id"],
                                        попытка=int(под.get("попытка") or 0) + 1, статус="в очереди")
    return {"статус": "в очереди", "агент": исп["id"], "job_id": job["id"]}


async def проверить_подзадачу(под: dict) -> dict:
    """DoD-проверка подзадачи по её прогону (лениво, при чтении задачи).

    done + вердикт ok + выход навыка структурирован → выполнено. Иначе попытка + 1; есть запас —
    заново в очередь; две исчерпаны — эскалация человеку с причинами. Человек решает: довести
    руками, поправить исполнителя или закрыть задачу — все исходы законны.
    """
    job_id = под.get("прогон") or ""
    if not job_id:
        return под
    job = await run_queue.get(job_id)
    if not job or job.get("status") not in ("done", "failed"):
        return под
    причины: list = []
    if job.get("status") == "failed":
        причины.append("прогон завершился ошибкой: " + str(job.get("error") or "")[:200])
    else:
        run_id = str(job.get("run_id") or "")
        from . import run_store
        full = await run_store.get(run_id) if run_id else None
        выходы = [o for o in ((full or {}).get("skill_outputs") or [])
                  if isinstance(o, dict) and o.get("skill") == под.get("навык")]
        if full is not None and not (full.get("verdict") or {}).get("ok", True):
            причины.append("вердикт прогона с замечаниями")
        if not выходы or not any(o.get("structured") for o in выходы):
            причины.append("выход навыка не структурирован — артефакта на доске нет")
        if причины:
            причины.append("run_id: " + (run_id or "—"))
    if not причины:
        row = await dept_tasks.обновить_подзадачу(под["id"], статус="выполнено")
        await audit_store.record("супервайзер", "dept.dod.ok", под["id"],
                                 {"задача": под["task_id"], "прогон": job_id})
        return row or dict(под, статус="выполнено")
    попытка = int(под.get("попытка") or 0)
    if попытка >= dept_tasks.ПОПЫТОК_МАКС:
        row = await dept_tasks.обновить_подзадачу(под["id"], статус="эскалация", причины=причины)
        await audit_store.record("супервайзер", "dept.escalate", под["id"],
                                 {"задача": под["task_id"], "попытка": попытка, "причины": причины})
        return row or dict(под, статус="эскалация", причины=причины)
    # возврат на доработку: тот же исполнитель, попытка уже учтена при запуске
    row = await dept_tasks.обновить_подзадачу(под["id"], статус="возврат", причины=причины)
    return row or dict(под, статус="возврат", причины=причины)


async def возобновить(под: dict, отдел: str, предмет: dict, actor: str) -> dict:
    """Возврат в работу: попытки ещё есть — ставим прогон заново (запуск сам вырастит счётчик)."""
    if int(под.get("попытка") or 0) >= dept_tasks.ПОПЫТОК_МАКС:
        await dept_tasks.обновить_подзадачу(под["id"], статус="эскалация",
                                            причины=под.get("причины") or ["попытки исчерпаны"])
        return {"статус": "эскалация"}
    return await запустить_подзадачу(отдел, под["task_id"], под, предмет, actor)
