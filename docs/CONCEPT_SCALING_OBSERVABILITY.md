# Концепция: Enterprise-масштабирование и Observability ABOP

> Предварительная проработка (Трек B, Хартия агентных процессов v3.5). Статус: **черновик концепции**, до реализации. Опирается на фактическое состояние кода на 2026-09-16.

## 0. Зачем (постановка задачи)

Демо-контур ABOP функционально закрыт (Data Plane, 1С-Аудитор, «Расследование от симптома», конформанс, RBAC/ABAC, триггеры). Но **архитектурно это монолит с синхронным исполнением** — то самое «красивое демо в ноутбуке», о котором предупреждает Хартия. Чтобы стать enterprise-платформой на много пользователей/процессов, нужно развести исполнение и приём, убрать двойные запуски, сделать сервисы stateless и добавить сквозную наблюдаемость каждого этапа.

Правило Хартии: **быстрее ≠ успех**. Целевые метрики фиксируем ДО старта (см. §7).

## 1. Точка отсчёта — фактическое состояние (ground truth)

| Аспект | Как сейчас | Файл-якорь | Проблема для масштаба |
|---|---|---|---|
| Исполнение прогона | Синхронно внутри HTTP-запроса, `await` инлайн | `web_api.py:execute_agent_run` → `runner.run_live` | Запрос держит соединение всю LLM-раскладку; нет очереди/бэкпрешера |
| Параллелизм LLM | `asyncio.Semaphore`, `ABOP_RUN_LLM_CONCURRENCY=2` | `runner.py:22,132,172` | Тюнится, но в пределах одного процесса |
| Планировщик | In-process asyncio-цикл (TICK=45с), стартует на каждой реплике | `web_api.py` startup, `triggers.py:scheduler_loop` | **Двойной запуск на 2+ репликах**, нет leader-election |
| Курсор событий | В Postgres, но read-modify-write без блокировки | `trigger_store.py` | Гонка курсора → дубли событий |
| Состояние | Postgres + ~12 module-level `_MEM`/кэшей | все `*_store.py`, `ape.SKILL_DATASOURCES` | Правка на реплике A невидима B до рефреша |
| run_store / метрики | Полный payload + `run_metrics` (cost/tokens/verdict/hitl) | `run_store.py`, `runner.py:96-211` | Нет trace_id, нет per-step тайминга |
| Внешние вызовы | timeout есть; retry только каскад LLM | `clients.py`, `ape.py:_adapter_*` | Нет circuit-breaker/backoff/TTL-кэша |
| HITL | Пишется `pending_hitl`; `/api/hitl/*` = **501** | `runner.py:77-80`, `web_api.py:1703-1714` | Нет resume — прогон «зависает» |
| Observability | `logging.basicConfig`, `/api/health`, `audit_log`, `cost_journal` | `main.py:22`, `web_api.py:130` | Нет OTel/Prometheus/trace/langfuse (стаб пустой) |
| Kafka | Только запись в реестре систем | `systems_store.py:159` | Клиента/топиков нет |
| Деплой | 1 uvicorn-процесс, `--network host`, 1 контейнер | `Dockerfile.webapi`, `.env.server.example` | Один worker; масштаб только вручную |
| MCP-шлюз | In-process ABAC (FastMCP + FastAPI), не отдельный прокси | `access.py`, `main.py` | Enforcement — вызовы функций, не сетевая граница |

## 2. Целевые принципы (из Хартии v3.5)

