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


# Каналы, уходящие за периметр: отправка по ним необратима, их и гейтим. Локальные (файл, PDF)
# остаются свободными — результат никуда не уезжает.
_LOCAL_CHANNELS = ("pdf", "file", "chat", "")
# Автономия, с которой контракт разрешает действовать наружу без человека. Ниже — только с
# подтверждением: право на безнадзорное действие даёт контракт, а не галочка в инспекторе.
_UNATTENDED_FROM = 3   # индекс A3


def check_graph(graph: dict, intake: dict, safety_of: Callable[[str], dict],
                *, reads_data: set | None = None) -> dict:
    """Вернёт {errors[], warnings[], hitl_count, autonomy_max}. errors → сохранение запрещено.

    `reads_data` — навыки, читающие данные НАПРЯМУЮ (вход `from: data` в контракте). Нужны для
    карантина действующего пути: без них доказать карантин нечем, и безнадзорная отправка наружу
    не разрешается. Не передали — считаем, что карантин не доказан.
    """
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

    # ── Узел вывода: отправка наружу без человека требует права по контракту И карантина ──
    # Прежде проверялся только навык-действие (ADR-014), а отправляет результат узел доставки.
    acting = {n["skill"] for n in skill_nodes
              if (safety_of(n["skill"]) or {}).get("mode") == "action"}
    raw_readers = sorted((reads_data or set()) & ({n["skill"] for n in skill_nodes} if reads_data is not None else set()))
    quarantined = reads_data is not None and not (acting & set(reads_data))
    for n in nodes:
        if n.get("kind") != "out":
            continue
        cfg = n.get("out") or {}
        channel = str(cfg.get("channel") or "")
        if channel in _LOCAL_CHANNELS:
            continue
        if cfg.get("hitl"):
            hitl_count += 1
            continue
        where = f"узел вывода «{n.get('title') or n.get('id')}» (канал {channel})"
        if autonomy_max < _UNATTENDED_FROM:
            errors.append(f"{where}: отправка наружу без подтверждения требует автономии A3 по контракту, "
                          f"сейчас {A_LEVELS[autonomy_max]} (ADR-014)")
        elif not quarantined:
            who = ", ".join(sorted(acting & set(reads_data or ()))) or "действующий навык"
            errors.append(f"{where}: без подтверждения действующий шаг не должен читать данные напрямую — "
                          f"«{who}» читает их сам; поставьте перед ним читающий навык или верните подтверждение")

    return {"errors": errors, "warnings": warnings, "hitl_count": hitl_count,
            "autonomy_max": A_LEVELS[autonomy_max], "raw_readers": raw_readers}
