# -*- coding: utf-8 -*-
"""«Перечитать начисто» чистит записи СВОЕГО рецепта, а не всю сущность.

Одну сущность питают несколько рецептов: дорожную карту — файл демо, живой трекер и план
MS Project (три рецепта), расчёт смещения — по рецепту на проект. Пока reset удалял сущность
целиком, прогон одного проекта стирал данные соседнего — и пропажа выглядела как «источник ничего
не отдал», то есть как проблема источника, а не как своя же затирка.

Проверяется на обоих хранилищах: файловом (в памяти) и на канонических записях Data Plane.
"""
from __future__ import annotations

import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))


def _записи(dr, entity: str) -> list:
    """Содержимое памяти стора: ключ включает арендатора, поэтому спрашиваем его же."""
    from server import tenancy

    return dr._MEM.get(tenancy.ключ_памяти(entity), [])


def _запись(ид: str, рецепт: str, проект: str) -> dict:
    return {"id": ид, "проект": проект, "provenance": {"recipe": рецепт, "row": 0}}


def test_начисто_не_трогает_записи_другого_рецепта(monkeypatch):
    from server import dataplane_records as dr

    monkeypatch.setattr(dr, "_pool", None, raising=False)
    monkeypatch.setattr(dr, "_has_pg", lambda: False)
    dr._MEM.clear()

    dr.append("roadmap_item", [_запись("a1", "plan_roadmap", "PRJ-2451"),
                               _запись("a2", "plan_roadmap", "PRJ-2451")])
    dr.append("roadmap_item", [_запись("b1", "plan_roadmap_pr2930", "Пр2930")])

    # Перечитали начисто ТОЛЬКО план Пр2930: карта PRJ-2451 обязана остаться на месте.
    dr.append("roadmap_item", [_запись("b1", "plan_roadmap_pr2930", "Пр2930"),
                               _запись("b2", "plan_roadmap_pr2930", "Пр2930")],
              reset=True, recipe="plan_roadmap_pr2930")

    есть = {(r["id"], r["provenance"]["recipe"]) for r in _записи(dr, "roadmap_item")}
    assert ("a1", "plan_roadmap") in есть and ("a2", "plan_roadmap") in есть, (
        "прогон одного проекта стёр данные другого")
    assert len([1 for i, р in есть if р == "plan_roadmap_pr2930"]) == 2
    assert len(есть) == 4


def test_без_имени_рецепта_поведение_прежнее(monkeypatch):
    """Старые вызовы (и сторонние хранилища) не должны менять смысл молча."""
    from server import dataplane_records as dr

    monkeypatch.setattr(dr, "_pool", None, raising=False)
    monkeypatch.setattr(dr, "_has_pg", lambda: False)
    dr._MEM.clear()
    dr.append("plan_line", [_запись("x", "plan_shift", "PRJ-2451")])
    dr.append("plan_line", [_запись("y", "plan_shift_pr2930", "Пр2930")], reset=True)
    assert [r["id"] for r in _записи(dr, "plan_line")] == ["y"], "без рецепта чистится вся сущность"


def test_ядро_передаёт_имя_рецепта_в_хранилище(monkeypatch):
    """Иначе исправление не работает: сторона, которая знает рецепт, — это data_run."""
    import ape

    вызовы = []

    class Стор:
        def append(self, entity, records, *, reset=False, recipe=""):
            вызовы.append({"entity": entity, "reset": reset, "recipe": recipe,
                           "сколько": len(records)})
            return len(records)

        def compact(self, entity):
            pass

    monkeypatch.setattr(ape, "data_load_recipe", lambda имя: {"recipe": имя, "entity": "roadmap_item"})
    monkeypatch.setattr(ape, "_apply_recipe", lambda r, name="": ("roadmap_item", [{"id": "1"}], 0, 0))
    ape.set_record_store(Стор())
    try:
        ape.data_run("plan_roadmap_pr2930", True)
    finally:
        ape.set_record_store(None)
    assert вызовы == [{"entity": "roadmap_item", "reset": True,
                       "recipe": "plan_roadmap_pr2930", "сколько": 1}]


def test_хранилище_без_поддержки_рецепта_не_ломает_прогон(monkeypatch):
    """Чужой стор со старой сигнатурой обязан продолжать работать."""
    import ape

    вызовы = []

    class СтарыйСтор:
        def append(self, entity, records, *, reset=False):
            вызовы.append((entity, reset))
            return len(records)

        def compact(self, entity):
            pass

    monkeypatch.setattr(ape, "data_load_recipe", lambda имя: {"recipe": имя, "entity": "plan_line"})
    monkeypatch.setattr(ape, "_apply_recipe", lambda r, name="": ("plan_line", [{"id": "1"}], 0, 0))
    ape.set_record_store(СтарыйСтор())
    try:
        ape.data_run("plan_shift", True)
    finally:
        ape.set_record_store(None)
    assert вызовы == [("plan_line", True)]
