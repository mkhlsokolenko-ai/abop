# -*- coding: utf-8 -*-
"""Проводка в 1С (№6 спринта 2): HITL-исключение из ADR-072, решённое владельцем 19.11.

Гарантии: проводится ТОЛЬКО подтверждённая находка со способом «Провести документ»,
uuid совпадает с предметом, документ в границе расчёта клиента; уже проведённый —
идемпотентно «уже проведён»; отказ 1С доезжает как есть; каждый вызов в журнале аудита.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}


def _мир(monkeypatch, *, состояние="подтверждено", способ="Провести документ",
         предмет="uuid-док-1", проведён="False", патч_код=204, патч_ошибка=""):
    from server.routes import clients as кр
    from server import findings_registry as fr, dataplane_records as dr
    находка = {"id": "f1", "код": "E1", "предмет": предмет, "состояние": состояние,
               "способ": способ, "проект": "Жигулева А. В. ИП", "отдел": "finance"}
    monkeypatch.setattr(fr, "список", lambda *a, **kw: [находка] if состояние else [])
    monkeypatch.setattr(кр.ape, "карточка_клиента",
                        lambda ид: {"id": "zhig", "организация": "Жигулева А. В. ИП",
                                    "адрес": "https://x", "токен_файл": "/s"})
    док = {"id": предмет, "Номер": "0000-000130", "Дата": "2026-06-23", "тип": "ПоступлениеТоваровУслуг",
           "СуммаДокумента": "5829.89", "Проведён": проведён, "Контрагент": "ООО ТоргДом"}
    патчи = []

    def _fake_dr_init():
        pass

    def _fake_query(entity, фильтр=None, поля=None, лимит=0):
        assert entity == "doc1c"
        return [док] if фильтр and фильтр.get("id") == предмет else []

    def _патч(src, сущность, guid, тело):
        патчи.append((сущность, guid, тело))
        return {"код": патч_код, "ошибка": патч_ошибка} if патч_ошибка else {"код": патч_код}

    monkeypatch.setattr(dr, "init", _fake_dr_init)
    monkeypatch.setattr(dr, "query", _fake_query)
    monkeypatch.setattr(кр.ape, "_odata1c_патч", _патч)
    записано = {}

    async def _запись(кем, событие, ид, детали, severity="info"):
        записано[событие] = dict(детали)
        return "audit-1"

    monkeypatch.setattr(кр.audit_store, "record", _запись)
    return патчи, записано


def test_проводит_подтверждённую_по_способу(monkeypatch):
    from server.routes import clients as кр
    патчи, записано = _мир(monkeypatch)
    р = asyncio.run(кр.post_document("zhig", {"finding_id": "f1", "uuid": "uuid-док-1"}, АДМИН))
    assert р["ok"] and р["результат"] == "проведён"
    assert патчи == [("Document_ПоступлениеТоваровУслуг", "uuid-док-1", {"Posted": True})]
    assert записано["1c.post.document"]["результат"] == "проведён"


def test_уже_проведён_идемпотентно(monkeypatch):
    from server.routes import clients as кр
    патчи, _ = _мир(monkeypatch, проведён="True")
    р = asyncio.run(кр.post_document("zhig", {"finding_id": "f1", "uuid": "uuid-док-1"}, АДМИН))
    assert р["результат"] == "уже проведён" and not патчи


def test_не_проводит_неподтверждённую(monkeypatch):
    from server.routes import clients as кр
    from fastapi import HTTPException
    _мир(monkeypatch, состояние="найдено")
    try:
        asyncio.run(кр.post_document("zhig", {"finding_id": "f1", "uuid": "uuid-док-1"}, АДМИН))
        raise AssertionError("ожидали 422")
    except HTTPException as e:
        assert e.status_code == 422


def test_не_проводит_чужой_способ(monkeypatch):
    from server.routes import clients as кр
    from fastapi import HTTPException
    _мир(monkeypatch, способ="Запросить счёт-фактуру у поставщика")
    try:
        asyncio.run(кр.post_document("zhig", {"finding_id": "f1", "uuid": "uuid-док-1"}, АДМИН))
        raise AssertionError("ожидали 422")
    except HTTPException as e:
        assert e.status_code == 422


def test_отказ_1с_доезжает(monkeypatch):
    from server.routes import clients as кр
    патчи, записано = _мир(monkeypatch, патч_код=403, патч_ошибка="Права записи нет")
    р = asyncio.run(кр.post_document("zhig", {"finding_id": "f1", "uuid": "uuid-док-1"}, АДМИН))
    assert not р["ok"] and р["результат"] == "отказ 1С" and "Права записи" in р["ошибка"]
    assert записано["1c.post.document"]["результат"].startswith("отказ 1С")
