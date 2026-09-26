#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Коннектор-воркер ABOP (Блок 3, шаг 2): исполняет команды навыков из шины.

Читает все топики `abop.<система>.commands` (consumer-group `abop-connectors`), для каждой команды
`abop.command/1.0` {id, system, type, payload, actor, trace_id}:
  1. дедуп по id (SQLite в томе) — повторная доставка не создаёт второй задачи/письма;
  2. адаптер системы выполняет действие (Redmine / BookStack / Mailpit / 1С-запрос экспорта);
  3. результат публикуется событием `command.done` (или `command.failed`) в `abop.<система>.events`
     с payload {command_id, type, result|error, actor, trace_id};
  4. сетевые/5xx ошибки — ретраи с паузой, после исчерпания — `abop.dlq` (заголовки source-topic/error/x-trace-id).

Секреты внешних систем живут ТОЛЬКО здесь (не в процессе с LLM). Governance (ABAC/HITL/автономия)
проверяется на стороне ABOP до публикации команды — воркер исполняет всё, что дошло до топика.
/healthz и /metrics (Prometheus) на CONNECTOR_PORT (по умолчанию 9105).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import smtplib
import sqlite3
import time
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("abop.connector")

BROKERS = [b.strip() for b in (os.getenv("ABOP_KAFKA_BROKERS") or "127.0.0.1:9092").split(",") if b.strip()]
SASL_USER = os.getenv("ABOP_KAFKA_SASL_USER") or ""
SASL_PASS = os.getenv("ABOP_KAFKA_SASL_PASSWORD") or ""
GROUP = os.getenv("ABOP_CONNECTOR_GROUP", "abop-connectors")
TOPIC_DLQ = os.getenv("ABOP_KAFKA_TOPIC_DLQ", "abop.dlq")
RETRIES = max(0, int(os.getenv("ABOP_CONNECTOR_RETRIES", "3")))
DB_PATH = os.getenv("ABOP_CONNECTOR_DB", "/data/connector.sqlite")
PORT = int(os.getenv("CONNECTOR_PORT", "9105"))
DRY_RUN = os.getenv("ABOP_CONNECTOR_DRY_RUN", "0") == "1"   # ничего наружу не шлём, только событие command.done с пометкой

REDMINE_BASE = os.getenv("REDMINE_BASE", "http://5.129.192.63:3000").rstrip("/")
REDMINE_API_KEY = os.getenv("REDMINE_API_KEY", "")
REDMINE_PROJECT = os.getenv("REDMINE_PROJECT", "")
BOOKSTACK_URL = os.getenv("BOOKSTACK_URL", "http://5.129.192.63:6875").rstrip("/")
BOOKSTACK_TOKEN = os.getenv("BOOKSTACK_TOKEN", "")          # "id:secret"
MAILPIT_HOST = os.getenv("MAILPIT_HOST", "5.129.192.63")
MAILPIT_PORT = int(os.getenv("MAILPIT_PORT", "1025"))
MAIL_FROM = os.getenv("MAIL_FROM", "abop@demo.local")
ONE_C_DIR = os.getenv("ONE_C_REQUEST_DIR", "/data/1c-requests")

METRICS = {"consumed": 0, "done": 0, "failed": 0, "dlq": 0, "duplicate": 0, "by_type": {}}


def _kw() -> dict:
    kw: dict = {"bootstrap_servers": BROKERS}
    if SASL_USER:
        kw.update(security_protocol="SASL_PLAINTEXT", sasl_mechanism="SCRAM-SHA-256",
                  sasl_plain_username=SASL_USER, sasl_plain_password=SASL_PASS)
    return kw


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def jlog(event: str, **kw) -> None:
    log.info(json.dumps({"ts": round(time.time(), 3), "event": event, **kw}, ensure_ascii=False))


