"""Очередь заданий исполнения (гейт масштабирования, Фаза 1 → блок 1 доработок).

Задание (`run_jobs`) — это прогон агента (`kind=run`) ИЛИ цепочка агентов (`kind=pipeline`).
Исполняет пул воркеров (`ABOP_RUN_WORKERS`), захват через `FOR UPDATE SKIP LOCKED` — работает на
нескольких репликах API без брокера (Kafka — драйвер `run_bus.KafkaBus`, Фаза 2).

Правила честной очереди:
  * не больше ABOP_USER_CONCURRENT (по умолчанию 1) одновременных заданий на пользователя;
  * дедуп: одинаковое задание (dedupe_key), пока оно ждёт/выполняется, возвращается повторно;
  * бэкпрешер по памяти: воркер не берёт задание, пока RSS процесса выше ABOP_RUN_MEM_SOFT_MB;
  * таймаут ABOP_RUN_TIMEOUT на попытку; heartbeat; зависшие перекладываются один раз, затем failed;
  * отмена: queued → cancelled сразу; running → cancel_requested (навыки, что не начались, не стартуют);
  * HITL-возобновление: задание может уйти в `awaiting_hitl` с чекпоинтом (цепочка: индекс шага и
    контекст) — решение оператора по заявке возвращает его в очередь, воркер продолжает с чекпоинта.
Статусы: queued → running → done | failed | cancelled | awaiting_hitl (→ queued).
Память-фолбэк без Postgres — для локальной разработки.
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime as _dt
import json
import os
import secrets
import time

from . import observability as obs
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'run',       -- run | pipeline
    agent_id      TEXT NOT NULL,
    actor         TEXT NOT NULL,
    dedupe_key    TEXT,
    payload       JSONB NOT NULL,
    checkpoint    JSONB,                             -- прогресс/точка возобновления (цепочка)
    status        TEXT NOT NULL DEFAULT 'queued',    -- queued | running | awaiting_hitl | done | failed | cancelled
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
ALTER TABLE run_jobs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'run';
ALTER TABLE run_jobs ADD COLUMN IF NOT EXISTS checkpoint JSONB;
CREATE INDEX IF NOT EXISTS idx_run_jobs_status ON run_jobs (status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_run_jobs_actor ON run_jobs (actor, status);
CREATE INDEX IF NOT EXISTS idx_run_jobs_dedupe ON run_jobs (dedupe_key) WHERE status IN ('queued','running','awaiting_hitl');
"""

WORKERS = max(1, int(os.getenv("ABOP_RUN_WORKERS", "2")))
USER_CONCURRENT = max(1, int(os.getenv("ABOP_USER_CONCURRENT", "1")))
RUN_TIMEOUT = max(30, int(os.getenv("ABOP_RUN_TIMEOUT", "600")))
MEM_SOFT_MB = max(0, int(os.getenv("ABOP_RUN_MEM_SOFT_MB", "1500")))
STALE_AFTER = max(60, int(os.getenv("ABOP_RUN_STALE_SEC", str(RUN_TIMEOUT + 120))))
MAX_ATTEMPTS = 2
ACTIVE = ("queued", "running", "awaiting_hitl")

# текущее задание в контексте исполнения (execute_agent_run → доставка → HITL-заявка знает job_id)
CURRENT_JOB: contextvars.ContextVar[str | None] = contextvars.ContextVar("abop_current_job", default=None)

_MEM: dict[str, dict] = {}
_MEM_LOCK = asyncio.Lock()
_CANCEL_FLAGS: set[str] = set()


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def rss_mb() -> float:
    """RSS текущего процесса в МБ (Linux /proc; иначе resource; иначе psutil; иначе 0)."""
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


def cancel_requested(job_id: str | None) -> bool:
    return bool(job_id) and job_id in _CANCEL_FLAGS


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


_COLS = ("id,kind,agent_id,actor,dedupe_key,payload,checkpoint,status,priority,attempts,locked_by,heartbeat_at,"
         "cancel_requested,run_id,error,created_at,started_at,finished_at")
_KEYS = _COLS.split(",")


def _row(r) -> dict:
    d = dict(zip(_KEYS, r))
    for k in ("heartbeat_at", "created_at", "started_at", "finished_at"):
        d[k] = _iso(d.get(k))
    return d


