# -*- coding: utf-8 -*-
"""Канонические записи Data Plane в Postgres (п.13 исследования 08.10).

**Зачем.** Эмитированные записи жили `.jsonl`-файлами в `~/.ape/data/`, и скрипт деплоя говорил
прямо: «канонические записи — только в томе, том обязателен». Отсюда два следствия, которые
упирались друг в друга: вторая реплика на другом хосте видела пустой Data Plane (потолок
горизонтального масштаба), а сам том был единственной точкой потери данных. Рецепты и коннекторы
в Postgres уехали давно — записи остались последним файловым слоем.

**Что сохранено без изменений.** Версионная семантика (решение владельца 18.09): чтение
дедуплицирует по `id` и отдаёт ПОСЛЕДНЮЮ СОЗДАННУЮ версию (max `provenance.fetched_at`); последняя
версия не протухает по одному лишь времени; TTL вытесняет только перекрытые версии. Запись
append-only. Это ровно то, что делал файл, поэтому поведение агентов не меняется.

**Почему синхронный пул.** Единственный потребитель канонических записей — `cli/ape.py`, а он
синхронный и вызывается из навыков. Городить мост sync→async ради одного слоя дороже, чем взять
`psycopg_pool.ConnectionPool`: соединения всё равно к той же базе, пул маленький.

**Почему инъекцией, а не импортом.** `ape` работает и как CLI без сервера. Сервер зовёт
`ape.set_record_store(...)` на старте — тем же приёмом, что уже применён к рецептам и коннекторам
(`set_recipe_store`). Стор не инжектирован ⇒ `ape` продолжает читать файлы.
"""
from __future__ import annotations

