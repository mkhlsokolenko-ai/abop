# -*- coding: utf-8 -*-
"""Дрейф DoD объясняется данными, а не подписью на полоске.

В панели здоровья стояло одно число — «Дрейф DoD +0%» — и подсказка из одной фразы. Почему дрейф,
на каких днях и какие прогоны его уронили, интерфейс не говорил; при этом на стенде без прогонов
клиент подставлял DoD 100 % и рисовал ровный дрейф на пустоте — та же выдумка, что раньше была с
55 % в спарклайне. Разбор считает сервер: клиент его только показывает, второго расчёта нет.
"""
from __future__ import annotations

import datetime as dt
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

from server import run_store, tenancy, web_api  # noqa: E402


@pytest.fixture()
def client():
    run_store._MEM.clear()   # журнал прогонов в памяти общий для модуля — иначе дни складываются
    with TestClient(web_api.app) as c:
        yield c
    run_store._MEM.clear()


def _прогон(client, *, ok: bool, дней_назад: int, агент="audit-1c.v1", рид=""):
    """Прогон задним числом. `run_store.save` ставит время записи сам (и правильно — это факт
    записи), поэтому дату правим в строке журнала: иначе историю восьми дней не собрать."""
    import asyncio
    рид = рид or f"r-{дней_назад}-{int(ok)}"
    asyncio.new_event_loop().run_until_complete(
        run_store.save({"id": рид, "agent_id": агент, "contract_audit_id": "audit-1c",
                        "verdict": {"ok": ok}}))
    когда = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=дней_назад)
    # Журнал прогонов изолирован по арендатору (шаг 5), и фолбэк в памяти ключуется так же —
    # иначе тест на изоляцию зеленел бы в памяти и падал на Postgres.
    run_store._MEM[tenancy.ключ_памяти(рид)]["created_at"] = когда.isoformat()


def _дод(client) -> dict:
    r = client.get("/api/fleet")
    assert r.status_code == 200
    d = r.json()
    assert "dod" in d, "разбор дрейфа не отдаётся — показывать нечего, кроме числа"
    return d["dod"]


# ── честность на пустоте ──────────────────────────────────────────────────────────────────────────

def test_без_прогонов_значения_нет(client):
    """Главное: пустой стенд не выдаёт «DoD 100 %». Нет прогонов — значение не определено."""
    д = _дод(client)
    assert д["now"] is None and д["base"] is None and д["drift"] is None
    assert д["days_with_runs"] == 0 and д["runs"] == 0


def test_клиент_не_подставляет_сотню():
    """Тот же запрет в разметке: раньше здесь стоял литерал 100 на случай пустого списка."""
    текст = (ROOT / "webapp" / "src" / "template.html").read_text(encoding="utf-8")
    блок = текст[текст.index("const _dodSrv"):текст.index("const dodHasData")]
    assert ": 100;" not in блок and "? 100" not in блок, "DoD снова выдумывается на пустых данных"


# ── разбор ────────────────────────────────────────────────────────────────────────────────────────

def test_день_несёт_число_прогонов(client):
    """Пустой день отличается от дня с нулевым DoD только по n — без него клиент гадает."""
    _прогон(client, ok=True, дней_назад=1)
    r = client.get("/api/fleet").json()
    assert all("n" in x and "ok" in x for x in r["series"])
    сегодня = [x for x in r["series"] if x["n"]]
    assert сегодня and сегодня[-1]["dod"] == 100


def test_дрейф_считается_от_предыдущих_дней(client):
    """Норма — среднее по предыдущим дням с прогонами, а не по всем восьми."""
    _прогон(client, ok=True, дней_назад=3, рид="a")
    _прогон(client, ok=False, дней_назад=3, рид="b")   # день −3: 50 %
    _прогон(client, ok=True, дней_назад=0, рид="c")    # сегодня: 100 %
    д = _дод(client)
    assert д["base"] == 50 and д["now"] == 100 and д["drift"] == 50


def test_просадка_показывает_что_её_уронило(client):
    """«Где именно» — это конкретные прогоны с незакрытым вердиктом, с агентом и временем."""
    _прогон(client, ok=True, дней_назад=2, рид="ok1")
    _прогон(client, ok=False, дней_назад=0, рид="bad1")
    д = _дод(client)
    assert д["drift"] is not None and д["drift"] < 0
    assert д["failed_total"] >= 1
    один = д["failed"][0]
    for поле in ("at", "agent", "why", "run_id"):
        assert один.get(поле), f"в разборе нет поля {поле}"


def test_один_день_с_прогонами_не_даёт_дрейфа(client):
    """Сравнивать не с чем — и это надо сказать, а не показать ±0 %."""
    _прогон(client, ok=True, дней_назад=0, рид="solo")
    д = _дод(client)
    assert д["now"] == 100 and д["base"] is None and д["drift"] is None


def test_список_упавших_ограничен(client):
    """Разбор не превращается в выгрузку журнала: показываем часть, называя общее число."""
    for i in range(20):
        _прогон(client, ok=False, дней_назад=i % 7, рид=f"f{i}")
    д = _дод(client)
    assert len(д["failed"]) <= 14 and д["failed_total"] >= 20
