# -*- coding: utf-8 -*-
"""Агент опирается на НЕСКОЛЬКО сводов знаний, и видно, из какого взята норма.

Управляющий графиком цитирует два документа разной природы: внутренний регламент говорит «сколько
рабочих дней у стадии», свод PMBOK — «почему резерв важнее срока». Раньше брался первый узел с
корпусом, и второй свод молча терялся: половина обоснований исчезала из отчёта без ошибки в логе.

Отдельно проверяется подпись корпуса у куска: регламент организации и отраслевой свод — разные по
силе основания, и читатель должен видеть, откуда норма.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

from server import run_exec  # noqa: E402

ТЕЛО = json.load(io.open(КОРЕНЬ / "demo" / "plan" / "agent_body.json", encoding="utf-8"))


def _агент(узлы):
    return {"id": "a.v1", "family": "management", "graph": {"nodes": узлы, "edges": []}}


@pytest.fixture
def ответы(monkeypatch):
    """Подменная sLAVA: отвечает по коллекции, чтобы видеть, к каким корпусам ходили."""
    спрошено = []

    async def _query(collection, text, *, top_k=5, tenant="abop"):
        спрошено.append(collection)
        return {"sources": [{"text": "норма из " + collection}]}

    monkeypatch.setattr(run_exec.slava, "query", _query)
    return спрошено


def test_оба_корпуса_опрашиваются(ответы):
    узлы = [n for n in ТЕЛО["graph"]["nodes"] if n.get("kind") == "doc"]
    assert len(узлы) == 2, "в графе агента должны быть оба свода знаний"
    fn = run_exec._agent_knowledge_fn(_агент(узлы), "кто-то")
    куски = asyncio.run(fn("plan-shift", ["plan_line"], "смещение сроков"))
    assert sorted(ответы) == ["slava_fam_management", "slava_pm_body"], ответы
    assert len(куски) == 2
    # Подпись корпуса видна в куске: иначе норма регламента и норма PMBOK неразличимы.
    assert any("[Регламент: производственный процесс]" in к for к in куски), куски
    assert any("[Свод знаний PMBOK 6]" in к for к in куски), куски


def test_без_узлов_берётся_корпус_семьи(ответы):
    fn = run_exec._agent_knowledge_fn(_агент([{"id": "s", "kind": "source", "entity": "plan_line"}]),
                                      "кто-то")
    asyncio.run(fn("plan-shift", [], "сроки"))
    assert ответы == ["slava_fam_management"], "фолбэк на корпус семьи потерян"


def test_недоступный_корпус_не_отменяет_доступный(ответы, monkeypatch):
    """Закрытый по ABAC корпус выбрасывается, остальные продолжают работать."""
    monkeypatch.setattr(run_exec.access, "can_reach_family",
                        lambda своя, чужая: чужая != "architecture")

    async def _audit(*a, **k):
        return None

    monkeypatch.setattr(run_exec.access, "audit_denial", _audit)
    узлы = [
        {"id": "kn_arch", "kind": "doc", "title": "Корпус архитектуры",
         "corpus": {"family": "architecture", "top_k": 2}},
        {"id": "kn_pmbok", "kind": "doc", "title": "Свод знаний PMBOK 6",
         "corpus": {"collection": "slava_pm_body", "top_k": 2}},
    ]
    fn = run_exec._agent_knowledge_fn(_агент(узлы), "кто-то")
    куски = asyncio.run(fn("plan-shift", [], "резервы"))
    assert ответы == ["slava_pm_body"], "закрытый корпус не должен опрашиваться"
    assert len(куски) == 1


def test_падение_одного_корпуса_не_роняет_прогон(monkeypatch):
    async def _query(collection, text, *, top_k=5, tenant="abop"):
        if collection == "slava_pm_body":
            raise RuntimeError("корпус недоступен")
        return {"sources": [{"text": "норма регламента"}]}

    monkeypatch.setattr(run_exec.slava, "query", _query)
    узлы = [n for n in ТЕЛО["graph"]["nodes"] if n.get("kind") == "doc"]
    fn = run_exec._agent_knowledge_fn(_агент(узлы), "кто-то")
    куски = asyncio.run(fn("plan-shift", [], "сроки"))
    assert len(куски) == 1 and "норма регламента" in куски[0]
