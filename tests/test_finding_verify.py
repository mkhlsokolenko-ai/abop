# -*- coding: utf-8 -*-
"""Петля доведения (№5 спринта 1): точечная проверка исправления принятой находки.

Путь «нашли → признали → исправили» замыкается кнопкой «Исправлено — проверить»:
вычитка клиента → детерминированная сверка (правила считает код, без LLM) →
воспроизводится ли ИМЕННО эта находка (совпадение код+предмет). Не воспроизводится →
состояние «исправлено» и доведение отдела растёт; воспроизводится → состояние остаётся,
факт проверки — в журнале решения.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}


def test_исправлено_ставится_только_на_подтверждённую(monkeypatch):
    """Гвард состояния: «исправлено» на нерешённой находке — ошибка, не зачёт."""
    from server import findings_registry as fr
    monkeypatch.setattr(fr, "_строки", lambda *a, **kw: [
        {"id": "f0", "код": "E1", "предмет": "x", "состояние": "найдено"}])
    with pytest.raises(ValueError):
        fr.решение("f0", состояние="исправлено", кем="т")


def test_проверка_исправления_по_коду_и_предмету(monkeypatch):
    """Совпадение — по коду И предмету (uuid документа): совпал только код — ещё воспроизводится."""
    from server import findings_registry as fr
    from server.routes import inbox
    находка = {"id": "f1", "код": "B2", "предмет": "uuid-док-1", "состояние": "подтверждено",
               "проект": "Жигулева А. В. ИП", "организация": "Жигулева А. В. ИП"}
    monkeypatch.setattr(fr, "список", lambda *a, **kw: [находка])
    записано = {}

    def _решение(ид, *, состояние, кем, почему="", до=""):
        записано[ид] = (состояние, почему)
        return {"id": ид, "состояние": состояние}

    monkeypatch.setattr(fr, "решение", _решение)
    monkeypatch.setattr(inbox.ape, "клиенты", lambda все=False: [
        {"id": "zhig", "организация": "Жигулева А. В. ИП"}])
    monkeypatch.setattr(inbox.ape, "обновить_данные_клиента", lambda ид, р=(), reset=False: {})
    monkeypatch.setattr(inbox.ape, "audit1c_build_graph", lambda: {"docs": []})
    # сверка нашла ту же пару код+предмет → воспроизводится
    monkeypatch.setattr(inbox.ape, "audit1c_сверка", lambda g, орг="", дата="", предмет="", клиент="":
                        {"находки": [{"правило": "B2", "документ": {"uuid": "uuid-док-1"}}]})

    р = asyncio.run(inbox.findings_verify("f1", АДМИН))
    assert р["вердикт"] == "воспроизводится"
    assert записано["f1"][0] == "подтверждено"          # состояние осталось, проверка — в журнале

    # теперь тот же код, но ДРУГОЙ документ (предмет другой) → не воспроизводится → исправлено
    monkeypatch.setattr(inbox.ape, "audit1c_сверка", lambda g, орг="", дата="", предмет="", клиент="":
                        {"находки": [{"правило": "B2", "документ": {"uuid": "uuid-док-ДРУГОЙ"}}]})
    р = asyncio.run(inbox.findings_verify("f1", АДМИН))
    assert р["вердикт"] == "исправлено"
    assert записано["f1"][0] == "исправлено"
    assert "не воспроизводится" in записано["f1"][1]


def test_проверка_требует_подтверждённую(monkeypatch):
    from server import findings_registry as fr
    from server.routes import inbox
    from fastapi import HTTPException
    monkeypatch.setattr(fr, "список", lambda *a, **kw: [
        {"id": "f2", "код": "E1", "предмет": "x", "состояние": "найдено", "проект": "Жигулева А. В. ИП"}])
    monkeypatch.setattr(inbox.ape, "клиенты", lambda все=False: [
        {"id": "zhig", "организация": "Жигулева А. В. ИП"}])
    try:
        asyncio.run(inbox.findings_verify("f2", АДМИН))
        raise AssertionError("ожидали 422")
    except HTTPException as e:
        assert e.status_code == 422
