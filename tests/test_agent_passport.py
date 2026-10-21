# -*- coding: utf-8 -*-
"""Карточка агента на экране «Агенты» показывает ЭТОГО агента, а не роспись семьи.

Клик по действующему агенту открывал модалку через `/api/agents/spec?family=…&member=…` — спеку
семьи из code-ростера `AGENT_FAMILIES`. О самом агенте спека не знает ничего, и отсюда два дефекта:

* семья, которой в ростере нет (кастомная, завели из UI) → ValueError → 404 → модалка пустая:
  ни навыков, ни зависимостей, ни привязок;
* роль, которой в ростере нет (агент собран из чата) → спека молча брала ПЕРВУЮ роль семьи и
  показывала её навыки. Это хуже пустоты: карточка уверенно показывала чужой набор как его.

Паспорт (`/api/agents/{id}/passport`) читает сохранённую версию агента: навыки из его графа,
зависимости по данным (сущности с наполненностью), привязки (расписания, посадки, цепочки) и
оценку (вердикт верификации, журнал прогонов, прогноз стоимости).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

from server import agent_store, pipeline_store, run_store, web_api  # noqa: E402

ГРАФ = {"nodes": [{"id": "n1", "kind": "skill", "skill": "plan-portfolio", "autonomy": "A1"},
                  {"id": "n2", "kind": "skill", "skill": "status-report", "autonomy": "A1"},
                  {"id": "trg-1", "kind": "trigger", "title": "Расписание 09:00",
                   "trig": {"type": "schedule", "cron": "09:00", "enabled": True,
                            "source": "chat.recurring", "deliver": "chat"}}],
        "edges": [{"from": "n1", "to": "n2"}]}


def _сохранить(*, audit_id: str, family: str, role: str, name: str, graph=None) -> dict:
    return asyncio.new_event_loop().run_until_complete(agent_store.save(
        name=name, audit_id=audit_id, version=1, graph=graph or ГРАФ, autonomy_max="A2",
        family=family, role=role, source="authored", created_by="владелец",
        verification={"verified": True, "mode": "auto-envelope"}))


@pytest.fixture()
def client():
    agent_store._MEM.clear()
    run_store._MEM.clear()
    pipeline_store._MEM.clear()
    with TestClient(web_api.app) as c:
        yield c
    agent_store._MEM.clear()
    run_store._MEM.clear()
    pipeline_store._MEM.clear()


# ── почему модалка была пустой ────────────────────────────────────────────────────────────────────

def test_спека_семьи_не_знает_про_кастомную_семью(client):
    """Воспроизведение дефекта: на кастомной семье спека отдаёт 404 — клиенту нечего показать."""
    r = client.get("/api/agents/spec", params={"family": "sales", "member": "portfolio-keeper"})
    assert r.status_code == 404, "если бы спека отвечала, пустой модалки бы не было"


def test_спека_подставляет_навыки_чужой_роли(client):
    """Роли «собран-из-чата» в ростере нет → спека берёт первую роль семьи. Это и видел владелец."""
    r = client.get("/api/agents/spec", params={"family": "management", "member": "собран-из-чата"})
    assert r.status_code == 200
    навыки = {s["id"] for s in r.json()["skills"]}
    assert "plan-portfolio" not in навыки, "спека не умеет отдавать навыки конкретного агента"


# ── паспорт: тот же агент, но настоящий ───────────────────────────────────────────────────────────

def test_паспорт_кастомной_семьи_отдаёт_навыки_из_графа(client):
    a = _сохранить(audit_id="pf-sales", family="sales", role="собран-из-чата", name="Свод портфеля")
    r = client.get(f"/api/agents/{a['id']}/passport")
    assert r.status_code == 200, "паспорт не зависит от ростера семьи"
    d = r.json()
    assert [s["id"] for s in d["skills"]] == ["plan-portfolio", "status-report"]
    assert d["name"] == "Свод портфеля" and d["version"] == 1
    assert d["envelope"]["почему"], "потолок автономии объяснён словами, а не только буквой A*"


def test_паспорт_показывает_зависимости_и_нехватку_входов(client):
    a = _сохранить(audit_id="pf-dep", family="management", role="portfolio-keeper", name="Портфель")
    d = client.get(f"/api/agents/{a['id']}/passport").json()
    deps = d["depends"]
    assert "data" in deps and "gaps" in deps
    for строка in deps["data"]:
        assert "records" in строка and "recipe_status" in строка, \
            "зависимость без наполненности — это снова «источник объявлен», а не факт"


def test_паспорт_показывает_привязки(client):
    a = _сохранить(audit_id="pf-bind", family="management", role="portfolio-keeper", name="Портфель")
    asyncio.new_event_loop().run_until_complete(
        pipeline_store.save("ch-1", "Свод по понедельникам",
                            [{"agent_id": a["id"]}, {"agent_id": "other.v1"}], owner="владелец"))
    b = client.get(f"/api/agents/{a['id']}/passport").json()["bindings"]
    assert [t["cron"] for t in b["triggers"]] == ["09:00"], "расписание агента — его привязка"
    assert b["triggers"][0]["enabled"] is True
    assert [c["id"] for c in b["chains"]] == ["ch-1"], "цепочка, где агент шаг, тоже привязка"
    assert b["chains"][0]["at"] == [1]
    assert b["seats"] == [], "посадок на карте нет — так и говорим, без выдумки"


def test_паспорт_оценка_без_прогонов_честно_пустая(client):
    a = _сохранить(audit_id="pf-est", family="management", role="portfolio-keeper", name="Портфель")
    оц = client.get(f"/api/agents/{a['id']}/passport").json()["assessment"]
    assert оц["runs"]["total"] == 0 and оц["runs"]["avg_rub"] is None
    assert оц["verify"] == "ok", "авто-верификация прошла — это и показываем"
    assert оц["estimate"]["partial"] is True, "без истории прогноз обязан быть помечен неполным"
    assert set(оц["estimate"]["unknown"]) == {"plan-portfolio", "status-report"}


def test_паспорт_считает_прогоны_по_всем_версиям(client):
    a = _сохранить(audit_id="pf-runs", family="management", role="portfolio-keeper", name="Портфель")
    asyncio.new_event_loop().run_until_complete(run_store.save(
        {"id": "r-1", "agent_id": "pf-runs.v1", "contract_audit_id": "pf-runs",
         "verdict": {"ok": True}, "run_metrics": {"cost": {"rub": 1.5}}}))
    asyncio.new_event_loop().run_until_complete(run_store.save(
        {"id": "r-2", "agent_id": "pf-runs.v2", "contract_audit_id": "pf-runs",
         "verdict": {"ok": False}, "run_metrics": {"cost": {"rub": 0.5}}}))
    журнал = client.get(f"/api/agents/{a['id']}/passport").json()["assessment"]["runs"]
    assert журнал["total"] == 2 and журнал["ok"] == 1
    assert журнал["cost_rub"] == 2.0 and журнал["avg_rub"] == 1.0


def test_паспорт_чужого_агента_не_отдаётся(client, monkeypatch):
    a = _сохранить(audit_id="pf-abac", family="finance", role="portfolio-keeper", name="Чужой")
    from server.routes import agents as route
    monkeypatch.setattr(route, "can_see_family", lambda u, f: False)
    r = client.get(f"/api/agents/{a['id']}/passport")
    assert r.status_code == 403


def test_паспорт_несуществующего_агента(client):
    assert client.get("/api/agents/нет-такого.v1/passport").status_code == 404
