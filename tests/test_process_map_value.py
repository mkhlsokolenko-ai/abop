# -*- coding: utf-8 -*-
"""Карта процессов как продуктовое отличие (п.20 исследования 08.10).

Карта отвечала «как устроен процесс сейчас». Этого мало: у Camunda есть схема без факта, у
реестров агентов — факт без схемы, и ABOP отличается от обоих только если умеет четыре вещи
сразу: принять чужой процесс, показать его историю, сравнить редакции и сказать, где в процессе
дыры. Тесты сторожат смысл каждой, а не наличие ручки.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from server import bpmn_import as bi  # noqa: E402
from server import process_store as ps  # noqa: E402

BPMN = (КОРЕНЬ / "tests" / "fixtures" / "audit1c.bpmn").read_text(encoding="utf-8")

ДЕРЕВО = {"id": "org", "kind": "org", "label": "Организация", "children": [
    {"id": "p1", "kind": "process", "label": "Учёт", "area": "finance", "children": [
        {"id": "s1", "kind": "subprocess", "label": "Аудит", "children": [
            {"id": "o1", "kind": "task", "label": "Связи"},
            {"id": "o2", "kind": "task", "label": "Проверки"},
            {"id": "o3", "kind": "task", "label": "Отчёт"},
            {"id": "e1", "kind": "event", "label": "Выгрузка"},
        ]}]}]}


@pytest.fixture()
def карта():
    asyncio.run(ps._сброс_для_тестов())
    asyncio.run(ps.save_map(ps.КАРТА, ДЕРЕВО, editor="admin", note="сид"))
    return "finance"


# ── импорт чужого процесса ───────────────────────────────────────────────────────────────────────

def test_bpmn_превращается_в_карту():
    """Главное в импорте: организация не рисует процессы заново, а приносит свои."""
    r = bi.разобрать(BPMN)
    с = r["stats"]
    assert с["task"] == 3 and с["gateway"] == 1, с
    assert с["hitl"] == 1, "userTask обязан стать шагом человека, а не операцией для агента"
    assert с["wait"] == 1, "событие с таймером обязано стать ожиданием"
    assert с["subprocess"] == 1, "вложенный подпроцесс потерян"


def test_условный_поток_отличается_от_обычного():
    """Иначе развилка из шлюза читается как две одинаковые стрелки (ADR-047)."""
    процесс = bi.разобрать(BPMN)["tree"]["children"][0]
    типы = {(e["from"], e["to"]): e["kind"] for e in процесс["edges"]}
    assert типы[("g1", "u1")] == "cond", "условие на потоке потеряно"
    assert типы[("g1", "e1")] == "seq"


def test_отброшенное_названо_поимённо():
    """Молча терять элементы чужой схемы нельзя: человек должен знать, что не доехало."""
    r = bi.разобрать(BPMN)
    имена = {x["element"] for x in r["skipped"]}
    assert "sequenceFlow-через-границу" in имена, "сквозной поток отброшен молча"


def test_безымянные_элементы_получают_подпись():
    """Шлюзы и события в BPMN сплошь безымянные, а человеку подпись нужна."""
    процесс = bi.разобрать(BPMN)["tree"]["children"][0]
    пустые = [c for c in процесс["children"] if not (c.get("label") or "").strip()]
    assert not пустые, "узлы без подписи: " + str(пустые)


def test_не_bpmn_отбивается_внятно():
    for мусор in ("<html><body>нет</body></html>", "{}", "<definitions/>"):
        with pytest.raises(bi.НеBPMN):
            bi.разобрать(мусор)


def test_импорт_идёт_обычной_редакцией():
    """Второй путь записи мимо согласования обесценил бы подписываемую редакцию (ADR-042)."""
    правки = bi.правки_импорта(bi.разобрать(BPMN)["tree"])
    виды = {p["kind"] for p in правки}
    assert виды <= {"add_node", "add_edge"}, "импорт изобрёл свой вид правки: " + str(виды)
    assert правки[0]["target"] == "org", "первая правка цепляется не к корню"


# ── история ──────────────────────────────────────────────────────────────────────────────────────

def test_история_показывает_кто_и_когда(карта):
    asyncio.run(ps.save_map(ps.КАРТА, ДЕРЕВО, editor="petrova", note="вторая"))
    h = asyncio.run(ps.history(ps.КАРТА))
    assert len(h) == 2 and h[0]["revision"] > h[1]["revision"], "порядок не от свежих"
    assert h[0]["applied_by"] == "petrova" and h[0]["note"] == "вторая"
    assert h[0]["nodes"] > 0, "размер редакции не посчитан"


def test_редакцию_можно_прочитать_по_номеру(карта):
    было = asyncio.run(ps.map_at(ps.КАРТА, 1))
    assert было and было["revision"] == 1
    assert asyncio.run(ps.map_at(ps.КАРТА, 99)) is None


# ── сравнение редакций ───────────────────────────────────────────────────────────────────────────

def test_сравнение_говорит_о_сущностях_а_не_о_строках():
    """Текстовый diff двух JSON отвечает «поменялись строки», человеку нужно «появился шаг»."""
    было = {"id": "org", "kind": "org", "label": "О", "children": [
        {"id": "a", "kind": "task", "label": "Раз"},
        {"id": "b", "kind": "task", "label": "Два"}], "edges": [{"from": "a", "to": "b"}]}
    стало = {"id": "org", "kind": "org", "label": "О", "children": [
        {"id": "a", "kind": "task", "label": "Первый"},
        {"id": "c", "kind": "task", "label": "Три"}], "edges": []}
    d = ps.разница(было, стало)
    assert [x["id"] for x in d["added"]] == ["c"]
    assert [x["id"] for x in d["removed"]] == ["b"]
    assert d["renamed"] == [{"id": "a", "from": "Раз", "to": "Первый"}]
    assert len(d["edges_removed"]) == 1 and not d["edges_added"]
    assert d["total"] == 4


def test_переезд_узла_не_считается_удалением():
    """Иначе перенос операции в другой подпроцесс читался бы как «удалили и создали»."""
    было = {"id": "org", "kind": "org", "children": [
        {"id": "p1", "kind": "process", "children": [{"id": "x", "kind": "task", "label": "X"}]},
        {"id": "p2", "kind": "process", "children": []}]}
    стало = {"id": "org", "kind": "org", "children": [
        {"id": "p1", "kind": "process", "children": []},
        {"id": "p2", "kind": "process", "children": [{"id": "x", "kind": "task", "label": "X"}]}]}
    d = ps.разница(было, стало)
    assert not d["added"] and not d["removed"], "переезд показан как удаление и создание"
    assert d["moved"] == [{"id": "x", "label": "X", "from": "p1", "to": "p2"}]


def test_одинаковые_деревья_дают_пустую_разницу():
    assert ps.разница(ДЕРЕВО, ДЕРЕВО)["total"] == 0


# ── покрытие: где агенты и где дыры ──────────────────────────────────────────────────────────────

def _покрытие(посадки, состояния=None):
    live = {"ops": {op: {"state": st, "runs": 3 if st == "подтверждён" else 0, "silent_days": 5}
                    for op, st in (состояния or {}).items()}, "outside": []}
    return ps.покрытие(tree=ДЕРЕВО, seats=посадки, live=live)


def test_операция_без_агента_это_дыра():
    """Шаг без агента держится на человеке — это и есть ответ «где ещё не автоматизировано»."""
    о = _покрытие([])
    assert о["operations"] == 3, "событие посчитано как операция"
    assert о["by_state"]["без агента"] == 3
    assert len(о["gaps"]) == 3 and "держится на человеке" in о["gaps"][0]["why"]
    assert о["coverage_pct"] == 0


def test_посажен_но_молчит_тоже_дыра():
    """Главное отличие от схемы без факта: посадка сама по себе ничего не доказывает."""
    о = _покрытие([{"op_id": "o1", "audit_id": "a1"}], {"o1": "тихо"})
    тихая = [g for g in о["gaps"] if g["op_id"] == "o1"]
    assert тихая and "не работает" in тихая[0]["why"]
    assert о["coverage_pct"] == 0, "молчащий агент засчитан как покрытие"


def test_подтверждённый_прогонами_закрывает_участок():
    о = _покрытие([{"op_id": "o1", "audit_id": "a1"}], {"o1": "подтверждён"})
    assert о["by_state"]["подтверждён"] == 1
    assert о["coverage_pct"] == 33, о["coverage_pct"]
    assert all(g["op_id"] != "o1" for g in о["gaps"])


def test_осиротевший_участок_назван_отдельно():
    о = _покрытие([{"op_id": "o2", "audit_id": "a2"}], {"o2": "осиротел"})
    сирота = [g for g in о["gaps"] if g["op_id"] == "o2"]
    assert сирота and "Лимб" in сирота[0]["why"]


def test_в_отчёт_попадают_только_операции():
    """Событие, шлюз и ожидание — устройство процесса, а не работа: агента туда не сажают."""
    о = _покрытие([])
    assert {r["op_id"] for r in о["rows"]} == {"o1", "o2", "o3"}
    assert о["non_task_leaves"] == 1
