# -*- coding: utf-8 -*-
"""Применение редакции разворачивает посаженных агентов — на всех путях, не только через HTTP.

Разрыв, найденный на проде после переноса карты: `scripts/migrate_process_map.py` вызывал
`process_store.apply` напрямую, а хук развёртывания жил внутри web_api. В итоге карта утверждала,
что агент работает участок, а агент стоял черновиком и триггеров не принимал — ровно то расхождение
между картой и жизнью, против которого вводилось подтверждение прогонами (ADR-044).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from server import agent_store, process_store as ps  # noqa: E402
from server.process_deploy import deploy_seated  # noqa: E402

ДЕРЕВО = {"id": "org", "kind": "org", "label": "Организация", "children": [
    {"id": "p2", "kind": "process", "label": "Учёт", "area": "finance", "children": [
        {"id": "s21", "kind": "subprocess", "label": "Аудит", "children": [
            {"id": "o1", "kind": "task", "label": "Связи"},
            {"id": "o2", "kind": "task", "label": "Отчёт"},
        ]}]}]}


async def _агент(audit_id, *, версия=1, статус="draft"):
    row = await agent_store.save(name="Аудитор " + audit_id, audit_id=audit_id, version=версия,
                                 graph={}, autonomy_max="A1", created_by="admin", family="finance")
    if статус != "draft":
        await agent_store.set_status(row["id"], статус)
    return row


@pytest.fixture()
def стенд():
    async def подготовить():
        await agent_store._сброс_для_тестов() if hasattr(agent_store, "_сброс_для_тестов") else None
        await agent_store.init()
        await ps._сброс_для_тестов()
        await ps.save_map(ps.КАРТА, ДЕРЕВО, editor="admin", note="сид")
    asyncio.run(подготовить())
    return "finance"


def _посадить(area, *, op, audit_id):
    итог = asyncio.run(ps.append(area, base_version=0, author="migration", changes=[
        {"kind": "add_seat", "target": op, "payload": {"audit_id": audit_id}}]))
    return asyncio.run(ps.apply(area, base_version=итог["version"], actor="migration"))


def test_посаженный_черновик_разворачивается(стенд):
    """Главное: посадка на участок переводит агента в работу, иначе участок мёртв."""
    агент = asyncio.run(_агент("audit-1c"))
    assert агент["status"] == "draft"
    готово = _посадить(стенд, op="o1", audit_id="audit-1c")
    развёрнуты = asyncio.run(deploy_seated(готово["seats"], actor="migration"))
    assert развёрнуты == [агент["id"]]
    assert asyncio.run(agent_store.get(агент["id"]))["status"] == "deployed"


def test_приостановленного_не_будим(стенд):
    """Пауза — решение человека; применение чужой редакции её не отменяет."""
    агент = asyncio.run(_агент("paused-1", статус="paused"))
    готово = _посадить(стенд, op="o1", audit_id="paused-1")
    assert asyncio.run(deploy_seated(готово["seats"], actor="migration")) == []
    assert asyncio.run(agent_store.get(агент["id"]))["status"] == "paused"


def test_разворачивается_последняя_версия(стенд):
    """Посадка ссылается на агента без версии (ADR-043) — в работу идёт свежая."""
    asyncio.run(_агент("multi", версия=1, статус="retired"))
    свежая = asyncio.run(_агент("multi", версия=2))
    готово = _посадить(стенд, op="o1", audit_id="multi")
    assert asyncio.run(deploy_seated(готово["seats"], actor="migration")) == [свежая["id"]]


def test_неизвестный_агент_не_ломает_перенос(стенд):
    """Назначение на агента, которого нет в реестре, — обычный случай старого стенда."""
    assert asyncio.run(deploy_seated([{"op_id": "o1", "audit_id": "его-нет"}], actor="migration")) == []


def test_миграция_зовёт_хук():
    """Сам разрыв: скрипт переноса обязан пройти через тот же хук, что и HTTP-применение."""
    текст = (ROOT / "scripts" / "migrate_process_map.py").read_text(encoding="utf-8")
    assert "deploy_seated" in текст, "перенос снова применяет редакцию мимо развёртывания"
    веб = (ROOT / "server" / "web_api.py").read_text(encoding="utf-8")
    assert "process_deploy.deploy_seated" in веб, "у HTTP-пути своя копия хука — разъедутся"
