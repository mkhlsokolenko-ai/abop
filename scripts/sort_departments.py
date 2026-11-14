# -*- coding: utf-8 -*-
"""Сортировка агентов по отделам (шаг 3 «Отделов агентов», решение владельца 14.11.2026).

Роль агента — то, чем он НАЗВАН в отделе; аудитор назначения (ADR-079) сверяет с ней граф.
Разбор прода 14.11 нашёл агентов, чья роль не совпадает с фактической работой: authored-агенты
из чата получали роль по intake контракта, а не по графу; у трёх контрактных роль осталась
пустой. Здесь — ЯВНАЯ карта решений, каждая строчка проверена по пересечению навыков графа с
ростером семьи; перед записью скрипт требует, чтобы с НОВОЙ ролью аудит назначения не давал
крит (иначе сортировка сама бы создала подмену).

Меняются ТОЛЬКО поля family/role (ABAC, карточки, аудит): граф и версия не трогаются, новой
версии не создаётся. Каждое изменение — запись в журнал аудита.

Запуск ВНУТРИ контейнера (docker exec -i abop-webapi python - < scripts/sort_departments.py --apply).
Без --apply — сухой прогон: покажет, что изменилось бы, но не запишет.
"""
from __future__ import annotations

import asyncio
import sys

# Каждое решение названо и обосновано; роль — по фактическому графу, не по intake.
РЕШЕНИЯ = [
    {"агент": "authored-4daa76-сверь-дорожную-карту.v14", "семья": "management", "роль": "project-controller",
     "почему": "граф — roadmap-fact: это работа контролёра проекта, а не проектного менеджера"},
    {"агент": "authored-77fa87-email-draft.v1", "семья": "management", "роль": "comms",
     "почему": "граф — email-draft: письма — работа коммуникаций"},
    {"агент": "authored-77fa87-анализ-идеи.v1", "семья": "analytics", "роль": "business-analyst",
     "почему": "граф — jtbd/market-research/idea-scorer: это бизнес-аналитика, не финансовая"},
    {"агент": "authored-77fa87-черновик-бфт.v1", "семья": "management", "роль": "bft-writer",
     "почему": "граф — bft-draft: БФТ пишет аналитик БФТ"},
    {"агент": "plan-portfolio.v1", "семья": "management", "роль": "portfolio-keeper",
     "почему": "граф — plan-portfolio: управляющий портфелем; роль была пустой"},
    {"агент": "plan-pr2930.v1", "семья": "management", "роль": "schedule-keeper",
     "почему": "граф — plan-shift: управляющий графиком; роль была пустой"},
    {"агент": "plan-prj2451.v1", "семья": "management", "роль": "schedule-keeper",
     "почему": "граф — plan-shift: управляющий графиком; роль была пустой"},
    {"агент": "oddc-2026q3.v1", "семья": "finance", "роль": "accounting",
     "почему": "граф — oddc-build: закрытие/ОДДС — работа бухгалтерии; роль была пустой"},
    {"агент": "finance-sverka-2026.v2", "семья": "audit", "роль": "sverщик",
     "почему": "живой пилотный сверщик: checks+root-cause+rank+sverka-report — своя роль в ростере, а не демо-«Аудитор 1С»"},
]


def решения(агенты: dict, ростер: dict) -> tuple[list, list]:
    """Проверить карту против фактов. Возвращает (готовые, отклонённые).

    Отклонение — это отказ СКРИПТАМЕНТА: агента нет, роль не в ростере семьи, с новой ролью
    аудит назначения дал бы крит. Сухой прогон и запись одинаково проходят через эту проверку.
    """
    from server import assignment_audit
    готовы, отклонены = [], []
    for р in РЕШЕНИЯ:
        a = агенты.get(р["агент"])
        if not a:
            отклонены.append(dict(р, отказ="агента в реестре нет"))
            continue
        роль_навыки = (((ростер.get(р["семья"]) or {}).get("members") or {}).get(р["роль"]) or (None, []))[1]
        verdict = assignment_audit.проверка_по_ростеру(a.get("graph") or {}, р["семья"], р["роль"],
                                                       {р["роль"]: (р["роль"], роль_навыки)})
        if verdict["серьёзность"] == "crit":
            отклонены.append(dict(р, отказ="с новой ролью аудит назначения дал бы крит: " + verdict["причина"]))
            continue
        if (a.get("family") or "") == р["семья"] and (a.get("role") or "") == р["роль"]:
            отклонены.append(dict(р, отказ="уже отсортирован"))
            continue
        готовы.append(dict(р, было=f"{a.get('family') or '—'}/{a.get('role') or '—'}"))
    return готовы, отклонены


async def main(argv: list) -> int:
    применять = "--apply" in argv
    # Скрипт идёт внутрь контейнера через stdin (`docker exec -i python -`): sys.path свежего
    # процесса не знает /app/cli, где живёт ростер. Локально путь не существует — и не мешает.
    import os
    for p in ("/app/cli",):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    from server import agent_store, assignment_audit, audit_store, families_store
    from cli import ape
    from server.config import settings
    pg = bool(settings.pg_dsn)
    активные = await agent_store.list_for(None, limit=500)
    агенты = {}
    for a in активные:
        полный = await agent_store.get(a["id"]) or {}
        полный.update({k: a[k] for k in ("family", "role") if a.get(k)})
        агенты[a["id"]] = полный
    fams = {f["id"]: f for f in (await families_store.all() or [])}
    if not fams:
        fams = {fid: {"members": fam["members"]} for fid, fam in ape.AGENT_FAMILIES.items()}
    ростер = {fid: {"members": f.get("members") or {}} for fid, f in fams.items()}

    готовы, отклонены = решения(агенты, ростер)
    for р in отклонены:
        print(f"ОТКАЗ  {р['агент']}: {р['отказ']}")
    for р in готовы:
        действие = "меняю" if применять else "изменю"
        print(f"{действие}: {р['агент']}: {р['было']} -> {р['семья']}/{р['роль']} ({р['почему']})")
    if применять and pg and готовы:
        from server.db import _conn
        changed = 0
        async with _conn() as conn:
            for р in готовы:
                await conn.execute(
                    "UPDATE agent_versions SET family=%s, role=%s WHERE id=%s AND tenant=(SELECT tenant FROM agent_versions WHERE id=%s)",
                    (р["семья"], р["роль"], р["агент"], р["агент"]))
                await audit_store.record("сортировка отделов", "agent.sort_departments", р["агент"],
                                         {"было": р["было"], "стало": f"{р['семья']}/{р['роль']}",
                                          "почему": р["почему"]})
                changed += 1
        print(f"записано изменений: {changed}")
    elif применять and not pg:
        print("PG нет — запись идёт только на стенде с Postgres; сухой прогон выше")
    print(f"итого: к изменению {len(готовы)}, отказов {len(отклонены)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
