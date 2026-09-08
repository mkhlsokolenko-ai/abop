"""Governance-проверка собранного графа агента против контракта LUDA.

Инварианты шва (жёсткие, до сохранения AgentVersion):
- ADR-013: автономия любого узла НЕ выше DeploymentContract.autonomy_level (потолок).
- ADR-014: узел с внешним действием (mode=action, egress=external) обязан быть под HITL.
- SDD §4.4: требуемые контрактом навыки должны быть покрыты (иначе предупреждение).
Безопасность узла берём АВТОРИТЕТНО из каталога навыков (ape.skill_safety), не из клиента.
"""
from __future__ import annotations

from typing import Callable

A_LEVELS = ["A0", "A1", "A2", "A3", "A4"]


def _aidx(a: str | None) -> int:
    try:
        return A_LEVELS.index(a or "A0")
    except ValueError:
        return 0


def check_graph(graph: dict, intake: dict, safety_of: Callable[[str], dict]) -> dict:
    """Вернёт {errors[], warnings[], hitl_count, autonomy_max}. errors → сохранение запрещено."""
    errors: list[str] = []
    warnings: list[str] = []
    nodes = (graph or {}).get("nodes") or []
    ceiling = intake.get("autonomy_ceiling") or "A0"
    ceil_idx = _aidx(ceiling)

    skill_nodes = [n for n in nodes if n.get("kind") == "skill" and n.get("skill")]
    gap_nodes = [n for n in nodes if n.get("kind") == "gap"]
    present = {n["skill"] for n in skill_nodes}
    hitl_count = 0
    autonomy_max = 0

    for n in skill_nodes:
        sid = n["skill"]
        a = n.get("autonomy") or "A0"
        autonomy_max = max(autonomy_max, _aidx(a))
        if _aidx(a) > ceil_idx:
            errors.append(f"узел «{sid}»: автономия {a} выше потолка контракта {ceiling} (ADR-013)")
        safety = safety_of(sid) or {}
        external_action = safety.get("mode") == "action" and safety.get("egress") == "external"
        if n.get("hitl"):
            hitl_count += 1
        if external_action and not n.get("hitl"):
            errors.append(f"узел «{sid}»: внешнее действие (action/external) без HITL — запрещено (ADR-014)")

    # узлы-пробелы (требуется контрактом, нет в каталоге ABOP) — не собрать
    for n in gap_nodes:
        errors.append(f"узел «{n.get('skill') or n.get('id')}»: нет в каталоге ABOP — создайте навык (SDD §4.4)")

    # покрытие требуемых контрактом навыков
    required = set(intake.get("skills") or [])
    for sid in sorted(required - present):
        warnings.append(f"требуемый контрактом навык «{sid}» не размещён на канве (SDD §4.4)")

    # покрытие точек HITL из контракта
    req_hitl = len(intake.get("hitl_points") or [])
    if req_hitl and hitl_count < req_hitl:
        warnings.append(f"контракт требует {req_hitl} точек HITL, на канве отмечено {hitl_count}")

    return {"errors": errors, "warnings": warnings, "hitl_count": hitl_count,
            "autonomy_max": A_LEVELS[autonomy_max]}
