"""RunBus — транспорт заданий прогонов за единым интерфейсом (CONCEPT_SCALING_OBSERVABILITY §6, Фаза 1→2).

Источник истины по заданию всегда `run_queue` (таблица run_jobs: статус, позиция, результат, отмена).
Шина лишь доставляет сигнал «есть задание» воркерам и «есть результат» подписчикам:

  * `PgBus`    — Фаза 1 (по умолчанию): воркеры сами опрашивают очередь (`FOR UPDATE SKIP LOCKED`),
                 ноль новой инфраструктуры, работает на нескольких репликах.
  * `KafkaBus` — Фаза 2: топики `abop.runs.requests` (ключ партиции — contract_audit_id, порядок в
                 рамках процесса), `abop.runs.results`, `abop.runs.dlq`; consumer-group воркеров;
                 заголовок `traceparent`/`x-trace-id` пробрасывает trace_id. Идемпотентность — job_id.
                 Включается `ABOP_BUS=kafka` + `ABOP_KAFKA_BROKERS`; нужен пакет `aiokafka`.
                 Брокера нет/недоступен → честный лог и откат на PgBus (задание не теряется — оно в PG).

Смена драйвера не меняет API (`POST /api/runs {async:true}` → 202, `GET /api/runs/jobs/{id}`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from . import observability as obs
from . import run_queue

log = logging.getLogger("abop.run_bus")

BUS_KIND = (os.getenv("ABOP_BUS") or "pg").strip().lower()
KAFKA_BROKERS = [b.strip() for b in (os.getenv("ABOP_KAFKA_BROKERS") or "").split(",") if b.strip()]
TOPIC_REQ = os.getenv("ABOP_KAFKA_TOPIC_REQUESTS", "abop.runs.requests")
TOPIC_RES = os.getenv("ABOP_KAFKA_TOPIC_RESULTS", "abop.runs.results")
TOPIC_DLQ = os.getenv("ABOP_KAFKA_TOPIC_DLQ", "abop.runs.dlq")
GROUP = os.getenv("ABOP_KAFKA_GROUP", "abop-run-workers")


class PgBus:
    """Фаза 1: сигнал = сама таблица. publish — no-op (задание уже в run_jobs)."""
    kind = "pg"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def publish_request(self, job: dict, trace_id: str = "") -> None:
        return None

    async def publish_result(self, job: dict, out: dict | None, error: str = "") -> None:
        return None

    async def next_job_hint(self) -> str | None:
        """Нет внешнего сигнала — воркер опрашивает очередь сам."""
        return None


class KafkaBus(PgBus):
    """Фаза 2: сигналы через Kafka. Воркер получает job_id из топика и забирает задание из run_jobs
    (claim идемпотентен: второй воркер той же группы задание не получит — оно уже running)."""
    kind = "kafka"

    def __init__(self) -> None:
        self._producer = None
        self._consumer = None
        self._ok = False

    async def start(self) -> None:
        try:
            from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore
        except ImportError:
            log.warning("ABOP_BUS=kafka, но пакет aiokafka не установлен — работаю через PgBus")
            return
        if not KAFKA_BROKERS:
            log.warning("ABOP_BUS=kafka без ABOP_KAFKA_BROKERS — работаю через PgBus")
            return
        try:
            self._producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BROKERS, acks="all",
                                              enable_idempotence=True, request_timeout_ms=10000)
            await self._producer.start()
            self._consumer = AIOKafkaConsumer(TOPIC_REQ, bootstrap_servers=KAFKA_BROKERS, group_id=GROUP,
                                              enable_auto_commit=True, auto_offset_reset="earliest")
            await self._consumer.start()
            self._ok = True
            obs.log_event("info", "bus.kafka.started", brokers=",".join(KAFKA_BROKERS), topic=TOPIC_REQ)
        except Exception as ex:  # noqa: BLE001 — брокер недоступен → PgBus, задания в PG не пропадут
            log.warning("Kafka недоступна (%s) — работаю через PgBus", ex)
            await self.stop()

    async def stop(self) -> None:
        for c in (self._consumer, self._producer):
            try:
                if c is not None:
                    await c.stop()
            except Exception:  # noqa: BLE001
                pass
        self._consumer = self._producer = None
        self._ok = False

    @property
    def active(self) -> bool:
        return self._ok

    async def publish_request(self, job: dict, trace_id: str = "") -> None:
        if not self._ok:
            return
        key = (job.get("payload") or {}).get("contract_audit_id") or job.get("agent_id") or job["id"]
        msg = {"schema": "abop.run_request/1.0", "job_id": job["id"], "agent_id": job["agent_id"],
               "actor": job["actor"], "contract_audit_id": (job.get("payload") or {}).get("contract_audit_id")}
        headers = [("x-trace-id", (trace_id or "").encode())] if trace_id else []
        try:
            await self._producer.send_and_wait(TOPIC_REQ, json.dumps(msg, ensure_ascii=False).encode(),
                                               key=str(key).encode(), headers=headers)
            obs.inc("abop_bus_messages_total", topic=TOPIC_REQ)
        except Exception as ex:  # noqa: BLE001
            log.warning("kafka publish_request failed: %s", ex)

    async def publish_result(self, job: dict, out: dict | None, error: str = "") -> None:
        if not self._ok:
            return
        topic = TOPIC_DLQ if error else TOPIC_RES
        saved = (out or {}).get("saved") or {}
        msg = {"schema": "abop.run_result/1.0", "job_id": job["id"], "agent_id": job["agent_id"],
               "run_id": saved.get("id"), "error": error or None,
               "verdict_ok": bool(((out or {}).get("result") or {}).get("verdict", {}).get("ok")) if out else None}
        try:
            await self._producer.send_and_wait(topic, json.dumps(msg, ensure_ascii=False).encode(),
                                               key=str(job["id"]).encode())
            obs.inc("abop_bus_messages_total", topic=topic)
        except Exception as ex:  # noqa: BLE001
            log.warning("kafka publish_result failed: %s", ex)

    async def next_job_hint(self) -> str | None:
        """Ждать сообщение из requests и вернуть job_id (или None по таймауту — воркер опросит PG сам)."""
        if not self._ok:
            return None
        try:
            batch = await self._consumer.getmany(timeout_ms=1000, max_records=1)
            for _tp, msgs in batch.items():
                for m in msgs:
                    try:
                        return json.loads(m.value.decode()).get("job_id")
                    except Exception:  # noqa: BLE001
                        return None
        except Exception as ex:  # noqa: BLE001
            log.warning("kafka consume failed: %s", ex)
        return None


_bus: PgBus | None = None


def bus() -> PgBus:
    global _bus
    if _bus is None:
        _bus = KafkaBus() if BUS_KIND == "kafka" else PgBus()
    return _bus


async def start() -> None:
    await bus().start()


def describe() -> dict:
    b = bus()
    return {"bus": b.kind, "kafka_active": bool(getattr(b, "active", False)),
            "brokers": KAFKA_BROKERS if b.kind == "kafka" else [],
            "topics": {"requests": TOPIC_REQ, "results": TOPIC_RES, "dlq": TOPIC_DLQ} if b.kind == "kafka" else {}}


async def worker_loop(worker_id: str, executor, *, load_agent, load_contract) -> None:
    """Воркер поверх run_queue: при KafkaBus сначала ждёт сигнал, но всегда умеет опросить PG сам
    (задания, положенные до старта брокера или при его сбое, не теряются)."""
    b = bus()

    async def _on_done(job, out):
        await b.publish_result(job, out)

    if b.kind == "kafka":
        # подсказка из Kafka лишь ускоряет реакцию; claim остаётся единственным арбитром
        async def _hinting():
            while True:
                try:
                    await b.next_job_hint()
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(1)
        asyncio.create_task(_hinting())
    await run_queue.worker_loop(worker_id, executor, load_agent=load_agent, load_contract=load_contract,
                                on_done=_on_done)
