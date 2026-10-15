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

from . import tenancy

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
-- Арендатор (шаг 5). Миграция безопасна: уже лежащие записи достаются арендатору
-- по умолчанию — той единственной компании, для которой система и работала.
ALTER TABLE dp_records ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS dp_records_tenant
  ON dp_records (tenant, entity, rec_id, fetched_at DESC);
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
    ключ, арендатор = tenancy.ключ_памяти(entity), tenancy.текущий()
    if not _has_pg():
        if reset:
            _MEM[ключ] = []
        _MEM.setdefault(ключ, []).extend(records)
        return len(records)
    with _pool.connection() as conn:
        with conn.transaction():
            if reset:        # «перечитать начисто» — только СВОЮ сущность, не соседа
                conn.execute("DELETE FROM dp_records WHERE entity=%s AND tenant=%s",
                             (entity, арендатор))
            for r in records:
                conn.execute(
                    "INSERT INTO dp_records (entity, rec_id, fetched_at, ttl_sec, payload, tenant) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (entity, str(r.get("id", "")), _ts(r), _ttl(r),
                     json.dumps(r, ensure_ascii=False), арендатор))
    return len(records)


# ── чтение ───────────────────────────────────────────────────────────────────────────────────────

def query(entity: str, filters: dict | None = None, fields: list | None = None,
          limit: int = 50, include_stale: bool = False, offset: int = 0) -> list[dict]:
    """Та же семантика, что у файлового `ape.data_query`."""
    арендатор = tenancy.текущий()
    if not _has_pg():
        recs = list(_MEM.get(tenancy.ключ_памяти(entity), []))
        res = recs if include_stale else _последние(recs)
    elif include_stale:
        with _pool.connection() as conn:
            cur = conn.execute("SELECT payload FROM dp_records WHERE entity=%s AND tenant=%s "
                               "ORDER BY seq", (entity, арендатор))
            res = [_строка(x[0]) for x in cur.fetchall()]
    else:
        # DISTINCT ON отдаёт по одной строке на rec_id — ту, у которой свежее fetched_at;
        # при равенстве берём позже добавленную (seq), как делал файл при равных метках.
        with _pool.connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT ON (rec_id) payload FROM dp_records "
                "WHERE entity=%s AND tenant=%s "
                "ORDER BY rec_id, fetched_at DESC, seq DESC", (entity, арендатор))
            res = [_строка(x[0]) for x in cur.fetchall()]
    # Семантика фильтра — одна с файловым путём ядра, поэтому и функция одна: иначе выборка из БД
    # и из файла однажды ответят по-разному, и объяснить это будет нечем.
    from cli.ape import фильтр_проходит
    for k, v in (filters or {}).items():
        res = [r for r in res if фильтр_проходит(r, k, v)]
    if fields:
        res = [{k: r.get(k) for k in fields} for r in res]
    off = max(0, int(offset or 0))
    return res[off:off + limit]


def _строка(payload) -> dict:
    return payload if isinstance(payload, dict) else json.loads(payload)


def get(entity: str, rec_id: str, include_stale: bool = False) -> dict | None:
    hits = query(entity, {"id": rec_id}, limit=1, include_stale=include_stale)
    return hits[0] if hits else None


def count(entity: str) -> int:
    if not _has_pg():
        return len(_последние(_MEM.get(tenancy.ключ_памяти(entity), [])))
    with _pool.connection() as conn:
        cur = conn.execute("SELECT count(DISTINCT rec_id) FROM dp_records "
                           "WHERE entity=%s AND tenant=%s", (entity, tenancy.текущий()))
        return int(cur.fetchone()[0])


def entities() -> list[str]:
    """Сущности, у которых есть записи У ЭТОГО арендатора.

    Сущность соседа не должна появляться даже в перечне: по составу Data Plane читается, чем
    занимается другая компания.
    """
    префикс = tenancy.ключ_памяти("")
    if not _has_pg():
        return sorted(k[len(префикс):] for k, v in _MEM.items() if v and k.startswith(префикс))
    with _pool.connection() as conn:
        cur = conn.execute("SELECT DISTINCT entity FROM dp_records WHERE tenant=%s ORDER BY entity",
                           (tenancy.текущий(),))
        return [x[0] for x in cur.fetchall()]


# ── компакция ────────────────────────────────────────────────────────────────────────────────────