- **Принцип 12 «Один процесс — один агент».** Единая копия процесса (один промпт, одна инфра, предсказуемая стоимость). «Отдельные агенты на пользователя» = **эфемерные stateless worker-инстансы** одного процесса с изоляцией контекста per-задача/пользователь, а НЕ персональные боты-«зоопарк».
- **Stateless & наименьшие привилегии.** Воркер живёт от задачи до задачи, без долгой памяти; доступ scoped через ABAC/MCP.
- **Шина событий (Kafka).** Раздельные топики requests/results, DLQ, Schema Registry, audit log, трассировка через Kafka-заголовки.
- **Наблюдаемость всей цепочки.** OpenTelemetry + логи всех шагов + цифровой след каждого этапа (langfuse на server-2 — переиспользовать).
- **Adapter Layer.** Legacy через плоский JSON, кэш, circuit-breaker, TTL.
- **Graceful degradation.** Агент опционален, ручной маршрут всегда; агент «перескакивает» 3-5 этапов, не несущая конструкция.
- **Метрики приёмки до старта.** First-Time-Right, Escalation Rate, Time-to-Recovery, Cost per transaction.

## 3. Целевая архитектура (плоскости)

```
                         ┌──────────────────────── Caddy / LB (TLS) ────────────────────────┐
   Пользователь / UI ───▶│                                                                     │
                         ▼                                                                     ▼
             ┌───────────────────────┐                                        ┌───────────────────────┐
             │  CONTROL PLANE (API)  │  stateless, N реплик                   │   MCP GATEWAY         │
             │  FastAPI + Keycloak   │  CRUD агентов/контрактов/рецептов       │  ABAC/least-priv,     │
             │  ABAC-манифест        │  публикует запрос прогона в шину        │  Qdrant tenant/family │
             └───────────┬───────────┘                                        └───────────┬───────────┘
                         │ publish abop.runs.requests (traceparent)                        │ enforce
                         ▼                                                                   ▼
   ┌──────────────────────────────  MESSAGE BUS (Kafka + Schema Registry)  ──────────────────────────┐
   │  abop.runs.requests · abop.runs.results · abop.runs.dlq · abop.events.<src> · abop.audit · ...    │
   └───────┬───────────────────────────────────┬───────────────────────────────────┬─────────────────┘
           │ consume (consumer group)           │ resume (HITL)                      │ events
           ▼                                     ▼                                    ▼
 ┌───────────────────────┐          ┌───────────────────────┐          ┌───────────────────────┐
 │  WORKER PLANE         │          │  HITL STATE MACHINE   │          │  SCHEDULER (leader)   │
 │  stateless run-воркеры│          │  pending→approve→resume│         │  singleton / PG-lock  │
 │  runner.run_live      │          │  таблица hitl_items    │          │  cron + event-poll    │
 │  → run_store, метрики │          │  /api/hitl/* (реализ.) │         │  publish requests     │
 └──────────┬────────────┘          └───────────────────────┘          └───────────────────────┘
            │ external calls через Adapter Layer (timeout+retry+breaker+TTL)
            ▼
   RouteAI(LLM) · sLAVA(RAG) · Qdrant · Postgres · 1С/Redmine/BookStack/Mailpit...

  ══ OBSERVABILITY SPINE (сквозь все плоскости) ═════════════════════════════════════════════════
   OTel SDK (trace_id/span_id, propagation через Kafka-заголовки) → OTel Collector → Tempo/Jaeger
   langfuse (LLM-трейсы: волны/скиллы/токены/стоимость, линк на trace_id)
   Prometheus (/metrics: latency, lag, DLQ, cost, FTR, escalation) → Grafana
   structured JSON logs (trace_id/run_id/actor) → Loki
```

### 3.1 Control plane (API + MCP-шлюз)
FastAPI остаётся синхронной поверхностью для CRUD/аутентификации/ABAC-манифеста, но **перестаёт исполнять прогоны** — только публикует `abop.run_request` в шину и сразу возвращает `202 Accepted {run_id, status:accepted}`. Делаем stateless: авторитетное состояние — только Postgres; in-process кэши либо request-scoped, либо инвалидируются через шину/`LISTEN/NOTIFY` (§6). Реплики за LB.

### 3.2 Message bus (Kafka)
Топики: `abop.runs.requests`, `abop.runs.results`, `abop.runs.dlq`, `abop.events.<source>`, `abop.audit`, `abop.hitl`, `abop.cache.invalidate`. Schema Registry со схемами `abop.run_request/1.0`, `abop.run_result/1.0` (совместимо с уже существующими `luda.*`/`abop.run_metrics/1.0`). Ключ партиционирования — `contract_audit_id` (порядок в рамках процесса + равномерная нагрузка). Идемпотентность — `run_request_id` (dedup в воркере).