async def enqueue(*, agent_id: str, actor: str, payload: dict, dedupe_key: str | None = None,
                  priority: int = 5, kind: str = "run") -> dict:
    """Положить задание. Если такое же (dedupe_key) уже активно — вернуть его (deduped)."""
    if dedupe_key:
        cur = await find_active(dedupe_key)
        if cur:
            cur["deduped"] = True
            return cur
    jid = "job-" + secrets.token_hex(6)
    if not _has_pg():
        async with _MEM_LOCK:
            _MEM[jid] = {"id": jid, "kind": kind, "agent_id": agent_id, "actor": actor, "dedupe_key": dedupe_key,
                         "payload": payload, "checkpoint": None, "status": "queued", "priority": priority,
                         "attempts": 0, "locked_by": None, "heartbeat_at": None, "cancel_requested": False,
                         "run_id": None, "error": None, "created_at": _iso(_now()), "started_at": None,
                         "finished_at": None}
            return dict(_MEM[jid])
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO run_jobs (id,kind,agent_id,actor,dedupe_key,payload,priority) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (jid, kind, agent_id, actor, dedupe_key, json.dumps(payload, ensure_ascii=False), priority))
    return await get(jid)


async def find_active(dedupe_key: str) -> dict | None:
    if not _has_pg():
        for j in _MEM.values():
            if j.get("dedupe_key") == dedupe_key and j["status"] in ACTIVE:
                return dict(j)
        return None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            f"SELECT {_COLS} FROM run_jobs WHERE dedupe_key=%s AND status IN ('queued','running','awaiting_hitl') "
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
        q = sorted([x for x in _MEM.values() if x["status"] == "queued"], key=lambda x: (x["priority"], x["created_at"]))
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
            running_by: dict = {}
            for x in _MEM.values():
                if x["status"] == "running":
                    running_by[x["actor"]] = running_by.get(x["actor"], 0) + 1
            q = sorted([x for x in _MEM.values() if x["status"] == "queued"], key=lambda x: (x["priority"], x["created_at"]))
            for x in q:
                if running_by.get(x["actor"], 0) < USER_CONCURRENT:
                    x.update({"status": "running", "locked_by": worker_id, "attempts": x["attempts"] + 1,
                              "started_at": x.get("started_at") or _iso(_now()), "heartbeat_at": _iso(_now())})
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
                "started_at=COALESCE(started_at, now()), heartbeat_at=now() WHERE id=%s", (worker_id, jid))
    return await get(jid)


async def heartbeat(job_id: str) -> bool:
    """Продлить lock; False — если запрошена отмена."""
    if not _has_pg():
        j = _MEM.get(job_id)
        if j:
            j["heartbeat_at"] = _iso(_now())
            return not j.get("cancel_requested")
        return True
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("UPDATE run_jobs SET heartbeat_at=now() WHERE id=%s RETURNING cancel_requested", (job_id,))
        r = await cur.fetchone()
    return not (r and r[0])


async def set_checkpoint(job_id: str, checkpoint: dict) -> None:
    """Сохранить прогресс (цепочка: пройденные шаги) — виден в статусе, переживает рестарт воркера."""
    if not _has_pg():
        j = _MEM.get(job_id)
        if j:
            j["checkpoint"] = checkpoint
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("UPDATE run_jobs SET checkpoint=%s WHERE id=%s", (json.dumps(checkpoint, ensure_ascii=False), job_id))


async def finish(job_id: str, run_id: str | None, checkpoint: dict | None = None) -> None:
    if not _has_pg():
        j = _MEM.get(job_id)
        if j:
            j.update({"status": "done", "run_id": run_id, "finished_at": _iso(_now()), "locked_by": None})
            if checkpoint is not None:
                j["checkpoint"] = checkpoint
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("UPDATE run_jobs SET status='done', run_id=%s, finished_at=now(), locked_by=NULL, "
                           "checkpoint=COALESCE(%s, checkpoint) WHERE id=%s",
                           (run_id, json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None, job_id))


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


async def await_hitl(job_id: str, checkpoint: dict) -> None:
    """Задание ждёт решения оператора: слот воркера освобождается, чекпоинт сохранён."""
    if not _has_pg():
        j = _MEM.get(job_id)
        if j:
            j.update({"status": "awaiting_hitl", "checkpoint": checkpoint, "locked_by": None})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("UPDATE run_jobs SET status='awaiting_hitl', checkpoint=%s, locked_by=NULL WHERE id=%s",
                           (json.dumps(checkpoint, ensure_ascii=False), job_id))


