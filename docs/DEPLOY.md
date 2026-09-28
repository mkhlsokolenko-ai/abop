# ABOP — разворачивание, восстановление, настройка (runbook)

> Актуально на 2026-09-27. Прод: **server-1 `5.129.192.63`** (API, БД, auth, мониторинг) + **server-2 `201.51.5.24`** (шина Redpanda, коннектор-воркер, sLAVA) + арендованный **GPU-бокс** с LLM. Полный список эндпоинтов/портов — в [`API_ENDPOINTS_PORTS.md`](API_ENDPOINTS_PORTS.md). Инструкция для пользователя (как запускать агентов) — [`INSTRUKCIYA_ZAPUSK_AGENTA.md`](INSTRUKCIYA_ZAPUSK_AGENTA.md). Индекс документации — [`README.md`](README.md).

---

## 1. Что где работает (топология)

### server-1 `5.129.192.63` (Timeweb) — сервисы

| Что | Где | Порт | Персистентность |
|---|---|---|---|
| **ABOP Web API** (`abop-webapi`) | docker, `--network host`, `--memory=1536m` | 8091 | код в образе; данные — в томе `abop_ape` + Postgres |
| Воркеры очереди прогонов | внутри процесса API (`ABOP_RUN_WORKERS=4`) | — | очередь `run_jobs` в Postgres |
| Postgres | server-1 | 5433 | том `ape_pg` (agents/contracts/runs/run_jobs/skills/schema_templates/report_templates/рецепты/…) |
| Keycloak | server-1 | 8811 | realm `abop` (JWT); **аутентификация на проде включена**, API без токена → 401 |
| Prometheus | docker (`ops/`) | 9091 | том `abop_prom_data`; цели: abop-webapi, redpanda, abop-connector |
| Grafana | docker (`ops/`) | 3300 | том `abop_grafana_data`; дашборд `abop-overview` из `ops/grafana/dashboards/` |
| Демо-системы реестра (Redmine :3000 / BookStack :6875 / Twenty :3002 / Mailpit :8025,1025 / NocoDB / Gitea / Kroki / MinIO) | server-1 | см. API-док | свои тома |

### server-2 `201.51.5.24` — шина, коннектор, RAG

| Что | Порт | Заметки |
|---|---|---|
| **Redpanda** (Kafka-совместимая шина) | 9092 (SASL/SCRAM-SHA-256, user `abop`), admin 9644 | single node, `--memory 1G`, том `abop_redpanda`, пароль в `/root/abop_redpanda.env`; 9092/9644 открыты в DOCKER-USER/ufw **только для server-1** |
| **Коннектор-воркер** `abop-connector` | 9105 (`/healthz`, `/metrics`) | образ из `connector/`, том `abop_connector` (SQLite-дедуп), секреты систем в `/opt/abop-connector/systems.env` — см. [`CONNECTOR_WORKER.md`](CONNECTOR_WORKER.md) |
| sLAVA API + Qdrant | 8000 / 6333 | граф-RAG по семьям и нормам |
| Gotenberg | 3050 | HTML→PDF для отчётов |
| Снимок 1С (дамп + веб-клиент) | 8092 | `audit-data/dump.json`; ссылки из карточек находок (`ABOP_1C_WEB_URL`) |

### GPU-бокс (аренда Vast, L40S) — LLM

vLLM с **Qwen3-30B-A3B-Instruct-2507-FP8** (`http://95.3.33.46:44633/v1`, модель `qwen3-30b-a3b`), OpenAI-совместимый API, `response_format=json_schema` (xgrammar). Стоимость для ABOP = 0 ₽. IP:port меняются при пересоздании бокса — см. §6в. Ключи Vast/HF — вне репо. **Второй инстанс на том же аккаунте (red-team.tech) не трогать.**

**Где живут данные (важно для восстановления):**
- **Postgres (`ape_pg`)**: агенты, контракты, прогоны и очередь заданий, навыки-правки, **шаблоны навыков** (`schema_templates` — источник правды для рантайма), **шаблоны отчётов** (`report_templates`), определения рецептов/коннекторов (`dp_recipes`/`dp_connectors`), admin-config (в т.ч. override LLM), RBAC, память агентов, HITL-заявки, разметка находок, конформанс.
- **Том `abop_ape` (`/root/.ape`)**: **эмитированные канонические данные** `data/*.jsonl` (doc1c/ref1c/transaction/…), файловые копии рецептов/коннекторов, сессии/память CLI. ⚠️ Эмитированные записи Data Plane живут ТОЛЬКО здесь — без тома пропадут при перенакате.
- **Том `abop_connector` (server-2)**: `connector.sqlite` (id исполненных команд — защита от дублей) и `1c-requests/` (заявки на выгрузку 1С).

