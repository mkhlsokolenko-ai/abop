# -*- coding: utf-8 -*-
"""Аудитор назначения агента — шаг 1 плана «Отделы агентов» (docs/OTDELY_AGENTOV.md §11).

Сверяет граф агента с ростером его семьи: делает ли агент то, чем назван. Эталон — ростер
семьи (families_store, единый реестр), а не контракт LUDA: контракт пересобирается вместе
с агентом и потому ничего не замечает. Случай, ради которого это написано: у контракта
audit1c-holding-2026 версия v11 «Аудитор данных в 1С» потеряла audit1c-checks и несла граф
следователя — прогоны зелёные, вердикт «ok», находок ноль, неделю никто не замечал. Ростер
семьи живёт дольше версий агента и не пересобирается вместе с ними.

Только машинные факты: пересечение навыков графа с ростом ролей семьи. Три исхода:
- ok   — граф соответствует объявленной роли, или судить не на чем (семья без ростера,
         кастомная; граф без узлов-навыков);
- warn — частичные расхождения: не хватает отдельных навыков роли; заявленной роли нет
         в ростере, но граф похож на другую;
- crit — агент не делает того, чем назван: навыки графа лучше ложатся на ДРУГУЮ роль семьи
         или не ложатся ни на одну.

Решение о судьбе — не здесь. Сохранить с предупреждением можно (пересмотр роли — легитимный
этап работы); развернуть агента с crit нельзя без force_assignment, и override записывается
в журнал аудита. Уже развёрнутые провалы показывает флот и ручка /api/agents/assignment-audit.
"""
from __future__ import annotations

from . import families_store


def навыки_графа(graph: dict) -> set[str]:
    """Навыки узлов графа — та же выборка, что в assembly.check_graph (узлы kind=skill)."""
    return {n["skill"] for n in ((graph or {}).get("nodes") or [])
            if isinstance(n, dict) and n.get("kind") == "skill" and n.get("skill")}


def _ростер(members: dict) -> dict[str, set[str]]:
    """Роль → множество навыков. members: role → [title, [skills]] (семьи в коде дают кортеж)."""
    out: dict[str, set[str]] = {}
    for rid, m in (members or {}).items():
        sk = m[1] if isinstance(m, (list, tuple)) and len(m) > 1 else []
        skills = {s for s in (sk or []) if isinstance(s, str)}
        if skills:
            out[rid] = skills
    return out


def проверка_по_ростеру(graph: dict, role: str, members: dict) -> dict:
    """Чистая функция вердикта: ростер уже в руках вызывающего, тестам не нужен стор.

    Правила серьёзности:
    - crit, если у ЛУЧШЕЙ чужой роли есть навыки, которых нет у объявленной, и граф их несёт
      (граф — работа другой роли), либо в графе нет ни одного навыка объявленной роли;
    - warn, если не хватает части навыков роли или роль не объявлена в ростере, но граф
      на кого-то похож;
    - иначе ok. Лишние навыки (вне ростера роли) не поднимают серьёзность сами по себе —
      ростер может отставать от каталога, посев ничего не удаляет.
    """
    v = {"ok": True, "серьёзность": "ok", "фактическая_роль": None,
         "отсутствуют": [], "лишние": [], "причина": ""}
    roster = _ростер(members)
    skills = навыки_графа(graph)
    if not roster or not skills:
        return v  # судить не на чем: кастомная семья без ростера или граф без навыков
    scores = {rid: len(sk & skills) for rid, sk in roster.items()}
    declared = roster.get(role or "", set())
    declared_score = scores.get(role or "", 0)
    best = max(scores, key=lambda rid: scores[rid])  # type: ignore[arg-type]
    лишние = sorted(skills - declared)
    if declared and best != role and scores.get(best, 0) > declared_score and (skills & (roster[best] - declared)):
        return dict(v, ok=False, серьёзность="crit", фактическая_роль=best,
                    отсутствуют=sorted(declared - skills), лишние=лишние,
                    причина=(f"граф делает работу другой роли «{best}» ({scores[best]} навыков из "
                             f"{len(skills)}) — не объявленную «{role}» ({declared_score}); "
                             "агент не делает того, чем назван"))
    if declared and declared_score == 0:
        return dict(v, ok=False, серьёзность="crit", фактическая_роль=None,
                    отсутствуют=sorted(declared), лишние=лишние,
                    причина=(f"в графе нет ни одного навыка роли «{role}» — "
                             "агент не делает того, чем назван"))
    if not declared:
        if scores.get(best, 0) > 0:
            return dict(v, ok=False, серьёзность="warn", фактическая_роль=best, лишние=лишние,
                        причина=(f"роль «{role or '—'}» в ростере семьи не объявлена; "
                                 f"граф по навыкам похож на роль «{best}»"))
        return dict(v, лишние=лишние)
    missing = sorted(declared - skills)
    if missing:
        return dict(v, ok=False, серьёзность="warn", отсутствуют=missing, лишние=лишние,
                    причина="не хватает навыков роли: " + ", ".join(missing))
    return dict(v, лишние=лишние)


async def проверка(graph: dict, family: str, role: str) -> dict:
    """Вердикт назначения для агента: ростер из реестра семей, дефолт — семьи в коде.

    Код cli.ape.AGENT_FAMILIES — посадочная роспись builtin-семей: реестр сеется из него при
    старте, но тестам и системам без PG код доступен всегда, поэтому служит запасным эталоном.
    """
    fam = await families_store.get(family or "") if family else None
    members = (fam or {}).get("members")
    if not members:
        from cli import ape  # локальный импорт: ape тяжёлый, нужен только как дефолт-роспись
        members = (ape.AGENT_FAMILIES.get(family or "") or {}).get("members")
    return проверка_по_ростеру(graph, role, members)


def текст(v: dict) -> str:
    """Человекочитаемая причина — для warning'а при сохранении и алертов флота."""
    if v.get("серьёзность") == "ok":
        return ""
    return str(v.get("причина") or "назначение не подтверждено")
