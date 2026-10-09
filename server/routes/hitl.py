# -*- coding: utf-8 -*-
"""Подтверждения человека: очередь заявок, решение и продолжение прогона.

Внешнее действие (письмо, задача, публикация) не уходит, пока человек не увидел, ЧТО
именно уйдёт, и не сказал «да» (ADR-014). Здесь очередь таких заявок, решение по ним и
продолжение остановленного прогона после подтверждения.

Правило домена: отказ — такой же законный исход, как согласие, и он тоже попадает в
журнал. Молча «передумать» нельзя ни человеку, ни среде.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from fastapi.responses import JSONResponse

from .. import agent_store, audit_store, hitl_store, run_bus, run_queue, systems_store

from .registry import access_check
from ..deps import can_see_family, require_level, user

from .. import observability as obs
from ..run_exec import _send_channel

router = APIRouter()


# Каталог каналов OUT-узла: объявлен РЯДОМ с кодом, который их исполняет (`_send_channel`), и
# отдаётся интерфейсу. Раньше список жил в вёрстке канвы и разошёлся с рантаймом: предлагался
# YouGile, которого нет на стенде, и отсутствовал Redmine, в который уходит аудит 1С.
# `to` описывает, что именно спрашивать у человека для адреса: у вики это номер книги, у почты
# адрес, у файла имя. Эти подписи тоже были тремя тернарниками в вёрстке.
OUT_CHANNELS = [
    {"id": "redmine",   "label": "Redmine — задача",        "system": "redmine",
     "to": {"label": "проект трекера", "placeholder": "demo", "kind": "text"}},
    {"id": "bookstack", "label": "BookStack — страница вики", "system": "bookstack",
     "to": {"label": "книга BookStack (book_id)", "placeholder": "1", "kind": "int", "field": "book_id"}},
    {"id": "email",     "label": "Почта стенда (Mailpit) — наружу не уходит", "system": "mailpit",
     "to": {"label": "адрес получателя", "placeholder": "glavbuh@demo.local", "kind": "email"}},
    {"id": "yandex",    "label": "Яндекс.Почта — реальная отправка", "system": "mailpit",
     "to": {"label": "адрес получателя", "placeholder": "user@yandex.ru", "kind": "email"}},
    {"id": "yougile",   "label": "YouGile — задача",        "system": "yougile",
     "to": {"label": "колонка YouGile (column_id)", "placeholder": "ID колонки", "kind": "text"}},
    {"id": "pdf",       "label": "PDF-файл",                "system": None,
     "to": {"label": "имя файла", "placeholder": "report", "kind": "text"}},
    {"id": "file",      "label": "Файл",                    "system": None,
     "to": {"label": "имя файла", "placeholder": "report", "kind": "text"}},
]


@router.get("/api/out-channels")
async def out_channels(family: str = "", u: dict = Depends(user)) -> dict:
    """Куда узел вывода может доставить результат — по факту, а не по списку в вёрстке.

    Канал показывается всегда (чтобы было видно, что платформа умеет), но помечается: есть ли его
    система в реестре и открыт ли к ней доступ отделу. Собирать узел на систему, которой нет, —
    та же ошибка, что показывать демо-данные как настоящие.
    """
    known = {s.get("id"): s for s in (await systems_store.all() or [])}
    fam = str(family or "").strip()
    out = []
    for c in OUT_CHANNELS:
        sysid = c.get("system")
        entry = {**c, "registered": True, "allowed": True, "note": ""}
        if sysid:
            sysrec = known.get(sysid)
            entry["registered"] = bool(sysrec)
            if not sysrec:
                entry["allowed"] = False
                entry["note"] = f"системы «{sysid}» нет в реестре — узел соберётся, но доставки не будет"
            elif fam:
                try:
                    chk = await access_check({"family": fam, "system_id": sysid}, u)
                    entry["allowed"] = bool(chk.get("allowed"))
                    if not entry["allowed"]:
                        entry["note"] = str(chk.get("reason") or "отделу закрыт доступ к этой системе")
                except Exception:  # noqa: BLE001 — проверка доступа усиление, а не условие показа
                    pass
        else:
            entry["note"] = "локальный файл, наружу не уходит"
        out.append(entry)
    return {"channels": out, "family": fam}


@router.get("/api/hitl/queue")
async def hitl_queue(u: dict = Depends(user)) -> dict:
    """Очередь HITL-подтверждений: pending-заявки на доставку наружу (ABAC по семье агента)."""
    items = await hitl_store.list_pending()
    return {"queue": [i for i in items if can_see_family(u, i.get("family"))]}


@router.get("/api/hitl/{item_id}")
async def hitl_item(item_id: str, u: dict = Depends(user)) -> dict:
    """Одна HITL-заявка с содержимым доставки (html/cfg) — превью перед подтверждением
    (UX-аудит 25.09 D-C3: подтверждали письмо, не видя его текста)."""
    item = await hitl_store.get(item_id)
    if not item:
        raise HTTPException(404, "нет такой HITL-заявки")
    if not can_see_family(u, item.get("family")):
        raise HTTPException(403, "нет доступа к семье заявки")
    payload = item.get("payload") or {}
    cfg = payload.get("cfg") or {}
    cp = payload.get("payload") if payload.get("kind") == "command" and isinstance(payload.get("payload"), dict) else {}
    # Команда в систему (задача трекера, страница вики) шла на подтверждение без текста: html пуст,
    # и человек видел «Содержимое не приложено». Отдаём тело и остальные поля команды — подтверждают
    # ровно то, что уйдёт.
    _body, _fields = "", []
    if cp:
        for _k in ("description", "body", "text", "content", "comment", "message", "html"):
            _v = cp.get(_k)
            if isinstance(_v, str) and _v.strip():
                _body = _v[:20000]
                break
        for _k, _v in cp.items():
            if _k in ("description", "body", "text", "content", "comment", "message", "html"):
                continue
            if isinstance(_v, (str, int, float, bool)) and str(_v).strip():
                _fields.append([_k, str(_v)[:300]])
        _fields = _fields[:12]
    return {"body": _body, "command_fields": _fields,
            "id": item.get("id"), "state": item.get("state"), "agent_id": item.get("agent_id"),
            "agent_name": payload.get("agent_name"), "title": item.get("title"), "channel": item.get("channel"),
            "to": item.get("to_addr"), "format": cfg.get("format") or ("command" if cp else None),
            "subject": cfg.get("subject") or cfg.get("title") or cp.get("subject") or cp.get("title"),
            "html": (payload.get("html") or "")[:20000], "created_at": item.get("created_at"),
            "requested_by": item.get("requested_by"),
            "kind": payload.get("kind") or "report", "system": payload.get("system"), "type": payload.get("type"),
            "command_id": payload.get("command_id"), "result_state": payload.get("result_state"),
            "result": payload.get("result"), "source": payload.get("source")}


@router.post("/api/hitl/{item_id}/approve")
async def hitl_approve(item_id: str, body: dict = None, u: dict = Depends(user)) -> JSONResponse:
    """Одобрить/отклонить HITL-заявку. Тело: {decision: approve|reject, reason?}. При approve —
    выполняется РЕАЛЬНАЯ доставка отчёта в канал OUT-узла. manager+ (analyst — только чтение)."""
    require_level(u, "manager")
    item = await hitl_store.get(item_id)
    if not item:
        raise HTTPException(404, "нет такой HITL-заявки")
    if not can_see_family(u, item.get("family")):
        raise HTTPException(403, "нет доступа к семье заявки")
    if item.get("state") != "pending":
        raise HTTPException(409, f"заявка уже обработана: {item.get('state')}")
    decision = str((body or {}).get("decision", "approve")).lower()
    reason = str((body or {}).get("reason", ""))
    actor = u.get("name") or u.get("sub") or "operator"
    payload = item.get("payload") or {}
    if decision == "reject":
        await hitl_store.decide(item_id, "rejected", actor, reason)
        await audit_store.record(actor, "hitl.reject", item_id,
                                 {"agent_id": item.get("agent_id"), "channel": item.get("channel")}, severity="warn")
        obs.inc("abop_hitl_total", decision="reject")
        resumed = await _resume_job_after_hitl(item_id, payload, "reject")
        return JSONResponse({"id": item_id, "state": "rejected", "resumed_job": resumed})
    # approve: заявка на ЗАПУСК (триггер с HITL-на-создание) → прогон в очередь заданий
    if payload.get("kind") == "spawn":
        ag = await agent_store.get(item.get("agent_id") or "")
        if not ag:
            raise HTTPException(404, "агент заявки не найден")
        job = await run_queue.enqueue(agent_id=ag["id"], actor=actor, kind="run",
                                      payload={"contract_audit_id": ag.get("contract_audit_id"), "use_cache": False,
                                               "user_context": "", "deliver_filter": "",
                                               "trigger": payload.get("trigger_node"), "trace_id": obs.current_trace_id()})
        await run_bus.bus().publish_request(job, obs.current_trace_id())
        await hitl_store.decide(item_id, "approved", actor, reason)
        await audit_store.record(actor, "hitl.approve", item_id, {"agent_id": ag["id"], "channel": "spawn", "job_id": job["id"]})
        obs.inc("abop_hitl_total", decision="approve")
        return JSONResponse({"id": item_id, "state": "approved", "job_id": job["id"], "delivery": "прогон поставлен в очередь"})
    # approve: команда навыка (канал command) → в шину, исполнит коннектор-воркер
    if payload.get("kind") == "command":
        if not getattr(run_bus.bus(), "active", False):
            raise HTTPException(503, "шина не подключена — команду некуда публиковать")
        res = await run_bus.publish_command(str(payload.get("system") or ""), str(payload.get("type") or "command"),
                                            payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                                            actor=actor, trace_id=str(payload.get("trace_id") or obs.current_trace_id()))
        await hitl_store.decide(item_id, "approved", actor, reason)
        await hitl_store.update_payload(item_id, {"command_id": (res.get("command") or {}).get("id"), "topic": res.get("topic")})
        await audit_store.record(actor, "hitl.approve", item_id, {"agent_id": item.get("agent_id"), "channel": "command",
                                                                  "topic": res.get("topic"), "command_id": (res.get("command") or {}).get("id")})
        obs.inc("abop_hitl_total", decision="approve")
        resumed = await _resume_job_after_hitl(item_id, payload, "approve")
        return JSONResponse({"id": item_id, "state": "approved", "delivery": "команда опубликована в " + str(res.get("topic")),
                             "command_id": (res.get("command") or {}).get("id"), "resumed_job": resumed})
    # approve → реальная отправка в канал
    cfg = payload.get("cfg") or {}
    out = await _send_channel(cfg, payload.get("agent_name"), payload.get("html") or "", real=True)
    await hitl_store.decide(item_id, "approved", actor, reason)
    await audit_store.record(actor, "hitl.approve", item_id,
                             {"agent_id": item.get("agent_id"), "channel": item.get("channel"),
                              "result": str(out)[:200]}, severity="info")
    obs.inc("abop_hitl_total", decision="approve")
    obs.log_event("info", "hitl.approved", item_id=item_id, channel=item.get("channel"))
    resumed = await _resume_job_after_hitl(item_id, payload, "approve")
    return JSONResponse({"id": item_id, "state": "approved", "delivery": str(out)[:400], "resumed_job": resumed})


async def _resume_job_after_hitl(item_id: str, payload: dict, decision: str) -> str | None:
    """Если заявка принадлежит заданию очереди, которое ждёт решения (цепочка на HITL-шаге) —
    вернуть его в очередь; воркер продолжит со следующего шага с учётом решения."""
    job = None
    jid = payload.get("job_id")
    if jid:
        job = await run_queue.get(jid)
    if not job or job.get("status") != "awaiting_hitl":
        job = await run_queue.find_awaiting_by_hitl(item_id)
    if not job:
        return None
    await run_queue.resume(job["id"], decision, item_id)
    await run_bus.bus().publish_request(job, obs.current_trace_id())
    obs.log_event("info", "run_queue.resumed", job_id=job["id"], hitl_id=item_id, decision=decision)
    return job["id"]