---

## 2. Раскатка кода = пересборка образа `abop-webapi`

Каноничный скрипт: [`../ops/deploy-webapi.sh`](../ops/deploy-webapi.sh) — жёстко фиксирует том `abop_ape`, env-файл, health-check. В образ (`Dockerfile.webapi`) копируются `server/ cli/ skills/ webapp/ demo/ desktop/ui` — то есть **навыки (`SKILL.md` + `template.json`) и UI десктопа едут вместе с кодом**; их правка на диске без пересборки живёт до пересоздания контейнера (`docker cp` — только для горячей проверки).

```bash
# на server-1
cd /opt/abop
# 1) выкатить код (из git или tar из git archive)
git pull   # или: tar -xf _deploy.tar

# 2) собрать и перезапустить (ВСЕГДА с томом abop_ape!)
bash ops/deploy-webapi.sh
```

Что делает скрипт (эквивалент вручную):
```bash
docker build -q -f Dockerfile.webapi -t abop-webapi .
docker rm -f abop-webapi 2>/dev/null || true
docker run -d --name abop-webapi --network host --restart unless-stopped --memory=1536m \
  --env-file /opt/abop/.env -v abop_ape:/root/.ape abop-webapi
curl -sf http://127.0.0.1:8091/api/health   # → {"ok":true,...}
```

> ⚠️ **Никогда не запускать без `-v abop_ape:/root/.ape`** — потеряете canonical store Data Plane.

### Деплой с локальной машины (как в разработке)
```bash
git archive HEAD cli server skills webapp demo Dockerfile.webapi ops desktop/ui -o _deploy.tar
scp _deploy.tar root@5.129.192.63:/opt/abop/
ssh root@5.129.192.63 'cd /opt/abop && tar -xf _deploy.tar && bash ops/deploy-webapi.sh'
```
> `/opt/abop` на сервере — не git-клон, а распакованный архив. `desktop/ui` в архиве нужен для **сайдкар-UI-прокси** (§10): ABOP отдаёт свежий UI десктопа через `/desktop-ui-bundle`. Перед раскаткой: `python webapp/build.py check` (бандл веба собран из `webapp/src`) и `ABOP_DEV_AUTH=1 pytest -q` — см. [`RELEASE_DISCIPLINE.md`](RELEASE_DISCIPLINE.md).

### 2а. Раскатка шаблонов навыков БЕЗ пересборки образа

Рантайм читает шаблоны только из БД (`schema_templates`); репо — посев. При старте образа `server/skill_templates.seed` идемпотентно сеет `skills/<sid>/template.json` (builtin, маркер отпечатка). Чтобы обновить шаблоны на работающем проде, не трогая образ:

```bash
python scripts/check_templates.py skills/*/template.json        # strict-совместимость + секция delivery
python scripts/push_templates.py --base http://5.129.192.63:8091 --user <admin> --password '...'   # все
python scripts/push_templates.py --base ... --only audit1c-rank,audit1c-explain --force            # выбранные, перезаписать ручные
```
Скрипт логинится (`POST /api/auth/login`) и шлёт `POST /api/schema-templates/import {source:"repo"}` (admin). Ручные правки в UI (`builtin=false`) без `--force` не перезаписываются; `POST /api/schema-templates/{id}/reset` возвращает версию из образа. Подробно — [`SKILL_TEMPLATES.md`](SKILL_TEMPLATES.md).

Шаблоны **отчётов** (`reports/<id>.html` → `report_templates`) сеются так же как builtin; правка в веб-редакторе (Данные → «Редактор шаблонов», `POST /api/report-templates/{id}`) имеет приоритет и переживает перенакат.

---

## 3. Observability-стек (Prometheus + Grafana)