async def resume(job_id: str, decision: str, hitl_id: str = "") -> dict | None:
    """Решение по HITL-заявке возвращает задание из awaiting_hitl в очередь; решение кладётся в чекпоинт."""
    j = await get(job_id)
    if not j or j["status"] != "awaiting_hitl":
        return j
    cp = dict(j.get("checkpoint") or {})
    dec = dict(cp.get("hitl_decisions") or {})
    if hitl_id:
        dec[hitl_id] = decision
    cp["hitl_decisions"] = dec
    cp["resumed_at"] = _iso(_now())
    if not _has_pg():
        _MEM[job_id].update({"status": "queued", "checkpoint": cp})
        return dict(_MEM[job_id])
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("UPDATE run_jobs SET status='queued', checkpoint=%s WHERE id=%s AND status='awaiting_hitl'",
                           (json.dumps(cp, ensure_ascii=False), job_id))
    obs.inc("abop_run_jobs_total", status="resumed")
    return await get(job_id)


async def cancel(job_id: str, actor: str | None = None, admin: bool = False) -> dict | None:
    """queued/awaiting_hitl → cancelled; running → cancel_requested. Только автор или admin."""
    j = await get(job_id)
    if not j:
        return None
    if not admin and actor and j["actor"] != actor:
        return {"id": job_id, "status": j["status"], "denied": True}
    if j["status"] in ("queued", "awaiting_hitl"):
        if not _has_pg():
            _MEM[job_id].update({"status": "cancelled", "finished_at": _iso(_now())})
        else:
            from .db import _conn
            async with _conn() as conn:
                await conn.execute("UPDATE run_jobs SET status='cancelled', finished_at=now() WHERE id=%s "
                                   "AND status IN ('queued','awaiting_hitl')", (job_id,))
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
    """running без heartbeat дольше STALE_AFTER: первая попытка → назад в очередь, потом failed."""
    if not _has_pg():
        n = 0
        cutoff = _now() - _dt.timedelta(seconds=STALE_AFTER)
        for j in _MEM.values():
            if j["status"] == "running" and j.get("heartbeat_at") and _dt.datetime.fromisoformat(j["heartbeat_at"]) < cutoff:
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
        return cur.rowcount or 0


