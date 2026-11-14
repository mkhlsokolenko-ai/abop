# -*- coding: utf-8 -*-
"""Шаг 4 «Отделов агентов»: слои доски working/validated/cross/user.

Главные гарантии: чужой слой — не запрет, а опечатка (молча положить в validated нельзя);
переиспользование берёт ТОЛЬКО признанное (validated/cross) и свежее — черновик чужого отдела
и протухший факт не истина; ключ переиспользования — точный (организация+период+правило+версия),
эмбеддинги не участвуют; продвигать может человек manager+, чужой отдел — 403.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}
АНАЛИТ = {"sub": "u2", "name": "бухгалтер", "roles": ["analyst"], "level": "analyst", "department": "finance"}


def test_put_по_умолчанию_working_и_чужой_слой_отказ(monkeypatch):
    from server import blackboard as bb
    monkeypatch.setattr(bb, "_has_pg", lambda: False)

    async def flow():
        bb._MEM.clear()
        await bb.put_fact("fam-finance", "вывод", "сверка сошлась", author="агент")
        await bb.put_fact("fam-finance", "акт", "акт №5", author="агент", слой="validated")
        try:
            await bb.put_fact("fam-finance", "кривой", "x", author="агент", слой="god-mode")
            raise AssertionError("чужой слой должен был отклониться")
        except ValueError:
            pass
        всё = await bb.load("fam-finance")
        рабочие = [r for r in всё if r["слой"] == "working"]
        признанные = [r for r in всё if r["слой"] == "validated"]
        assert len(рабочие) == 1 and len(признанные) == 1

    asyncio.run(flow())


def test_продвижение_меняет_слой_а_не_автора(monkeypatch):
    from server import blackboard as bb
    monkeypatch.setattr(bb, "_has_pg", lambda: False)

    async def flow():
        bb._MEM.clear()
        await bb.put_fact("fam-finance", "сверка", "сошлась", author="агент-сверщик")
        row = await bb.опубликовать("fam-finance", "сверка", "validated", кем="главбух")
        assert row["слой"] == "validated"
        assert row["author"] == "агент-сверщик", "продвижение не переписывает авторство работы"
        rows = await bb.load("fam-finance", слой="validated")
        assert [r["key"] for r in rows] == ["сверка"]
        assert not await bb.load("fam-finance", слой="working"), "из working запись ушла"

    asyncio.run(flow())


def test_переиспользование_только_признанное_и_свежее(monkeypatch):
    from server import blackboard as bb
    monkeypatch.setattr(bb, "_has_pg", lambda: False)

    async def flow():
        bb._MEM.clear()
        ключ = bb.ключ_расчёта("МАЙ ДЕНТАЛ", "2026-10", "audit1c-base", "2026-09-01")
        # черновик — НЕ истина
        await bb.put_fact("fam-audit", ключ, {"итог": "черновик"}, author="агент", слой="working")
        assert not await bb.переиспользование(ключ, ["audit"])
        # признанный — истина
        await bb.put_fact("fam-audit", ключ, {"итог": "сошлась, 3 расхождения"}, author="агент",
                          слой="validated")
        наш = await bb.переиспользование(ключ, ["audit"])
        assert наш and наш["value"]["итог"] == "сошлась, 3 расхождения"
        assert наш["слой"] == "validated" and наш["семья"] == "audit" and наш["run_id"] == ""
        # другой ключ — другой расчёт: «похожее» не подставляется
        другой = bb.ключ_расчёта("МАЙ ДЕНТАЛ", "2026-11", "audit1c-base", "2026-09-01")
        assert not await bb.переиспользование(другой, ["audit"])
        assert другой != ключ

    asyncio.run(flow())


def test_переиспользование_не_отдаёт_протухшее(monkeypatch):
    from server import blackboard as bb
    monkeypatch.setattr(bb, "_has_pg", lambda: False)

    async def flow():
        bb._MEM.clear()
        ключ = bb.ключ_расчёта("Жигулёва", "2026-Q3", "oddc", "v1")
        await bb.put_fact("fam-finance", ключ, "старый расчёт", author="агент", слой="cross",
                          ttl_sec=1)
        # искусственно состарим
        bb._MEM["fam-finance"][0]["at"] = "2026-01-01T00:00:00"
        assert not await bb.переиспользование(ключ, ["finance"]), "протухшее не истина"

    asyncio.run(flow())


def test_маршрут_публикации_только_manager(monkeypatch):
    from server import blackboard as bb
    monkeypatch.setattr(bb, "_has_pg", lambda: False)

    async def flow():
        bb._MEM.clear()
        await bb.put_fact("fam-finance", "акт", "акт №5", author="агент")

    asyncio.run(flow())

    from fastapi import HTTPException
    from server.routes import board as B
    from server import blackboard as bb
    исход_опубл = bb.опубликовать
    звали = []

    async def _опубл(scope, key, слой, *, кем):
        звали.append((scope, key, слой))
        return {"key": key, "слой": слой, "author": "агент"}

    bb.опубликовать = _опубл
    try:
        try:
            asyncio.run(B.board_publish("акт", {"слой": "validated", "kind": "family",
                                                "family": "finance"}, АНАЛИТ))
            raise AssertionError("analyst не должен продвигать")
        except HTTPException as ex:
            assert ex.status_code == 403
        ответ = asyncio.run(B.board_publish("акт", {"слой": "validated", "kind": "family",
                                                    "family": "finance"}, АДМИН))
        assert ответ["fact"]["слой"] == "validated" and звали[-1][2] == "validated"
    finally:
        bb.опубликовать = исход_опубл


def test_переиспользование_чужой_отдел_403(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from server.routes import board as B
    from server import blackboard as bb
    monkeypatch.setattr(bb, "_has_pg", lambda: False)

    async def _никогда(*a, **kw):
        raise AssertionError("чужую доску не должны были даже читать")

    monkeypatch.setattr(bb, "переиспользование", _никогда)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(B.board_reuse(организация="X", период="2026-10", правило="audit1c-base",
                                  family="audit", u=АНАЛИТ))   # аналитик отдела finance
    assert ex.value.status_code == 403