```bash
cd /opt/abop
docker compose -f ops/docker-compose.observability.yml up -d
```
- Prometheus `http://5.129.192.63:9091` — три цели (`ops/prometheus.yml`): `abop-webapi` (host :8091/metrics), `redpanda` (server-2 :9644/public_metrics, 30 с), `abop-connector` (server-2 :9105/metrics). Порты server-2 открыты только для server-1.
- Grafana `http://5.129.192.63:3300` (admin/admin — сменить). Дашборд `abop-overview` (26 панелей) из смонтированной папки `ops/grafana/dashboards/`: прогоны/успех/стоимость/LLM-inflight, латентности, HTTP, **очередь** (ждут / выполняются / ждут HITL / RSS, p95 ожидания и исполнения), **шина и коннектор** (up, DLQ, лаг консьюмеров по `abop.*`, сообщения/с, команды по типам, p99 брокера, память/диск).
- Метрики ABOP: `abop_http_*`, `abop_runs_total`, `abop_run_seconds`, `abop_findings_total`, `abop_run_cost_rub_total`, `abop_deliveries_total`, `abop_run_soft_errors_total`; очередь — `abop_run_queue_depth`, `abop_run_queue_jobs{status}`, `abop_run_workers`, `abop_process_rss_mb`, `abop_run_queue_wait_seconds`, `abop_run_job_seconds{kind}`, `abop_run_jobs_total{status}`, `abop_run_backpressure_total`; LLM — `abop_llm_inflight`, `abop_llm_fallback_total{model}`; шина — `abop_bus_messages_total{topic}`, `abop_bus_events_total{system}`, `abop_bus_errors_total`, `abop_bus_dlq_total{source}`; коннектор — `abop_connector_{consumed,done,failed,dlq,duplicate}`, `abop_connector_commands_total{kind}`.
- Сквозной `trace_id` — заголовок `X-Trace-Id` (генерится/пробрасывается), в прогоне поле `trace_id`, в сообщениях шины заголовок `x-trace-id`.
- LLM-трейсы (langfuse) — каркас; включаются заданием `LANGFUSE_URL/PUBLIC_KEY/SECRET_KEY`.

---

## 4. Восстановление Data Plane (если прогоны дают 0 находок)

Симптом: прогон отрабатывает <1с, `findings: 0`, LLM не звался. Диагностика по шагам:

```bash
# 1) том смонтирован?
docker inspect abop-webapi --format '{{json .Mounts}}' | grep abop_ape

# 2) файлы данных есть и непустые?
docker exec abop-webapi wc -l /root/.ape/data/*.jsonl
#   ожидаемо: doc1c.jsonl ~300+, ref1c.jsonl ~150+, transaction.jsonl ~15

# 3) data_query отдаёт записи?
docker exec abop-webapi python -c "import sys;sys.path.insert(0,'/app/cli');import ape;print('doc1c',len(ape.data_query('doc1c',limit=99999)))"
```

- **Файлы есть, но data_query=0** → это была бага бинарного TTL (исправлено 2026-09-18: версионная freshness — последняя версия не протухает по времени). Если снова всплывёт — проверить `_fresh`/`data_query` в `cli/ape.py`.
- **Файлы пусты** → прогнать рецепты наполнения:
```bash
TOKEN=$(curl -s -XPOST http://127.0.0.1:8091/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<pass>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
for r in audit1c_docs audit1c_refs invoices crm_deals payments redmine_issues; do
  curl -s -XPOST "http://127.0.0.1:8091/api/data/recipes/$r/run" -H "Authorization: Bearer $TOKEN"; echo
done
```
Рецепты тянут из источников (дамп 1С `201.51.5.24:8092`, CRM и т.п.) и пишут в `~/.ape/data/*.jsonl`.

**Версионная семантика (решение владельца 2026-09-18):** `data_query` держит ПОСЛЕДНЮЮ созданную версию записи всегда (не протухает по wall-clock); по TTL уходят только перекрытые старые версии (`_data_compact` при `data_run`). `include_stale=True` → вся история версий.

---

## 5. Запуск демо-агента (после разворачивания)

Три готовых сценария (кнопка «▶ Собрать и запустить» на канве, либо API):
```bash
# идемпотентно собрать агента из demo/<scenario>/ и запустить
curl -s -XPOST http://127.0.0.1:8091/api/demo/prepare -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"scenario":"audit1c"}'   # audit1c | invest1c | fin
# синхронно (201 с результатом) …
curl -s -XPOST http://127.0.0.1:8091/api/runs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"agent_id":"audit1c-holding-2026.v1"}'
# … или через очередь (202 + job_id, статус по GET /api/runs/jobs/{id})
curl -s -XPOST 'http://127.0.0.1:8091/api/runs?async=1' -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"agent_id":"audit1c-holding-2026.v1"}'
```
Демо-пакеты: `demo/audit1c/`, `demo/invest1c/`, `demo/fin/` (contract*.json + agent_body.json). Требуют наполненного Data Plane (§4). Результат аудитора с шаблонами доставки (`audit1c-explain`, `audit1c-rank`) создаёт HITL-заявки с превью задач Redmine — подтверждение в чате десктопа или в очереди HITL веба.

