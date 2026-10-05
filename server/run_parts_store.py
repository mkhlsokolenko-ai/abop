"""Готовые части прогона: что навык уже отдал — то больше не считаем заново.

Длинная цепочка не укладывалась в таймаут попытки (`ABOP_RUN_TIMEOUT`, 600 с), и работа терялась
ЦЕЛИКОМ: задание падало с «таймаут», а пять уже отработавших навыков — вместе с ним. Человек видел
«прогон не выполнен» после десяти минут ожидания и честно оплаченных токенов.

Теперь каждая готовая часть сразу ложится сюда, а следующая попытка берёт её отсюда и считает только
то, чего ещё нет. В конце части сводятся в отчёт, и кэш гасится — он живёт ровно столько, сколько
идёт прогон, и не притворяется долгой памятью (для неё есть доска).

Ключ — задание очереди (`job_id`), а не прогон: прогон у каждой попытки свой, а задание одно.
Вместе с частью храним отпечаток графа агента: если агента правили между попытками, старые части
уже не про него — берём заново, молча подсунуть результат другой методики нельзя.

run_parts{job_id, skill, rev, payload JSONB, created_at}
"""
from __future__ import annotations

import hashlib
import json
import time

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_parts (
    job_id      TEXT NOT NULL,
    skill       TEXT NOT NULL,
    rev         TEXT NOT NULL DEFAULT '',
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, skill)
);
CREATE INDEX IF NOT EXISTS run_parts_job_idx ON run_parts (job_id);
"""

# Фолбэк без Postgres: тот же вид данных, но живёт только в процессе (тесты, локальный запуск).
_MEM: dict[str, dict[str, dict]] = {}
# Часть прогона — это текст находок и структурный ответ навыка. Гигантские ответы в кэш не кладём:
# смысл в том, чтобы не считать заново, а не в том, чтобы хранить всё подряд.
MAX_PART_BYTES = 256_000


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        for chunk in SCHEMA.split(";"):
            stmt = chunk.strip()
            if stmt:
                await conn.execute(stmt)


def rev_of(agent: dict) -> str:
    """Отпечаток методики: граф агента и версия. Правка агента обесценивает готовые части."""
    g = (agent or {}).get("graph") or {}
    seed = json.dumps({"v": (agent or {}).get("version"), "nodes": g.get("nodes"), "edges": g.get("edges")},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


async def save(job_id: str, skill: str, payload: dict, *, rev: str = "") -> bool:
    """Отложить готовую часть. Слишком большую не храним: следующая попытка пересчитает её."""
    job_id, skill = str(job_id or ""), str(skill or "")
    if not job_id or not skill:
        return False
    blob = json.dumps(payload or {}, ensure_ascii=False, default=str)
    if len(blob.encode("utf-8")) > MAX_PART_BYTES:
        return False
    if not _has_pg():
        _MEM.setdefault(job_id, {})[skill] = {"rev": rev, "payload": payload, "at": time.time()}
        return True
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO run_parts (job_id, skill, rev, payload) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (job_id, skill) DO UPDATE SET payload=EXCLUDED.payload, rev=EXCLUDED.rev, "
            "created_at=now()", (job_id, skill, rev, blob))
    return True


async def load(job_id: str, *, rev: str = "") -> dict[str, dict]:
    """Готовые части задания. Части от другой версии агента не отдаём — методика изменилась."""
    job_id = str(job_id or "")
    if not job_id:
        return {}
    if not _has_pg():
        return {k: v["payload"] for k, v in (_MEM.get(job_id) or {}).items()
                if not rev or not v.get("rev") or v.get("rev") == rev}
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT skill, rev, payload FROM run_parts WHERE job_id=%s", (job_id,))
        rows = await cur.fetchall()
    out: dict[str, dict] = {}
    for sid, r, val in rows:
        if rev and r and r != rev:
            continue
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:  # noqa: BLE001 — битая часть не повод терять остальные
                continue
        out[str(sid)] = dict(val or {})
    return out


async def drop(job_id: str) -> int:
    """Погасить кэш задания: прогон сведён в отчёт, части больше ничего не значат."""
    job_id = str(job_id or "")
    if not job_id:
        return 0
    if not _has_pg():
        return len(_MEM.pop(job_id, {}) or {})
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM run_parts WHERE job_id=%s", (job_id,))
        return int(getattr(cur, "rowcount", 0) or 0)


async def sweep(older_than_sec: int = 86400) -> int:
    """Уборка за заданиями, которые не довели до конца (упали, отменены, забыты)."""
    if not _has_pg():
        n, now = 0, time.time()
        for jid in list(_MEM):
            parts = _MEM.get(jid) or {}
            if parts and all(now - float(p.get("at") or 0) > older_than_sec for p in parts.values()):
                n += len(_MEM.pop(jid, {}) or {})
        return n
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "DELETE FROM run_parts WHERE created_at < now() - (%s || ' seconds')::interval",
            (int(older_than_sec),))
        return int(getattr(cur, "rowcount", 0) or 0)