import json
import os
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS dp_records (
  seq         BIGSERIAL PRIMARY KEY,
  entity      TEXT NOT NULL,
  rec_id      TEXT NOT NULL,
  fetched_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
  ttl_sec     DOUBLE PRECISION NOT NULL DEFAULT 0,
  payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS dp_records_lookup ON dp_records (entity, rec_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS dp_records_entity ON dp_records (entity);
"""

_pool = None
_MEM: dict[str, list[dict]] = {}       # entity → список записей в порядке добавления (как в файле)
_ВКЛ = False


def _dsn() -> str:
    try:
        from .config import settings
        return settings.pg_dsn or ""
    except Exception:  # noqa: BLE001 — модуль должен работать и без конфига (CLI/тесты)
        return os.getenv("PG_DSN") or os.getenv("DATABASE_URL") or ""


def _has_pg() -> bool:
    return bool(_dsn())


def init() -> None:
    """Поднять схему. Без DSN — работаем в памяти (тесты, локальный CLI)."""
    global _pool, _ВКЛ
    _ВКЛ = True
    if not _has_pg():
        return
    from psycopg_pool import ConnectionPool
    if _pool is None:
        _pool = ConnectionPool(_dsn(), min_size=1, max_size=4, open=True)
    with _pool.connection() as conn:
        conn.execute(SCHEMA)


def _ts(rec: dict) -> float:
    return float((rec.get("provenance") or {}).get("fetched_at", 0) or 0)


def _ttl(rec: dict) -> float:
    try:
        return float(rec.get("ttl_sec", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _последние(recs: list[dict]) -> list[dict]:
    """Дедуп по id: остаётся последняя созданная версия. Порядок добавления сохраняем."""
    latest: dict = {}
    for r in recs:
        k = r.get("id", id(r))
        if k not in latest or _ts(r) >= _ts(latest[k]):
            latest[k] = r
    return list(latest.values())


# ── запись ───────────────────────────────────────────────────────────────────────────────────────

def append(entity: str, records: list[dict], *, reset: bool = False) -> int:
    """Дописать пачку записей. `reset=True` — перечитать сущность начисто (как `w` у файла)."""
    if not _has_pg():
        if reset:
            _MEM[entity] = []
        _MEM.setdefault(entity, []).extend(records)
        return len(records)
    with _pool.connection() as conn:
        with conn.transaction():
            if reset:
                conn.execute("DELETE FROM dp_records WHERE entity=%s", (entity,))
            for r in records:
                conn.execute(
                    "INSERT INTO dp_records (entity, rec_id, fetched_at, ttl_sec, payload) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (entity, str(r.get("id", "")), _ts(r), _ttl(r),
                     json.dumps(r, ensure_ascii=False)))
    return len(records)


# ── чтение ───────────────────────────────────────────────────────────────────────────────────────

def query(entity: str, filters: dict | None = None, fields: list | None = None,
          limit: int = 50, include_stale: bool = False) -> list[dict]:
    """Та же семантика, что у файлового `ape.data_query`."""
    if not _has_pg():
        recs = list(_MEM.get(entity, []))
        res = recs if include_stale else _последние(recs)
    elif include_stale:
        with _pool.connection() as conn:
            cur = conn.execute("SELECT payload FROM dp_records WHERE entity=%s ORDER BY seq", (entity,))
            res = [_строка(x[0]) for x in cur.fetchall()]
    else:
        # DISTINCT ON отдаёт по одной строке на rec_id — ту, у которой свежее fetched_at;
        # при равенстве берём позже добавленную (seq), как делал файл при равных метках.
        with _pool.connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT ON (rec_id) payload FROM dp_records WHERE entity=%s "
                "ORDER BY rec_id, fetched_at DESC, seq DESC", (entity,))
            res = [_строка(x[0]) for x in cur.fetchall()]
    for k, v in (filters or {}).items():
        res = [r for r in res if str(r.get(k, "")).lower() == str(v).lower()]
    if fields:
        res = [{k: r.get(k) for k in fields} for r in res]
    return res[:limit]


def _строка(payload) -> dict:
    return payload if isinstance(payload, dict) else json.loads(payload)


def get(entity: str, rec_id: str, include_stale: bool = False) -> dict | None:
    hits = query(entity, {"id": rec_id}, limit=1, include_stale=include_stale)
    return hits[0] if hits else None


def count(entity: str) -> int:
    if not _has_pg():
        return len(_последние(_MEM.get(entity, [])))
    with _pool.connection() as conn:
        cur = conn.execute("SELECT count(DISTINCT rec_id) FROM dp_records WHERE entity=%s", (entity,))
        return int(cur.fetchone()[0])


def entities() -> list[str]:
    if not _has_pg():
        return sorted(k for k, v in _MEM.items() if v)
    with _pool.connection() as conn:
        cur = conn.execute("SELECT DISTINCT entity FROM dp_records ORDER BY entity")
        return [x[0] for x in cur.fetchall()]


# ── компакция ────────────────────────────────────────────────────────────────────────────────────

def compact(entity: str, *, now: float | None = None) -> int:
    """Удалить перекрытые версии старше TTL. Последняя версия каждого id остаётся всегда."""
    сейчас = now if now is not None else time.time()
    if not _has_pg():
        recs = _MEM.get(entity, [])
        latest = {}
        for r in recs:
            k = r.get("id", id(r))
            if k not in latest or _ts(r) >= _ts(latest[k]):
                latest[k] = r
        keep = [r for r in recs
                if latest.get(r.get("id", id(r))) is r
                or not _ttl(r) or _ts(r) + _ttl(r) >= сейчас]
        убрано = len(recs) - len(keep)
        _MEM[entity] = keep
        return убрано
    with _pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM dp_records d WHERE d.entity=%s AND d.ttl_sec > 0 "
            "AND d.fetched_at + d.ttl_sec < %s "
            "AND EXISTS (SELECT 1 FROM dp_records n WHERE n.entity=d.entity AND n.rec_id=d.rec_id "
            "            AND (n.fetched_at, n.seq) > (d.fetched_at, d.seq))",
            (entity, сейчас))
        return cur.rowcount or 0


def _сброс_для_тестов() -> None:
    _MEM.clear()
    if _has_pg() and _pool is not None:
        with _pool.connection() as conn:
            conn.execute("TRUNCATE dp_records")
