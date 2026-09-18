"""Детерминированный исполнитель прогона агента (ABOP §9 Run, без LLM в раскладке).

Берёт сохранённый AgentVersion + его ContractSet и раскладывает граф по волнам
(топологические уровни DAG), формирует доску событий и governance-вердикт:
- автономия узла ≤ потолок контракта (ADR-013),
- узел с внешним действием (action/external) → HITL-гейт (ADR-014), dry_run.
Числа процесса НЕ выдумываются: RunMetrics несёт governance+провенанс, а метрики
эффекта помечены как требующие реальной телеметрии (SDD §4-bis, анти-галлюцинация).
"""
from __future__ import annotations

import os
import time

A_LEVELS = ["A0", "A1", "A2", "A3", "A4"]

# ── Настройки LLM-прогона (батчинг + rate-limit + обрезка промптов) ──
# ABOP_RUN_LLM_CONCURRENCY — сколько навыков анализируем ОДНОВРЕМЕННО (rate-limiter к RouteAI).
#   На единой RouteAI-карте держим низким (2). Когда поднимем свою карту с paged-attention/vLLM
#   и batch-sizing — можно повышать.
# ABOP_RUN_LLM_TRUNCATE=1 (dev) режет промпт/данные/вывод для скорости отладки.
#   НА ПРОДЕ ВЫСТАВИТЬ ABOP_RUN_LLM_TRUNCATE=0 — полные промпты (обрезка здесь временная, для теста).
# Параллелизм LLM-вызовов навыков. ВАЖНО: реальное значение задаётся env ABOP_RUN_LLM_CONCURRENCY
# в .env на сервере (перекрывает этот дефолт). ЗАМЕЧАНИЕ 2026-09-18: «замер round 2» (110с vs 299с)
# был искажён — env был жёстко =2, менялся только код-дефолт, поэтому оба прогона шли на 2; разница —
# это РАЗБРОС латентности RouteAI, не эффект параллелизма. Реальный и стабильный выигрыш дал ДАЙДЖЕСТ
# данных (токены 8×), а не параллелизм. На единой карте RouteAI большой параллелизм упирается в троттлинг.
# Backoff-ретрай — для устойчивости при 429/таймауте.
_LLM_CONCURRENCY = max(1, int(os.getenv("ABOP_RUN_LLM_CONCURRENCY", "3")))
_LLM_RETRIES = max(0, int(os.getenv("ABOP_RUN_LLM_RETRIES", "2")))       # ретраи при 429/таймауте
_LLM_BACKOFF = float(os.getenv("ABOP_RUN_LLM_BACKOFF", "1.5"))           # база backoff, сек
_LLM_TRUNCATE = os.getenv("ABOP_RUN_LLM_TRUNCATE", "1") != "0"
# rows — сколько строк ТЯНЕМ для точного счёта scope (дёшево, in-process); sample — сколько записей
# реально уходит в LLM-промпт (дорого по токенам); data — потолок символов дайджеста; body — методика.
# Оптимизация (2026-09-18): в LLM идёт ДАЙДЖЕСТ (counts по типам = весь scope + маленький сэмпл),
# а не полный дамп → в разы меньше токенов/времени. Находки audit считает КОД (детерминир.), не LLM.
_LIM = {"rows": 5000, "sample": 6, "body": 2000, "data": 4000, "max_tokens": 1200} if _LLM_TRUNCATE \
    else {"rows": 5000, "sample": 20, "body": 8000, "data": 20000, "max_tokens": 2500}


def _aidx(a: str | None) -> int:
    try:
        return A_LEVELS.index(a or "A0")
    except ValueError:
        return 0


