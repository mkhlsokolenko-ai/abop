# ABOP — Справочник эндпоинтов и портов

> Актуально на 2026-09-27. Источник истины: `server/web_api.py` (роуты, `grep -n '@app\.' server/web_api.py`), `server/systems_store.py` + `ops/prometheus.yml` + `connector/worker.py` (порты). Обновлять при добавлении роутов/сервисов. Runbook — [`DEPLOY.md`](DEPLOY.md).

---

## 1. Серверы и порты

Два сервера + GPU-бокс. **server-1** `5.129.192.63` — ABOP + БД/auth/мониторинг + прод-сервисы для демо-кейсов. **server-2** `201.51.5.24` — шина Redpanda, коннектор-воркер, sLAVA (RAG), рендер/данные 1С. **GPU-бокс** (Vast) — self-host LLM.

### 1.1 ABOP и инфраструктура (server-1: 5.129.192.63)
| Сервис | Порт | Назначение | Доступ |
|---|---|---|---|
| **ABOP Web API** | **8091** | REST/SSE фронта, десктоп-сайдкара и агентов (`uvicorn server.web_api:app`, `--network host`); внутри — воркеры очереди прогонов | наружу (за Caddy/LB) |
| Postgres | 5433 | Каноническое хранилище (agents/contracts/runs/run_jobs/skills/schema_templates/report_templates/hitl/finding_labels/…) | localhost |
| Keycloak | 8811 | SSO/OIDC, realm `abop` (JWT); логин наружу — только через `/api/auth/login` | localhost/за прокси |
| **Prometheus** | **9091** | Scrape: `abop-webapi` (:8091/metrics), `redpanda` (server-2 :9644), `abop-connector` (server-2 :9105) | внутр. |
| **Grafana** | **3300** | Дашборд `abop-overview` (admin/admin — сменить) | внутр. |

