# Коннектор-воркер: команды навыков через шину (2026-09-26)

Место: сервер-2 `201.51.5.24`, контейнер `abop-connector` (образ из `connector/`, том `abop_connector`, env `/opt/abop-connector/systems.env`), рядом с Redpanda. Метрики и здоровье: `http://127.0.0.1:9105/healthz`, `/metrics` (Prometheus).

## Поток

```
навык ──bus_publish──▶ governance (ABAC системы, режим навыка, HITL) ──▶ abop.<система>.commands
                                                                              │
                             abop.<система>.events ◀── command.done / command.failed ◀── коннектор (адаптер)
                                                                              │
                                                                          abop.dlq (после ретраев)
```

1. **Публикация под governance** (`server/skill_tools.publish_command_governed`): система должна быть в реестре и доступна семье агента (`access.can_reach_system`); навык в режиме `action` публикует сразу, в режиме `write` или при `payload.hitl=true` создаётся заявка HITL канала `command`, оператор одобряет в UI → `hitl_approve` публикует команду. Аналитик/навык не держит секретов внешних систем.
2. **Исполнение** (`connector/worker.py`): consumer-group `abop-connectors` на `^abop\..+\.commands$`, at-least-once + дедуп по `id` команды (SQLite в `/data`), ретраи на сеть/5xx (`ABOP_CONNECTOR_RETRIES`, по умолчанию 3), commit после батча.
3. **Ответ**: событие `command.done` (payload `{command_id, command_type, result, actor, ms}`) или `command.failed` (`error`) в `abop.<система>.events`; сбой дополнительно в `abop.dlq` с заголовками `source-topic`, `error`, `x-trace-id`, `command-id`. Служебные `command.*` не будят event-триггеры агентов, если триггер не подписан на этот тип явно (`event_type`).

## Адаптеры

| Система | Тип команды | Payload | Результат |
|---|---|---|---|
| redmine | `issue.create` | `subject, description, project?, priority_id?` | `issue_id, url` |
| bookstack | `page.publish` | `title, html \| markdown, book_id?` | `page_id, url` |
| mailpit | `email.send` | `to, subject, body, html?` | `to, via, web` |
| 1c | `export.request` | произвольная заявка на выгрузку | `request_id, path` (JSON в `/data/1c-requests`, читает процесс на стороне 1С) |
| любая | `echo` | любой | эхо (диагностика) |

Секреты только у коннектора: `REDMINE_BASE/REDMINE_API_KEY/REDMINE_PROJECT`, `BOOKSTACK_URL/BOOKSTACK_TOKEN`, `MAILPIT_HOST/MAILPIT_PORT`, `ABOP_KAFKA_*`. Адреса систем для сервера-2 — публичные (`http://5.129.192.63:3000`, `:6875`, `:1025`), не `127.0.0.1` из `.env` сервера-1. `ABOP_CONNECTOR_DRY_RUN=1` — ничего наружу, только события.

## Проверено на стенде 26.09

`POST /api/bus/publish` → `redmine/echo` — `command.done` за 5 мс; `mailpit/email.send` — письмо в Mailpit; `redmine/nope.do` — `command.failed` + запись в DLQ с текстом ошибки; `redmine/issue.create` и `bookstack/page.publish` — реальные задача и страница (см. ниже в отчёте сессии). Триггеры на `command.*` не сработали (`fired: 0`).

## Эксплуатация

- Логи: `docker logs abop-connector` (JSON-строки `connector.started/done/failed/duplicate`).
- Новый адаптер = функция `async def f(payload) -> dict` + запись в `ADAPTERS[(система, тип)]`; временные ошибки — `raise Transient(...)`.
- Повторная доставка одной команды (ребаланс, рестарт) не создаёт дубля: id хранится в `/data/connector.sqlite`.
- DLQ читается `GET /api/bus/dlq` (support+); чтобы переиграть команду — опубликовать её заново с новым id.

## Наблюдаемость (Grafana, 27.09)

Prometheus сервера-1 (`ops/prometheus.yml`) собирает три цели: `abop-webapi` (:8091/metrics), `redpanda` (сервер-2 :9644/public_metrics, admin API Redpanda; порт опубликован Docker и открыт в DOCKER-USER только для сервера-1) и `abop-connector` (сервер-2 :9105/metrics; INPUT-правила: только сервер-1 и loopback). Дашборд `abop-overview` получил секцию «Шина (Redpanda · сервер-2) и коннектор-воркер»: брокер/коннектор up, команд исполнено/с ошибкой, DLQ (ABOP + коннектор), максимальный лаг консьюмеров по топикам `abop.*`, сообщения/с по топикам и событиям систем, команды по типам, записи/чтения и p99 брокера, память/диск/недореплицированные партиции. Метрики самого ABOP по шине: `abop_bus_messages_total{topic}`, `abop_bus_events_total{system}`, `abop_bus_errors_total`, `abop_bus_dlq_total{source}`; коннектора: `abop_connector_{consumed,done,failed,dlq,duplicate}`, `abop_connector_commands_total{kind}`.

⚠️ У `redpanda start` этой версии нет флага `--admin-addr` (контейнер уходит в рестарт-цикл) — admin API и так слушает 0.0.0.0:9644 внутри контейнера, достаточно `-p 9644:9644`.