def _waves(nodes: list[dict], edges: list[dict]) -> list[list[dict]]:
    """Топологическая раскладка по уровням: узлы без незакрытых предков идут вместе."""
    ids = [n["id"] for n in nodes]
    preds = {i: set() for i in ids}
    for e in edges or []:
        if e.get("to") in preds and e.get("from") in preds:
            preds[e["to"]].add(e["from"])
    placed: set[str] = set()
    waves: list[list[dict]] = []
    by_id = {n["id"]: n for n in nodes}
    guard = 0
    while len(placed) < len(ids) and guard < len(ids) + 2:
        layer = [i for i in ids if i not in placed and preds[i] <= placed]
        if not layer:  # цикл/висяк — кладём остаток одной волной, не зависаем
            layer = [i for i in ids if i not in placed]
        waves.append([by_id[i] for i in layer])
        placed |= set(layer)
        guard += 1
    return waves


def run_agent(agent: dict, contract: dict, safety_of) -> dict:
    """Выполнить прогон. Возвращает {run_id-less} запись: waves, board, verdict, governance."""
    graph = agent.get("graph") or {}
    nodes = [n for n in (graph.get("nodes") or []) if n.get("kind") == "skill"]
    edges = graph.get("edges") or []
    intake = contract.get("intake") or {}
    ceiling = intake.get("autonomy_ceiling") or "A0"

    waves = _waves(nodes, edges)
    board: list[dict] = []
    hitl_gates: list[dict] = []
    autonomy_used = 0

    for wi, layer in enumerate(waves, start=1):
        board.append({"kind": "wave", "wave": wi, "text": f"Волна {wi}: {', '.join(n.get('skill') or n['id'] for n in layer)}"})
        for n in layer:
            sid = n.get("skill") or n["id"]
            a = n.get("autonomy") or "A0"
            autonomy_used = max(autonomy_used, _aidx(a))
            sf = safety_of(sid) or {}
            external_action = sf.get("mode") == "action" and sf.get("egress") == "external"
            if external_action or n.get("hitl"):
                hitl_gates.append({"node": n["id"], "skill": sid})
                board.append({"kind": "hitl", "agent": sid,
                              "text": f"{sid}: внешнее действие в dry_run — ждёт подтверждения оператора"})
            else:
                board.append({"kind": "result", "agent": sid,
                              "text": f"{sid}: шаг выполнен в периметре (mode={sf.get('mode','read')}, egress={sf.get('egress','internal')})"})

    within = autonomy_used <= _aidx(ceiling)
    dod = [
        {"ok": within, "text": f"Автономия прогона {A_LEVELS[autonomy_used]} ≤ потолок {ceiling} (ADR-013)"},
        {"ok": len(nodes) > 0, "text": f"Граф исполнен по волнам ({len(waves)})"},
        {"ok": True, "text": f"Внешние действия под HITL: {len(hitl_gates)}"},
    ]
    verdict_ok = all(d["ok"] for d in dod)

    # RunMetrics (обратная петля ABOP→LUDA, SDD §4-bis). Эффект-метрики — из baseline-ключей,
    # но БЕЗ фабрикации значений: помечаем как требующие реальной телеметрии прод-прогонов.
    baseline = (contract.get("bundle") or {}).get("baseline_measurement") or {}
    metric_keys = sorted((baseline.get("metrics") or {}).keys())
    run_metrics = {
        "schema": "abop.run_metrics/1.0",
        "agent_id": agent.get("id"),
        "baseline_ref": {"audit_id": intake.get("audit_id") or agent.get("contract_audit_id")},
        "governance": {"autonomy_used": A_LEVELS[autonomy_used], "autonomy_ceiling": ceiling,
                       "within_envelope": within, "hitl_count": len(hitl_gates)},
        "provenance": {"n_waves": len(waves), "n_nodes": len(nodes)},
        "actuals": {k: None for k in metric_keys},
        "actuals_note": "значения эффекта требуют реальной телеметрии прод-прогонов (не фабрикуются)",
    }

    return {
        "agent_id": agent.get("id"),
        "contract_audit_id": agent.get("contract_audit_id"),
        "waves": [[{"id": n["id"], "skill": n.get("skill")} for n in layer] for layer in waves],
        "board": board,
        "hitl_gates": hitl_gates,
        "verdict": {"ok": verdict_ok, "dod": dod, "within_envelope": within,
                    "autonomy_used": A_LEVELS[autonomy_used]},
        "run_metrics": run_metrics,
    }


