# -*- coding: utf-8 -*-
"""Цепочки: сборка из навыков, предложение по фразе, запуск и состояние.

Цепочка — не произвольный граф, а последовательность навыков, сцепленных по контрактам: выход
одного обязан покрывать вход следующего. Предложение цепочки по фразе идёт через разбор этапов
(server/nlu.py) и переранжирование кандидатов — угадывать единственный навык под «сделай отчёт»
среда не станет (ADR-037).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from fastapi.responses import JSONResponse

from .. import agent_store, audit_store, clients, contract_store, lexicon, nlu, pipeline_graph
from .. import pipeline_store, run_bus, run_queue

# Обогащение цепочки и перенос результата в контекст следующего шага живут в домене
# шины: их зовёт и повтор задания из DLQ. Ссылка по имени, не копия.
from .bus import _enrich_pipeline, _result_to_context
from ..deps import _uid_of, user

from .. import observability as obs
from ..nlu import nlu_config
from ..run_exec import execute_agent_run
from .agents import agents_match
from .runs import _rate_check

router = APIRouter()


@router.get("/api/pipelines")
async def pipelines_list(u: dict = Depends(user)) -> dict:
    items = await pipeline_store.all()
    return {"pipelines": [await _enrich_pipeline(p) for p in items]}


@router.post("/api/pipelines")
async def pipeline_save(body: dict, u: dict = Depends(user)) -> dict:
    name = str((body or {}).get("name") or "").strip()
    steps = (body or {}).get("steps") or []
    if not name or len(steps) < 2:
        raise HTTPException(422, "нужно имя и минимум 2 шага (цепочка)")
    # граф проверяем до сохранения: ссылка в никуда, цикл или неверное слияние иначе всплывут посреди
    # прогона, когда часть шагов уже отработала и потратила токены
    gerr = pipeline_graph.validate(steps)
    if gerr:
        raise HTTPException(422, "граф цепочки: " + "; ".join(gerr[:5]))
    import uuid as _uuid
    pid = str((body or {}).get("id") or "").strip() or ("pl_" + _uuid.uuid4().hex[:10])
    owner = _uid_of(u) or (u.get("name") or "dev")
    saved = await pipeline_store.save(pid, name, steps, owner)
    await audit_store.record(owner, "pipeline.save", pid, {"steps": len(saved.get("steps") or [])})
    return await _enrich_pipeline(saved)


@router.delete("/api/pipelines/{pid}")
async def pipeline_delete(pid: str, u: dict = Depends(user)) -> dict:
    ok = await pipeline_store.delete(pid)
    if not ok:
        raise HTTPException(404, "нет такой цепочки")
    await audit_store.record(_uid_of(u) or "dev", "pipeline.delete", pid, {})
    return {"ok": True}


@router.post("/api/pipelines/{pid}/run")
async def pipeline_run(pid: str, body: dict, u: dict = Depends(user),
                       request_async: str = Query(default="", alias="async")) -> JSONResponse:
    """Исполнить цепочку по порядку: выход шага → контекст следующего. Промежуточные шаги идут в чат
    (без внешней доставки), последний — как настроено в шаге. Возвращает результаты по шагам."""
    p = await pipeline_store.get(pid)
    if not p:
        raise HTTPException(404, "нет такой цепочки")
    steps = p.get("steps") or []
    if len(steps) < 2:
        raise HTTPException(422, "в цепочке меньше 2 шагов")
    actor = u.get("name") or u.get("sub") or "dev"
    if not _rate_check(actor):
        raise HTTPException(429, "слишком много прогонов — подождите")
    base_ctx = str((body or {}).get("context") or "").strip()[:20000]
    if bool((body or {}).get("async")) or str(request_async or "").lower() in ("1", "true", "yes"):
        # цепочка через очередь: шаги — задания воркера, HITL-пауза между шагами, чекпоинт переживает рестарт
        job = await run_queue.enqueue(agent_id=str((steps[0] or {}).get("agent_id") or pid), actor=actor, kind="pipeline",
                                      payload={"pid": pid, "name": p.get("name"), "context": base_ctx,
                                               "trace_id": obs.current_trace_id()})
        await run_bus.bus().publish_request(job, obs.current_trace_id())
        obs.inc("abop_run_jobs_total", status="queued")
        pos = await run_queue.position(job["id"])
        return JSONResponse({**run_queue.public(job, pos), "pipeline": pid, "name": p.get("name"),
                             "poll": f"/api/runs/jobs/{job['id']}"}, status_code=202)
    prev_ctx = ""
    prev_src = None   # от кого пришёл вход: заполняется после первого шага
    out_steps = []
    for i, st in enumerate(steps):
        agent = await agent_store.get(st.get("agent_id"))
        if not agent:
            out_steps.append({"agent_id": st.get("agent_id"), "error": "агент не найден", "skipped": True})
            continue
        contract = await contract_store.get(agent.get("contract_audit_id")) or \
            {"intake": {"autonomy_ceiling": agent.get("autonomy_max") or "A2"}, "bundle": {}}
        last = i == len(steps) - 1
        # выход предыдущего шага + исходная задача → контекст текущего (grounded-передача)
        ctx_parts = []
        if base_ctx:
            ctx_parts.append(base_ctx)
        if prev_ctx:
            ctx_parts.append(f"=== РЕЗУЛЬТАТ ПРЕДЫДУЩЕГО АГЕНТА ЦЕПОЧКИ (вход для тебя) ===\n{prev_ctx}")
        step_ctx = "\n\n".join(ctx_parts)
        deliver = st.get("deliver") or ("" if last else "chat")   # промежуточные — только в чат
        try:
            res = await execute_agent_run(agent, contract, actor, use_cache=False,
                                          user_context=step_ctx, deliver_filter=deliver,
                                          input_from=prev_src)
            result = res["result"]
            prev_ctx = _result_to_context(agent, result)
            prev_src = {"шаг": st.get("id") or "", "агент": agent.get("name") or agent.get("id"),
                        "прогон": res["saved"]["id"]}
            _sc = ((res.get("saved") or {}).get("run_metrics") or {}).get("cost") or {}
            _step_tokens = int(_sc.get("input_tokens") or 0) + int(_sc.get("output_tokens") or 0)
            out_steps.append({"agent_id": agent["id"], "agent_name": agent.get("name"),
                              "run_id": res["saved"]["id"], "deliver": deliver,
                              "tokens": _step_tokens,       # токены шага — повод для оптимизации (#3)
                              # полный формат прогона (как одиночный запуск) → клиент рендерит runCard:
                              # реальные находки, доставка и HITL по каждому шагу видны в чате (#4/#7)
                              "findings": result.get("findings") or [],
                              "findings_total": (result.get("findings_summary") or {}).get("total")
                              or len(result.get("findings") or []),
                              "investigations_total": len(result.get("investigations") or []),
                              "delivery": result.get("delivery") or [],
                              "verdict": result.get("verdict") or {},
                              "trace_id": result.get("trace_id") or (result.get("verdict") or {}).get("trace_id") or ""})
        except Exception as ex:  # noqa: BLE001 — сбой шага не рушит всю цепочку, помечаем и продолжаем
            out_steps.append({"agent_id": agent["id"], "agent_name": agent.get("name"), "error": str(ex)[:300]})
    await audit_store.record(actor, "pipeline.run", pid, {"steps": len(out_steps)})
    return JSONResponse({"pipeline": pid, "name": p.get("name"), "steps": out_steps}, status_code=201)


async def _rerank_chain(q: str, stage_rows: list[dict], cands: list[dict], ids: list[str], reason: str):
    """Низкая уверенность детерминированного подбора → модель выбирает из УЖЕ отобранных кандидатов
    на каждый этап. Порядок этапов не трогаем: он задан фразой."""
    import json as _json
    lines = []
    for r in stage_rows:
        opts = [{"id": r["agent_id"], "name": r["agent_name"]}] + [{"id": a["id"], "name": a["name"]} for a in (r.get("alternatives") or [])]
        lines.append(f'этап {r["order"]} ({r.get("kind") or "шаг"}, «{r["text"][:70]}»): ' +
                     "; ".join(f'{o["id"]}={o["name"]}' for o in opts))
    prompt = ("Для каждого этапа задачи выбери ОДНОГО агента из предложенных на этот этап. Порядок этапов менять нельзя.\n"
              'Ответ строго JSON: {"pick":{"1":"<id>","2":"<id>"}}\n\nЗАДАЧА: ' + q + "\n\n" + "\n".join(lines))
    try:
        resp = await clients.chat(messages=[{"role": "user", "content": prompt}], profile="standard", max_tokens=300)
        txt = resp.get("text") or ""
        i, j = txt.find("{"), txt.rfind("}")
        pick = (_json.loads(txt[i:j + 1]) if i >= 0 and j > i else {}).get("pick") or {}
        out = []
        for r in stage_rows:
            allowed = {r["agent_id"]} | {a["id"] for a in (r.get("alternatives") or [])}
            chosen = str(pick.get(str(r["order"])) or "")
            out.append(chosen if chosen in allowed else r["agent_id"])
        if out and out != ids:
            return out, reason + " · уточнено моделью", True
        return ids, reason + " · модель подтвердила", True
    except Exception:  # noqa: BLE001 — модель недоступна: остаёмся на детерминированном подборе
        return ids, reason + " · модель недоступна, подбор по словарю", False


@router.post("/api/pipelines/suggest")
async def pipeline_suggest(body: dict, u: dict = Depends(user)) -> dict:
    """Авто-сборка цепочки под задачу (#5, гибрид): семантический матчер даёт кандидатов → LLM собирает
    из них упорядоченную линейную цепочку по контексту + выбирает доставку. Дешевле полного LLM-
    планирования: кандидатов отбирает детерминированный /match, LLM лишь упорядочивает/отсекает."""
    q = str((body or {}).get("q") or "").strip()
    if not q:
        return {"steps": []}
    cands = (await agents_match(body, u)).get("matches", [])[:8]
    if not cands:
        return {"steps": [], "reason": "нет подходящих агентов в вашем отделе"}
    # ── детерминированный разбор: этапы фразы задают ПОРЯДОК шагов, модель его не меняет ──
    parsed = nlu.describe(q)
    stages = nlu.split_stages(q)
    lexmap = await lexicon.all_terms()

    def _pick(stage) -> list[dict]:
        """Кандидаты на этап по словарю лексем, от лучшего к худшему."""
        out = []
        for c in cands:
            terms = lexmap.get(c["id"]) or {}
            sc = lexicon.score(stage.terms, terms) if terms else 0.0
            out.append({"id": c["id"], "name": c.get("name"), "score": round(sc, 3), "channels": c.get("channels") or []})
        return sorted(out, key=lambda x: -x["score"])

    stage_rows: list[dict] = []
    if len(stages) > 1:
        used: set[str] = set()
        carry: set[str] = set()          # предмет предыдущего этапа: «объясни их» ссылается на него
        for st in stages:
            own = st.terms - {st.trigger}
            if len(own) < 1:
                st.before = list(set(st.before) | carry)     # местоимение/пустое окружение → контекст выше
            else:
                carry = own
            ranked = _pick(st)
            best = next((r for r in ranked if r["id"] not in used), ranked[0] if ranked else None)
            if not best:
                continue
            alts = [r for r in ranked if r["id"] != best["id"]][:2]
            used.add(best["id"])
            top_alt = alts[0]["score"] if alts else 0.0
            stage_rows.append({**st.as_dict(), "agent_id": best["id"], "agent_name": best["name"],
                               "score": best["score"], "margin": round(max(0.0, best["score"] - top_alt), 3),
                               "ambiguous": bool(best["score"] > 0 and top_alt > 0 and (best["score"] - top_alt) / best["score"] < 0.1),
                               "alternatives": alts})
    if len(stage_rows) >= 2:
        ids = [r["agent_id"] for r in stage_rows]
        cfg = await nlu_config()
        conf = round(min(min(r["score"], 1.0) for r in stage_rows), 3)
        weak = [r for r in stage_rows if r["score"] < cfg["weak_stage"]]
        amb = [r for r in stage_rows if r.get("ambiguous")]
        low = conf < cfg["min_confidence"] or bool(weak)
        deliver = next((r["channel"] for r in reversed(stage_rows) if r.get("channel")), "chat")
        name = ("Цепочка: " + " → ".join(str(r.get("kind") or r.get("trigger") or "шаг") for r in stage_rows))[:60]
        reason = "разбор фразы: " + " → ".join(f'{r["order"]}. {r.get("kind") or "шаг"}' for r in stage_rows)
        reranked = False
        if low and cfg.get("rerank"):   # низкая уверенность → реранк моделью среди кандидатов этапа
            ids, reason, reranked = await _rerank_chain(q, stage_rows, cands, ids, reason)
        nm = {c["id"]: c.get("name") for c in cands}
        for r, aid in zip(stage_rows, ids):          # этапы и шаги обязаны совпадать в UI
            if r["agent_id"] != aid:
                r["agent_id"], r["agent_name"], r["picked_by"] = aid, nm.get(aid, aid), "модель"
            else:
                r.setdefault("picked_by", "словарь")
        warn = ""
        if weak:
            warn = ("Слабый подбор на шагах: " + ", ".join(f'{r["order"]} ({r.get("kind") or "шаг"})' for r in weak)
                    + " — проверьте перед запуском")
        elif low:
            warn = "Уверенность подбора низкая — проверьте шаги перед запуском"
        elif amb:
            warn = ("Равные кандидаты на шагах: " + ", ".join(str(r["order"]) for r in amb) + " — можно заменить агента")
        return {"steps": [{"agent_id": i, "agent_name": nm.get(i, i)} for i in ids],
                "name": name, "deliver": deliver, "reason": reason,
                "confidence": conf, "low_confidence": bool(low), "ambiguous": bool(amb), "reranked": reranked,
                "warning": warn, "stages": stage_rows, "parse": parsed}
    if len(cands) < 2:
        return {"steps": [], "reason": "для цепочки нужно 2+ подходящих агента", "parse": parsed}
    cands = cands[:5]
    lines = "\n".join(
        f'- id={c["id"]} · {c.get("name")} · семья={c.get("family")} · роль={c.get("role") or "—"} · каналы={c.get("channels") or []}'
        for c in cands)
    prompt = (
        "Ты оркестратор агентов. Пользователь описал задачу; ниже агенты-кандидаты. Собери ЛИНЕЙНУЮ "
        "цепочку (выход одного агента → вход следующего), если задача требует нескольких шагов; порядок "
        "важен. Верни СТРОГО JSON без пояснений:\n"
        '{"steps":["<id>",...],"name":"<кратко>","deliver":"chat|email|redmine","reason":"<1 фраза>"}\n'
        "Бери ТОЛЬКО id из списка. Если хватает одного агента — один шаг. deliver — куда финальный результат.\n\n"
        f"ЗАДАЧА:\n{q}\n\nАГЕНТЫ:\n{lines}")
    import json as _json
    ids, name, deliver, reason = [], "Авто-цепочка", "chat", ""
    try:
        resp = await clients.chat(messages=[{"role": "user", "content": prompt}],
                                  profile="standard", max_tokens=700)
        txt = resp.get("text") or ""
        i, j = txt.find("{"), txt.rfind("}")
        data = _json.loads(txt[i:j + 1]) if i >= 0 and j > i else {}
        valid = {c["id"] for c in cands}
        ids = [s for s in (data.get("steps") or []) if s in valid]
        name = (data.get("name") or name)[:60]
        if data.get("deliver") in ("chat", "email", "redmine"):
            deliver = data["deliver"]
        reason = (data.get("reason") or "")[:200]
    except Exception:  # noqa: BLE001 — LLM недоступен → фолбэк на топ-2 семантики
        pass
    if len(ids) < 2:
        ids = [c["id"] for c in cands[:2]]
        reason = reason or "по семантике (LLM-докрутка недоступна)"
    nm = {c["id"]: c.get("name") for c in cands}
    steps = [{"agent_id": i, "agent_name": nm.get(i, i)} for i in ids]
    return {"steps": steps, "name": name, "deliver": deliver, "reason": reason}
