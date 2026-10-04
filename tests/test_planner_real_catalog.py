# -*- coding: utf-8 -*-
"""Подбор на НАСТОЯЩЕМ каталоге: шесть запросов, шесть ожидаемых исполнителей.

Правила подбора проверяются рядом на игрушечном каталоге из четырёх навыков — там видно, какое
правило сработало. Здесь проверяется другое: что на всех 59 навыках поставки знакомые запросы
по-прежнему попадают в того, кто их умеет. Именно это и сломалось дважды: «нарежь задачи в трекере»
уходило в разбор почты, а «проанализируй идею» — в статус-отчёт проекта.

Каталог собирается так же, как его собирает `/api/plan/auto`: методика из SKILL.md, контракт и
объявленная доставка из template.json.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from cli import ape
from server import planner

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Сущности, в которых на стенде есть данные: без них навык считается невыполнимым и в план не идёт.
ENTITIES = {"doc1c", "ref1c", "email", "issue", "customer", "transaction",
            "roadmap_item", "contractor_report", "acceptance", "project"}

CASES = [
    ("проанализируй идею сервиса: надстройка над GitHub, агрегирует открытые репозитории по тегам, "
     "анализирует readme и рейтинги, рубрикатор с подборками и топами", "idea-scorer"),
    ("нарежь задачи в трекере", "to-tickets"),
    ("разбери почту и сделай план на день", "mail-triage"),
    ("оформи решение о выборе архитектуры как ADR", "adr-writer"),
    ("примени лимитную политику к кредитной заявке", "limit_policy_enforcement"),
    ("оцени размер рынка под гипотезу и конкурентов", "market-research"),
]


def _catalog() -> dict:
    out = {}
    for sid in list(ape.SKILLS):
        meta = ape.SKILLS.get(sid) or ()
        p = ROOT / "skills" / sid / "template.json"
        tpl = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        out[sid] = {"title": meta[0] if meta else sid,
                    "short": meta[1] if len(meta) > 1 else "",
                    "body": (ape.load_skill_body(sid) or (meta[2] if len(meta) > 2 else ""))[:6000],
                    "inputs": tpl.get("inputs") or {}, "produces": tpl.get("produces") or {},
                    "delivery": tpl.get("delivery") or {},
                    "mode": (ape.skill_safety(sid) or {}).get("mode") or "read"}
    return out


CATALOG = _catalog()


def test_каталог_собрался():
    assert len(CATALOG) > 40, f"навыков в каталоге всего {len(CATALOG)}"
    assert (CATALOG.get("to-tickets") or {}).get("body"), "методика навыка не прочиталась"


@pytest.mark.parametrize("task,want", CASES, ids=[c[1] for c in CASES])
def test_запрос_попадает_в_умеющего(task, want):
    p = planner.plan(task, CATALOG, entities=ENTITIES, slots=set(), max_steps=4)
    got = [s.get("skill") for s in (p.get("steps") or [])]
    assert want in got, f"«{task[:48]}…» → {got}, а должен был попасть в {want}"


@pytest.mark.parametrize("task,want", CASES, ids=[c[1] for c in CASES])
def test_умеющий_стоит_первым(task, want):
    """Не просто попал в план, а возглавил его: первым идёт тот, кто делает главное."""
    p = planner.plan(task, CATALOG, entities=ENTITIES, slots=set(), max_steps=4)
    got = [s.get("skill") for s in (p.get("steps") or [])]
    assert got and got[0] == want, f"«{task[:48]}…» → первым {got[:1]}, ожидали {want}"