### 3.3 Worker plane (stateless исполнители)
N идентичных воркеров в consumer-group читают `abop.runs.requests`, исполняют `runner.run_live` (уже async), пишут `run_store` + метрики + трейс, публикуют `abop.run_result`. Масштаб — числом партиций/реплик. Воркер принимает **scope агента** (ABAC) и ходит наружу только через Adapter Layer и MCP-шлюз. Это и есть «эфемерный worker-инстанс одного процесса» (Принцип 12).

### 3.4 Scheduler / trigger plane (единый владелец)
Планировщик выносится из API-реплик в **singleton** (или leader-elected через Postgres advisory-lock / отдельный сервис). Он только **публикует** запросы прогонов и опрашивает источники событий (курсор в PG, один владелец) — **не исполняет инлайн**. Это устраняет двойной запуск.

### 3.5 HITL state machine
Таблица `hitl_items {id, run_id, agent_id, gate, state, payload, created_at, decided_by, decided_at}`; конечный автомат `created → running → awaiting_hitl → approved|rejected → resumed → closed`. Воркер на HITL-гейте чекпоинтит продолжение и освобождает слот; `POST /api/hitl/{id}/approve` публикует resume в `abop.hitl` → воркер добирает. Реализуем заглушки `web_api.py:1703-1714`.

### 3.6 Observability spine
- **Трассировка:** OpenTelemetry, `trace_id` рождается в API, пробрасывается через Kafka-заголовки (`traceparent`) в воркер и дальше в каждый внешний вызов. Экспорт в OTel Collector → Tempo/Jaeger.
- **LLM-трейсы:** langfuse (уже на server-2 — переиспользуем) оборачивает `clients.chat/embed/rerank`; спан на волну/скилл с токенами/стоимостью/латентностью; линк на OTel `trace_id`.
- **Метрики:** Prometheus-клиент + middleware: run latency (p50/p95), глубина очереди/consumer-lag, размер DLQ, cost/run, FTR, escalation-rate; эндпоинт `/metrics`; дашборды Grafana.
- **Логи:** structured JSON c `trace_id/run_id/actor` → Loki.
- **Метрики приёмки** (§7) считаются из `runs`/`run_metrics` + baseline и выводятся и в Grafana, и в UI ABOP («Прогоны»/«Флот»).

### 3.7 Adapter Layer (устойчивость интеграций)
Единый резилиенс-хелпер поверх внешних вызовов (RouteAI, sLAVA, http/postgres/vector-адаптеры, опрос источников): timeout + retry с экспоненциальным backoff + circuit-breaker + bulkhead; TTL-кэш для emit рецептов (свежесть Data Plane управляемо). Legacy — через плоский JSON (модель уже такая).

## 4. Убрать stateful-ловушки (обязательный энейблер)

Инвентаризация in-process кэшей и перевод на общий источник:
- **Авторитет — Postgres** (уже так для агентов/контрактов/прогонов/систем/рецептов).
- **Кэши → инвалидация:** `ape.SKILL_DATASOURCES`, dataplane-кэш, `systems`, `admin_config` — держать с инвалидацией через `abop.cache.invalidate` (или Postgres `LISTEN/NOTIFY`), а не «рефреш только на своей реплике».
- **Курсоры триггеров** — только у scheduler-leader (устраняет гонку).
- **Убрать `_MEM`-фолбэки из горячего пути** прод-профиля (оставить для dev/тестов): в enterprise DSN обязателен.

## 5. Модель многопользовательности и изоляции

- **Один процесс — много воркер-инстансов**, изоляция контекста per-задача (никаких персональных ботов).
- **ABAC на шлюзе:** воркер исполняет под scope агента; данные семьи X недоступны агенту семьи Y (уже есть `access.can_reach_*`), Qdrant-тенант `slava_fam_<family>`.
- **Квоты/рейт-лимиты per-tenant** на публикацию запросов (расширение текущих `WEEKLY_TOKEN_LIMIT`/`cost_journal`).
- **Deny-by-default + аудит every allow/deny с trace_id.**

