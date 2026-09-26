"""RunBus — событийная шина ABOP за единым интерфейсом (CONCEPT_SCALING_OBSERVABILITY §6, Фаза 2 / Блок 3).

Источник истины по заданию всегда `run_queue` (таблица run_jobs). Шина доставляет сигналы и события:

  * `PgBus`    — Фаза 1 (по умолчанию): воркеры сами опрашивают очередь; publish — no-op, события
                 систем недоступны (инструменты навыков честно отвечают «шина не подключена»).
  * `KafkaBus` — Фаза 2: Redpanda/Kafka (`ABOP_BUS=kafka`, `ABOP_KAFKA_BROKERS`, SASL/SCRAM через
                 `ABOP_KAFKA_SASL_USER/PASSWORD`). Топики:
                   abop.runs.requests / abop.runs.results       — задания и результаты прогонов (ключ = contract_audit_id)
                   abop.<система>.events / abop.<система>.commands — ПАРА НА СИСТЕМУ из реестра systems_store:
                       events   — входящие события системы → триггер-узлы агентов (тип события в поле `type`)
                       commands — исходящие действия навыков (инструмент `bus_publish`) → коннектор системы
                   abop.dlq                                     — единый DLQ: заголовки source-topic / error / x-trace-id
                 Топики создаются идемпотентно при старте (`ensure_topics`). Брокер недоступен → откат на PgBus.

Схема сообщения события/команды: {"schema":"abop.event/1.0","system","type","payload","ts","trace_id","actor"}.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import uuid

from . import observability as obs
from . import run_queue

log = logging.getLogger("abop.run_bus")

BUS_KIND = (os.getenv("ABOP_BUS") or "pg").strip().lower()
KAFKA_BROKERS = [b.strip() for b in (os.getenv("ABOP_KAFKA_BROKERS") or "").split(",") if b.strip()]
TOPIC_REQ = os.getenv("ABOP_KAFKA_TOPIC_REQUESTS", "abop.runs.requests")
TOPIC_RES = os.getenv("ABOP_KAFKA_TOPIC_RESULTS", "abop.runs.results")
TOPIC_DLQ = os.getenv("ABOP_KAFKA_TOPIC_DLQ", "abop.dlq")
GROUP = os.getenv("ABOP_KAFKA_GROUP", "abop-run-workers")
EVENTS_GROUP = os.getenv("ABOP_KAFKA_EVENTS_GROUP", "abop-triggers")
SASL_USER = os.getenv("ABOP_KAFKA_SASL_USER") or ""
SASL_PASS = os.getenv("ABOP_KAFKA_SASL_PASSWORD") or ""
PARTITIONS = max(1, int(os.getenv("ABOP_KAFKA_PARTITIONS", "3")))
_SYS_RE = re.compile(r"[^a-z0-9._-]+")


def system_topics(system_id: str) -> tuple[str, str]:
    """Пара топиков системы: (events, commands). Имя системы нормализуется под правила Kafka."""
    sid = _SYS_RE.sub("-", str(system_id or "").strip().lower()).strip("-.") or "unknown"
    return f"abop.{sid}.events", f"abop.{sid}.commands"


def _kw() -> dict:
    kw: dict = {"bootstrap_servers": KAFKA_BROKERS}
    if SASL_USER:
        kw.update(security_protocol="SASL_PLAINTEXT", sasl_mechanism="SCRAM-SHA-256",
                  sasl_plain_username=SASL_USER, sasl_plain_password=SASL_PASS)
    return kw


def envelope(system: str, etype: str, payload: dict | None, *, actor: str = "", trace_id: str = "",
             schema: str = "abop.event/1.0") -> dict:
    return {"schema": schema, "id": uuid.uuid4().hex[:16], "system": system, "type": etype,
            "payload": payload or {}, "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "trace_id": trace_id or obs.current_trace_id() if hasattr(obs, "current_trace_id") else trace_id,
            "actor": actor}


class PgBus:
    """Фаза 1: сигнал = сама таблица. Публикации — no-op, чтение — пусто."""
    kind = "pg"
    active = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def ensure_topics(self, names: list[str]) -> list[str]:
        return []

    async def publish(self, topic: str, msg: dict, *, key: str = "", headers: list | None = None) -> bool:
        return False

    async def publish_request(self, job: dict, trace_id: str = "") -> None:
        return None

    async def publish_result(self, job: dict, out: dict | None, error: str = "") -> None:
        return None

    async def publish_dlq(self, source_topic: str, error: str, payload: dict | None, trace_id: str = "") -> bool:
        return False

    async def tail(self, topic: str, limit: int = 20) -> list[dict]:
        return []

    async def next_job_hint(self) -> str | None:
        return None

    def topics(self) -> list[str]:
        return []


class KafkaBus(PgBus):
    """Фаза 2: Redpanda/Kafka. Воркер получает job_id из топика и забирает задание из run_jobs
    (claim идемпотентен). События систем читает `events_loop`, команды навыков уходят в commands."""
    kind = "kafka"

    def __init__(self) -> None:
        self._producer = None
        self._consumer = None
        self._ok = False
        self._topics: set[str] = set()

    @property
    def active(self) -> bool:  # type: ignore[override]
        return self._ok

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
            self._producer = AIOKafkaProducer(acks="all", enable_idempotence=True, request_timeout_ms=10000, **_kw())
            await self._producer.start()
            await self.ensure_topics([TOPIC_REQ, TOPIC_RES, TOPIC_DLQ])
            self._consumer = AIOKafkaConsumer(TOPIC_REQ, group_id=GROUP, enable_auto_commit=True,
                                              auto_offset_reset="earliest", **_kw())
            await self._consumer.start()
            self._ok = True
            obs.log_event("info", "bus.kafka.started", brokers=",".join(KAFKA_BROKERS), topic=TOPIC_REQ,
                          sasl=bool(SASL_USER))
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

    async def ensure_topics(self, names: list[str]) -> list[str]:
        """Создать недостающие топики (идемпотентно). Возвращает список созданных."""
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic  # type: ignore
        created: list[str] = []
        adm = AIOKafkaAdminClient(**_kw())
        await adm.start()
        try:
            have = set(await adm.list_topics())
            missing = [n for n in names if n not in have]
            if missing:
                try:
                    await adm.create_topics([NewTopic(n, num_partitions=PARTITIONS, replication_factor=1) for n in missing])
                    created = missing
                except Exception as ex:  # noqa: BLE001 — гонка с другой репликой: уже создан
                    log.info("create_topics: %s", ex)
            self._topics = have | set(names)
        finally:
            await adm.close()
        if created:
            obs.log_event("info", "bus.topics.created", topics=",".join(created))
        return created

    def topics(self) -> list[str]:
        return sorted(t for t in self._topics if not t.startswith("_"))

    async def publish(self, topic: str, msg: dict, *, key: str = "", headers: list | None = None) -> bool:
        if not self._ok or self._producer is None:
            return False
        try:
            hdrs = [(k, (v if isinstance(v, bytes) else str(v).encode())) for k, v in (headers or [])]
            await self._producer.send_and_wait(topic, json.dumps(msg, ensure_ascii=False).encode(),
                                               key=(str(key).encode() if key else None), headers=hdrs)
            obs.inc("abop_bus_messages_total", topic=topic)
            return True
        except Exception as ex:  # noqa: BLE001
            log.warning("kafka publish %s failed: %s", topic, ex)
            obs.inc("abop_bus_errors_total", topic=topic)
            return False

    async def publish_request(self, job: dict, trace_id: str = "") -> None:
        key = (job.get("payload") or {}).get("contract_audit_id") or job.get("agent_id") or job["id"]
        msg = {"schema": "abop.run_request/1.0", "job_id": job["id"], "agent_id": job["agent_id"],
               "actor": job.get("actor"), "kind": job.get("kind") or "run",
               "contract_audit_id": (job.get("payload") or {}).get("contract_audit_id")}
        await self.publish(TOPIC_REQ, msg, key=str(key), headers=[("x-trace-id", trace_id)] if trace_id else None)

    async def publish_result(self, job: dict, out: dict | None, error: str = "") -> None:
        saved = (out or {}).get("saved") or {}
        msg = {"schema": "abop.run_result/1.0", "job_id": job["id"], "agent_id": job["agent_id"],
               "kind": job.get("kind") or "run", "run_id": (out or {}).get("run_id") or saved.get("id"),
               "error": error or None}
        if error:
            await self.publish_dlq(TOPIC_REQ, error, msg)
        else:
            await self.publish(TOPIC_RES, msg, key=str(job["id"]))

    async def publish_dlq(self, source_topic: str, error: str, payload: dict | None, trace_id: str = "") -> bool:
        msg = {"schema": "abop.dlq/1.0", "source_topic": source_topic, "error": str(error)[:2000],
               "payload": payload or {}, "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "trace_id": trace_id}
        ok = await self.publish(TOPIC_DLQ, msg, key=source_topic,
                                headers=[("source-topic", source_topic), ("error", str(error)[:200]), ("x-trace-id", trace_id or "")])
        if ok:
            obs.inc("abop_bus_dlq_total", source=source_topic)
        return ok

    async def tail(self, topic: str, limit: int = 20) -> list[dict]:
        """Последние N сообщений топика (без consumer-group): для /api/bus/dlq и отладки коннекторов.
        Подписка на топик в конструкторе — так метаданные и назначение партиций приходят при start()."""
        if not self._ok:
            return []
        from aiokafka import AIOKafkaConsumer  # type: ignore
        c = AIOKafkaConsumer(topic, enable_auto_commit=False, auto_offset_reset="latest", **_kw())
        await c.start()
        out: list[dict] = []
        try:
            tps = []
            for _ in range(30):                       # без group_id партиции назначаются сразу после start, но не мгновенно
                tps = list(c.assignment())
                if tps:
                    break
                await asyncio.sleep(0.1)
            if not tps:
                return []
            ends = await c.end_offsets(tps)
            begs = await c.beginning_offsets(tps)
            per = max(1, limit // max(1, len(tps)) + 1)
            for tp in tps:
                c.seek(tp, max(begs[tp], ends[tp] - per))
            deadline = asyncio.get_event_loop().time() + 4.0
            while asyncio.get_event_loop().time() < deadline and len(out) < limit * 2:
                batch = await c.getmany(timeout_ms=800, max_records=limit * 2)
                if not batch:
                    break
                for tp, msgs in batch.items():
                    for m in msgs:
                        try:
                            val = json.loads(m.value.decode())
                        except Exception:  # noqa: BLE001
                            val = {"raw": m.value.decode(errors="replace")[:500]}
                        out.append({"topic": topic, "partition": tp.partition, "offset": m.offset,
                                    "ts": _dt.datetime.fromtimestamp(m.timestamp / 1000, _dt.timezone.utc).isoformat(timespec="seconds") if m.timestamp else None,
                                    "key": m.key.decode(errors="replace") if m.key else None,
                                    "headers": {k: v.decode(errors="replace") for k, v in (m.headers or [])}, "value": val})
        finally:
            try:
                await c.stop()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — quirk aiokafka при stop без group_id
                pass
        out.sort(key=lambda x: (x.get("ts") or "", x["offset"]), reverse=True)
        return out[:limit]

    async def next_job_hint(self) -> str | None:
        if not self._ok or self._consumer is None:
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

    async def events_loop(self, handler) -> None:
        """Слушает abop.<система>.events (все системы, pattern) в consumer-group триггеров; handler(system, event, headers)
        вызывается на каждое событие, ошибка обработчика → DLQ (событие не теряется)."""
        from aiokafka import AIOKafkaConsumer  # type: ignore
        c = AIOKafkaConsumer(group_id=EVENTS_GROUP, enable_auto_commit=True, auto_offset_reset="latest", **_kw())
        await c.start()
        c.subscribe(pattern=r"^abop\..+\.events$")
        obs.log_event("info", "bus.events.listening", group=EVENTS_GROUP)
        try:
            while True:
                batch = await c.getmany(timeout_ms=1000, max_records=20)
                for tp, msgs in batch.items():
                    for m in msgs:
                        hdrs = {k: v.decode(errors="replace") for k, v in (m.headers or [])}
                        try:
                            ev = json.loads(m.value.decode())
                        except Exception as ex:  # noqa: BLE001
                            await self.publish_dlq(tp.topic, f"bad json: {ex}", {"raw": m.value.decode(errors="replace")[:500]})
                            continue
                        sysid = ev.get("system") or tp.topic.split(".")[1]
                        obs.inc("abop_bus_events_total", system=sysid)
                        try:
                            await handler(sysid, ev, hdrs)
                        except Exception as ex:  # noqa: BLE001
                            log.exception("bus event handler failed")
                            await self.publish_dlq(tp.topic, f"{type(ex).__name__}: {ex}", ev, hdrs.get("x-trace-id", ""))
        finally:
            await c.stop()


_bus: PgBus | None = None


def bus() -> PgBus:
    global _bus
    if _bus is None:
        _bus = KafkaBus() if BUS_KIND == "kafka" else PgBus()
    return _bus


async def start() -> None:
    await bus().start()


async def ensure_system_topics(system_ids: list[str]) -> dict[str, tuple[str, str]]:
    """Пара топиков на каждую систему реестра (идемпотентно). Возвращает {sid: (events, commands)}."""
    pairs = {sid: system_topics(sid) for sid in system_ids if sid}
    names = [t for p in pairs.values() for t in p]
    b = bus()
    if getattr(b, "active", False) and names:
        try:
            await b.ensure_topics(names)
        except Exception as ex:  # noqa: BLE001
            log.warning("ensure_system_topics: %s", ex)
    return pairs


def describe() -> dict:
    b = bus()
    return {"bus": b.kind, "kafka_active": bool(getattr(b, "active", False)),
            "brokers": KAFKA_BROKERS if b.kind == "kafka" else [], "sasl": bool(SASL_USER),
            "topics": {"requests": TOPIC_REQ, "results": TOPIC_RES, "dlq": TOPIC_DLQ} if b.kind == "kafka" else {},
            "known_topics": b.topics()}


async def publish_event(system: str, etype: str, payload: dict | None, *, actor: str = "", trace_id: str = "") -> dict:
    """Событие системы (входящее: коннектор/вебхук/экспорт 1С → триггеры агентов)."""
    ev = envelope(system, etype, payload, actor=actor, trace_id=trace_id)
    ok = await bus().publish(system_topics(system)[0], ev, key=etype, headers=[("x-trace-id", trace_id or "")])
    return {"ok": ok, "topic": system_topics(system)[0], "event": ev}


async def publish_command(system: str, ctype: str, payload: dict | None, *, actor: str = "", trace_id: str = "") -> dict:
    """Команда системе (исходящее действие навыка → коннектор системы)."""
    cmd = envelope(system, ctype, payload, actor=actor, trace_id=trace_id, schema="abop.command/1.0")
    ok = await bus().publish(system_topics(system)[1], cmd, key=ctype, headers=[("x-trace-id", trace_id or "")])
    return {"ok": ok, "topic": system_topics(system)[1], "command": cmd}


async def worker_loop(worker_id: str, handler) -> None:
    """Воркер поверх run_queue: при KafkaBus сначала ждёт сигнал, но всегда умеет опросить PG сам
    (задания, положенные до старта брокера или при его сбое, не теряются). Ошибки → DLQ."""
    b = bus()

    async def _on_done(job, out):
        await b.publish_result(job, out)

    async def _on_error(job, error):
        await b.publish_result(job, None, error=error)

    if b.kind == "kafka":
        async def _hinting():
            while True:
                try:
                    await b.next_job_hint()
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(1)
        asyncio.create_task(_hinting())
    await run_queue.worker_loop(worker_id, handler, on_done=_on_done, on_error=_on_error)
