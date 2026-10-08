"""Пул соединений Postgres — единственная точка подключения для всех сторов ABOP.

Раньше в этом файле жила ещё и курсовая механика: журнал стоимости `cost_journal` с привязкой к
`student_id` и трёхуровневые квоты «5M на сессию / 5 сессий в неделю / 25M в неделю». К ABOP она
не относилась и не вызывалась ниоткуда; удалена 08.10 вместе с остальным курсовым слоем.

Учёт расхода в ABOP устроен иначе: стоимость считается по `RunMetrics` прогона и тарифнику
`server/pricing.py`, квота среды лежит в `app_config`. Если понадобится честный пожильный журнал
трат, его надо проектировать вокруг Identity Map ABOP, а не вокруг `student_id`.

Таблица `cost_journal` на существующих стендах остаётся — миграция её не удаляет: пустую таблицу
дешевле оставить, чем рисковать `DROP` на проде ради чистоты схемы.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from .config import settings

_pool: AsyncConnectionPool | None = None


async def init_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(settings.pg_dsn, min_size=1, max_size=8, open=False)
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def _conn():
    pool = await init_pool()
    async with pool.connection() as conn:
        yield conn
