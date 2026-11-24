# -*- coding: utf-8 -*-
"""Сброс суточного лимита расходов отдела: заданный лимит можно убрать, а не только менять число.

До 24.11.2026 сброса не было вовсе: стор умел только писать число, и заданный однажды лимит
оставался навсегда — 0 ₽ не «отсутствие лимита», а «превышен при любом расходе». Просьба владельца
после аудита: «давай добавляй сброс».

Цепочка: `PUT /api/departments/{fid}/budget` с `лимит_руб: null` → стор удаляет строку →
пульт снова пишет «лимит не задан» и предлагает «задать», а не «изменить».
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "desktop"))
for _k in ("KEYCLOAK_JWKS_URI", "KEYCLOAK_JWKS_INTERNAL", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(_k, None)
os.environ.setdefault("ABOP_RUN_WORKERS", "0")
os.environ["ABOP_DEV_AUTH"] = "1"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from server import web_api
    with TestClient(web_api.app) as c:
        yield c


def test_стор_задаёт_и_сбрасывает():
    import asyncio
    from server import budget_store

    async def flow():
        assert await budget_store.лимит("test-fam") is None
        await budget_store.задать_лимит("test-fam", 137.5, "tester")
        assert await budget_store.лимит("test-fam") == 137.5
        await budget_store.сбросить_лимит("test-fam")
        assert await budget_store.лимит("test-fam") is None
        await budget_store.сбросить_лимит("test-fam")            # повторный сброс — не ошибка
    asyncio.run(flow())


def test_маршрут_null_сбрасывает_число_задаёт(client):
    r = client.put("/api/departments/finance/budget", json={"лимит_руб": None})
    assert r.status_code == 200 and r.json()["лимит_руб"] is None and r.json()["ok"] is True
    assert client.get("/api/departments/finance/budget").json()["лимит_руб"] is None

    r = client.put("/api/departments/finance/budget", json={"лимит_руб": 150})
    assert r.status_code == 200 and r.json()["лимит_руб"] == 150.0
    assert client.get("/api/departments/finance/budget").json()["лимит_руб"] == 150.0

    r = client.put("/api/departments/finance/budget", json={"лимит_руб": None})
    assert r.status_code == 200 and r.json()["лимит_руб"] is None
    assert client.get("/api/departments/finance/budget").json()["лимит_руб"] is None   # вернули как было


def test_маршрут_отличает_мусор_от_сброса(client):
    assert client.put("/api/departments/finance/budget", json={}).status_code == 400
    assert client.put("/api/departments/finance/budget", json={"лимит_руб": "много"}).status_code == 400
    assert client.put("/api/departments/finance/budget", json={"лимит_руб": -5}).status_code == 400
    # сброс после мусора по-прежнему работает и состояние не тронуто
    assert client.put("/api/departments/finance/budget", json={"лимит_руб": None}).json()["лимит_руб"] is None


def test_сайдкар_пропускает_null_на_сброс(monkeypatch):
    from sidecar.modules.agents import module as agents

    seen = {}

    def fake_set(fid, руб):
        seen.update(fid=fid, руб=руб)
        return {"ok": True, "лимит_руб": руб}

    def fake_reset(fid):
        seen.update(fid=fid, сброс=True)
        return {"ok": True, "лимит_руб": None}

    monkeypatch.setattr(agents.abop, "department_budget_set", fake_set)
    monkeypatch.setattr(agents.abop, "department_budget_reset", fake_reset)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    c = TestClient(FastAPI())
    # маршрут с префиксом монтируется приложением; для теста берём router напрямую
    c.app.include_router(agents.router, prefix="/api/modules/agents")

    assert c.put("/api/modules/agents/departments/finance/budget", json={"лимит_руб": 100}).json()["лимит_руб"] == 100
    assert seen["руб"] == 100.0
    assert c.put("/api/modules/agents/departments/finance/budget", json={"лимит_руб": None}).json()["лимит_руб"] is None
    assert seen.get("сброс") is True and seen["fid"] == "finance"
    assert c.put("/api/modules/agents/departments/finance/budget", json={}).json()["status"] == 400


def test_клиент_несёт_null_в_теле(monkeypatch):
    from sidecar import abop_client

    seen = {}

    def fake(method, path, body=None, timeout=120):
        seen.clear()
        seen.update(method=method, path=path, body=body)
        return {"ok": True, "лимит_руб": None}

    monkeypatch.setattr(abop_client, "_req", fake)
    assert abop_client.department_budget_reset("finance") == {"ok": True, "лимит_руб": None}
    assert seen == {"method": "PUT", "path": "/api/departments/finance/budget", "body": {"лимит_руб": None}}


def test_пульт_предлагает_сброс():
    """Диалог на пульте: кнопка сброса есть только при заданном лимите, шлёт null, живая строка
    снова зовёт кнопку «задать», а не «изменить»."""
    panel = io.open(КОРЕНЬ / "desktop" / "ui" / "modules" / "agents" / "panel.js", encoding="utf-8").read()
    assert 'id="limReset"' in panel and "${тек ?" in panel, "кнопки сброса в диалоге нет"
    assert '{ лимит_руб: null }' in panel, "сброс не шлёт null"
    assert '${лим != null ? "изменить" : "задать"}' in panel, "кнопка не различает задан/не задан"
    assert 'установите 1.0.50' in panel, "на старом сайдкаре сброс не объясняет, чего не хватает"