## 6. Дорожная карта миграции (crawl → walk → run, низкий риск)

**Фаза 0 — упрочнение без новой инфраструктуры (быстрый выигрыш):**
1. `trace_id`/correlation-id + structured JSON logs + `/metrics` (Prometheus).
2. Резилиенс-обёртка (timeout/retry/breaker) вокруг внешних вызовов + TTL-кэш адаптеров.
3. HITL state machine + реальные `/api/hitl/*` (resume).
4. Postgres advisory-lock guard на планировщике — **чинит двойной запуск без Kafka**.
5. Control plane → stateless-safe (инвалидация кэшей).

**Фаза 1 — асинхронное исполнение, минимальная шина:**
- Durable-очередь прогонов за интерфейсом `RunBus`. **Рекомендация: Postgres-очередь** (`SELECT … FOR UPDATE SKIP LOCKED`) + пул воркеров — ноль новой инфры, максимальное развязывание. Интерфейс спроектировать так, чтобы переход на Kafka был сменой драйвера.
- API отдаёт `202` + `run_id`; воркеры исполняют; результат — в `run_store` (+ SSE/polling для UI).

**Фаза 2 — Kafka по Хартии:**
- Kafka + Schema Registry; requests/results/events/audit/hitl → топики; consumer-group воркеры; DLQ; проброс trace-заголовков.
- Полный observability-стек: langfuse + OTel Collector + Tempo + Prometheus + Grafana + Loki.

**Фаза 3 — масштаб и HA:**
- Много воркер-реплик по партициям; автоскейл по consumer-lag; Postgres HA/read-replicas; Qdrant HA; DR и метрика Time-to-Recovery; per-tenant квоты/burst.

Каждая фаза самодостаточна и даёт ценность; Фаза 0 снимает главные production-риски (двойной запуск, зависший HITL, отсутствие следов) ещё до Kafka.

## 7. Метрики приёмки (фиксируем ДО старта)

| Метрика | Источник | Целевой ориентир (уточнить) |
|---|---|---|
| First-Time-Right | `runs.verdict_ok` vs baseline | ≥ acceptance контракта (напр. 0.90) |
| Escalation Rate | доля прогонов с HITL/замечаниями | ≤ acceptance (напр. 0.15) |
| Time-to-Recovery | время от сбоя до восстановления | задать SLA (напр. < 5 мин) |
| Cost per transaction | `run_metrics.cost.rub` / прогон | тренд вниз, потолок на прогон |
| p95 latency прогона | OTel/Prometheus | задать SLA |
| Consumer lag / DLQ | Prometheus | ≈ 0 в устойчивом режиме |

## 8. Открытые решения для владельца

1. **Шина сейчас или позже:** Postgres-очередь как Фаза 1 (рекомендую) → Kafka в Фазе 2, **или** сразу Kafka.
2. **Целевой рантайм:** остаёмся на docker-compose (два сервера) **или** переезд на Kubernetes (нужен для авто-HA/autoscale).
3. **Observability-стек:** подтвердить переиспользование langfuse на server-2 + разворачиваем ли Grafana/Prometheus/Loki/Tempo (или частично).
4. **Целевые SLA/масштаб:** сколько одновременных прогонов, пользователей, процессов, p95 — нужно для расчёта партиций/реплик.
5. **MCP-шлюз как сетевая граница:** оставить in-process enforcement или вынести egress данных/инструментов физически через шлюз-процесс (defense-in-depth).

## 9. Что НЕ трогаем

Доменную логику прогона (`runner.run_live`, детерминированные проверки/трассировка 1С, конформанс, RBAC/ABAC-правила) — она переезжает в воркер **как есть**. Меняется только оболочка исполнения и наблюдаемость, не бизнес-логика.
```
