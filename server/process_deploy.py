# -*- coding: utf-8 -*-
"""Развёртывание агентов, посаженных применённой редакцией карты (ADR-043, ADR-044).

Отдельный модуль, а не метод web_api: применяет редакцию не только HTTP-ручка, но и перенос со
старого стенда (`scripts/migrate_process_map.py`). Когда хук жил внутри web_api, миграция
проходила мимо него — карта утверждала, что агент работает участок, пока агент стоял черновиком и
триггеров не принимал. Один вызов на обоих путях снимает это расхождение.
"""
from __future__ import annotations

from . import agent_store, audit_store


async def deploy_seated(посадки: list[dict], *, actor: str) -> list[str]:
    """Развернуть агентов, которых редакция посадила на участки. Уже развёрнутые и приостановленные
    не трогаем: пауза — осознанное решение человека, и применение чужой редакции её не отменяет."""
    развёрнуты: list[str] = []
    for s in посадки:
        версии = await agent_store.list_for(str(s.get("audit_id") or ""), limit=50)
        if not версии:
            continue
        свежая = max(версии, key=lambda a: a.get("version") or 0)
        if свежая.get("status") != "draft":
            continue
        await agent_store.set_status(свежая["id"], "deployed")
        await audit_store.record(actor, "agent.status", свежая["id"],
                                 {"status": "deployed",
                                  "reason": "посадка на участок " + str(s.get("op_id"))})
        развёрнуты.append(свежая["id"])
    return развёрнуты
