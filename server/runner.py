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

A_LEVELS = ["A0", "A1", "A2", "A3", "A4"]

# ── Настройки LLM-прогона (батчинг + rate-limit + обрезка промптов) ──
# ABOP_RUN_LLM_CONCURRENCY — сколько навыков анализируем ОДНОВРЕМЕННО (rate-limiter к RouteAI).
#   На единой RouteAI-карте держим низким (2). Когда поднимем свою карту с paged-attention/vLLM
#   и batch-sizing — можно повышать.
# ABOP_RUN_LLM_TRUNCATE=1 (dev) режет промпт/данные/вывод для скорости отладки.
#   НА ПРОДЕ ВЫСТАВИТЬ ABOP_RUN_LLM_TRUNCATE=0 — полные промпты (обрезка здесь временная, для теста).
_LLM_CONCURRENCY = max(1, int(os.getenv("ABOP_RUN_LLM_CONCURRENCY", "2")))
_LLM_TRUNCATE = os.getenv("ABOP_RUN_LLM_TRUNCATE", "1") != "0"
_LIM = {"rows": 40, "body": 2500, "data": 5000, "max_tokens": 1600} if _LLM_TRUNCATE \
    else {"rows": 1000, "body": 100000, "data": 200000, "max_tokens": 4096}


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
                   load_body, chat_fn, blocked_entities=None) -> dict:
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
        body = (load_body(sid) or "")[:_LIM["body"]]
        prompt = ("Ты — навык агента ABOP. Ниже методика навыка и РЕАЛЬНЫЕ данные из Data Plane (canonical, с provenance).\n\n"
                  "=== МЕТОДИКА ===\n" + body + "\n\n"
                  "=== ДАННЫЕ (JSON по сущностям) ===\n" + _json.dumps(data, ensure_ascii=False)[:_LIM["data"]] + "\n\n"
                  "ЗАДАЧА: примени методику к данным. Верни КОНКРЕТНЫЕ находки/расхождения списком — "
                  "каждая со ссылкой на id записи и суммой. Только из данных, ничего не выдумывай. "
                  "Если расхождений нет — так и скажи.")
        async with sem:  # батчинг: семафор пускает по _LLM_CONCURRENCY вызовов за раз
            try:
                resp = await chat_fn(messages=[{"role": "user", "content": prompt}], profile="standard",
                                     max_tokens=_LIM["max_tokens"])
                txt = (resp.get("text") or "").strip() or "(пустой ответ модели)"
                model = resp.get("model", "")
                tin, tout = int(resp.get("input_tokens") or 0), int(resp.get("output_tokens") or 0)
            except Exception as ex:  # noqa: BLE001 — LLM недоступен → честно помечаем, прогон не падает
                txt, model, tin, tout = f"(LLM недоступен: {type(ex).__name__}: {ex})", "", 0, 0
        return {"skill": sid, "entities": entities, "model": model, "text": txt,
                "input_tokens": tin, "output_tokens": tout}

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
    base["live"] = True
    return base
