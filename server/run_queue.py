"""Очередь прогонов (гейт масштабирования, шаг 1: «walk»).

Зачем: до этого прогон исполнялся СИНХРОННО внутри HTTP-запроса — десятки пользователей означали
десятки параллельных LLM-раскладок в одном процессе (OOM/конкуренция), клиент держал соединение
минутами, второй запуск того же пользователя конкурировал с первым.

Теперь: `POST /api/runs {async:true}` кладёт задание в таблицу `run_jobs` и отвечает 202 сразу.
Пул воркеров (ABOP_RUN_WORKERS) забирает задания через `FOR UPDATE SKIP LOCKED` — это работает и на
нескольких репликах API без отдельного брокера (Kafka — следующая фаза по CONCEPT_SCALING_OBSERVABILITY).
Правила честной очереди:
  * не больше ABOP_USER_CONCURRENT (по умолчанию 1) одновременных прогонов на пользователя —
    остальные его задания ждут в очереди, не мешая соседям;
  * дедуп: одинаковое задание (пользователь+агент+idempotency_key), пока оно в очереди или
    выполняется, возвращается повторно, а не дублируется;
  * бэкпрешер по памяти: воркер не берёт новое задание, пока RSS процесса выше ABOP_RUN_MEM_SOFT_MB;
  * таймаут ABOP_RUN_TIMEOUT на прогон; зависшие «running» с протухшим heartbeat перекладываются в
    очередь один раз, затем помечаются failed;
  * отмена: queued → cancelled сразу; running → cancel_requested (ядро проверяет между волнами).
Память-фолбэк без Postgres — для локальной разработки (один процесс).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import secrets
import time

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_jobs (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    actor         TEXT NOT NULL,
    dedupe_key    TEXT,
    payload       JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | cancelled
    priority      INT NOT NULL DEFAULT 5,
    attempts      INT NOT NULL DEFAULT 0,
    locked_by     TEXT,
    heartbeat_at  TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    run_id        TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_run_jobs_status ON run_jobs (status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_run_jobs_actor ON run_jobs (actor, status);
CREATE INDEX IF NOT EXISTS idx_run_jobs_dedupe ON run_jobs (dedupe_key) WHERE status IN ('queued','running');
"""

WORKERS = max(1, int(os.getenv("ABOP_RUN_WORKERS", "2")))
USER_CONCURRENT = max(1, int(os.getenv("ABOP_USER_CONCURRENT", "1")))
RUN_TIMEOUT = max(30, int(os.getenv("ABOP_RUN_TIMEOUT", "600")))
MEM_SOFT_MB = max(0, int(os.getenv("ABOP_RUN_MEM_SOFT_MB", "1500")))
STALE_AFTER = max(60, int(os.getenv("ABOP_RUN_STALE_SEC", str(RUN_TIMEOUT + 120))))
MAX_ATTEMPTS = 2

_MEM: dict[str, dict] = {}
_MEM_LOCK = asyncio.Lock()


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def rss_mb() -> float:
    """RSS текущего процесса в МБ (Linux через /proc или resource; иначе psutil; иначе 0)."""
    try:
        with open("/proc/self/statm", encoding="ascii") as f:
            pages = int(f.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096) / 1048576
    except Exception:  # noqa: BLE001
        pass
    try:
        import resource  # type: ignore
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:  # noqa: BLE001
        pass
    try:
        import psutil  # type: ignore
        return psutil.Process().memory_info().rss / 1048576
    except Exception:  # noqa: BLE001
        return 0.0


def memory_pressure() -> bool:
    return MEM_SOFT_MB > 0 and rss_mb() > MEM_SOFT_MB


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


def _row(r) -> dict:
    keys = ["id", "agent_id", "actor", "dedupe_key", "payload", "status", "priority", "attempts", "locked_by",
            "heartbeat_at", "cancel_requested", "run_id", "error", "created_at", "started_at", "finished_at"]
    d = dict(zip(keys, r))
    for k in ("heartbeat_at", "created_at", "started_at", "finished_at"):
        d[k] = _iso(d.get(k))
    return d


_COLS = ("id,agent_id,actor,dedupe_key,payload,status,priority,attempts,locked_by,heartbeat_at,"
         "cancel_requested,run_id,error,created_at,started_at,finished_at")