---

## 6. Конфигурация (`.env` на server-1)

Полный список — в [`API_ENDPOINTS_PORTS.md` §3](API_ENDPOINTS_PORTS.md). Ключевое:

| Группа | Переменные | Заметки |
|---|---|---|
| База / auth | `POSTGRES_DSN`, `KEYCLOAK_ISSUER`, `KEYCLOAK_JWKS_URI`, `KEYCLOAK_JWKS_INTERNAL`, `KEYCLOAK_AUDIENCE`, `ABOP_EXTRA_JWKS` | Без `KEYCLOAK_JWKS_URI` API **fail-closed** (401). `ABOP_DEV_AUTH=1` — аноним = admin, **только локальная разработка/CI**, на проде не задавать. `ABOP_EXTRA_JWKS` — доп. issuer для десктопа |
| LLM | `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL`, `LOCAL_LLM_API_KEY` (`EMPTY`), `ROUTEAI_BASE_URL`, `ROUTEAI_API_KEY`, `ROUTEAI_STANDARD_CASCADE` (`local/qwen3-30b-a3b,…` — local первым, RouteAI fallback), `ROUTEAI_RESEARCH_CASCADE`, `ROUTEAI_CODE_CASCADE`, `ABOP_LOCAL_LLM_TIMEOUT` (300 — свой бокс, длинные схемы), `ABOP_LLM_TIMEOUT` (120 — облако), `ABOP_LLM_MAX_INFLIGHT` (12) | Override адреса бокса — §6в |
| Прогон | `ABOP_RUN_LLM_CONCURRENCY` (8 на своём боксе; 2–3 на RouteAI), `ABOP_RUN_MAX_TOKENS` (1600, нарратив), `ABOP_RUN_MAX_TOKENS_TEMPLATE` (3200 — навыки с подробной схемой без своего `max_tokens`), `ABOP_RUN_MAX_TOKENS_FREE`, `ABOP_RUN_STRUCTURED` (1), `ABOP_RUN_LLM_RETRIES/BACKOFF/TRUNCATE`, `ABOP_RUN_CACHE`, `ABOP_TOOL_STEPS` (2 — шагов tool-calling на навык) | |
| Очередь | `ABOP_RUN_WORKERS` (4 на проде, код-дефолт 2), `ABOP_USER_CONCURRENT` (1), `ABOP_RUN_TIMEOUT` (600), `ABOP_RUN_STALE_SEC` (90 — переклад зависших раз в минуту), `ABOP_RUN_MEM_SOFT_MB` (бэкпрешер по RSS) | |
| Шина | `ABOP_BUS` (`kafka` \| `pg`), `ABOP_KAFKA_BROKERS` (`201.51.5.24:9092`), `ABOP_KAFKA_SASL_USER`, `ABOP_KAFKA_SASL_PASSWORD`, `ABOP_KAFKA_TOPIC_REQUESTS/RESULTS/DLQ`, `ABOP_KAFKA_GROUP` (`abop-run-workers`), `ABOP_KAFKA_EVENTS_GROUP` (`abop-triggers`), `ABOP_KAFKA_PARTITIONS` (3) | Брокер недоступен → откат на PgBus, задания не теряются |
| Rate-limit | `ABOP_USER_RATE_MAX` (30), `ABOP_USER_RATE_WINDOW` (300 с) | 429 при превышении |
| Планировщик | `ABOP_SCHEDULER` (0=выкл), `ABOP_SCHEDULER_TICK`, `ABOP_SCHEDULER_MIN_INTERVAL`, `ABOP_SCHEDULER_MAX_FIRES` | leader-election через advisory lock |
| RAG / данные | `SLAVA_API_BASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `EMBED_MODEL` (`baai/bge-m3`), `ABOP_1C_WEB_URL` (веб-клиент 1С для ссылок из карточек) | |
| OUT-доставка | `GOTENBERG_URL`, `BOOKSTACK_URL/TOKEN`, `MAILPIT_HOST/PORT`, `REDMINE_*`; реальные API — `YOUGILE_TOKEN/COLUMN_ID`, `YANDEX_SMTP_*` | Без токенов — dry_run. Команды через шину исполняет коннектор со **своими** секретами (`systems.env` на server-2) |
| Observability | `LANGFUSE_URL/PUBLIC_KEY/SECRET_KEY`, `LOG_LEVEL` | |

## 6а. Аудируемость, трассировка, наблюдаемость

- **Grafana дашборд** (`ops/grafana/`, auto-provisioning): datasource Prometheus + дашборд «ABOP — Обзор» (см. §3). Grafana `:3300`, Prometheus `:9091`.
- **Аудит-трасса прогона** (`result.trace`): по каждому навыку — какие сущности читал + **происхождение данных** (`ape.entity_provenance`: рецепт/источник/сколько записей/свежесть `fetched_at`), какая модель рассуждала, токены, тайминг, формат вывода, вызовы инструментов (`kind=tool` на доске). В каждом прогоне (`GET /api/runs/{id}`); структурированные ответы навыков — `result.skill_outputs`.
- **Сквозной trace_id** (`X-Trace-Id`), структурные JSON-логи (`docker logs abop-webapi`), per-skill тайминги (`run_metrics.timings`), «не молчим» (`soft_errors`).
- **Langfuse LLM-трейсы** (`server/langfuse_trace.py`): каркас, no-op без ключей. Self-host langfuse v3 требует ClickHouse+Redis+PG — на memory-tight хост не влезает, использовать облако или отдельную машину.

## 6б. Шина Redpanda и коннектор-воркер (server-2)

- **Топики** (создаются идемпотентно при старте API): `abop.runs.requests`, `abop.runs.results`, на каждую из систем реестра (`GET /api/systems`) — `abop.<система>.events` и `abop.<система>.commands`, единый `abop.dlq` (заголовки `source-topic`, `error`, `x-trace-id`, `command-id`). Схемы `abop.event/1.0` / `abop.command/1.0`: `{system, type, payload, ts, trace_id, actor}`.
- **События → триггеры**: `run_bus.events_loop` (группа `abop-triggers`) → `triggers.on_bus_event` → последние версии агентов с event-триггером на систему (ABAC по семье, фильтр `event_type`); служебные `command.*` триггеры не будят без явной подписки.
- **Команды → коннектор**: навык публикует через `publish_command_governed` (система в реестре + доступна семье + режим навыка; `write`/`payload.hitl` → HITL канала `command`), коннектор исполняет и отвечает `command.done|failed`.
- **Проверка**: `GET /api/bus` (драйвер, брокеры, топики), `GET /api/bus/tail?topic=abop.redmine.events`, `GET /api/bus/dlq`; `POST /api/bus/publish {system:"redmine", kind:"commands", type:"echo", payload:{}}` → `command.done` за миллисекунды. На server-2: `docker logs abop-connector`, `curl 127.0.0.1:9105/healthz`.
- **Поднять/обновить коннектор** (server-2): `docker build -t abop-connector connector/ && docker run -d --name abop-connector --restart unless-stopped --env-file /opt/abop-connector/systems.env -v abop_connector:/data -p 9105:9105 abop-connector`. `ABOP_CONNECTOR_DRY_RUN=1` — ничего наружу. Ошибочную команду переиграть = опубликовать заново с новым id.
- ⚠️ У `redpanda start` этой версии нет флага `--admin-addr` — admin API слушает 0.0.0.0:9644 внутри контейнера, достаточно `-p 9644:9644`.
- Подробно: [`CONNECTOR_WORKER.md`](CONNECTOR_WORKER.md), [`CONCEPT_SCALING_OBSERVABILITY.md` §15](CONCEPT_SCALING_OBSERVABILITY.md).

## 6в. LLM-бокс: адрес, override, каскад

- **Два места адреса.** Базовый — `.env` (`LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL`), применяется при старте контейнера. Поверх — **runtime-override** `POST /api/admin/llm {base_url, model?, api_key?}` (manager+; хранится в `admin_config` ключ `llmOverride`, переживает рестарт, разлетается по репликам через cachebus); пустой `base_url` → сброс на env. Текущее: `GET /api/admin/llm` или `GET /api/models` (`local.base_url`, `local.override`, `profiles.standard.cascade`, `active`).
- **Пересоздали бокс (новый IP:port)** → либо `POST /api/admin/llm` с новым адресом (без рестарта), либо поправить `.env` и пересоздать контейнер. Если override указывает на недостижимый бокс, при старте он игнорируется — лог `llm.override_unreachable` (fallback на env).
- **Признаки отката каскада** (local → RouteAI, платно): в логах `docker logs abop-webapi | grep llm.cascade_fallback` (модель, base_url, ошибка), метрика `abop_llm_fallback_total{model}` в Prometheus/Grafana, в прогоне `model` навыка ≠ `local/qwen3-30b-a3b` и стоимость > 0. Типичная причина — ReadTimeout на длинных схемах: у своего бокса таймаут `ABOP_LOCAL_LLM_TIMEOUT=300`, у облака `ABOP_LLM_TIMEOUT=120`.
- **Проверка бокса напрямую**: `curl http://<ip>:<port>/v1/models`; в ABOP — `GET /api/models` и тестовый прогон (`skill_outputs` с моделью local, стоимость 0 ₽).
- Инцидент 27.09: основной бокс застрял в очереди Vast, на время подняли новый; когда старый поднялся — вернулись на него (адрес выше), временный удалить/удалён. Правило: после смены бокса — `GET /api/models` и тестовый прогон с `model=local/…`, 0 ₽. Ключи и IP боксов — вне репо.

