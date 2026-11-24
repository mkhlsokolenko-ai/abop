# -*- coding: utf-8 -*-
"""Окно «Заявки методолога» на пульте: интерфейс → сайдкар → сервер, по маршруту на каждом шаге.

До 24.11.2026 интерфейс звал `/api/methodology/*` прямо в сайдкар, а у сайдкара таких маршрутов не
было: «Применить» и пересчёт отвечали 404 с момента появления окна (20.11). Маршруты сервера были,
но стык «интерфейс → сайдкар» не сверял никто (docs/AUDIT_DESKTOP_2026-11-23.md, п. 2.1).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ / "desktop"))          # сайдкар импортируется как пакет `sidecar`
ПУЛЬТ = io.open(КОРЕНЬ / "desktop" / "ui" / "modules" / "agents" / "panel.js", encoding="utf-8").read()


def test_пульт_зовёт_маршруты_сайдкара_а_не_сервера_напрямую():
    assert '"/api/modules/agents/methodology/patrol"' in ПУЛЬТ, "пересчёт снова идёт мимо сайдкара"
    assert '"/api/modules/agents/methodology/" + encodeURIComponent(b.dataset.otdel) + "/apply"' in ПУЛЬТ, \
        "«Применить» снова идёт мимо сайдкара"
    assert '"/api/methodology/' not in ПУЛЬТ, "в сайдкаре маршрутов /api/methodology/* нет — это 404"


def test_пересчёт_достижим_кнопкой_в_окне():
    """Двойной клик по чипу пересчёта не доставал: первый клик открывает окно, второй попадает в его
    фон. Пересчёт — кнопкой в самом окне (найдено 24.11 при проверке п. 2.1 аудита вживую)."""
    assert 'id="patrolRecalc"' in ПУЛЬТ, "в окне заявок нет кнопки пересчёта"
    assert "rb.onclick = () => { ov.close(false); пересчитатьМетодолога(); };" in ПУЛЬТ, "кнопка пересчёта не подключена"


def test_клиент_несёт_расхождение_в_теле(monkeypatch):
    from sidecar import abop_client

    seen = {}

    def fake(method, path, body=None, timeout=120):
        seen.clear()
        seen.update(method=method, path=path, body=body)
        return {"ok": True}

    monkeypatch.setattr(abop_client, "_req", fake)
    assert abop_client.methodology_apply("finance", {"тип": "методология"}) == {"ok": True}
    assert seen == {"method": "POST", "path": "/api/methodology/finance/apply",
                    "body": {"расхождение": {"тип": "методология"}}}
    abop_client.methodology_patrol()
    assert seen["method"] == "GET" and seen["path"] == "/api/methodology/patrol"


def test_маршруты_отдают_ответ_сервера_и_не_пропускают_мусор(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sidecar.modules.agents import module as agents

    app = FastAPI()
    app.include_router(agents.router, prefix="/api/modules/agents")
    calls = []

    def fake_patrol():
        calls.append("patrol")
        return {"всего": 3, "заявки": []}

    def fake_apply(fid, diff):
        calls.append(("apply", fid, diff))
        return {"ok": True, "применено": ["роль_в_отделе"]}

    monkeypatch.setattr(agents.abop, "methodology_patrol", fake_patrol)
    monkeypatch.setattr(agents.abop, "methodology_apply", fake_apply)
    c = TestClient(app)
    assert c.get("/api/modules/agents/methodology/patrol").json()["всего"] == 3
    r = c.post("/api/modules/agents/methodology/finance/apply", json={"расхождение": {"тип": "методология"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls[-1] == ("apply", "finance", {"тип": "методология"})
    # расхождение пришло не словарём — на сервер не уходит ни строки от него
    c.post("/api/modules/agents/methodology/finance/apply", json={"расхождение": "oops"})
    assert calls[-1] == ("apply", "finance", {})


def test_отказ_сервера_возвращается_кодом_и_причиной(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sidecar.modules.agents import module as agents

    app = FastAPI()
    app.include_router(agents.router, prefix="/api/modules/agents")

    def refuse(fid, diff):
        raise agents.abop.AbopError(403, "отдел другого отдела")

    monkeypatch.setattr(agents.abop, "methodology_apply", refuse)
    r = TestClient(app).post("/api/modules/agents/methodology/x/apply", json={"расхождение": {"тип": "методология"}})
    # Не 500 «Internal Server Error»: у сайдкара должен быть `_err` — 20–23.11 его тут не было бы так же,
    # как в «Решениях» (см. tests/test_undefined_names_guard.py).
    assert r.status_code == 403 and r.json() == {"ok": False, "error": "отдел другого отдела", "status": 403}