async def enqueue(*, agent_id: str, actor: str, payload: dict, dedupe_key: str | None = None,
                  priority: int = 5) -> dict:
    """Положить задание. Если такое же (dedupe_key) уже ждёт или выполняется — вернуть его."""
    if dedupe_key:
        cur = await find_active(dedupe_key)
        if cur:
            cur["deduped"] = True
            return cur
    jid = "job-" + secrets.token_hex(6)
    if not _has_pg():
        async with _MEM_LOCK:
            _MEM[jid] = {"id": jid, "agent_id": agent_id, "actor": actor, "dedupe_key": dedupe_key,
                         "payload": payload, "status": "queued", "priority": priority, "attempts": 0,
                         "locked_by": None, "heartbeat_at": None, "cancel_requested": False, "run_id": None,
                         "error": None, "created_at": _iso(_now()), "started_at": None, "finished_at": None}
            return dict(_MEM[jid])
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO run_jobs (id,agent_id,actor,dedupe_key,payload,priority) VALUES (%s,%s,%s,%s,%s,%s)",
            (jid, agent_id, actor, dedupe_key, json.dumps(payload, ensure_ascii=False), priority))
    return await get(jid)


async def find_active(dedupe_key: str) -> dict | None:
    if not _has_pg():
        for j in _MEM.values():
            if j.get("dedupe_key") == dedupe_key and j["status"] in ("queued", "running"):
                return dict(j)
        return None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            f"SELECT {_COLS} FROM run_jobs WHERE dedupe_key=%s AND status IN ('queued','running') "
            "ORDER BY created_at LIMIT 1", (dedupe_key,))
        r = await cur.fetchone()
    return _row(r) if r else None


async def get(job_id: str) -> dict | None:
    if not _has_pg():
        j = _MEM.get(job_id)
        return dict(j) if j else None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(f"SELECT {_COLS} FROM run_jobs WHERE id=%s", (job_id,))
        r = await cur.fetchone()
    return _row(r) if r else None


async def position(job_id: str) -> int:
    """Место в очереди (1 = следующий). 0 — не в очереди."""
    j = await get(job_id)
    if not j or j["status"] != "queued":
        return 0
    if not _has_pg():
        q = sorted([x for x in _MEM.values() if x["status"] == "queued"],
                   key=lambda x: (x["priority"], x["created_at"]))
        return next((i + 1 for i, x in enumerate(q) if x["id"] == job_id), 0)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM run_jobs WHERE status='queued' AND (priority, created_at) <= "
            "(SELECT priority, created_at FROM run_jobs WHERE id=%s)", (job_id,))
        r = await cur.fetchone()
    return int(r[0] or 0)


async def claim(worker_id: str) -> dict | None:
    """Забрать следующее задание с учётом лимита на пользователя. Атомарно (SKIP LOCKED)."""
    if not _has_pg():
        async with _MEM_LOCK:
            running_by = {}
            for x in _MEM.values():
                if x["status"] == "running":
                    running_by[x["actor"]] = running_by.get(x["actor"], 0) + 1
            q = sorted([x for x in _MEM.values() if x["status"] == "queued"],
                       key=lambda x: (x["priority"], x["created_at"]))
            for x in q:
                if running_by.get(x["actor"], 0) < USER_CONCURRENT:
                    x.update({"status": "running", "locked_by": worker_id, "attempts": x["attempts"] + 1,
                              "started_at": _iso(_now()), "heartbeat_at": _iso(_now())})
                    return dict(x)
            return None
    from .db import _conn
    async with _conn() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id FROM run_jobs j WHERE status='queued' AND "
                "(SELECT count(*) FROM run_jobs r WHERE r.actor=j.actor AND r.status='running') < %s "
                "ORDER BY priority, created_at LIMIT 1 FOR UPDATE SKIP LOCKED", (USER_CONCURRENT,))
            r = await cur.fetchone()
            if not r:
                return None
            jid = r[0]
            await conn.execute(
                "UPDATE run_jobs SET status='running', locked_by=%s, attempts=attempts+1, "
                "started_at=now(), heartbeat_at=now() WHERE id=%s", (worker_id, jid))
    return await get(jid)