async def stats() -> dict:
    if not _has_pg():
        by: dict = {}
        for j in _MEM.values():
            by[j["status"]] = by.get(j["status"], 0) + 1
        return {"by_status": by, "workers": WORKERS, "user_concurrent": USER_CONCURRENT, "timeout_sec": RUN_TIMEOUT,
                "avg_wait_sec_1h": 0.0, "rss_mb": round(rss_mb(), 1), "mem_soft_mb": MEM_SOFT_MB,
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


async def publish_gauges() -> None:
    """Глубина очереди/RSS/воркеры → Prometheus gauge (для Grafana и алертов)."""
    st = await stats()
    for s in ("queued", "running", "awaiting_hitl", "done", "failed", "cancelled"):
        obs.gauge("abop_run_queue_jobs", st["by_status"].get(s, 0), status=s)
    obs.gauge("abop_run_queue_depth", st["by_status"].get("queued", 0))
    obs.gauge("abop_run_workers", st["workers"])
    obs.gauge("abop_process_rss_mb", st["rss_mb"])
    obs.gauge("abop_run_memory_pressure", 1 if st["memory_pressure"] else 0)
    obs.gauge("abop_run_queue_avg_wait_seconds_1h", st.get("avg_wait_sec_1h") or 0)


async def gauges_loop(period: float = 15.0) -> None:
    while True:
        try:
            await publish_gauges()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(period)


async def list_jobs(actor: str | None = None, limit: int = 50) -> list[dict]:
    if not _has_pg():
        rows = [dict(j) for j in _MEM.values() if not actor or j["actor"] == actor]
        rows.sort(key=lambda x: x["created_at"] or "", reverse=True)
        return rows[:limit]
    from .db import _conn
    async with _conn() as conn:
        if actor:
            cur = await conn.execute(f"SELECT {_COLS} FROM run_jobs WHERE actor=%s ORDER BY created_at DESC LIMIT %s", (actor, limit))
        else:
            cur = await conn.execute(f"SELECT {_COLS} FROM run_jobs ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = await cur.fetchall()
    return [_row(r) for r in rows]


async def find_awaiting_by_hitl(hitl_id: str) -> dict | None:
    """Задание, ждущее решения по этой заявке (по чекпоинту)."""
    if not _has_pg():
        for j in _MEM.values():
            if j["status"] == "awaiting_hitl" and hitl_id in ((j.get("checkpoint") or {}).get("hitl_ids") or []):
                return dict(j)
        return None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            f"SELECT {_COLS} FROM run_jobs WHERE status='awaiting_hitl' AND checkpoint->'hitl_ids' ? %s LIMIT 1", (hitl_id,))
        r = await cur.fetchone()
    return _row(r) if r else None


def public(job: dict, position_: int = 0) -> dict:
    """Ответ клиенту без внутренностей (payload/locked_by)."""
    cp = job.get("checkpoint") or {}
    return {"job_id": job["id"], "kind": job.get("kind") or "run", "status": job["status"], "agent_id": job["agent_id"],
            "run_id": job.get("run_id"), "error": job.get("error"), "position": position_,
            "created_at": job.get("created_at"), "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
            "cancel_requested": bool(job.get("cancel_requested")), "attempts": job.get("attempts", 0),
            "progress": {"step": cp.get("step"), "steps_total": cp.get("steps_total"),
                         "steps_done": len(cp.get("steps") or []), "awaiting_hitl": cp.get("hitl_ids") or []} if cp else None}


async def worker_loop(worker_id: str, handler, *, on_done=None) -> None:
    """Вечный цикл воркера. handler(job) → {"run_id"?, "checkpoint"?} | {"await_hitl": checkpoint}.
    Не падает на ошибках задания; таймаут, heartbeat и отмена — здесь."""
    import logging
    log = logging.getLogger("abop.run_queue")
    idle = 0.0
    while True:
        try:
            if memory_pressure():
                obs.inc("abop_run_backpressure_total")
                await asyncio.sleep(3)
                continue
            job = await claim(worker_id)
            if not job:
                idle = min(3.0, idle + 0.25)
                await asyncio.sleep(idle)
                continue
            idle = 0.0
            try:
                created = _dt.datetime.fromisoformat(job["created_at"]) if job.get("created_at") else None
                if created:
                    obs.observe("abop_run_queue_wait_seconds", max(0.0, (_now() - created).total_seconds()))
            except Exception:  # noqa: BLE001
                pass
            t0 = time.perf_counter()
            hb_task = None
            tok = CURRENT_JOB.set(job["id"])
            try:
                async def _hb():
                    while True:
                        await asyncio.sleep(15)
                        try:
                            if not await heartbeat(job["id"]):
                                _CANCEL_FLAGS.add(job["id"])
                        except Exception:  # noqa: BLE001
                            pass
                hb_task = asyncio.create_task(_hb())
                out = await asyncio.wait_for(handler(job), timeout=RUN_TIMEOUT) or {}
                if out.get("await_hitl"):
                    await await_hitl(job["id"], out["await_hitl"])
                    obs.inc("abop_run_jobs_total", status="awaiting_hitl")
                else:
                    await finish(job["id"], out.get("run_id"), out.get("checkpoint"))
                    obs.inc("abop_run_jobs_total", status="done")
                    if on_done:
                        try:
                            await on_done(job, out)
                        except Exception:  # noqa: BLE001
                            pass
            except asyncio.TimeoutError:
                await fail(job["id"], f"таймаут ({RUN_TIMEOUT} с)")
                obs.inc("abop_run_jobs_total", status="failed")
            except Exception as ex:  # noqa: BLE001
                msg = f"{type(ex).__name__}: {ex}"
                log.exception("job failed: %s", job["id"])
                rq = job.get("attempts", 1) < MAX_ATTEMPTS
                await fail(job["id"], msg, requeue=rq)
                obs.inc("abop_run_jobs_total", status="requeued" if rq else "failed")
            finally:
                CURRENT_JOB.reset(tok)
                if hb_task:
                    hb_task.cancel()
                _CANCEL_FLAGS.discard(job["id"])
                obs.observe("abop_run_job_seconds", time.perf_counter() - t0, kind=job.get("kind") or "run")
                log.info("job %s (%s) finished in %.1fs", job["id"], job.get("kind"), time.perf_counter() - t0)
        except Exception:  # noqa: BLE001 — сам цикл не умирает
            log.exception("worker loop error")
            await asyncio.sleep(2)