# ───────────── дедуп (идемпотентность)
class Dedupe:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = sqlite3.connect(path)
        self.c.execute("CREATE TABLE IF NOT EXISTS done (id TEXT PRIMARY KEY, topic TEXT, ts TEXT, status TEXT, result TEXT)")
        self.c.commit()

    def seen(self, cid: str) -> dict | None:
        r = self.c.execute("SELECT status, result FROM done WHERE id=?", (cid,)).fetchone()
        return {"status": r[0], "result": r[1]} if r else None

    def mark(self, cid: str, topic: str, status: str, result: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO done(id,topic,ts,status,result) VALUES(?,?,?,?,?)",
                       (cid, topic, _now(), status, (result or "")[:2000]))
        self.c.commit()


# ───────────── адаптеры: (system, type) → coroutine(payload) -> dict result
class Transient(Exception):
    """Временная ошибка (сеть/5xx) — можно повторить."""


async def _http(method: str, url: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.request(method, url, **kw)
        except httpx.TransportError as ex:
            raise Transient(f"сеть: {ex}") from ex
    if r.status_code >= 500:
        raise Transient(f"HTTP {r.status_code}: {r.text[:200]}")
    return r


async def redmine_create_issue(p: dict) -> dict:
    if not REDMINE_API_KEY:
        raise RuntimeError("нет REDMINE_API_KEY у коннектора")
    project = str(p.get("project") or REDMINE_PROJECT or "")
    if not project:
        raise RuntimeError("не задан проект Redmine (payload.project или REDMINE_PROJECT)")
    body = {"issue": {"project_id": project, "subject": str(p.get("subject") or "Задача от ABOP")[:250],
                      "description": str(p.get("description") or "")[:20000]}}
    if p.get("priority_id"):
        body["issue"]["priority_id"] = int(p["priority_id"])
    r = await _http("POST", REDMINE_BASE + "/issues.json", json=body,
                    headers={"X-Redmine-API-Key": REDMINE_API_KEY, "Content-Type": "application/json"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Redmine HTTP {r.status_code}: {r.text[:300]}")
    iss = (r.json() or {}).get("issue") or {}
    return {"issue_id": iss.get("id"), "url": f"{REDMINE_BASE}/issues/{iss.get('id')}", "project": project}


async def bookstack_publish_page(p: dict) -> dict:
    if ":" not in BOOKSTACK_TOKEN:
        raise RuntimeError("нет BOOKSTACK_TOKEN (id:secret) у коннектора")
    tid, tsec = BOOKSTACK_TOKEN.split(":", 1)
    body = {"book_id": int(p.get("book_id") or 1), "name": str(p.get("title") or "Страница от ABOP")[:190]}
    if p.get("markdown"):
        body["markdown"] = str(p["markdown"])[:200000]
    else:
        body["html"] = str(p.get("html") or "<p>(пусто)</p>")[:200000]
    r = await _http("POST", BOOKSTACK_URL + "/api/pages", json=body,
                    headers={"Authorization": f"Token {tid}:{tsec}", "Content-Type": "application/json"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"BookStack HTTP {r.status_code}: {r.text[:300]}")
    j = r.json() or {}
    return {"page_id": j.get("id"), "slug": j.get("slug"), "url": f"{BOOKSTACK_URL}/books/{j.get('book_slug') or body['book_id']}/page/{j.get('slug') or ''}"}


def _smtp_send(to: str, subject: str, body: str, html: str | None) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
    msg.attach(MIMEText(body or "", "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(MAILPIT_HOST, MAILPIT_PORT, timeout=20) as s:
        s.send_message(msg)


async def mailpit_send_email(p: dict) -> dict:
    to = str(p.get("to") or "").strip()
    if not to:
        raise RuntimeError("payload.to пустой")
    try:
        await asyncio.to_thread(_smtp_send, to, str(p.get("subject") or "Письмо от ABOP")[:250], str(p.get("body") or ""), p.get("html"))
    except (ConnectionError, OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as ex:
        raise Transient(f"SMTP: {ex}") from ex
    return {"to": to, "via": f"{MAILPIT_HOST}:{MAILPIT_PORT}", "web": f"http://{MAILPIT_HOST}:8025"}


async def one_c_request_export(p: dict) -> dict:
    """Запрос экспорта из 1С: кладём JSON-заявку в каталог, который читает процесс на стороне 1С (сервер-2)."""
    os.makedirs(ONE_C_DIR, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    path = os.path.join(ONE_C_DIR, f"req-{rid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"id": rid, "ts": _now(), "request": p}, f, ensure_ascii=False, indent=1)
    return {"request_id": rid, "path": path}


async def echo(p: dict) -> dict:
    return {"echo": p}


ADAPTERS = {
    ("redmine", "issue.create"): redmine_create_issue,
    ("bookstack", "page.publish"): bookstack_publish_page,
    ("mailpit", "email.send"): mailpit_send_email,
    ("1c", "export.request"): one_c_request_export,
}
for _sys in ("redmine", "bookstack", "mailpit", "1c", "gitea", "nocodb", "twenty", "minio", "kroki", "qdrant", "postgres", "keycloak", "routeai", "kafka"):
    ADAPTERS[(_sys, "echo")] = echo   # диагностика: любая система отвечает эхом


# ───────────── исполнение
async def execute(system: str, ctype: str, payload: dict) -> dict:
    fn = ADAPTERS.get((system, ctype))
    if fn is None:
        raise RuntimeError(f"нет адаптера для {system}/{ctype}; доступны: " + ", ".join(sorted(f"{s}/{t}" for s, t in ADAPTERS if t != "echo")))
    if DRY_RUN and ctype != "echo":
        return {"dry_run": True, "would": f"{system}/{ctype}", "payload_keys": sorted(payload.keys())}
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            return await fn(payload)
        except Transient as ex:
            last = ex
            await asyncio.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"после {RETRIES + 1} попыток: {last}")


async def publish(prod: AIOKafkaProducer, topic: str, msg: dict, key: str = "", headers: list | None = None) -> None:
    hdrs = [(k, str(v).encode()) for k, v in (headers or [])]
    await prod.send_and_wait(topic, json.dumps(msg, ensure_ascii=False).encode(), key=(key.encode() if key else None), headers=hdrs)


async def handle(prod: AIOKafkaProducer, dd: Dedupe, topic: str, raw: bytes, hdrs: dict) -> None:
    METRICS["consumed"] += 1
    try:
        cmd = json.loads(raw.decode())
    except Exception as ex:  # noqa: BLE001
        METRICS["dlq"] += 1
        await publish(prod, TOPIC_DLQ, {"schema": "abop.dlq/1.0", "source_topic": topic, "error": f"bad json: {ex}",
                                        "payload": {"raw": raw.decode(errors="replace")[:500]}, "ts": _now()},
                      key=topic, headers=[("source-topic", topic), ("error", "bad json")])
        return
    cid = str(cmd.get("id") or uuid.uuid4().hex[:16])
    system = str(cmd.get("system") or topic.split(".")[1])
    ctype = str(cmd.get("type") or "")
    payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
    trace = str(cmd.get("trace_id") or hdrs.get("x-trace-id") or "")
    events_topic = f"abop.{system}.events"
    prev = dd.seen(cid)
    if prev:
        METRICS["duplicate"] += 1
        jlog("connector.duplicate", id=cid, system=system, type=ctype, prev=prev["status"])
        return
    t0 = time.perf_counter()
    try:
        result = await execute(system, ctype, payload)
        dd.mark(cid, topic, "done", json.dumps(result, ensure_ascii=False))
        METRICS["done"] += 1
        METRICS["by_type"][f"{system}/{ctype}"] = METRICS["by_type"].get(f"{system}/{ctype}", 0) + 1
        ev = {"schema": "abop.event/1.0", "id": uuid.uuid4().hex[:16], "system": system, "type": "command.done",
              "payload": {"command_id": cid, "command_type": ctype, "result": result, "actor": cmd.get("actor"), "ms": round((time.perf_counter() - t0) * 1000)},
              "ts": _now(), "trace_id": trace, "actor": "connector"}
        await publish(prod, events_topic, ev, key="command.done", headers=[("x-trace-id", trace), ("command-id", cid)])
        jlog("connector.done", id=cid, system=system, type=ctype, ms=ev["payload"]["ms"], result=str(result)[:160])
    except Exception as ex:  # noqa: BLE001
        err = f"{type(ex).__name__}: {ex}"
        dd.mark(cid, topic, "failed", err)
        METRICS["failed"] += 1
        METRICS["dlq"] += 1
        ev = {"schema": "abop.event/1.0", "id": uuid.uuid4().hex[:16], "system": system, "type": "command.failed",
              "payload": {"command_id": cid, "command_type": ctype, "error": err[:1000], "actor": cmd.get("actor")},
              "ts": _now(), "trace_id": trace, "actor": "connector"}
        await publish(prod, events_topic, ev, key="command.failed", headers=[("x-trace-id", trace), ("command-id", cid)])
        await publish(prod, TOPIC_DLQ, {"schema": "abop.dlq/1.0", "source_topic": topic, "error": err[:2000], "payload": cmd, "ts": _now(), "trace_id": trace},
                      key=topic, headers=[("source-topic", topic), ("error", err[:200]), ("x-trace-id", trace), ("command-id", cid)])
        jlog("connector.failed", id=cid, system=system, type=ctype, error=err[:300])


async def http_server() -> None:
    """Минимальный /healthz и /metrics без внешних зависимостей."""
    async def cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            path = line.decode(errors="replace").split(" ")[1] if b" " in line else "/"
            while (await asyncio.wait_for(reader.readline(), timeout=5)).strip():
                pass
            if path.startswith("/metrics"):
                body = "".join(f"abop_connector_{k} {v}\n" for k, v in METRICS.items() if k != "by_type")
                body += "".join(f'abop_connector_commands_total{{kind="{k}"}} {v}\n' for k, v in METRICS["by_type"].items())
                ctype = "text/plain; version=0.0.4"
            else:
                body = json.dumps({"ok": True, "brokers": BROKERS, "group": GROUP, "dry_run": DRY_RUN, **{k: v for k, v in METRICS.items() if k != "by_type"}})
                ctype = "application/json"
            data = body.encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype.encode() + b"\r\nContent-Length: " + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()
    srv = await asyncio.start_server(cb, "0.0.0.0", PORT)
    async with srv:
        await srv.serve_forever()


async def main() -> None:
    dd = Dedupe(DB_PATH)
    prod = AIOKafkaProducer(acks="all", enable_idempotence=True, **_kw())
    await prod.start()
    cons = AIOKafkaConsumer(group_id=GROUP, enable_auto_commit=False, auto_offset_reset="earliest", **_kw())
    await cons.start()
    cons.subscribe(pattern=r"^abop\..+\.commands$")
    asyncio.create_task(http_server())
    jlog("connector.started", brokers=BROKERS, group=GROUP, adapters=sorted(f"{s}/{t}" for s, t in ADAPTERS if t != "echo"), dry_run=DRY_RUN, port=PORT)
    try:
        while True:
            batch = await cons.getmany(timeout_ms=1000, max_records=20)
            for tp, msgs in batch.items():
                for m in msgs:
                    hdrs = {k: v.decode(errors="replace") for k, v in (m.headers or [])}
                    await handle(prod, dd, tp.topic, m.value, hdrs)
            if batch:
                await cons.commit()   # at-least-once + дедуп по id команды = эффективно один раз
    finally:
        await cons.stop()
        await prod.stop()


if __name__ == "__main__":
    asyncio.run(main())
