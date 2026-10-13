# -*- coding: utf-8 -*-
"""Лента контроля руководителя: смотреть свершившееся и управлять полномочиями.

Домен тонкий намеренно: он читает сторы и отдаёт результат `control_feed.лента`. Вся сборка —
чистые функции в `server/control_feed.py`, там же и аномалии; здесь только права и чтения.

**Две проверки прав, и они разные.**

* Ленту видит `manager+`: это сводка по ЛЮДЯМ отдела, и аналитику показывать её незачем — у него и
  своих решений хватает (ADR-060: руководитель не нажимает кнопки, но картину отдела видит он).
* Снятие разрешения ограничено **отделом** и проверяется в самом запросе к базе
  (`channel_tokens.снять(…, отдел=…)`), а не здесь: проверка в маршруте защищала бы от интерфейса,
  а не от запроса.

**Почему единственное действие — снять разрешение.** Отправленное не вернуть. Кнопка «отозвать
письмо» была бы ложью, а кнопка «запретить этот агент» ударила бы по работе всего отдела из-за
одного случая. Рычаг здесь один и честный: сузить то, что будет можно дальше.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import audit_store, channel_tokens, control_feed, hitl_store
from ..deps import can_see_family, require_level, user

router = APIRouter()


@router.get("/api/control/feed")
async def control_feed_route(u: dict = Depends(user),
                             days: int = Query(default=control_feed.ОКНО_ДНЕЙ, ge=1, le=90)) -> dict:
    """Лента контроля: аномалии, что ушло наружу, что не дошло, какие разрешения действуют."""
    require_level(u, "manager")
    отдел = str(u.get("department") or "")
    решённые = [з for з in await hitl_store.решённые(дней=days)
                if can_see_family(u, з.get("family"))]
    ждущие = [з for з in await hitl_store.list_pending() if can_see_family(u, з.get("family"))]
    разрешения = await channel_tokens.действующие(отдел=отдел if отдел not in ("", "*") else "*")
    прежние = await hitl_store.адресаты_до(дней=days)
    лента = control_feed.лента(решённые=решённые, ждущие=ждущие, разрешения=разрешения,
                               прежние_адресаты=прежние)
    лента["окно_дней"] = days
    лента["отдел"] = отдел or "*"
    return лента


@router.delete("/api/control/permissions/{binding}")
async def снять_разрешение(binding: str, u: dict = Depends(user)) -> JSONResponse:
    """Снять постоянное разрешение (привязку канала) человека из своего отдела.

    Запись в журнал обязательна: снятие полномочия — управленческое действие, и через месяц
    вопрос «почему у меня перестало работать» должен иметь ответ с именем и временем.
    """
    require_level(u, "manager")
    отдел = str(u.get("department") or "")
    снято = await channel_tokens.снять(binding, отдел=отдел if отдел not in ("", "*") else "*")
    if not снято:
        # Одинаковый ответ на «нет такой привязки» и «чужой отдел» — намеренно: иначе по кодам
        # ответа можно перебрать, у кого какие привязки есть.
        raise HTTPException(404, "привязка не найдена или не в вашем отделе")
    кто = u.get("name") or u.get("sub") or "manager"
    await audit_store.record(кто, "control.revoke_permission", binding,
                             {"кто_лишился": снято.get("кто"), "канал": снято.get("канал"),
                              "отдел": отдел}, severity="warn")
    return JSONResponse({"ok": True, "снято": снято,
                         "что_дальше": ("человек снова сможет привязать канал сам — разрешение "
                                        "снято, а не запрещено навсегда")})
