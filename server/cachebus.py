"""Cache-bus: инвалидация in-process кэшей между репликами API через Postgres LISTEN/NOTIFY.

Проблема многопользовательского/горизонтального режима: часть состояния инжектится в модуль `ape`
(навык-источники, рецепты/коннекторы Data Plane). При записи на реплике A реплика B держит устаревший
кэш. Решение: писатель делает локальный refresh (для своей консистентности) И шлёт NOTIFY 'abop_cache'
с топиком — все реплики (через LISTEN) перечитывают нужный кэш из Postgres. Без PG — no-op (одна реплика).
Разблокирует запуск 2+ реплик API. См. docs/CONCEPT_SCALING_OBSERVABILITY.md §4.
"""
from __future__ import annotations

import asyncio

from . import observability as obs
from .config import settings

_CHANNEL = "abop_cache"
_HANDLERS: dict = {}  # topic -> async refresh fn


def register(topic: str, fn) -> None:
    """Привязать топик инвалидации к функции перечитывания кэша из PG."""
    _HANDLERS[topic] = fn


async def notify(topic: str) -> None:
    """Разослать инвалидацию всем репликам (включая себя — LISTEN доставит и отправителю)."""
    if not settings.pg_dsn:
        return
    try:
        import psycopg
        async with await psycopg.AsyncConnection.connect(settings.pg_dsn, autocommit=True) as c:
            await c.execute("SELECT pg_notify(%s, %s)", (_CHANNEL, topic))
    except Exception as ex:  # noqa: BLE001 — инвалидация опциональна, запрос не должен падать
        obs.log_event("warn", "cachebus.notify_fail", topic=topic, error=f"{type(ex).__name__}: {ex}")


async def listen_loop() -> None:
    """Фоновый слушатель: на выделенном коннекте LISTEN abop_cache → refresh кэша по топику.
    Переподключается при обрыве. Стартует в _startup."""
    if not settings.pg_dsn:
        return
    import psycopg
    while True:
        try:
            conn = await psycopg.AsyncConnection.connect(settings.pg_dsn, autocommit=True)
            await conn.execute(f"LISTEN {_CHANNEL}")
            obs.log_event("info", "cachebus.listening", channel=_CHANNEL)
            async for n in conn.notifies():
                fn = _HANDLERS.get(n.payload)
                if not fn:
                    continue
                try:
                    await fn()
                    obs.log_event("info", "cachebus.invalidated", topic=n.payload)
                except Exception as ex:  # noqa: BLE001
                    obs.log_event("warn", "cachebus.refresh_fail", topic=n.payload,
                                  error=f"{type(ex).__name__}: {ex}")
        except Exception as ex:  # noqa: BLE001 — обрыв LISTEN-коннекта → переподключаемся
            obs.log_event("warn", "cachebus.reconnect", error=f"{type(ex).__name__}: {ex}")
            await asyncio.sleep(3)