def compact(entity: str, *, now: float | None = None) -> int:
    """Удалить перекрытые версии старше TTL. Последняя версия каждого id остаётся всегда."""
    сейчас = now if now is not None else time.time()
    if not _has_pg():
        recs = _MEM.get(tenancy.ключ_памяти(entity), [])
        latest = {}
        for r in recs:
            k = r.get("id", id(r))
            if k not in latest or _ts(r) >= _ts(latest[k]):
                latest[k] = r
        keep = [r for r in recs
                if latest.get(r.get("id", id(r))) is r
                or not _ttl(r) or _ts(r) + _ttl(r) >= сейчас]
        убрано = len(recs) - len(keep)
        _MEM[tenancy.ключ_памяти(entity)] = keep
        return убрано
    with _pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM dp_records d WHERE d.entity=%s AND d.tenant=%s AND d.ttl_sec > 0 "
            "AND d.fetched_at + d.ttl_sec < %s "
            "AND EXISTS (SELECT 1 FROM dp_records n WHERE n.entity=d.entity "
            "            AND n.tenant=d.tenant AND n.rec_id=d.rec_id "
            "            AND (n.fetched_at, n.seq) > (d.fetched_at, d.seq))",
            (entity, tenancy.текущий(), сейчас))
        return cur.rowcount or 0


def _сброс_для_тестов() -> None:
    _MEM.clear()
    if _has_pg() and _pool is not None:
        with _pool.connection() as conn:
            conn.execute("TRUNCATE dp_records")


# ── Определения Data Plane синхронно: рецепты и коннекторы по арендатору ─────────────────────────
# Ядро (`cli/ape.py`) синхронно, а рецепты ему нужны на каждом прогоне. Раньше сервер инжектил в
# него ГОТОВЫЙ СПИСОК на старте — и этот список не знал про арендатора: вендор, открывший данные
# клиента, видел рецепты своего. Поймано живой проверкой на проде, а не тестом: статический сторож
# таких дыр не видит, потому что данные приходят не из запроса, а из кэша процесса.
#
# Закономерность, которую стоит помнить: **инъекция объектом знает арендатора, инъекция готовым
# списком — нет**. Поэтому здесь не кэш, а чтение по месту — через тот же синхронный пул.

def _из_памяти(какие: str) -> list[dict]:
    from . import dataplane_store as _ds
    склад = _ds._MEM_RECIPES if какие == "recipes" else _ds._MEM_CONNECTORS
    префикс = tenancy.ключ_памяти("")
    return [v for k, v in склад.items() if k.startswith(префикс)]


def рецепты() -> list[dict]:
    """Рецепты текущего арендатора. Синхронно — для ядра."""
    if not _has_pg():
        return _из_памяти("recipes")
    with _pool.connection() as conn:
        cur = conn.execute("SELECT spec FROM dp_recipes WHERE tenant=%s", (tenancy.текущий(),))
        return [_строка(r[0]) for r in cur.fetchall()]


def коннекторы() -> list[dict]:
    """Коннекторы текущего арендатора. Синхронно — для ядра."""
    if not _has_pg():
        return _из_памяти("connectors")
    with _pool.connection() as conn:
        cur = conn.execute("SELECT spec FROM dp_connectors WHERE tenant=%s", (tenancy.текущий(),))
        return [_строка(r[0]) for r in cur.fetchall()]


def _правки_навыков() -> list[tuple[str, dict]]:
    """Правки карточек навыков текущего арендатора — синхронно, для ядра."""
    if not _has_pg():
        from . import skill_store as _ss
        префикс = tenancy.ключ_памяти("")
        return [(k[len(префикс):], v) for k, v in _ss._MEM.items() if k.startswith(префикс)]
    with _pool.connection() as conn:
        cur = conn.execute("SELECT sid, patch, datasources FROM skill_overrides WHERE tenant=%s",
                           (tenancy.текущий(),))
        return [(r[0], {"patch": r[1] or {}, "datasources": r[2]}) for r in cur.fetchall()]


def правки_источников() -> dict:
    """{sid: datasources[]} арендатора — data-need навыка, настроенный в его интерфейсе."""
    return {sid: (r.get("datasources") or []) for sid, r in _правки_навыков()
            if r.get("datasources") is not None}


def правки_вывода() -> dict:
    """{sid: output} арендатора — флаг «структура или документ» из карточки навыка."""
    out = {}
    for sid, r in _правки_навыков():
        формат = (r.get("patch") or {}).get("output")
        if формат:
            out[sid] = формат
    return out