---

## 7. Производительность прогона (замеры 2026-09-18 … 2026-09-26)

Оптимизация прогона audit1c (10 находок, снимок 1С):
| | до | дайджест данных | повтор |
|---|---|---|---|
| время | 357 с | **110 с** | 299 с (разброс латентности) |
| входные токены | 779 655 | **93 836** | 93 836 |
| стоимость | 15.35 ₽ | **2.80 ₽** | 2.76 ₽ |
| находки/нормы | 10 / 5 | **10 / 5** (без изменений) | 10 / 6 |

**Выводы:** реальный и СТАБИЛЬНЫЙ выигрыш — **дайджест данных** в LLM-промпт (счётчики по типам = полный scope + сэмпл, вместо полного дампа): −8× входных токенов, −5× стоимость, качество 1:1 (детекцию считает детерминированный код, не LLM). Разброс времени 110↔299с — вариативность латентности RouteAI. Параллелизм на **одной** карте RouteAI упирается в троттлинг; выше 2-3 имеет смысл только со своим vLLM.

**Выделенный инстанс LLM (с 2026-09-18):** self-host **Qwen3-30B-A3B FP8** на Vast, local первым в каскаде, RouteAI fallback. На выделенной карте параллелизм заиграл: **conc=8 → 77с**, срез нарратива **max_tokens=1600 → ~50с**. Structured output (`response_format=json_schema`) + grounded объяснение (детекцию считает код ДО прогона, навык объясняет реальные находки): **357с → ~40с (≈9×)**, стоимость LLM ≈0, находки 10 (A1 B6 C1 D2) 1:1.