async def heartbeat(job_id: str) -> bool:
    """Продлить lock; False — если запрошена отмена (воркер должен остановиться между волнами)."""
    if not _has_pg():
        j = _MEM.get(job_id)
        if j:
            j["heartbeat_at"] = _iso(_now())
            return not j.get("cancel_requested")
        return True
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "UPDATE run_jobs SET heartbeat_at=now() WHERE id=%s RETURNING cancel_requested", (job_id,))
        r = await cur.fetchone()
    return not (r and r[0])


async def finish(job_id: str, run_id: str) -> None:
    if not _has_pg():
        j = _MEM.get(job_id)
        if j:
            j.update({"status": "done", "run_id": run_id, "finished_at": _iso(_now()), "locked_by": None})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("UPDATE run_jobs SET status='done', run_id=%s, finished_at=now(), locked_by=NULL "
                           "WHERE id=%s", (run_id, job_id))


async def fail(job_id: str, error: str, *, requeue: bool = False) -> None:
    status = "queued" if requeue else "failed"
    if not _has_pg():
        j = _MEM.get(job_id)
        if j:
            j.update({"status": status, "error": error[:1000], "locked_by": None,
                      "finished_at": None if requeue else _iso(_now())})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "UPDATE run_jobs SET status=%s, error=%s, locked_by=NULL, "
            "finished_at=CASE WHEN %s THEN NULL ELSE now() END WHERE id=%s",
            (status, error[:1000], requeue, job_id))


async def cancel(job_id: str, actor: str | None = None, admin: bool = False) -> dict | None:
    """queued → cancelled; running → cancel_requested. Только автор или admin."""
    j = await get(job_id)
    if not j:
        return None
    if not admin and actor and j["actor"] != actor:
        return {"id": job_id, "status": j["status"], "denied": True}
    if j["status"] == "queued":
        if not _has_pg():
            _MEM[job_id].update({"status": "cancelled", "finished_at": _iso(_now())})
        else:
            from .db import _conn
            async with _conn() as conn:
                await conn.execute("UPDATE run_jobs SET status='cancelled', finished_at=now() WHERE id=%s "
                                   "AND status='queued'", (job_id,))
        return await get(job_id)
    if j["status"] == "running":
        if not _has_pg():
            _MEM[job_id]["cancel_requested"] = True
        else:
            from .db import _conn
            async with _conn() as conn:
                await conn.execute("UPDATE run_jobs SET cancel_requested=TRUE WHERE id=%s", (job_id,))
        return await get(job_id)
    return j


async def requeue_stale() -> int:
    """Задания running без heartbeat дольше STALE_AFTER: 1-я попытка → назад в очередь, потом failed."""
    n = 0
    if not _has_pg():
        cutoff = _now() - _dt.timedelta(seconds=STALE_AFTER)
        for j in _MEM.values():
            if j["status"] == "running" and j.get("heartbeat_at") and \
                    _dt.datetime.fromisoformat(j["heartbeat_at"]) < cutoff:
                j.update({"status": "queued" if j["attempts"] < MAX_ATTEMPTS else "failed",
                          "error": "воркер не отвечал (heartbeat протух)", "locked_by": None})
                n += 1
        return n
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "UPDATE run_jobs SET status=CASE WHEN attempts < %s THEN 'queued' ELSE 'failed' END, "
            "error='воркер не отвечал (heartbeat протух)', locked_by=NULL, "
            "finished_at=CASE WHEN attempts < %s THEN NULL ELSE now() END "
            "WHERE status='running' AND heartbeat_at < now() - make_interval(secs => %s)",
            (MAX_ATTEMPTS, MAX_ATTEMPTS, STALE_AFTER))
        n = cur.rowcount or 0
    return n


async def stats() -> dict:
    if not _has_pg():
        by = {}
        for j in _MEM.values():
            by[j["status"]] = by.get(j["status"], 0) + 1
        return {"by_status": by, "workers": WORKERS, "user_concurrent": USER_CONCURRENT,
                "timeout_sec": RUN_TIMEOUT, "rss_mb": round(rss_mb(), 1), "mem_soft_mb": MEM_SOFT_MB,
                "memory_pressure": memory_pressure()}
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT status, count(*) FROM run_jobs GROUP BY status")
        rows = await cur.fetchall()
        cur2 = await conn.execute(
            "SELECT COALESCE(EXTRACT(EPOCH FROM avg(started_at - created_at)),0) FROM run_jobs "
            "WHERE started_at IS NOT NULL AND created_at > now() - interval '1 hour'")
        w = await cur2.fetchone()
    return {"by_status": {r[0]: int(r[1]) for r in rows}, "workers": WORKERS, "user_concurrent": USER_CONCURRENT,
            "timeout_sec": RUN_TIMEOUT, "avg_wait_sec_1h": round(float(w[0] or 0), 1),
            "rss_mb": round(rss_mb(), 1), "mem_soft_mb": MEM_SOFT_MB, "memory_pressure": memory_pressure()}


