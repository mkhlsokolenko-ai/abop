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
import base64
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

from urllib.parse import urlsplit

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
OPENPROJECT_BASE = os.getenv("OPENPROJECT_BASE", "http://5.129.192.63:8096").rstrip("/")
OPENPROJECT_TOKEN_FILE = os.getenv("OPENPROJECT_TOKEN_FILE", "/app/secrets/openproject_token")
OPENPROJECT_TOKEN = os.getenv("OPENPROJECT_TOKEN", "")
OPENPROJECT_PROJECT = os.getenv("OPENPROJECT_PROJECT", "")
BOOKSTACK_URL = os.getenv("BOOKSTACK_URL", "http://5.129.192.63:6875").rstrip("/")
DOLIBARR_URL = os.getenv("DOLIBARR_URL", "http://5.129.192.63:8095").rstrip("/")
DOLIBARR_API_KEY = os.getenv("DOLIBARR_API_KEY", "")
BOOKSTACK_TOKEN = os.getenv("BOOKSTACK_TOKEN", "")          # "id:secret"
MAILPIT_HOST = os.getenv("MAILPIT_HOST", "5.129.192.63")
MAILPIT_PORT = int(os.getenv("MAILPIT_PORT", "1025"))
MAIL_FROM = os.getenv("MAIL_FROM", "abop@demo.local")
MINIO_URL = os.getenv("MINIO_URL", "http://5.129.192.63:9000").rstrip("/")
MINIO_KEY = os.getenv("MINIO_KEY", "")
MINIO_SECRET = os.getenv("MINIO_SECRET", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "")
NOCODB_URL = os.getenv("NOCODB_URL", "http://5.129.192.63:8090").rstrip("/")
NOCODB_TOKEN = os.getenv("NOCODB_TOKEN", "")
NOCODB_TABLE = os.getenv("NOCODB_TABLE", "")
GITEA_URL = os.getenv("GITEA_URL", "http://5.129.192.63:3001").rstrip("/")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")
GITEA_REPO = os.getenv("GITEA_REPO", "")
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


def _openproject_key() -> str:
    """Ключ проектного сервера: файл надёжнее переменной среды — его нет ни в логах, ни в inspect."""
    if OPENPROJECT_TOKEN_FILE and os.path.isfile(OPENPROJECT_TOKEN_FILE):
        with open(OPENPROJECT_TOKEN_FILE, encoding="utf-8") as f:
            ключ = f.read().strip()
            if ключ:
                return ключ
    return OPENPROJECT_TOKEN


async def openproject_create_task(p: dict) -> dict:
    """Задача в бэклоге проектного сервера по итогам разбора смещения сроков.

    Пишется ТОЛЬКО задача: даты плана агент не трогает. Перенос срока — изменение базового
    расписания, его утверждает приёмочная комиссия (PMBOK 4.3/4.6), а задача фиксирует работу,
    которую для этого нужно сделать.
    """
    ключ = _openproject_key()
    if not ключ:
        raise RuntimeError("нет ключа проектного сервера (OPENPROJECT_TOKEN_FILE/OPENPROJECT_TOKEN)")
    проект = str(p.get("project") or OPENPROJECT_PROJECT or "")
    if not проект:
        raise RuntimeError("не задан проект (payload.project или OPENPROJECT_PROJECT)")
    тело = {"subject": str(p.get("subject") or "Задача от ABOP")[:250],
            "description": {"format": "markdown", "raw": str(p.get("description") or "")[:20000]}}
    if p.get("start_date"):
        тело["startDate"] = str(p["start_date"])[:10]
    if p.get("due_date"):
        тело["dueDate"] = str(p["due_date"])[:10]
    авторизация = base64.b64encode(("apikey:" + ключ).encode()).decode()
    r = await _http("POST", f"{OPENPROJECT_BASE}/api/v3/projects/{проект}/work_packages",
                    json=тело, headers={"Authorization": "Basic " + авторизация,
                                        "Content-Type": "application/json"})
    if r.status_code not in (200, 201):
        # 403 здесь значит «ключ принят, прав нет»: в проекте выключен модуль задач или ключ
        # выписан пользователю без членства. Это стоило часа разбирательств — пусть будет в тексте.
        подсказка = " (ключ принят, но прав на проект нет: проверьте модуль задач и владельца ключа)"             if r.status_code == 403 else ""
        raise RuntimeError(f"OpenProject HTTP {r.status_code}{подсказка}: {r.text[:300]}")
    з = r.json() or {}
    return {"work_package_id": з.get("id"), "project": проект,
            "url": f"{OPENPROJECT_BASE}/work_packages/{з.get('id')}"}


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