**Формат вывода навыка** — per-skill флаг `output` (structured|freeform), тумблер в редакторе навыка; хранится в PG (`skill_overrides.patch.output`). С 27.09 у 51 навыка есть свой **шаблон** (подробная схема + `max_tokens`), см. [`SKILL_TEMPLATES.md`](SKILL_TEMPLATES.md). Глобальный kill-switch `ABOP_RUN_STRUCTURED=0`.

**Нагрузка очереди (2026-09-26, `bench/LOAD_RESULTS.md`):** 20 пользователей Keycloak × 3 прогона, `ABOP_USER_CONCURRENT=1`: 2 воркера → 6,6 прогона/мин (ожидание p50 386 с), **4 воркера → 16,9 прогона/мин** (p50 99 с, исполнение p95 18 с), 0 сбоев, приём задания p95 516 мс, RSS < 100 МБ при лимите 1,5 ГБ, 0,34 ₽/прогон. Узкое место — воркеры × задержка LLM, не память/CPU. По ходу починены: падение сервера от `SystemExit` CLI-инструмента, вечно «running» задания после рестарта, файловые чтения в event loop.

**HTTP (2026-09-20, 1 uvicorn-воркер):** `/api/health` ~450 RPS; `/api/skills` 33→100-127 RPS после кэша парсинга `.md`; `/api/runs` ~50 RPS — потолок одного uvicorn-воркера (GIL, PG-пул 8); дальше — `--workers`/вторая реплика (§7а).

## 7а. Горизонтальное масштабирование (2+ реплики API)

