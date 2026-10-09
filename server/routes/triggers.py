# -*- coding: utf-8 -*-
"""Триггеры: что запускает агента без человека.

Расписание, событие из системы или входящая задача. Здесь список триггеров,
собственные расписания пользователя и ручной запуск триггера «прямо сейчас».

Правило домена: прогон по триггеру помнит участок карты (`op_ids`), а ручной запуск —
нет. Это и есть признак «вне карты» в отчёте покрытия (ADR-044).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import agent_store, audit_store, run_store, trigger_store, triggers

from ..deps import _uid_of, require_level, user

from ..run_exec import execute_agent_run

router = APIRouter()


@router.get("/api/triggers")
async def triggers_list(u: dict = Depends(user)) -> dict:
    """Стартовые события агентов (триггеры из графов) + последний фаер. Планировщик фаерит только
    enabled schedule/event-триггеры. §триггер-узел (persistence-localstorage-hole)."""
    return {"triggers": await triggers.list_triggers(), "fires": await trigger_store.list_fires(limit=30)}


@router.post("/api/triggers/{agent_id}/{trigger_id}/fire")
async def trigger_fire(agent_id: str, trigger_id: str, u: dict = Depends(user)) -> dict:
    """Ручной запуск триггера (кнопка/тест): чтит политику spawn (автономия ≤ контракт, HITL-на-создание),
    но игнорирует enabled/min-interval. Уровень manager+ (запуск = мутация среды)."""
    require_level(u, "manager")
    res = await triggers.fire_manual(agent_id, trigger_id, execute_agent_run)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "trigger.fire", trigger_id,
                             {"agent_id": agent_id, "status": res.get("status")})
    return res


@router.get("/api/triggers/mine")
async def triggers_mine(u: dict = Depends(user)) -> dict:
    """Расписания, заведённые ТЕКУЩИМ пользователем (created_by агента = я, или trig.owner = я): агент,
    cron, вкл/выкл, куда доставка (чат/система), последний фаер + краткий итог последнего прогона.
    Для раздела «Мои расписания» и уведомлений о завершении."""
    me = _uid_of(u)
    admin = u.get("department") in ("*", None)
    out = []
    for t in await triggers.list_triggers():
        if (t.get("source") or "") != "chat.recurring":
            continue
        agent = await agent_store.get(t["agent_id"])
        owner = ((_node_trig(agent, t.get("trigger_id")) or {}).get("owner")) or (agent or {}).get("created_by")
        if not admin and owner != me:
            continue
        last_run = None
        fires = [f for f in await trigger_store.list_fires(limit=200)
                 if f.get("agent_id") == t["agent_id"] and f.get("trigger_id") == t.get("trigger_id")]
        if fires:
            rid = fires[0].get("run_id")
            at = fires[0].get("fired_at")
            if rid:
                r = await run_store.get(rid)
                if r:
                    last_run = {"run_id": rid, "at": at,
                                "findings": (r.get("findings_summary") or {}).get("total"),
                                "status": fires[0].get("status")}
                else:
                    last_run = {"run_id": rid, "at": at, "status": fires[0].get("status")}
            else:
                last_run = {"at": at, "status": fires[0].get("status"), "note": fires[0].get("note")}
        node = _node_trig(agent, t.get("trigger_id")) or {}
        out.append({"agent_id": t["agent_id"], "agent": t.get("agent"), "trigger_id": t.get("trigger_id"),
                    "title": t.get("title"), "cron": t.get("cron"), "enabled": t.get("enabled"),
                    "deliver": node.get("deliver") or "chat", "last_fire": t.get("last_fire"),
                    "last_run": last_run})
    return {"schedules": out, "count": len(out)}


def _node_trig(agent: dict, trigger_id: str) -> dict | None:
    for n in ((agent or {}).get("graph") or {}).get("nodes") or []:
        if n.get("kind") == "trigger" and n.get("id") == trigger_id:
            return n.get("trig") or {}
    return None