async def dolibarr_create_invoice(p: dict) -> dict:
    """Счёт покупателю в ERP Dolibarr. Создаём ЧЕРНОВИК и, только если явно просят, проводим.

    Проведённый счёт — документ учёта: его нельзя удалить, он попадает в отчётность и в НДС.
    Поэтому проведение требует отдельного `validate: true`, а не случается само по себе; по
    умолчанию человек видит черновик и проводит его руками.
    """
    if not DOLIBARR_API_KEY:
        raise RuntimeError("нет DOLIBARR_API_KEY у коннектора")
    socid = p.get("socid") or p.get("контрагент_id")
    if not socid:
        raise RuntimeError("payload.socid пуст: счёт нельзя выставить без контрагента")
    строки = p.get("lines") or [{"desc": str(p.get("описание") or "Услуги"),
                                 "subprice": float(p.get("сумма") or 0), "qty": 1,
                                 "tva_tx": float(p.get("ндс_ставка") or 20)}]
    тело = {"socid": int(socid), "type": 0, "lines": строки}
    if p.get("date"):
        тело["date"] = p["date"]
    h = {"DOLAPIKEY": DOLIBARR_API_KEY, "Content-Type": "application/json"}
    r = await _http("POST", DOLIBARR_URL + "/api/index.php/invoices", json=тело, headers=h)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Dolibarr HTTP {r.status_code}: {r.text[:300]}")
    try:
        инвойс = int(r.json())
    except (ValueError, TypeError):
        raise RuntimeError(f"Dolibarr вернул не идентификатор счёта: {r.text[:200]}")
    проведён = False
    if p.get("validate") is True:
        v = await _http("POST", f"{DOLIBARR_URL}/api/index.php/invoices/{инвойс}/validate", json={}, headers=h)
        проведён = v.status_code in (200, 201)
    return {"invoice_id": инвойс, "validated": проведён,
            "url": f"{DOLIBARR_URL}/compta/facture/card.php?id={инвойс}"}


# ── хранилище S3 (MinIO): подпись AWS SigV4 ──────────────────────────────────────────────────────
def _s3_подпись(метод: str, хост: str, путь: str, тело: bytes, ключ: str, секрет: str,
                регион: str = "us-east-1") -> dict:
    """Заголовки SigV4 для одиночного запроса к S3-совместимому хранилищу.

    Своя реализация вместо boto3: в образе коннектора нужен один запрос PUT, а не SDK на 50 МБ.
    """
    import hashlib
    import hmac as _hmac

    теперь = dt.datetime.now(dt.timezone.utc)
    штамп, день = теперь.strftime("%Y%m%dT%H%M%SZ"), теперь.strftime("%Y%m%d")
    хеш = hashlib.sha256(тело).hexdigest()
    заголовки = {"host": хост, "x-amz-content-sha256": хеш, "x-amz-date": штамп}
    подписываемые = ";".join(sorted(заголовки))
    канон = "\n".join([
        метод, путь, "",
        "".join("%s:%s\n" % (к, заголовки[к]) for к in sorted(заголовки)),
        подписываемые, хеш])
    область = "%s/%s/s3/aws4_request" % (день, регион)
    к_подписи = "\n".join(["AWS4-HMAC-SHA256", штамп, область,
                           hashlib.sha256(канон.encode()).hexdigest()])
    к = ("AWS4" + секрет).encode()
    for часть in (день, регион, "s3", "aws4_request"):
        к = _hmac.new(к, часть.encode(), hashlib.sha256).digest()
    подпись = _hmac.new(к, к_подписи.encode(), hashlib.sha256).hexdigest()
    заголовки["Authorization"] = (
        "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (ключ, область, подписываемые, подпись))
    return заголовки


async def minio_put_object(p: dict) -> dict:
    """Положить файл в объектное хранилище (отчёт, выгрузка, вложение)."""
    if not (MINIO_KEY and MINIO_SECRET):
        raise RuntimeError("нет ключей хранилища (MINIO_KEY/MINIO_SECRET) у коннектора")
    бакет = str(p.get("bucket") or MINIO_BUCKET or "")
    ключ = str(p.get("key") or p.get("name") or "").lstrip("/")
    if not (бакет and ключ):
        raise RuntimeError("нужны bucket и key (имя объекта)")
    содержимое = p.get("content")
    тело = (base64.b64decode(p["base64"]) if p.get("base64")
            else str(содержимое or "").encode("utf-8"))
    из_url = urlsplit(MINIO_URL)
    путь = "/%s/%s" % (бакет, ключ)
    заголовки = _s3_подпись("PUT", из_url.netloc, путь, тело, MINIO_KEY, MINIO_SECRET)
    заголовки["Content-Type"] = str(p.get("content_type") or "application/octet-stream")
    r = await _http("PUT", MINIO_URL + путь, content=тело, headers=заголовки)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"MinIO HTTP {r.status_code}: {r.text[:300]}")
    return {"bucket": бакет, "key": ключ, "size": len(тело),
            "url": "%s/%s/%s" % (MINIO_URL, бакет, ключ)}