Механика готова, реплики безопасно работают параллельно:
- **Очередь на N репликах**: `run_jobs` с `FOR UPDATE SKIP LOCKED` — воркеры любой реплики берут задания; дедуп по `idempotency_key`; `run_jobs.priority` есть в схеме (классы приоритетов — не реализованы).
- **Инвалидация кэшей между репликами** (`server/cachebus.py`): Postgres `LISTEN/NOTIFY` канал `abop_cache` (топики `skills`, `dataplane`, `llm`). Проверка: `docker logs abop-webapi | grep cachebus`.
- **Leader-election планировщика** (`triggers.scheduler_loop`): `pg_try_advisory_lock(0x41424F50)` — фаерит только лидер; лог `scheduler.leader_acquired`.
- **Admission control к LLM**: глобальный семафор `ABOP_LLM_MAX_INFLIGHT` (12); метрики `abop_llm_inflight`/`_max`.
- **Per-user rate-limit** и **идемпотентность прогонов** (двойной клик → 1 реальный + 1 cached). NB: rate-limit и idempotency-lock — per-replica (в памяти).
- **Что уже общее (PG):** agents/contracts/runs/run_jobs/skills/шаблоны/рецепты/admin/RBAC/память/HITL. **Canonical Data Plane** — в томе `abop_ape` (при нескольких хостах нужен общий том/перенос эмита в PG).
- **Вторая реплика — НЕ поднята**: на server-1 свободно ~370 МБ; реплике на server-2 нужен доступ к Postgres server-1 (туннель/TLS или перенос PG) + свой порт и LB/Caddy upstream.

## 8. Частые проблемы

| Симптом | Причина / решение |
|---|---|
| API отвечает 401 на всё | Auth fail-closed: не задан `KEYCLOAK_JWKS_URI` (или токен не от известного issuer — см. `ABOP_EXTRA_JWKS`). `ABOP_DEV_AUTH=1` — только локально |
| Прогон 0 находок, <1с | Data Plane пуст/протух — §4 |
| Задание висит `queued` | воркеры заняты (`GET /api/runs/queue`: глубина, воркеры, память) или лимит «1 прогон на пользователя»; `running` без движения > `ABOP_RUN_STALE_SEC` перекладывается автоматически |
| Прогон стал платным / медленным | каскад откатился на RouteAI: `grep llm.cascade_fallback`, `abop_llm_fallback_total`; проверить бокс `GET /api/models`, override `GET /api/admin/llm` — §6в |
| Ответ навыка «⚠ обрезан» | лимит токенов шаблона: поднять `max_tokens` в `template.json` (или `ABOP_RUN_MAX_TOKENS_TEMPLATE`) и переимпортировать — §2а |
| Команда коннектора не исполнилась | `GET /api/bus/dlq` (текст ошибки), `docker logs abop-connector` на server-2; система не в реестре/недоступна семье → `mode: denied` в прогоне; `ABOP_CONNECTOR_DRY_RUN=1` — только события |
| HITL-заявка без «✓ выполнено» | коннектор не ответил `command.done` за 90 с (десктоп перестаёт опрашивать) — смотреть `GET /api/hitl/{id}` (`result_state`, `result`) и DLQ |
| «Собрать и запустить» → «нет демо-пакета» | сценарий без `demo/<key>/` (готовы: audit1c, invest1c, fin) |
| «нет доступа к семье» | ABAC: у пользователя нет прав на семью — войти админом |
| OUT не отправился реально | у OUT-узла нет `✓ Реальная отправка` / стоит `Под HITL` / нет токенов в env |
| Фронт не обновился после деплоя | hard refresh (Ctrl+F5); бандл должен быть собран из `webapp/src` (`build.py check`) |
| Шаблон навыка в UI не тот, что в репо | ручная правка (`builtin=false`) имеет приоритет: `POST /api/schema-templates/{id}/reset` или `push_templates.py --force` |
| Двойной запуск триггера | планировщик фаерит только лидер (advisory lock); проверить `pg_locks objid=1094864720` |

---

## 9. Что дальше (см. `CONCEPT_SCALING_OBSERVABILITY.md` §15, `SESSION_SUMMARY_2026-09-27.md`)

Фаза 1 и Блоки 1–5 реализованы (очередь/async API, цепочки и HITL через очередь, шина + коннектор + tool-calling, продукт пилота 1С, фронт в исходниках + CI). Не сделано: вторая реплика API, приоритетные классы очереди и лимиты по отделам, доставка по шаблонам для остальных кейсов (инвест → BookStack, дайджест → письмо, БФТ → задача), страница «Шина/DLQ» в админке, прогресс прогона по навыкам, баннер состояния модели, диф находок между версиями.

