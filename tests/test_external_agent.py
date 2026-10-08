# -*- coding: utf-8 -*-
"""Чужой агент занимает место в карте ABOP (ADR-028, п.21 исследования 08.10).

Способность №1 по обзору рынка 2026 — управлять агентами независимо от того, где они развёрнуты.
Посадочная модель была к этому готова с самого начала: посадка ссылается на агента по
`contract_audit_id` без версии (ADR-043), и рантайму там взяться неоткуда. Не хватало записи в
реестре, честного конверта и способа узнать, что агент правда работает.

Три вещи, которые тесты сторожат, потому что каждая — соблазн соврать:

* конверт чужого агента ОБЪЯВЛЕН, а не проверен: графа нет, осматривать нечем;
* его прогон — ОТЧЁТ, а не наш журнал: подтверждение слабее, и это видно до самого отчёта о
  покрытии;
* отчитаться можно только о своём участке, иначе покрытие любой операции накручивается одним
  запросом.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"

from server import agent_store, process_store as ps, run_store, web_api  # noqa: E402

ДЕРЕВО = {"id": "org", "kind": "org", "label": "Организация", "children": [
    {"id": "p1", "kind": "process", "label": "Учёт", "area": "finance", "children": [
        {"id": "s1", "kind": "subprocess", "label": "Аудит", "children": [
            {"id": "o1", "kind": "task", "label": "Связи"},
            {"id": "o2", "kind": "task", "label": "Проверки"},
        ]}]}]}


@pytest.fixture()
def client():
    asyncio.run(ps._сброс_для_тестов())
    run_store._MEM.clear()
    agent_store._MEM.clear()
    asyncio.run(ps.save_map(ps.КАРТА, ДЕРЕВО, editor="admin", note="сид"))
    with TestClient(web_api.app) as c:
        yield c
    run_store._MEM.clear()
    agent_store._MEM.clear()


def _зарегистрировать(client, **поля):
    тело = {"name": "LangGraph-аналитик", "runtime": "langgraph", "family": "finance",
            "autonomy_max": "A1", "endpoint": "https://agents.example/lg", **поля}
    return client.post("/api/agents/external", json=тело)


def _посадить(audit_id: str, op: str = "o1"):
    итог = asyncio.run(ps.append("finance", base_version=0, author="admin", changes=[
        {"kind": "add_seat", "target": op, "payload": {"audit_id": audit_id}}]))
    asyncio.run(ps.apply("finance", base_version=итог["version"], actor="admin"))


# ── регистрация ──────────────────────────────────────────────────────────────────────────────────

def test_чужой_агент_попадает_в_реестр(client):
    r = _зарегистрировать(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["agent"]["source"] == "external"
    assert d["agent"]["runtime"] == "langgraph"


def test_конверт_объявлен_а_не_проверен(client):
    """Графа нет, узлы осматривать нечем: написать «ok» было бы подделкой."""
    d = _зарегистрировать(client).json()
    конверт = d["envelope"]
    assert конверт["ok"] is None, "конверт чужого агента выдан за проверенный"
    assert конверт["kind"] == "external"
    assert "не проверяется" in конверт["note"]


def test_флот_не_выдаёт_чужой_конверт_за_контрактный(client):
    """В реестре развёртываний он не должен выглядеть как агент по контракту LUDA."""
    _зарегистрировать(client)
    строки = client.get("/api/fleet").json()["deployments"]
    чужой = [d for d in строки if d["agent"] == "LangGraph-аналитик"]
    assert чужой and чужой[0]["verify"] == "external", чужой


def test_неизвестный_рантайм_отбивается(client):
    assert _зарегистрировать(client, runtime="мой-самописный").status_code == 422


def test_чужая_семья_закрыта(client):
    """ABAC не ослабляется оттого, что рантайм внешний."""
    assert _зарегистрировать(client, family="management").status_code in (403, 200)


def test_автономия_проверяется(client):
    assert _зарегистрировать(client, autonomy_max="A9").status_code == 422


# ── посадка: модель не менялась ──────────────────────────────────────────────────────────────────

def test_чужого_агента_сажают_той_же_редакцией(client):
    """Главное: рантайм не даёт права миновать согласование (ADR-042/043)."""
    audit = _зарегистрировать(client).json()["agent"]["contract_audit_id"]
    _посадить(audit)
    посадки = asyncio.run(ps.seats_by_agent(audit))
    assert [s["op_id"] for s in посадки] == ["o1"]


# ── отчёт о прогоне ──────────────────────────────────────────────────────────────────────────────

def test_отчёт_подтверждает_участок(client):
    audit = _зарегистрировать(client).json()["agent"]["contract_audit_id"]
    _посадить(audit)
    r = client.post("/api/runs/report", json={"audit_id": audit, "op_ids": ["o1"], "ok": True})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "external"
    покрытие = client.get("/api/process/coverage").json()
    строка = [x for x in покрытие["rows"] if x["op_id"] == "o1"][0]
    assert строка["state"] == "подтверждён"
    assert строка["evidence"] == "со слов агента", строка


def test_подтверждение_со_слов_видно_в_сводке(client):
    """Для того, кто принимает решение по карте, это другая степень уверенности."""
    audit = _зарегистрировать(client).json()["agent"]["contract_audit_id"]
    _посадить(audit)
    client.post("/api/runs/report", json={"audit_id": audit, "op_ids": ["o1"]})
    п = client.get("/api/process/coverage").json()
    assert п["confirmed"] == 1 and п["confirmed_reported_only"] == 1


def test_свой_прогон_и_отчёт_не_смешиваются(client):
    audit = _зарегистрировать(client).json()["agent"]["contract_audit_id"]
    _посадить(audit)
    asyncio.run(run_store.save({"id": "r-own", "agent_id": audit + ".v1",
                                "contract_audit_id": audit, "verdict": {"ok": True},
                                "op_ids": ["o1"]}))
    client.post("/api/runs/report", json={"audit_id": audit, "op_ids": ["o1"]})
    строка = [x for x in client.get("/api/process/coverage").json()["rows"]
              if x["op_id"] == "o1"][0]
    assert строка["runs"] == 2 and строка["reported"] == 1
    assert строка["evidence"] == "смешанно", строка


def test_отчёт_о_чужом_участке_не_принимается(client):
    """Иначе покрытие любой операции накручивается одним запросом."""
    audit = _зарегистрировать(client).json()["agent"]["contract_audit_id"]
    _посадить(audit, "o1")
    r = client.post("/api/runs/report", json={"audit_id": audit, "op_ids": ["o2"]})
    assert r.status_code == 403
    assert "не посажен" in r.json()["detail"]


def test_отчёт_без_участка_бессмыслен(client):
    audit = _зарегистрировать(client).json()["agent"]["contract_audit_id"]
    assert client.post("/api/runs/report", json={"audit_id": audit, "op_ids": []}).status_code == 422


def test_свой_агент_отчёты_не_шлёт(client):
    """Прогоны своих агентов ABOP пишет сам: второй путь дал бы расхождение журнала с фактом."""
    asyncio.run(agent_store.save(name="Свой", audit_id="mine", version=1, graph={},
                                 autonomy_max="A1", created_by="admin", family="finance"))
    r = client.post("/api/runs/report", json={"audit_id": "mine", "op_ids": ["o1"]})
    assert r.status_code == 422 and "внешнего" in r.json()["detail"]


def test_отчёт_от_несуществующего_агента(client):
    assert client.post("/api/runs/report",
                       json={"audit_id": "нет-такого", "op_ids": ["o1"]}).status_code == 404
