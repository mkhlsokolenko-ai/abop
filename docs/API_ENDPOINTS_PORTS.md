# ABOP — Справочник эндпоинтов и портов

> Актуально на 2026-09-18. Источник истины: `server/web_api.py` (роуты), `server/systems_store.py` + `cli/ape.py` + `ops/` (порты). Обновлять при добавлении роутов/сервисов.

---

## 1. Серверы и порты

Два сервера. **server-1** `5.129.192.63` — ABOP + прод-сервисы для демо-кейсов. **server-2** `201.51.5.24` — sLAVA (RAG) + рендер/данные 1С.

### 1.1 ABOP и инфраструктура (server-1: 5.129.192.63)
| Сервис | Порт | Назначение | Доступ |
|---|---|---|---|
| **ABOP Web API** | **8091** | REST/SSE фронта и агентов (`uvicorn server.web_api:app`, `--network host`) | наружу (за Caddy/LB) |
| Postgres | 5433 | Каноническое хранилище (agents/contracts/runs/skills/data/…) | localhost |
| Keycloak | 8811 | SSO/OIDC, realm `abop` (JWT-аутентификация) | localhost/за прокси |
| **Prometheus** | **9091** | Scrape метрик ABOP `/metrics` (Observability Фаза 0) | внутр. |
| **Grafana** | **3300** | Дашборды метрик (admin/admin — сменить) | внутр. |

### 1.2 Прод-сервисы для демо-кейсов (server-1: 5.129.192.63) — реестр систем
| Система | Порт | Kind | Назначение |
|---|---|---|---|
| Redmine | 3000 | rest | Трекер задач → entity:issue |
| Gitea | 3001 | rest | Git-хостинг (ABAC: architecture/engineering) |
| Twenty (CRM) | 3002 | rest | CRM → entity:customer (ABAC: management/analytics) |
| BookStack | 6875 | rest | Вики/документы → entity:document; **канал OUT-доставки** |
| Mailpit (web) | 8025 | rest | Просмотр демо-почты; событие-триггер |
| Mailpit (SMTP) | 1025 | smtp | Приём писем OUT-доставки (демо) |
| NocoDB | 8090 | rest | No-code БД (мок 1С) |
| Kroki | 8000 | rest | Рендер диаграмм (ABAC: architecture/engineering) |
| MinIO | 9000 | s3 | Объектное хранилище (отчёты/PDF) |

### 1.3 server-2 (201.51.5.24)
| Сервис | Порт | Назначение |
|---|---|---|
| **sLAVA API** | **8000** | Граф-RAG: `/api/v1/{ingest,query,collections}`; корпусы `slava_fam_*`, `slava_audit1c_norms` |
| Qdrant | 6333 | Вектор-БД sLAVA (localhost на server-2; ABOP ходит через sLAVA API) |
| Gotenberg | 3050 | HTML→PDF (рендер отчётов OUT) |
| Данные 1С (dump) | 8092 | `audit-data/dump.json` — снимок 1С для коннектора audit1c |

### 1.4 Внешние API (интернет)
| Сервис | Хост:порт | Назначение |
|---|---|---|
| RouteAI (LLM) | routerai.ru/api/v1 (443) | LLM-шлюз (chat/embed/rerank), каскады моделей |
| Яндекс.Почта (SMTP) | smtp.yandex.ru:465 | Реальная OUT-доставка (SSL; env YANDEX_SMTP_USER/PASSWORD) |
| YouGile | ru.yougile.com/api-v2 (443) | Реальная OUT-доставка — создание задачи (env YOUGILE_TOKEN/COLUMN_ID) |

---

## 2. API эндпоинты (ABOP Web API, база `http://5.129.192.63:8091`)

Аутентификация: JWT Keycloak в заголовке `Authorization: Bearer <token>` (фронт добавляет автоматически из `localStorage['abop.token']`). Уровни: **open** (без токена), **user** (любой аутентиф.), **manager+** (manager/support/admin), **ABAC** (видимость по семье/отделу). Все ответы прогонов несут сквозной `X-Trace-Id`.

