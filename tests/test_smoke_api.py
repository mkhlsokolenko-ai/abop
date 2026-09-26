# -*- coding: utf-8 -*-
"""Smoke-тесты API ABOP для CI (Блок 5): поднимаем FastAPI-приложение in-process без Postgres/Keycloak
(все сторы падают в память, пользователь — dev-admin) и проверяем, что ключевые ручки живы и
отвечают ожидаемой формой. Не проверяет LLM-прогоны (нет модели в CI)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "KEYCLOAK_JWKS_INTERNAL", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ.setdefault("ABOP_RUN_WORKERS", "0")
os.environ["ABOP_DEV_AUTH"] = "1"   # без Keycloak API закрыт (fail-closed); тесты — dev-admin явно


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from server import web_api
    with TestClient(web_api.app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200


def test_index_is_bundle(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert '__bundler/template' in body and '__bundler/manifest' in body
    assert 'data-theme' in body and '__abopA11y' in body       # тема + a11y-слой (Блок 2)


def test_me_dev_admin(client):
    r = client.get("/api/me")
    assert r.status_code == 200
    assert (r.json().get("user") or {}).get("level") == "admin"


def test_agents_and_families(client):
    assert client.get("/api/agents").status_code == 200
    assert "agents" in client.get("/api/agents").json()
    assert client.get("/api/families").status_code == 200


def test_fleet_shape(client):
    r = client.get("/api/fleet")
    assert r.status_code == 200
    d = r.json()
    for k in ("deployments", "live", "alerts", "series", "queue"):
        assert k in d
    assert len(d["series"]) == 8


def test_queue_and_jobs(client):
    assert client.get("/api/runs/queue").status_code == 200
    assert "jobs" in client.get("/api/runs/jobs").json()


def test_findings_journal_and_metrics(client):
    r = client.get("/api/findings")
    assert r.status_code == 200
    d = r.json()
    assert "items" in d and "metrics" in d
    assert d["metrics"]["thresholds"] == {"precision": 80, "cross_share": 50, "manual_miss": 3}


def test_norms_served(client):
    r = client.get("/api/audit1c/norms/01_nds_scheta_faktury.md")
    assert r.status_code == 200 and "Чем грозит" in r.text
    assert client.get("/api/audit1c/norms/../secret.md").status_code in (404, 422)


def test_findings_normalizer_on_fixture():
    from server import findings
    run = {"run_id": "r1", "agent_id": "a", "created_at": "2026-09-26T00:00:00",
           "findings": [{"id": "B1-РТ-0002", "класс": "B", "проверка": "Реализация без счёта-фактуры выданного", "серьёзность": "высокая",
                         "документ": {"uuid": "1-2", "тип": "РеализацияТоваровУслуг", "Номер": "РТ-0002", "Дата": "2024-02-15", "Контрагент": "ООО Т"},
                         "описание": "НДС не предъявлен", "доказательство": "нет СФ", "сумма": 194000.0}]}
    cards = findings.cards_for_run(run, {})
    assert len(cards) == 1
    c = cards[0]
    assert c["section_from"] == "Реализация" and c["section_to"] == "НДС" and c["cross"]
    assert c["amount_text"].startswith("194 000")
    assert any(l["broken"] for l in c["chain"])
    assert c["explain"]["action"] and c["norm"]["url"].endswith(".md")
    m = findings.pilot_metrics([dict(c, label={"decision": "confirmed", "manual_miss": True})])
    assert m["precision"] == 100 and m["manual_miss"] == 1 and m["cross_share"] == 100


def test_run_queue_memory_roundtrip():
    import asyncio
    from server import run_queue
    async def flow():
        job = await run_queue.enqueue(agent_id="a1", actor="dev", payload={"context": "x"}, kind="run")
        assert job["status"] == "queued"
        got = await run_queue.get(job["id"])
        assert got and got["id"] == job["id"]
        assert await run_queue.cancel(job["id"], "dev") is not None
    asyncio.run(flow())
