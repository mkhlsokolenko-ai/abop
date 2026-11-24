# -*- coding: utf-8 -*-
"""Патруль методолога (автоматизация шага 6, решение владельца 19.11): заявки собирает код.

Источники: сверка отделов, дыры способов (реестр), NCR, аудит назначения deployed-агентов.
Заявки детерминированы и типизированы; применимое машиной — только «роль_в_отделе».
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))


def test_патруль_собирает_четыре_источника(monkeypatch):
    from server import families_store, findings_registry as fr, methodology, ncr_store
    from server import agent_store, assignment_audit

    async def _все():
        return [{"id": "finance", "kind": "business"}, {"id": "audit", "kind": "business"}]

    async def _сверка_ок(отдел):
        return {"расхождения": [], "непокрытые_пункты": []}

    async def _сверка_метод(отдел):
        if отдел == "audit":
            return {"расхождения": [], "непокрытые_пункты": []}
        # Форма — как у настоящей methodology.сверка: тип «не_соответствует», параметр
        # «роль_в_отделе». Прежний мок нёс «тип: роль_в_отделе» — формы, которой сверка не
        # выдаёт вовсе: тест кодировал баг (применимое всегда False, кнопки «Применить» не было
        # с 20.11), вместо того чтобы его ловить.
        return {"расхождения": [{"тип": "не_соответствует", "пункт": "2[06][001]", "параметр": "роль_в_отделе",
                                 "цель": "svershik", "значение": "да", "матрица": "да",
                                 "причина": "роли расходятся"}],
                "непокрытые_пункты": [{"ключ": "2[05][004]"}]}

    monkeypatch.setattr(families_store, "all", _все)
    monkeypatch.setattr(methodology, "сверка_отдела", _сверка_метод)
    monkeypatch.setattr(fr, "сводка", lambda *a, **kw: {"без_способа": 12, "вариантов_на_выбор": 3})
    async def _ncr(*a, **kw):
        return [{"id": "ncr-1", "тип": "формат", "предмет": "audit1c-explain", "что_нарушено": "выход без структуры"},
                {"id": "ncr-2", "тип": "полнота", "предмет": "b", "что_нарушено": "x"},
                {"id": "ncr-3", "тип": "полнота", "предмет": "c", "что_нарушено": "y"},
                {"id": "ncr-4", "тип": "полнота", "предмет": "d", "что_нарушено": "z"},
                {"id": "ncr-5", "тип": "полнота", "предмет": "e", "что_нарушено": "q"},
                {"id": "ncr-6", "тип": "полнота", "предмет": "f", "что_нарушено": "w"},
                {"id": "ncr-7", "тип": "полнота", "предмет": "g", "что_нарушено": "r"},
                {"id": "ncr-8", "тип": "полнота", "предмет": "h", "что_нарушено": "t"},
                {"id": "ncr-9", "тип": "полнота", "предмет": "i", "что_нарушено": "u"},
                {"id": "ncr-10", "тип": "полнота", "предмет": "j", "что_нарушено": "o"},
                {"id": "ncr-11", "тип": "полнота", "предмет": "k", "что_нарушено": "p"}]
    monkeypatch.setattr(ncr_store, "список", _ncr)

    async def _list_for(audit_id=None, limit=100, archived=False):
        return [{"id": "a1.v1", "status": "deployed", "family": "finance", "role": "svershik"},
                {"id": "a2.v1", "status": "paused", "family": "audit", "role": "x"}]

    async def _get(aid):
        return {"id": aid, "graph": {"nodes": []}}

    async def _проверка(graph, family, role):
        if aid_кур[0] == "a1.v1":
            return {"серьёзность": "warn", "причина": "не хватает навыков"}
        return {"серьёзность": "ok"}

    aid_кур = [""]
    real_get = _get

    async def _get_с_запоминанием(aid):
        aid_кур[0] = aid
        return await real_get(aid)

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(agent_store, "get", _get_с_запоминанием)
    monkeypatch.setattr(assignment_audit, "проверка", _проверка)

    р = asyncio.run(methodology.патруль())
    типы = р["счёт"]
    assert типы.get("методология", 0) >= 2, "расхождение + непокрытый пункт из audit"
    assert типы.get("способ") == 2, "дыра способа + варианты на выбор"
    assert типы.get("качество") == 11, "10 NCR по карточкам + сводная «и ещё 1» (лист 10)"
    assert типы.get("назначение") == 1, "только deployed a1 — paused не тревожим"
    заявки_роль = [з for з in р["заявки"] if з.get("применимое")]
    assert заявки_роль and all(з["тип"] == "методология" for з in заявки_роль)
    assert р["всего"] == len(р["заявки"])
