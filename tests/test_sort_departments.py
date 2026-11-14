# -*- coding: utf-8 -*-
"""Сортировка агентов по отделам (решение владельца 14.11.2026).

Гарантия одна: после применения карты решений аудитор назначения (ADR-079) не даёт крита
ни одному отсортированному агенту — сортировка не может сама создать подмену. Графы в
фикстурах — слепки с прода на момент разбора.
"""
from __future__ import annotations

import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))


def _граф(*skills: str) -> dict:
    return {"nodes": [{"id": "n-" + s, "kind": "skill", "skill": s} for s in skills]}


# слепки с прода 14.11.2026: id → (семья, роль, навыки графа)
ПРОД = {
    "authored-4daa76-сверь-дорожную-карту.v14": ("management", "project-manager", ["roadmap-fact"]),
    "authored-77fa87-email-draft.v1": ("management", "project-manager", ["email-draft"]),
    "authored-77fa87-анализ-идеи.v1": ("analytics", "financial-analyst",
        ["adr-writer", "devils-advocate", "idea-scorer", "jtbd-formulator", "market-research",
         "report-editor", "researcher", "spec-reviewer"]),
    "authored-77fa87-черновик-бфт.v1": ("management", "project-manager", ["bft-draft"]),
    "plan-portfolio.v1": ("management", "", ["plan-portfolio"]),
    "plan-pr2930.v1": ("management", "", ["plan-shift"]),
    "plan-prj2451.v1": ("management", "", ["plan-shift"]),
    "oddc-2026q3.v1": ("finance", "", ["oddc-build"]),
    "finance-sverka-2026.v2": ("audit", "auditor-1c", ["audit1c-checks", "audit1c-rank",
                                                       "audit1c-root-cause", "sverka-report"]),
}


def _агенты():
    from cli import ape
    return {aid: {"id": aid, "family": fam, "role": role, "graph": _граф(*sk)}
            for aid, (fam, role, sk) in ПРОД.items()}


def test_роль_сверщика_есть_в_ростере_кода():
    from cli import ape
    members = ape.AGENT_FAMILIES["audit"]["members"]
    assert "sverщик" in members
    title, sk = members["sverщик"]
    assert set(sk) == {"audit1c-checks", "audit1c-root-cause", "audit1c-rank", "sverka-report"}


def test_сирот_навыков_нет():
    from cli import ape
    в_ростерах = set()
    for fam in ape.AGENT_FAMILIES.values():
        for rid, (t, sk) in fam["members"].items():
            в_ростерах.update(sk)
    каталог = set(ape.SKILLS)
    assert not каталог - в_ростерах, "навыки без отдела: " + str(sorted(каталог - в_ростерах))


def test_карта_проходит_проверку_целиком():
    from cli import ape
    from scripts.sort_departments import решения, РЕШЕНИЯ
    fams = {fid: {"members": fam["members"]} for fid, fam in ape.AGENT_FAMILIES.items()}
    готовы, отклонены = решения(_агенты(), fams)
    assert not отклонены, "карта содержит отказы: " + str(отклонены)
    assert {р["агент"] for р in готовы} == set(ПРОД)
    assert len(РЕШЕНИЯ) == len(ПРОД)


def test_после_сортировки_аудитор_не_даёт_крита():
    from cli import ape
    from server.assignment_audit import проверка_по_ростеру
    from scripts.sort_departments import РЕШЕНИЯ
    агенты = _агенты()
    for р in РЕШЕНИЯ:
        a = агенты[р["агент"]]
        a["family"], a["role"] = р["семья"], р["роль"]
    fams = {fid: {"members": fam["members"]} for fid, fam in ape.AGENT_FAMILIES.items()}
    for aid, a in агенты.items():
        members = (fams.get(a["family"]) or {}).get("members") or {}
        v = проверка_по_ростеру(a["graph"], a["role"], members)
        assert v["серьёзность"] != "crit", f"{aid}: крит после сортировки — {v['причина']}"