### 2.1 Служебные / Observability
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/health` | Живость + счётчики (семьи/навыки/адаптеры) | open |
| GET | `/metrics` | Метрики Prometheus (scrape) | open (внутр.) |
| GET | `/api/observability` | JSON-срез метрик (дашборды ABOP) | user |

### 2.2 Аутентификация / профиль
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| POST | `/api/auth/login` | Логин по username/password → JWT | open |
| GET | `/api/me` | Текущий пользователь (роль/отдел/уровень) | user |
| GET | `/api/me/scenarios` | Пер-юзер черновики сценариев (канва) | user |
| POST | `/api/me/scenarios` | Сохранить пер-юзер сценарии | user |

### 2.3 Контракты (приём из LUDA)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| POST | `/api/contracts/ingest` | Приём/валидация handoff-бандла `luda.*/1.0` | user |
| GET | `/api/contracts` | Список принятых ContractSet | user |
| GET | `/api/contracts/{audit_id}` | Полный контракт (для канвы) | user |

### 2.4 Агенты (сборка → AgentVersion)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/agents` | Список агентов (ABAC по семье; `?contract=`, `?archived=`) | user/ABAC |
| GET | `/api/agents/{agent_id}` | Полный AgentVersion (граф) | user |
| GET | `/api/agents/spec` | Spec агента по `{family,member}` | user |
| POST | `/api/agents` | Сохранить граф как AgentVersion (draft; `revise=true` — версия) | user |
| POST | `/api/agents/check` | Governance-проверка графа без сохранения | user |
| POST | `/api/agents/author` | Авторская сборка агента | user |
| POST | `/api/agents/{agent_id}/retire` | В архив (Лимб) | manager+ |
| POST | `/api/agents/{agent_id}/restore` | Вернуть из Лимба | manager+ |
| DELETE | `/api/agents/{agent_id}` | Жёсткое удаление | manager+ |
| POST | **`/api/demo/prepare`** | «Собрать и запустить»: idempotent ингест контракта + сохранение агента из `demo/<scenario>/` | user/ABAC |

### 2.5 Прогоны (Run)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| POST | `/api/runs` | Запуск прогона (`{agent_id}` или `{contract_audit_id}`) → 201, `{run_id, **result}` | user |
| GET | `/api/runs` | Журнал прогонов (ABAC; `?agent_id=`) | user/ABAC |
| GET | `/api/runs/{run_id}` | Полный прогон (findings/investigations/delivery/soft_errors/trace_id) | user |
| GET | `/api/runs/{run_id}/metrics` | RunMetrics (`abop.run_metrics/1.0`: governance/cost/timings) | user |
| GET | `/api/runs/{run_id}/stream` | SSE-поток прогона | user |

### 2.6 HITL (пока каркас)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/hitl/queue` | Очередь подтверждений (501 — наполняется при исполнении) | user |
| POST | `/api/hitl/{item_id}/approve` | Подтвердить (501 — resume не дорезан) | manager+ |

### 2.7 Data Plane (рецепты/коннекторы/данные)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/data/lineage` | Граф данных (источники→сущности→навыки) | user |
| GET | `/api/data/adapters` | Каталог адаптеров источников | user |
| GET | `/api/data/schema/{entity}` | Data Contract сущности | user |
| GET | `/api/data/query/{entity}` | Записи канонической сущности | user |
| GET | `/api/data/recipes` / `/api/data/recipes/{name}` | Рецепты трансформации | user |
| POST | `/api/data/recipes` | Сохранить рецепт (→PG) | manager+ |
| POST | `/api/data/recipes/{name}/run` | Прогнать рецепт (наполнить canonical) | manager+ |
| POST | `/api/data/recipes/{name}/rebind` | Перепривязка рецепта (с warning-модалкой) | manager+ |
| POST | `/api/data/recipe/preview` | Предпросмотр рецепта | user |
| GET | `/api/data/connectors` | Коннекторы источников | user |
| POST | `/api/data/connectors` / `/api/data/connectors/test` | Сохранить/протестировать коннектор | manager+ |

### 2.8 Навыки (skills)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/skills` / `/api/skills/{sid}` | Каталог/тело навыка (+PG-правки поверх `.md`) | user |
| POST | `/api/skills/{sid}` | Правка навыка новой версией (→PG) | user |
| POST | `/api/skills/{sid}/datasources` | Data-need навыка (→PG) | user |
| GET | `/api/families` | Семьи агентов и их роли | user |