async def nocodb_create_row(p: dict) -> dict:
    """Строка в таблицу NocoDB: универсальный приёмник реестров и журналов."""
    if not NOCODB_TOKEN:
        raise RuntimeError("нет NOCODB_TOKEN у коннектора")
    таблица = str(p.get("table_id") or NOCODB_TABLE or "")
    if not таблица:
        raise RuntimeError("не задана таблица (payload.table_id или NOCODB_TABLE)")
    поля = p.get("fields") if isinstance(p.get("fields"), dict) else {
        к: v for к, v in p.items() if к not in ("table_id", "fields")}
    r = await _http("POST", f"{NOCODB_URL}/api/v2/tables/{таблица}/records", json=поля,
                    headers={"xc-token": NOCODB_TOKEN, "Content-Type": "application/json"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"NocoDB HTTP {r.status_code}: {r.text[:300]}")
    о = r.json() or {}
    строка = (о[0] if isinstance(о, list) and о else о)
    return {"table": таблица, "row_id": строка.get("Id") or строка.get("id"), "fields": len(поля)}


async def gitea_create_issue(p: dict) -> dict:
    """Задача в репозитории (инженерный контур): замечание, долг, запрос на изменение."""
    if not GITEA_TOKEN:
        raise RuntimeError("нет GITEA_TOKEN у коннектора")
    репо = str(p.get("repo") or GITEA_REPO or "")
    if "/" not in репо:
        raise RuntimeError("нужен репозиторий вида «владелец/репозиторий» (payload.repo)")
    владелец, имя = репо.split("/", 1)
    тело = {"title": str(p.get("title") or "Задача от ABOP")[:250],
            "body": str(p.get("body") or p.get("description") or "")[:60000]}
    if p.get("labels"):
        тело["labels"] = p["labels"]
    r = await _http("POST", f"{GITEA_URL}/api/v1/repos/{владелец}/{имя}/issues", json=тело,
                    headers={"Authorization": "token " + GITEA_TOKEN,
                             "Content-Type": "application/json"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Gitea HTTP {r.status_code}: {r.text[:300]}")
    и = r.json() or {}
    return {"issue_id": и.get("number"), "repo": репо, "url": и.get("html_url")}


async def echo(p: dict) -> dict:
    return {"echo": p}


ADAPTERS = {
    ("redmine", "issue.create"): redmine_create_issue,
    ("bookstack", "page.publish"): bookstack_publish_page,
    ("mailpit", "email.send"): mailpit_send_email,
    ("openproject", "task.create"): openproject_create_task,
    ("minio", "object.put"): minio_put_object,
    ("nocodb", "row.create"): nocodb_create_row,
    ("gitea", "issue.create"): gitea_create_issue,
    ("1c", "export.request"): one_c_request_export,
    ("dolibarr", "invoice.create"): dolibarr_create_invoice,
}
for _sys in ("redmine", "bookstack", "mailpit", "1c", "dolibarr", "openproject", "gitea", "nocodb", "twenty", "minio", "kroki", "qdrant", "postgres", "keycloak", "routeai", "kafka"):
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
