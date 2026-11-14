# -*- coding: utf-8 -*-
"""Шаг 6 «Отделов агентов»: матрица методологии и сверка (ADR-084).

Главные гарантии: сверка — чистая функция без LLM (детерминированная, тестируемая); связь
«пункт → параметр» объявлена матрицей, а не выведена программой; чужой параметр в матрице —
отказ, а не молчаливая запись; непокрытые пункты считаются только по процессам матрицы,
регламент всей установки не валится в один отчёт; чужой отдел — 403.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}
АНАЛИТ = {"sub": "u2", "name": "аналитик", "roles": ["analyst"], "level": "analyst", "department": "finance"}


def test_сверка_ловит_несоответствие_с_цитатой():
    from server.methodology import сверка
    чанки = [{"nsi_key": "4[01][001]", "text": "План сверяется с фактом еженедельно."}]
    матрица = [{"ключ_нси": "4[01][001]", "параметр": "роль_в_отделе", "цель": "project-controller",
                "значение": "да"}]
    rep = сверка(чанки, матрица, {"роль_в_отделе": {"project-controller": "нет"}})
    assert rep["проверено"] == 1
    assert len(rep["расхождения"]) == 1
    р = rep["расхождения"][0]
    assert р["тип"] == "не_соответствует"
    assert "еженедельно" in р["цитата"], "расхождение обязано нести цитату пункта"


def test_сверка_тихая_когда_всё_сходится():
    from server.methodology import сверка
    чанки = [{"nsi_key": "4[01][001]", "text": "План сверяется еженедельно."}]
    матрица = [{"ключ_нси": "4[01][001]", "параметр": "роль_в_отделе", "цель": "project-controller",
                "значение": "да"}]
    rep = сверка(чанки, матрица, {"роль_в_отделе": {"project-controller": "да"}})
    assert rep["расхождения"] == [] and rep["проверено"] == 1


def test_непокрытые_только_процессов_матрицы():
    from server.methodology import сверка
    чанки = [{"nsi_key": "4[01][001]", "text": "пункт покрытый"},
             {"nsi_key": "4[02][003]", "text": "пункт непокрытый своего процесса"},
             {"nsi_key": "9[01][001]", "text": "чужой процесс — не в отчёт"}]
    матрица = [{"ключ_нси": "4[01][001]", "параметр": "роль_в_отделе", "цель": "r1", "значение": "да"}]
    rep = сверка(чанки, матрица, {"роль_в_отделе": {"r1": "да"}})
    keys = [п["ключ"] for п in rep["непокрытые_пункты"]]
    assert keys == ["4[02][003]"], "чужие процессы не должны попадать в непокрытые"


def test_осиротевшая_строка_и_несуществующая_цель():
    from server.methodology import сверка
    чанки = []   # пункт исчез из регламента
    матрица = [{"ключ_нси": "4[01][001]", "параметр": "правило_активно", "цель": "flow-dev",
                "значение": "да"}]
    rep = сверка(чанки, матрица, {"правило_активно": {}})
    assert rep["расхождения"][0]["тип"] == "осиротевшая_строка"

    чанки2 = [{"nsi_key": "4[02][002]", "text": "Метрики потока считаются непрерывно."}]
    матрица2 = [{"ключ_нси": "4[02][002]", "параметр": "правило_активно", "цель": "flow-dev",
                 "значение": "да"}]
    rep2 = сверка(чанки2, матрица2, {"правило_активно": {}})   # набора нет
    assert rep2["расхождения"][0]["тип"] == "нет_в_конфигурации"


def test_матрица_чужой_параметр_отказ(monkeypatch):
    from server import matrix_store
    monkeypatch.setattr(matrix_store, "_has_pg", lambda: False)

    async def flow():
        matrix_store._MEM.clear()
        try:
            await matrix_store.сохранить("finance", "4[01][001]", "магия", "цель", "да", "кто-то")
            raise AssertionError("чужой параметр должен был отклониться")
        except ValueError:
            pass
        строка = await matrix_store.сохранить("finance", "4[01][001]", "правило_активно",
                                              "audit1c-base", "нет", "главбух")
        assert строка["значение"] == "нет"
        строки = await matrix_store.записи("finance")
        assert len(строки) == 1 and строки[0]["автор"] == "главбух"

    asyncio.run(flow())


def test_ручка_сверки_чужой_отдел_403(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from server.routes import departments as D
    from server import methodology as M

    async def _никогда(*a, **kw):
        raise AssertionError("чужой отдел не должен был сверяться")

    monkeypatch.setattr(M, "сверка_отдела", _никогда)
    monkeypatch.setattr(D.methodology, "сверка_отдела", _никогда)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(D.methodology_sverka("audit", АНАЛИТ))   # аналитик finance
    assert ex.value.status_code == 403