### 2.9 Админка / RBAC / биллинг
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/admin/users` / `/api/admin/staff` | Пользователи/сотрудники (Keycloak) | manager+ |
| GET | `/api/admin/audit` | Журнал аудита | manager+ |
| GET | `/api/admin/rbac` | Роли/капабилити из Keycloak | manager+ |
| GET | `/api/admin/config` | Сохранённые правки конфига (модели/квоты/пороги/дерево) | user |
| POST | `/api/admin/config` | Записать правку конфига (whitelist ключей) | manager+ |
| GET | `/api/billing` | Реальные токены/₽ по прогонам (ABAC) + квота | user/ABAC |

### 2.10 Реестр систем + доступ (ABAC / MCP-шлюз)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/systems` / `/api/systems/{sid}` | Реестр эндпоинтов/топиков систем | user |
| POST | `/api/systems/{sid}` | Сохранить систему (scope/egress/…) | manager+ |
| DELETE | `/api/systems/{sid}` | Удалить систему | manager+ |
| GET | `/api/access/manifest` | Least-privilege манифест для области (`?key=`) | user |
| POST | `/api/access/check` | Проверка доступа семьи к системе | user |

### 2.11 Триггеры (стартовые события)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/triggers` | Все триггеры + последний фаер | user |
| POST | `/api/triggers/{agent_id}/{trigger_id}/fire` | Ручной запуск триггера | manager+ |

### 2.12 Регламент / конформанс
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/reglament` | Чанки регламента (НСИ-ключи) | user |
| POST | `/api/reglament/ingest` | Разметка+эмбеддинг регламента (DeepSeek) | manager+ |
| POST | `/api/reglament/graph/build` | Построить граф регламента (рёбра НСИ/нормы) | manager+ |
| POST | `/api/process/conformance` | Сверка процесса с регламентом (cosine) | user |
| GET | `/api/process/graph` | Граф регламента (для «Карты данных») | user |

### 2.13 Семьи / sLAVA (RAG)
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/family/collections` | Коллекции sLAVA | user |
| POST | `/api/family/query` | Запрос к корпусу семьи (gated) | user/ABAC |
| POST | `/api/family/ingest` | Загрузка в корпус семьи | manager+ |
| POST | `/api/family/seed` / `/api/family/seed-norms` | Посев данных/норм по семьям | manager+ |

### 2.14 Канва / планирование
| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/api/canvas/layout/{key}` | Раскладка узлов канвы | user |
| POST | `/api/canvas/layout/{key}` | Сохранить раскладку | user |
| POST | `/api/plan` | Проверка плана перед запуском | user |

---

## 3. Переменные окружения (интеграционная поверхность)

Задаются в `/opt/abop/.env` контейнера `abop-webapi`.

**База/аутентификация:** `POSTGRES_DSN`, `KEYCLOAK_ISSUER`, `KEYCLOAK_JWKS_URI`, `KEYCLOAK_JWKS_INTERNAL`, `KEYCLOAK_AUDIENCE`.
**LLM/RAG:** `ROUTEAI_BASE_URL`, `ROUTEAI_API_KEY`, `ROUTEAI_*_CASCADE`, `QDRANT_URL`, `QDRANT_API_KEY`, `SLAVA_API_BASE_URL`, `EMBED_MODEL` (`baai/bge-m3`).
**OUT-доставка (демо):** `GOTENBERG_URL`, `BOOKSTACK_URL`, `BOOKSTACK_TOKEN`, `MAILPIT_HOST`, `MAILPIT_PORT`.
**OUT-доставка (реальные API — каркас, включаются при заданных):** `YOUGILE_TOKEN`, `YOUGILE_COLUMN_ID`, `YANDEX_SMTP_USER`, `YANDEX_SMTP_PASSWORD` (пароль приложения), `YANDEX_SMTP_HOST`, `YANDEX_SMTP_PORT`.
**Observability:** `LANGFUSE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (каркас), `LOG_LEVEL`.
**Планировщик:** `ABOP_SCHEDULER` (0=выкл), `ABOP_SCHEDULER_TICK`, `ABOP_SCHEDULER_MIN_INTERVAL`, `ABOP_SCHEDULER_MAX_FIRES`.
**Прогон:** `ABOP_RUN_LLM_CONCURRENCY`, `ABOP_RUN_LLM_TRUNCATE`.

---

## 4. Как запустить/обновить сервисы

**ABOP Web API (server-1):**
```
cd /opt/abop && docker build -q -f Dockerfile.webapi -t abop-webapi . && \
docker rm -f abop-webapi; docker run -d --name abop-webapi --network host \
  --restart unless-stopped --env-file /opt/abop/.env -v abop_ape:/root/.ape abop-webapi
```
**Observability-стек (Prometheus :9091 + Grafana :3300):**
```
cd /opt/abop && docker compose -f ops/docker-compose.observability.yml up -d
```
