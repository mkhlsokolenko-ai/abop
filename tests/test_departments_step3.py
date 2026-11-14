# -*- coding: utf-8 -*-
"""Шаг 3 «Отделов агентов»: отдел как контекст.

Главное свойство: срез по отделу применяется ко ВСЕМ числам ответа — списку, сводке, группам.
Иначе список был бы бухгалтерским, а счётчики «за всё» — и человек считал бы чужую работу своей.
Принадлежность выводится из самой записи (находка → прогон → семья), как граница расчёта пилота
1С: у находки поля «семья» нет и быть не должно.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}


def _мир(monkeypatch):
    """Бухгалтерские и чужие находки; прогоны семьи finance — только r1."""
    from server import findings_registry as реестр
    from server import run_store

    строки = [
        {"id": "f1", "прогон": "r1", "код": "A1", "состояние": "найдено", "величина": "100 ₽"},
        {"id": "f2", "прогон": "r2", "код": "A1", "состояние": "найдено", "величина": "200 ₽"},
        {"id": "f3", "прогон": "r1", "состояние": "подтверждено", "код": "B2"},
    ]

    async def _runs(agent_id=None, limit=100):
        return [{"id": "r1", "family": "finance"}, {"id": "r2", "family": "management"}]

    monkeypatch.setattr(run_store, "list_runs", _runs)
    monkeypatch.setattr(реестр, "список", lambda *a, **kw: list(строки))
    # доли признания для порядка читают полный реестр — тесту хватит пустых: веса не объявлены,
    # и порядок честно останется прежним
    monkeypatch.setattr(реестр, "_строки", lambda *a, **kw: [])


def test_срез_по_отделу_режет_и_список_и_числа(monkeypatch):
    from server.routes import inbox
    _мир(monkeypatch)
    rep = asyncio.run(inbox.findings_list(отдел="finance", u=АДМИН))
    ид = [f["id"] for f in rep["findings"]]
    assert "f2" not in ид, "чужая находка (прогон другого отдела) не должна попадать в список"
    assert set(ид) == {"f1", "f3"}
    # сводка посчитана по СРЕЗУ, а не по полному реестру
    assert rep["сводка"]["всего"] == 2
    assert rep["сводка"]["подтверждено"] == 1
    # группа по причине A1 тоже режется: в ней только бухгалтерская находка
    а1 = [g for g in rep["группы"] if g["код"] == "A1"]
    assert а1 and а1[0]["находок"] == 1


def test_без_отдела_виден_весь_реестр(monkeypatch):
    from server.routes import inbox
    _мир(monkeypatch)
    rep = asyncio.run(inbox.findings_list(u=АДМИН))
    assert rep["всего"] == 3
    assert rep["сводка"]["всего"] == 3


def test_сводка_и_группы_принимают_готовый_срез(monkeypatch):
    """Чистые функции реестра: срез снаружи — та же математика, что и полный реестр."""
    from server import findings_registry as реестр
    срез = [{"состояние": "подтверждено"}, {"состояние": "найдено"}]
    assert реестр.сводка(строки=срез)["всего"] == 2
    assert реестр.сводка(строки=срез)["подтверждено"] == 1
    assert реестр.группы(строки=[{"состояние": "найдено", "код": "A1"}])[0]["код"] == "A1"