---

## 10. ABOP Desktop: сайдкар-UI-прокси и schema-driven вывод

### 10.1 Сайдкар-UI-прокси («путь А»)
Проблема: правки UI десктопа требовали пересборки `.exe`. Наивный «путь А» (грузить UI прямо с публичного ABOP) ломается о **Chromium Private Network Access (PNA)** — публичная страница не может делать fetch на `127.0.0.1`.

Решение: сайдкар отдаёт UI **с того же origin, что и API** (`127.0.0.1/ui`), а свежую версию берёт с сервера:
- ABOP: `GET /desktop-ui-bundle` → `{version, files:{relpath:text}}` по всем файлам `desktop/ui/`.
- Сайдкар (`desktop/sidecar/app.py`): `_sync_ui()` на старте тянет бандл в `DATA_DIR/ui-cache` (атомарно `.new`→rename), фолбэк `_bundled_ui()` (копия `ui_fallback`, вшитая PyInstaller). Монтирует `/ui` как StaticFiles.
- Electron (`main.js`): грузит `apiBase + "/ui/index.html?api=…"`; при сбое — локальный фолбэк.

**Итог:** правки UI выкатываются обычным деплоем (`desktop/ui` в архиве, §2) — `.exe` пересобирать не нужно. Пересборка (тег `desktop-vX.Y.Z`, ставит владелец) нужна только при изменении Python-сайдкара/Electron-оболочки — см. [`RELEASE_DISCIPLINE.md`](RELEASE_DISCIPLINE.md). Текущая версия — 1.0.8 (последний тег 1.0.7).

### 10.2 Schema-driven вывод (вид без передеплоя)
Вид отчёта и структура извлечения — в БД, правятся без пересборки образа:
- `report_templates` (`server/report_store.py`) — HTML/CSS/PDF-опции отчёта; посев из `reports/<id>.html` (default, audit1c, invest, digest). `render()` — безопасная подстановка `{{key}}` (без exec/Jinja); `{{skills}}` / `{{skill_<sid>}}` — структурированные ответы навыков. API `GET/POST/DELETE /api/report-templates`, `/preview`; отчёт прогона по требованию — `GET /api/runs/{id}/report?template=&format=html|pdf`; OUT-узел ссылается по `report_template_id`.
- `schema_templates` (`server/schema_store.py`, посев `server/skill_templates.py`) — JSON Schema извлечения + инструкция + `max_tokens` + `delivery`. `response_format()` → OpenAI `json_schema` strict. Навык ссылается по `schema_template_id`, иначе берётся шаблон с id навыка. API `GET/POST/DELETE /api/schema-templates`, `/import`, `/{id}/reset`.
- LLM только **форматирует** по схеме, не «сочиняет» поля.

### 10.3 Единые реестры и адресность
- `families_store` — единый реестр семей (сид из `ape.AGENT_FAMILIES` + кастомные из UI); семья=отдел (ABAC). API `/api/families`.
- `systems_store` — реестр систем (эндпоинты, egress, scope семей, Kafka-топики). API `/api/systems`; на каждую систему — пара топиков шины.
- `identity_store` — **сквозной ID**: `identity{uid,system,external_id,display,attrs}`, `bundle(uid)` → карта систем. Агент адресен: доставка идёт под аккаунтом юзера. API `/api/identity/*`.
- `edit_scope` агента: `family∈{management}` или `source=authored` → правит **пользователь**; специализированные (Аудитор 1С, Финаналитик, Следователь) → только **методолог**. Отдаётся как `can_edit`.

### 10.4 Планировщик — только последняя версия
`triggers._latest_versions()` дедуплицирует по `contract_audit_id`, оставляя max-версию. Чинит дубли писем (агент срабатывал во всех старых версиях) и «удаление расписания не работает».

## Остаток на GPU в интерфейсе (необязательно)

Баннер Обзора показывает кредит арендованного GPU, если серверу дан ключ провайдера:

```bash
echo "VAST_API_KEY=$(cat /root/.vast_api_key)" >> /opt/abop/.env && docker restart abop-webapi
```

Ключ читается только сервером (наружу не отдаётся), ответ кэшируется на 10 минут; без ключа блок GPU скрыт.
Без кредита бокс останавливают, и прогоны молча уходят в облачный каскад — баннер это показывает («откатов на облако: N»).