async def run_live(agent: dict, contract: dict, safety_of, *, data_query, skill_sources,
                   load_body, chat_fn, blocked_entities=None, knowledge_fn=None) -> dict:
    """НАСТОЯЩИЙ прогон: governance-каркас (run_agent) + для каждого навыка с data-scope
    собирает РЕАЛЬНЫЕ данные из canonical store (data_query) и прогоняет их через LLM
    (тело навыка = методика) → находки на доску. Числа — только из данных (анти-галлюцинация).
    blocked_entities — сущности, закрытые ABAC (система вне scope семьи): навык их НЕ читает."""
    import json as _json
    import asyncio
    blocked = set(blocked_entities or [])
    base = run_agent(agent, contract, safety_of)
    graph = agent.get("graph") or {}
    skills = [n.get("skill") or n["id"] for n in (graph.get("nodes") or []) if n.get("kind") == "skill"]
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)  # rate-limiter: не больше N одновременных вызовов к RouteAI

    async def _analyze(sid):
        entities = []
        for ds in (skill_sources(sid) or []):
            e = ds.get("entity")
            if e and e not in entities:
                entities.append(e)
        blk = [e for e in entities if e in blocked]        # ABAC: закрытые сущности
        entities = [e for e in entities if e not in blocked]
        if blk and not entities:  # весь data-scope навыка закрыт правами — навык не читает, честно помечаем
            return {"skill": sid, "entities": [], "model": "", "input_tokens": 0, "output_tokens": 0,
                    "text": "⛔ доступ к данным запрещён ABAC (система вне scope семьи): " + ", ".join(blk)}
        data = {}
        for e in entities:
            try:
                data[e] = data_query(e, limit=_LIM["rows"])
            except Exception:  # noqa: BLE001
                data[e] = []
        if not any(data.values()):
            return None  # навык без данных в store — LLM-анализ не запускаем
        # ДАЙДЖЕСТ вместо полного дампа: точный объём + разбивка по типам (весь scope) + ограниченный
        # сэмпл записей. Даёт LLM ситуативную осведомлённость без раздувания токенов (детект-находки —
        # у детерминир. движка). Резко режет стоимость/время (было ~100k токенов/вызов на полном дампе).
        digest = {}
        for e, rows in data.items():
            by_type: dict = {}
            for r in rows:
                if isinstance(r, dict) and r.get("тип"):
                    by_type[r["тип"]] = by_type.get(r["тип"], 0) + 1
            digest[e] = {"всего": len(rows), "по_типам": (by_type or None),
                         "сэмпл": rows[:_LIM["sample"]]}
        body = (load_body(sid) or "")[:_LIM["body"]]
        # RAG-знание (нормы/регламент) из корпуса семьи через sLAVA — только норм-цитирующим навыкам
        # (safety.cite). knowledge_fn уже с ABAC-гейтом (вернёт [], если семья без доступа к корпусу).
        know_block = ""
        if knowledge_fn and (safety_of(sid) or {}).get("cite"):
            try:
                chunks = await knowledge_fn(sid, entities, body)   # body = методика навыка → релевантный запрос
            except Exception:  # noqa: BLE001
                chunks = []
            if chunks:
                know_block = ("=== НОРМЫ/ЗНАНИЕ (RAG из корпуса семьи, sLAVA) ===\n"
                              + "\n---\n".join(c[:800] for c in chunks[:4]) + "\n\n")
        prompt = ("Ты — навык агента ABOP. Ниже методика навыка и РЕАЛЬНЫЕ данные из Data Plane (canonical, с provenance).\n\n"
                  "=== МЕТОДИКА ===\n" + body + "\n\n"
                  + know_block
                  + "=== ДАННЫЕ (дайджест: всего+по_типам = полный scope, сэмпл = примеры записей) ===\n"
                  + _json.dumps(digest, ensure_ascii=False)[:_LIM["data"]] + "\n\n"
                  "ЗАДАЧА: примени методику к данным. Верни КОНКРЕТНЫЕ находки/расхождения списком — "
                  "каждая со ссылкой на id записи и суммой" + (", и на норму из блока ЗНАНИЕ, если применимо" if know_block else "")
                  + ". Только из данных и приведённых норм, ничего не выдумывай. Если расхождений нет — так и скажи.")
        _t = time.perf_counter()
        async with sem:  # батчинг: семафор пускает по _LLM_CONCURRENCY вызовов за раз
            # backoff-ретрай (429/таймаут): рост параллелизма не должен портить вывод скилла —
            # при перегрузе RouteAI ждём и повторяем, а не отдаём «LLM недоступен». Качество без изменений.
            resp = None
            err = None
            for _attempt in range(_LLM_RETRIES + 1):
                try:
                    resp = await chat_fn(messages=[{"role": "user", "content": prompt}], profile="standard",
                                         max_tokens=_LIM["max_tokens"])
                    err = None
                    break
                except Exception as ex:  # noqa: BLE001
                    err = f"{type(ex).__name__}: {ex}"
                    if _attempt < _LLM_RETRIES:
                        await asyncio.sleep(_LLM_BACKOFF * (_attempt + 1))  # 1.5с, 3с, …
            if resp is not None:
                txt = (resp.get("text") or "").strip() or "(пустой ответ модели)"
                model = resp.get("model", "")
                tin, tout = int(resp.get("input_tokens") or 0), int(resp.get("output_tokens") or 0)
            else:
                txt, model, tin, tout = f"(LLM недоступен: {err})", "", 0, 0
        ms = round((time.perf_counter() - _t) * 1000, 1)  # per-skill тайминг (observability)
        return {"skill": sid, "entities": entities, "model": model, "text": txt,
                "input_tokens": tin, "output_tokens": tout, "ms": ms, "error": err}

    # навыки — параллельно, но с rate-limit (семафор): батч по _LLM_CONCURRENCY к RouteAI
    results = await asyncio.gather(*[_analyze(s) for s in skills])
    findings = [r for r in results if r]
    for f in findings:
        base["board"].append({"kind": "finding", "agent": f["skill"], "text": f["text"][:1800]})
    base["findings"] = findings
    # Реальный биллинг (7.1): токены из ответов RouteAI + тариф pricing.cost_rub → cost в RunMetrics
    # (НЕ хардкод). Пустые модели (LLM был недоступен) в стоимость не идут. Персистится в payload.
    from . import pricing
    by_model: dict[str, dict] = {}
    tin = tout = 0
    for f in findings:
        m = f.get("model") or ""
        if not m:
            continue
        i, o = int(f.get("input_tokens") or 0), int(f.get("output_tokens") or 0)
        tin += i; tout += o
        bm = by_model.setdefault(m, {"input_tokens": 0, "output_tokens": 0, "rub": 0.0, "calls": 0})
        bm["input_tokens"] += i; bm["output_tokens"] += o
        bm["rub"] = round(bm["rub"] + pricing.cost_rub(m, i, o), 4)
        bm["calls"] += 1
    base["run_metrics"]["cost"] = {
        "schema": "abop.run_cost/1.0",
        "rub": round(sum(v["rub"] for v in by_model.values()), 4),
        "input_tokens": tin, "output_tokens": tout,
        "calls": sum(v["calls"] for v in by_model.values()),
        "by_model": by_model,
    }
    # per-skill тайминги (observability): где узкое место цепочки; slowest — самый долгий навык
    by_skill = {f["skill"]: f.get("ms") for f in findings if f.get("ms") is not None}
    slowest = max(by_skill.items(), key=lambda kv: kv[1]) if by_skill else None
    base["run_metrics"]["timings"] = {
        "by_skill_ms": by_skill,
        "llm_ms_total": round(sum(by_skill.values()), 1),
        "slowest": ({"skill": slowest[0], "ms": slowest[1]} if slowest else None),
    }
    base["live"] = True
    return base