### 1.2 Прод-сервисы для демо-кейсов (server-1: 5.129.192.63) — реестр систем
| Система | Порт | Kind | Назначение |
|---|---|---|---|
| Redmine | 3000 | rest | Трекер задач → entity:issue; **цель доставки** `redmine/issue.create` (факт 27.09: задача #18 из находки аудитора) |
| Gitea | 3001 | rest | Git-хостинг (ABAC: architecture/engineering) |
| Twenty (CRM) | 3002 | rest | CRM → entity:customer (ABAC: management/analytics) |
| BookStack | 6875 | rest | Вики/документы → entity:document; OUT-доставка и `bookstack/page.publish` |
| Mailpit (web) | 8025 | rest | Просмотр демо-почты; событие-триггер |
| Mailpit (SMTP) | 1025 | smtp | Приём писем OUT-доставки и `mailpit/email.send` |
| NocoDB | 8090 | rest | No-code БД (мок 1С) |
| Kroki | 8000 | rest | Рендер диаграмм (ABAC: architecture/engineering) |
| MinIO | 9000 | s3 | Объектное хранилище (отчёты/PDF) |

Каждая система реестра получает пару топиков шины `abop.<id>.events` / `abop.<id>.commands` (см. §2.7).

### 1.3 server-2 (201.51.5.24)
| Сервис | Порт | Назначение | Доступ |
|---|---|---|---|
| **Redpanda** (Kafka API) | **9092** | Шина ABOP: `abop.runs.*`, `abop.<система>.events/commands`, `abop.dlq`; SASL/SCRAM user `abop` | только server-1 (DOCKER-USER/ufw) |
| Redpanda admin | 9644 | `/public_metrics` для Prometheus | только server-1 |
| **Коннектор-воркер** `abop-connector` | **9105** | `/healthz`, `/metrics`; исполняет `abop.*.commands` (Redmine/BookStack/Mailpit/1С/echo) | только server-1 и loopback |
| **sLAVA API** | **8000** | Граф-RAG: `/api/v1/{ingest,query,collections}`; корпусы `slava_fam_*`, `slava_audit1c_norms` | server-1 |
| Qdrant | 6333 | Вектор-БД sLAVA (localhost на server-2; ABOP ходит через sLAVA API) | локально |
| Gotenberg | 3050 | HTML→PDF (рендер отчётов) | server-1 |
| Данные 1С (dump + веб-клиент) | 8092 | `audit-data/dump.json` — снимок 1С для коннектора audit1c; `/audit/` — веб-клиент для ссылок из карточек находок | server-1 / браузер |

### 1.4 GPU-бокс и внешние API
| Сервис | Хост:порт | Назначение |
|---|---|---|
| **vLLM Qwen3-30B-A3B FP8** (Vast, L40S) | `95.3.33.46:44633/v1` (меняется при пересоздании; override через `POST /api/admin/llm`) | Основной LLM, первый в каскаде, 0 ₽; `response_format=json_schema` |
| RouteAI (LLM) | routerai.ru/api/v1 (443) | Fallback каскада (qwen/deepseek), эмбеддинги/rerank; откат виден в `abop_llm_fallback_total` |
| Яндекс.Почта (SMTP) | smtp.yandex.ru:465 | Реальная OUT-доставка (env `YANDEX_SMTP_*`) |
| YouGile | ru.yougile.com/api-v2 (443) | Реальная OUT-доставка — создание задачи (env `YOUGILE_*`) |

---

## 2. API эндпоинты (ABOP Web API, база `http://5.129.192.63:8091`)

Аутентификация: JWT Keycloak в заголовке `Authorization: Bearer <token>` (фронт добавляет из `localStorage['abop.token']`, десктоп — через сайдкар). **Fail-closed**: без настроенного `KEYCLOAK_JWKS_URI` любой запрос → 401; `ABOP_DEV_AUTH=1` (только dev/CI) делает анонима admin. Уровни: **open**, **user** (любой аутентиф.), **manager+** (manager/support/admin), **support+**, **admin**, **ABAC** (видимость по семье/отделу). Мутации — manager+. Все ответы несут сквозной `X-Trace-Id`.

### 2.1 Служебные / модели / observability
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/health` | Живость + счётчики (семьи/навыки/адаптеры) | open |
| GET | `/api/llm/status` | Состояние модели: активная и каскад, свой бокс (доступен/нет, override или .env), откаты на облако (сколько + последний), тариф за прогон, остаток GPU (если задан `VAST_API_KEY`) | user |
| GET | `/api/models` | Реальные каскады по профилям (`standard/research/code`, `active` = первая модель) + `local` (base_url/model/override/configured) | open |
| GET | `/api/admin/llm` | Текущий self-host LLM-эндпоинт (env + override) | user |
| POST | `/api/admin/llm` | Override бокса без рестарта: `{base_url, model?, api_key?}`; пустой `base_url` → сброс на env; хранится в `admin_config`, разлетается по репликам | manager+ |
| GET | `/metrics` | Метрики Prometheus (scrape) | open (внутр.) |
| GET | `/api/observability` | JSON-срез метрик (дашборды ABOP) | user |
| GET | `/desktop-ui-bundle` | Весь UI десктопа `{version, files}` — сайдкар тянет при старте (Путь А) | open |

### 2.2 Аутентификация / профиль / сквозной ID
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| POST | `/api/auth/login` | Прокси-логин к Keycloak по username/password → JWT | open |
| GET | `/api/me` | Текущий пользователь (роль/отдел/уровень) | user |
| GET | `/api/me/scenarios` / POST | Пер-юзер черновики сценариев канвы (PG) | user |
| GET | `/api/identity/me` | Сквозной профиль: мастер-UID + department/roles + аккаунты в системах | user |
| GET | `/api/identity/{uid}` | Профиль по UID (чужой — admin/support) | user/support+ |
| POST | `/api/identity/{uid}` | Привязать внешний аккаунт (account-linking) | admin |
| DELETE | `/api/identity/{uid}/{system}` | Отвязать аккаунт в системе | admin |

### 2.3 Чат
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| POST | `/api/chat` | Свободный LLM-ответ под JWT (единый канал десктоп-чата) | user |
| POST | `/api/chat/stream` | То же, SSE-стрим дельт | user |

### 2.4 Контракты (приём из LUDA)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| POST | `/api/contracts/ingest` | Приём/валидация handoff-бандла `luda.*/1.0` → ContractSet | manager+ |
| GET | `/api/contracts` | Список принятых ContractSet | user/ABAC |
| GET | `/api/contracts/{audit_id}` | Полный контракт (для канвы) | user/ABAC |

### 2.5 Агенты (сборка → AgentVersion, версии, триггеры)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/agents` | Список агентов (ABAC по семье; `?contract=`, `?archived=`) | user/ABAC |
| GET | `/api/agents/{agent_id}` | Полный AgentVersion (граф + метаданные) | user |
| GET | `/api/agents/spec` | Spec агента по `{family,member}` (ADR-032) | user |
| POST | `/api/agents` | Сохранить граф как AgentVersion (draft; `revise=true` — новая версия) | manager+ |
| POST | `/api/agents/check` | Governance-проверка графа без сохранения | user |
| POST | `/api/agents/match` | Подбор агента под задачу (лексика + семантика, дедуп до последней версии) | user |
| POST | `/api/agents/author` | Авторская сборка агента без контракта («мой агент») | user |
| POST | `/api/agents/{agent_id}/status` | deployed / paused | manager+ |
| GET / POST | `/api/agents/{agent_id}/rollback` | Диф с предыдущей версией / откат (текущая → Лимб) | user / manager+ |
| POST | `/api/agents/{agent_id}/retire` / `/restore` | В Лимб / вернуть | manager+ |
| DELETE | `/api/agents/{agent_id}` | Жёсткое удаление всех версий | manager+ |
| POST | `/api/agents/{agent_id}/triggers` | Сделать задачу регулярной (триггер-узел «расписание» → новая версия) | user |
| DELETE | `/api/agents/{agent_id}/triggers/{trigger_id}` | Убрать расписание (новая версия) | user |
| GET | `/api/triggers` | Все триггеры + последний фаер | user |
| GET | `/api/triggers/mine` | Мои расписания | user |
| POST | `/api/triggers/{agent_id}/{trigger_id}/fire` | Ручной запуск триггера (чтит spawn-политику/HITL) | manager+ |
| POST | `/api/demo/prepare` | «Собрать и запустить»: идемпотентный ингест + агент из `demo/<scenario>/` | user/ABAC |
| GET | `/api/fleet` | Флот из реальных источников: развёртывания, живые задания, алерты, история 8 дней | user/ABAC |

### 2.6 Прогоны, очередь, цепочки
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| POST | `/api/runs` | Запуск прогона `{agent_id}` или `{contract_audit_id}`. Синхронно → 201 с результатом; `{async:true}` или `?async=1` → **202** `{job_id, status, position, poll}` | user |
| GET | `/api/runs/jobs/{job_id}` | Статус задания: queued (позиция) / running (progress) / awaiting_hitl / done (+ результат) / failed / cancelled | user |
| POST | `/api/runs/jobs/{job_id}/cancel` | Отмена: из очереди сразу, выполняющееся — кооперативно | user |
| GET | `/api/runs/jobs` | Мои задания (admin/support — все) | user |
| GET | `/api/runs/queue` | Состояние очереди: глубина по статусам, воркеры, лимиты, RSS, шина | support+ |
| GET | `/api/runs` | Журнал прогонов (ABAC; `?agent_id=`, `?limit=1..500`) | user/ABAC |
| GET | `/api/runs/{run_id}` | Полный прогон: findings/investigations/delivery/soft_errors/trace/`skill_outputs`/trace_id | user |
| GET | `/api/runs/{run_id}/metrics` | RunMetrics (`abop.run_metrics/1.0`) | user |
| GET | `/api/runs/{run_id}/diff?vs=` | Сравнение прогонов: находки и расследования (появились/ушли/изменились), навыки, метрики; без `vs` — предыдущий прогон агента | user |
| GET | `/api/runs/{run_id}/report` | Отчёт прогона по шаблону: `?template=<id>&format=html|pdf` (default/audit1c/invest/digest; PDF через рендерер ABOP) | user |
| GET | `/api/runs/{run_id}/stream` | SSE-стрим прогона (501 — контракт зафиксирован, не реализован) | user |
| GET | `/api/billing` | Реальные токены/₽ по прогонам (ABAC) + квота | user/ABAC |
| GET / POST | `/api/pipelines` | Цепочки агентов (список / создать) | user |
| DELETE | `/api/pipelines/{pid}` | Удалить цепочку | user |
| POST | `/api/pipelines/{pid}/run` | Запуск цепочки; `?async=1` → 202, шаги через очередь, HITL-пауза `awaiting_hitl` | user |
| POST | `/api/pipelines/suggest` | Авто-сборка цепочки под задачу | user |

### 2.7 Шина (Redpanda)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/bus` | Драйвер (`kafka`/`pg`), брокеры, известные топики, пары топиков систем | user |
| POST | `/api/bus/publish` | `{system, kind: events|commands, type, payload}` — событие системы (вебхук/экспорт 1С) или команда коннектору | manager+ |
| GET | `/api/bus/tail?topic=` | Последние сообщения топика | support+ |
| GET | `/api/bus/dlq` | Ошибки из DLQ: source-topic, error, payload, trace_id + отметки разбора (`ack`: acked/replayed), `replayable`, `open` | support+ |
| POST | `/api/bus/dlq/replay` | `{partition, offset}` — повторить команду коннектора из DLQ (новый id, отметка replayed) | manager+ |
| POST | `/api/bus/dlq/ack` | `{partition, offset, note}` — списать сообщение DLQ (разобрано руками), отметка в аудит | manager+ |

### 2.8 HITL (очередь подтверждений)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/hitl/queue` | Pending-заявки (ABAC по семье агента) | user |
| GET | `/api/hitl/{item_id}` | Заявка с превью: `id, state, agent_id, agent_name, title, channel, to, format, subject, html, created_at, requested_by`, **`kind`** (`report`/`command`), **`system`**, **`type`** (напр. `issue.create`), `command_id`, **`result_state`**, **`result`** (`issue_id`, `url` или ошибка от коннектора), `source` | user |
| POST | `/api/hitl/{item_id}/approve` | `{decision: approve|reject, reason?}`: OUT-доставка наружу / команда в `abop.<система>.commands` / возобновление шага цепочки / spawn прогона | manager+ |

### 2.9 Шаблоны навыков (schema-templates) и отчётов (report-templates)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/schema-templates` / `/{tid}` | Шаблоны извлечения из БД (`id,name,json_schema,instruction,builtin,editor,max_tokens,delivery`) | user |
| POST | `/api/schema-templates/import` | Импорт `{templates:[…], source:"repo"|"manual", force}` без пересборки образа (`scripts/push_templates.py`) | admin |
| POST | `/api/schema-templates/{tid}` | Создать/править (JSON Schema + инструкция + max_tokens + delivery) → `builtin=false`; 422 при не-strict схеме | admin |
| POST | `/api/schema-templates/{tid}/reset` | Сбросить к версии из репо (`skills/<id>/template.json` в образе) | admin |
| DELETE | `/api/schema-templates/{tid}` | Удалить | admin |
| GET | `/api/report-templates` / `/{tid}` | Шаблоны отчётов (HTML+CSS+pdf_options; посев из `reports/`) | user |
| POST | `/api/report-templates/{tid}` | Создать/править шаблон (плейсхолдеры `{{title}}/{{findings}}/{{skills}}/{{skill_<sid>}}/…`) | admin |
| POST | `/api/report-templates/{tid}/preview` | Превью рендера на демо-данных → HTML | user |
| DELETE | `/api/report-templates/{tid}` | Удалить | admin |

### 2.10 Находки пилота 1С
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/runs/{run_id}/findings` | Карточки находок прогона (участки, тип, сумма, цепочка с разрывом, объяснение, норма, разметка) + метрики | user |
| POST | `/api/runs/{run_id}/findings/{finding_id}/label` | Слепая разметка: `{decision: confirmed|rejected|unsure, manual_miss, comment}` | user |
| DELETE | `/api/runs/{run_id}/findings/{finding_id}/label` | Снять экспертную разметку (ошибочная метка не искажает метрики пилота) | manager+ |
| DELETE | `/api/runs/{run_id}/findings/{finding_id}/label` | Снять разметку | user |
| GET | `/api/findings?limit=&runs=&agent_id=` | Журнал пилота по последним прогонам аудитора/следователя + метрики | user/ABAC |
| GET | `/api/audit1c/norms/{name}` | Текст нормы из справочника `demo/audit1c/norms` | user |

### 2.11 Data Plane (рецепты/коннекторы/данные/расчёты)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/data/lineage` | Граф данных (источники→сущности→навыки→агенты) | user |
| GET | `/api/impact?kind=&id=` | Анализ воздействия: что затронет правка сущности/рецепта/навыка/системы/семьи, ₽/токены | user |
| GET | `/api/data/adapters` | Каталог адаптеров источников | user |
| GET | `/api/data/schema/{entity}` | Data Contract сущности | user |
| GET | `/api/data/query/{entity}` | Записи канонической сущности (свежие) | user |
| GET | `/api/data/recipes` / `/{name}` | Рецепты трансформации | user |
| POST | `/api/data/recipes` | Сохранить рецепт (→PG) | manager+ |
| DELETE | `/api/data/recipes/{name}` | Удалить рецепт | manager+ |
| POST | `/api/data/recipes/{name}/run` / `/rebind` | Прогнать / перепривязать рецепт | manager+ |
| POST | `/api/data/recipe/preview` | Dry-run рецепта | user |
| GET | `/api/data/connectors` | Коннекторы источников (+ резолв системы реестра, `allowed`) | user |
| POST | `/api/data/connectors` / `/test` | Сохранить / протестировать коннектор | manager+ |
| DELETE | `/api/data/connectors/{cid}` | Удалить коннектор | manager+ |
| POST | `/api/compute` | Whitelisted-расчёт `stats/group_by/top/reconcile` над Data Plane (без exec) | user |

### 2.12 Навыки и семьи
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/skills` / `/{sid}` | Каталог/тело навыка (+PG-правки поверх `.md`, `tools:` из frontmatter) | user/ABAC |
| POST | `/api/skills/{sid}` | Правка навыка новой версией (→PG; `output`, `schema_template_id`) | user |
| POST | `/api/skills/{sid}/datasources` | Data-need навыка (→PG) | user |
| GET / POST | `/api/families` | Единый реестр семей (сид из кода + кастомные) | user / manager+ |
| DELETE | `/api/families/{fid}` | Удалить кастомную семью | admin |
| GET / POST | `/api/memory/{scope}` | Память агента/процесса (общая, PG) | user |

### 2.13 Админка / RBAC / реестр систем / доступ
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/admin/users` / `/staff` / `/audit` / `/rbac` | Пользователи Keycloak / штат из Redmine / журнал аудита / RBAC-матрица | support+ |
| GET / POST | `/api/admin/config` | Админ-настройки в PG (модели/квоты/пороги/дерево; `llmOverride`) | user / manager+ |
| GET | `/api/systems` / `/{sid}` | Реестр систем: эндпоинты, egress, scope семей, Kafka-топики | user |
| POST / DELETE | `/api/systems/{sid}` | Сохранить / удалить систему | manager+ |
| GET | `/api/access/manifest?key=` | Least-privilege манифест области | user |
| POST | `/api/access/check` | `{department|family, system_id}` → `{allowed, reason}` (отказ в аудит) | user |

### 2.14 Регламент / конформанс / sLAVA
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/reglament` | Чанки регламента (НСИ-ключи) | user |
| POST | `/api/reglament/ingest` | Разметка + эмбеддинг регламента | manager+ |
| POST | `/api/reglament/graph/build` | Граф регламента (иерархия НСИ + нормы) | manager+ |
| GET | `/api/process/graph` | Граф регламента для «Карты данных» | user |
| POST | `/api/process/conformance` | Сверка процесса с регламентом (drift) | user |
| GET | `/api/family/collections` | Коллекции sLAVA + доступность отделу | user |
| POST | `/api/family/query` | RAG-поиск по корпусу семьи (ABAC) | user/ABAC |
| POST | `/api/family/ingest` / `/seed` / `/seed-norms` | Загрузка знания / посев навыков / раздача норм по семьям | manager+ |

### 2.15 Канва / планирование
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET / POST | `/api/canvas/layout/{key}` | Раскладка узлов канвы | user |
| POST | `/api/plan` | Превью декомпозиции цели по семьям (без LLM) | user |

---

## 3. Переменные окружения (интеграционная поверхность)

Задаются в `/opt/abop/.env` контейнера `abop-webapi` (server-1); у коннектора — свой `/opt/abop-connector/systems.env` (server-2).

**База/аутентификация:** `POSTGRES_DSN`, `KEYCLOAK_ISSUER`, `KEYCLOAK_JWKS_URI` (без него — 401 на всё), `KEYCLOAK_JWKS_INTERNAL`, `KEYCLOAK_AUDIENCE`, `ABOP_EXTRA_JWKS` (issuer десктопа), `ABOP_DEV_AUTH` (=1 только dev/CI), `ABOP_JWT_CACHE_TTL/MAX`.
**LLM:** `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL`, `LOCAL_LLM_API_KEY`, `ROUTEAI_BASE_URL`, `ROUTEAI_API_KEY`, `ROUTEAI_STANDARD_CASCADE` / `ROUTEAI_RESEARCH_CASCADE` / `ROUTEAI_CODE_CASCADE`, `ABOP_LOCAL_LLM_TIMEOUT` (300), `ABOP_LLM_TIMEOUT` (120), `ABOP_LLM_MAX_INFLIGHT` (12).
**Прогон:** `ABOP_RUN_LLM_CONCURRENCY`, `ABOP_RUN_LLM_RETRIES`, `ABOP_RUN_LLM_BACKOFF`, `ABOP_RUN_LLM_TRUNCATE`, `ABOP_RUN_MAX_TOKENS`, `ABOP_RUN_MAX_TOKENS_TEMPLATE` (3200), `ABOP_RUN_MAX_TOKENS_FREE`, `ABOP_RUN_STRUCTURED`, `ABOP_RUN_CACHE`, `ABOP_TOOL_STEPS` (2), `ABOP_ENT_SIG_TTL`.
**Очередь прогонов:** `ABOP_RUN_WORKERS` (2; прод 4), `ABOP_USER_CONCURRENT` (1), `ABOP_RUN_TIMEOUT` (600), `ABOP_RUN_STALE_SEC` (90), `ABOP_RUN_MEM_SOFT_MB`.
**Шина:** `ABOP_BUS` (`kafka`|`pg`), `ABOP_KAFKA_BROKERS`, `ABOP_KAFKA_SASL_USER`, `ABOP_KAFKA_SASL_PASSWORD`, `ABOP_KAFKA_TOPIC_REQUESTS`, `ABOP_KAFKA_TOPIC_RESULTS`, `ABOP_KAFKA_TOPIC_DLQ` (`abop.dlq`), `ABOP_KAFKA_GROUP` (`abop-run-workers`), `ABOP_KAFKA_EVENTS_GROUP` (`abop-triggers`), `ABOP_KAFKA_PARTITIONS` (3).
**Коннектор (server-2):** `ABOP_KAFKA_*` (те же), `ABOP_CONNECTOR_GROUP` (`abop-connectors`), `ABOP_CONNECTOR_RETRIES` (3), `ABOP_CONNECTOR_DB` (`/data/connector.sqlite`), `ABOP_CONNECTOR_DRY_RUN`, `CONNECTOR_PORT` (9105), `REDMINE_BASE`/`REDMINE_API_KEY`/`REDMINE_PROJECT`, `BOOKSTACK_URL`/`BOOKSTACK_TOKEN`, `MAILPIT_HOST`/`MAILPIT_PORT` (публичные адреса server-1).
**Rate-limit / планировщик:** `ABOP_USER_RATE_MAX` (30), `ABOP_USER_RATE_WINDOW` (300), `ABOP_SCHEDULER` (0=выкл), `ABOP_SCHEDULER_TICK`, `ABOP_SCHEDULER_MIN_INTERVAL`, `ABOP_SCHEDULER_MAX_FIRES`.
**RAG / данные:** `SLAVA_API_BASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `EMBED_MODEL` (`baai/bge-m3`), `ABOP_1C_WEB_URL` (`http://201.51.5.24:8092/audit/`).
**OUT-доставка (демо):** `GOTENBERG_URL`, `BOOKSTACK_URL`, `BOOKSTACK_TOKEN`, `MAILPIT_HOST`, `MAILPIT_PORT`.
**OUT-доставка (реальные API, включаются при заданных):** `YOUGILE_TOKEN`, `YOUGILE_COLUMN_ID`, `YANDEX_SMTP_USER`, `YANDEX_SMTP_PASSWORD`, `YANDEX_SMTP_HOST`, `YANDEX_SMTP_PORT`.
**Observability:** `LANGFUSE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (каркас), `LOG_LEVEL`.

---

## 4. Как запустить/обновить сервисы

**ABOP Web API (server-1)** — `bash ops/deploy-webapi.sh` (эквивалент):
```
cd /opt/abop && docker build -q -f Dockerfile.webapi -t abop-webapi . && \
docker rm -f abop-webapi; docker run -d --name abop-webapi --network host --memory=1536m \
  --restart unless-stopped --env-file /opt/abop/.env -v abop_ape:/root/.ape abop-webapi
```
**Шаблоны навыков без пересборки:** `python scripts/push_templates.py --base http://5.129.192.63:8091 --user <admin> --password '…'`.
**Observability-стек (Prometheus :9091 + Grafana :3300):**
```
cd /opt/abop && docker compose -f ops/docker-compose.observability.yml up -d
```
**Коннектор-воркер (server-2):** образ из `connector/` (`EXPOSE 9105`, том `/data`), env `/opt/abop-connector/systems.env`, том `abop_connector` — см. [`CONNECTOR_WORKER.md`](CONNECTOR_WORKER.md) и [`DEPLOY.md` §6б](DEPLOY.md).

> `VAST_API_KEY` (необязательно, в `/opt/abop/.env`): включает блок остатка на арендованном GPU в `/api/llm/status` и баннере Обзора (кредит, ставка в час, часов до нуля; кэш 10 мин). Без ключа блок просто не показывается.