async def list_jobs(actor: str | None = None, limit: int = 50) -> list[dict]:
    if not _has_pg():
        rows = [dict(j) for j in _MEM.values() if not actor or j["actor"] == actor]
        rows.sort(key=lambda x: x["created_at"] or "", reverse=True)
        return rows[:limit]
    from .db import _conn
    async with _conn() as conn:
        if actor:
            cur = await conn.execute(f"SELECT {_COLS} FROM run_jobs WHERE actor=%s ORDER BY created_at DESC "
                                     "LIMIT %s", (actor, limit))
        else:
            cur = await conn.execute(f"SELECT {_COLS} FROM run_jobs ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = await cur.fetchall()
    return [_row(r) for r in rows]


def public(job: dict, position_: int = 0) -> dict:
    """Ответ клиенту без внутренностей (payload/locked_by)."""
    return {"job_id": job["id"], "status": job["status"], "agent_id": job["agent_id"], "run_id": job.get("run_id"),
            "error": job.get("error"), "position": position_, "created_at": job.get("created_at"),
            "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
            "cancel_requested": bool(job.get("cancel_requested")), "attempts": job.get("attempts", 0)}


async def worker_loop(worker_id: str, executor, *, load_agent, load_contract, on_done=None) -> None:
    """Вечный цикл воркера. executor = web_api.execute_agent_run. Не падает на ошибках задания."""
    import logging
    log = logging.getLogger("abop.run_queue")
    idle = 0.0
    while True:
        try:
            if memory_pressure():
                await asyncio.sleep(3)
                continue
            job = await claim(worker_id)
            if not job:
                idle = min(3.0, idle + 0.25)
                await asyncio.sleep(idle)
                continue
            idle = 0.0
            p = job.get("payload") or {}
            t0 = time.perf_counter()
            hb_task = None
            try:
                agent = await load_agent(job["agent_id"])
                if not agent:
                    await fail(job["id"], "агент не найден")
                    continue
                contract = await load_contract(agent, p.get("contract_audit_id") or "")

                async def _hb():
                    while True:
                        await asyncio.sleep(15)
                        try:
                            if not await heartbeat(job["id"]):
                                _CANCEL_FLAGS.add(job["id"])
                        except Exception:  # noqa: BLE001
                            pass

                hb_task = asyncio.create_task(_hb())
                out = await asyncio.wait_for(
                    executor(agent, contract, job["actor"], use_cache=bool(p.get("use_cache", False)),
                             user_context=p.get("user_context") or "", deliver_filter=p.get("deliver_filter") or "",
                             job_id=job["id"]),
                    timeout=RUN_TIMEOUT)
                await finish(job["id"], out["saved"]["id"])
                if on_done:
                    try:
                        await on_done(job, out)
                    except Exception:  # noqa: BLE001
                        pass
            except asyncio.TimeoutError:
                await fail(job["id"], f"таймаут прогона ({RUN_TIMEOUT} с)")
            except Exception as ex:  # noqa: BLE001
                msg = f"{type(ex).__name__}: {ex}"
                log.exception("run job failed: %s", job["id"])
                await fail(job["id"], msg, requeue=(job.get("attempts", 1) < MAX_ATTEMPTS and "timeout" not in msg))
            finally:
                if hb_task:
                    hb_task.cancel()
                _CANCEL_FLAGS.discard(job["id"])
                log.info("job %s finished in %.1fs", job["id"], time.perf_counter() - t0)
        except Exception:  # noqa: BLE001 — сам цикл не умирает
            log.exception("worker loop error")
            await asyncio.sleep(2)


_CANCEL_FLAGS: set[str] = set()


def cancel_requested(job_id: str | None) -> bool:
    return bool(job_id) and job_id in _CANCEL_FLAGS
