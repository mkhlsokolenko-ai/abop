# -*- coding: utf-8 -*-
"""Живой ход прогона по SSE (п.14 исследования 08.10) — последняя заглушка API.

`/api/runs/{id}/stream` был единственным честным 501 во всём API: контракт событий зафиксирован,
реализации нет, клиенты поллят задание. Из-за этого же оставались открытыми «прогнать один шаг» и
«что сейчас делает навык» из разбора 05.10 — показать ход прогона было нечем.

Главное требование к реализации: событий ровно столько, сколько рантайм действительно наблюдает.
Обещать `handoff`, которого никто не порождает, — тот же класс ошибки, что демо-данные в
интерфейсе, только в контракте API.
"""
from __future__ import annotations

import json
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

from server import run_queue, run_store, web_api  # noqa: E402

ИСХОДНИК = (КОРЕНЬ / "server" / "web_api.py").read_text(encoding="utf-8")


@pytest.fixture()
def client():
    run_queue._MEM.clear()
    run_store._MEM.clear()
    with TestClient(web_api.app) as c:
        yield c
    run_queue._MEM.clear()
    run_store._MEM.clear()


def _события(ответ) -> list[dict]:
    out = []
    for строка in ответ.text.splitlines():
        if строка.startswith("data: "):
            out.append(json.loads(строка[6:]))
    return out


# ── заглушки не осталось ─────────────────────────────────────────────────────────────────────────

def test_заглушек_в_api_больше_нет():
    """Был ровно один 501 и осиротевший текст рядом с ним."""
    assert "status_code=501" not in ИСХОДНИК, "в API снова есть заглушка"
    assert "_NOT_IMPL =" not in ИСХОДНИК, "осиротевшая константа вернулась"


def test_шапка_модуля_не_обещает_501():
    шапка = ИСХОДНИК[:ИСХОДНИК.index('from __future__')]
    assert "501" not in шапка, "описание модуля снова разошлось с реализацией"


# ── поведение ────────────────────────────────────────────────────────────────────────────────────

def test_неизвестный_прогон_это_404(client):
    r = client.get("/api/runs/нет-такого/stream")
    assert r.status_code == 404
    assert "не найдено" in r.json()["detail"]


def _задание(**поля):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        run_queue.enqueue(agent_id="a.v1", actor="dev", payload={}, **поля))


def test_стрим_здоровается_и_закрывается_на_финале(client):
    """Поток обязан закончиться: клиент не должен висеть на завершённом прогоне."""
    j = _задание()
    run_queue._MEM[j["id"]].update({"status": "done", "run_id": "run-1"})
    r = client.get("/api/runs/" + j["id"] + "/stream")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    ев = _события(r)
    assert ев[0]["type"] == "hello", ев[:2]
    assert ев[-1]["type"] == "run_done", ев[-2:]
    assert ев[-1]["status"] == "done"


def test_финал_несёт_вердикт_и_стоимость(client):
    import asyncio
    j = _задание()
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        run_store.save({"id": "run-2", "agent_id": "a.v1", "verdict": {"ok": True, "autonomy_used": "A1"},
                        "run_metrics": {"cost": {"rub": 1.5}}}))
    run_queue._MEM[j["id"]].update({"status": "done", "run_id": "run-2"})
    финал = [e for e in _события(client.get("/api/runs/run-2/stream")) if e["type"] == "run_done"][0]
    assert финал["verdict_ok"] is True
    assert финал["autonomy_used"] == "A1"
    assert финал["cost"]["rub"] == 1.5


def test_стрим_находится_по_идентификатору_прогона(client):
    """Клиент знает то job_id, то run_id — ручка принимает оба."""
    j = _задание()
    run_queue._MEM[j["id"]].update({"status": "failed", "run_id": "run-3", "error": "боом"})
    ев = _события(client.get("/api/runs/run-3/stream"))
    assert ев[0]["job_id"] == j["id"]
    assert ев[-1]["type"] == "run_done" and ев[-1]["error"] == "боом"


def test_шаги_навыков_и_фаза_приходят_событиями(client):
    """Это и есть ответ на «что сейчас делает агент» — его не было ничем показать."""
    j = _задание()
    run_queue._MEM[j["id"]].update({
        "status": "done", "run_id": "run-4",
        "checkpoint": {"run": {"phase": "навыки", "total": 2,
                               "skills": {"audit1c-explain": {"state": "done", "ms": 1200}}}}})
    ев = _события(client.get("/api/runs/" + j["id"] + "/stream"))
    типы = [e["type"] for e in ев]
    assert "wave_start" in типы, типы
    assert "agent_step" in типы, типы
    шаг = [e for e in ев if e["type"] == "agent_step"][0]
    assert шаг["skill"] == "audit1c-explain" and шаг["state"] == "done"


def test_событий_не_больше_чем_наблюдает_рантайм():
    """handoff и audit_verdict отдельными событиями не шлём — их никто не порождает."""
    блок = ИСХОДНИК[ИСХОДНИК.index('async def run_stream('):][:6000]
    for выдумка in ('"handoff"', '"audit_verdict"'):
        assert выдумка not in блок, "обещано событие, которого рантайм не порождает: " + выдумка
    for настоящее in ("wave_start", "agent_step", "board_event", "hitl_request", "run_done"):
        assert настоящее in блок, "потеряно событие " + настоящее
