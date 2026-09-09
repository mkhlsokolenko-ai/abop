"""Детерминированный исполнитель прогона агента (ABOP §9 Run, без LLM в раскладке).

Берёт сохранённый AgentVersion + его ContractSet и раскладывает граф по волнам
(топологические уровни DAG), формирует доску событий и governance-вердикт:
- автономия узла ≤ потолок контракта (ADR-013),
- узел с внешним действием (action/external) → HITL-гейт (ADR-014), dry_run.
Числа процесса НЕ выдумываются: RunMetrics несёт governance+провенанс, а метрики
эффекта помечены как требующие реальной телеметрии (SDD §4-bis, анти-галлюцинация).
"""
from __future__ import annotations

A_LEVELS = ["A0", "A1", "A2", "A3", "A4"]


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
