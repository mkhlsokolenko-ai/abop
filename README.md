# ABOP — Agent Build & Operations Platform

Среда сборки, запуска и эксплуатации ИИ-агентов в управляемом периметре. Инженерная
половина связки **LUDA → ABOP**: LUDA (аудитор) выдаёт бизнес-вердикт готовности и три
контракта, ABOP по ним собирает, запускает и эксплуатирует агентов под жёстким конвертом
governance.

> Отделён из монорепо `ai-product-engineer` (учебный портал остаётся там). Переиспользуемые
> части (MCP-шлюз, навыки, CLI APE, Keycloak, инфра) склонированы в оба репозитория.
> Связь между продуктами — только через версионированные контракты, не через код (ADR-027).

## Актуальная документация (эксплуатация)

> Проектные ADR/PRD/SDD/SCREENS в `docs/` — исторические (фаза дизайна, начало сентября). Для **разворачивания / восстановления / настройки** смотри свежие runbook-и:

- **[docs/DEPLOY.md](docs/DEPLOY.md)** — разворачивание, восстановление Data Plane, конфигурация, замеры производительности, частые проблемы.
- **[docs/API_ENDPOINTS_PORTS.md](docs/API_ENDPOINTS_PORTS.md)** — все эндпоинты API, порты сервисов (server-1/server-2/внешние), env-переменные.
- **[docs/INSTRUKCIYA_ZAPUSK_AGENTA.md](docs/INSTRUKCIYA_ZAPUSK_AGENTA.md)** — пошагово для не-технического пользователя: как собрать и запустить агента.
- **[docs/CONCEPT_SCALING_OBSERVABILITY.md](docs/CONCEPT_SCALING_OBSERVABILITY.md)** — концепт масштабирования, observability, поверхностей запуска.

## Прод (актуально 2026-09-18)

- **ABOP Web API** — `abop-webapi` (docker, `--network host`) на **server-1 `5.129.192.63:8091`**; том `abop_ape` (canonical Data Plane) + Postgres (`ape_pg`).
- **Observability**: сквозной `trace_id`, `/metrics` (Prometheus `:9091`) + Grafana (`:3300`), тайминги прогона/навыков, мягкие ошибки не глушатся (`soft_errors`).
- **Data Plane**: канонические записи в `~/.ape/data/*.jsonl` (том); версионная freshness (последняя версия не протухает по времени, старьё уходит по TTL).
- **Демо-сценарии**: `audit1c` (Аудитор 1С), `invest1c` (Расследование от симптома), `fin` (Финаналитик) — кнопка «▶ Собрать и запустить» / `POST /api/demo/prepare`.
- **OUT-доставка**: почта (Mailpit/Яндекс SMTP), BookStack, YouGile-задача, PDF — dry_run по умолчанию, реально под HITL/токены.

## Архитектура (кратко)

- **Канва-центр** (ADR-030): один граф процесса + линзы Строю / Запускаю / Эксплуатирую / Конверт.
- **Contract Ingress** (ADR-029): приём бандла LUDA (`luda.*/1.0`) → `ContractSet` → посев канвы.
- **Конверт governance** (ADR-028): автономия агента ≤ `DeploymentContract` от LUDA (ADR-013), HITL, egress-политика, аудит.
- **Обратная петля** (SDD §4-bis): ABOP публикует `RunMetrics` → LUDA сверяет с baseline.
- **Data Plane**: рецепты (source→canonical по `entity`) + OUT-узлы (доставка); версионное хранилище с provenance.
- **Триггеры**: стартовые события (cron/событие) + политика инстанциации (spawn) в графе агента.

## Структура

| Путь | Что |
|---|---|
| `server/` | FastAPI/FastMCP: шлюз к моделям (RouteAI), `web_api.py` (ABOP API), `ingress.py` + `contract_store.py` (Contract Ingress), auth/db/clients/pricing/config |
| `webapp/` | Buildless-React фронт ABOP (dark-first дизайн-система, токены `tokens.css`, вендоренный React) |
| `cli/` | **APE** — CLI доступа к моделям (общий с учебным репо) |
| `skills/` | Декларативные навыки агентов (mode/egress/cite); `audit1c-*`, `invest1c-*` и др. |
| `demo/` | Демо-пакеты сценариев: `audit1c/`, `invest1c/`, `fin/` (contract*.json + agent_body.json + recipes/norms) |
| `ops/` | Эксплуатация: `deploy-webapi.sh`, `docker-compose.observability.yml`, `prometheus.yml` |
| `bench/` | Выбор модели (гейт) + проверка RAG-цепочки и изоляции тенантов |
| `docs/` | Эксплуатация (DEPLOY/API_ENDPOINTS_PORTS/INSTRUKCIYA/CONCEPT) + исторические ADR/SDD/PRD/SCREENS |
| `keycloak/`, `db/`, `docker-compose.yml`, `Caddyfile` | Инфраструктура (OIDC, Postgres, TLS) |

## Запуск (dev)

```bash
# 1. окружение
cp .env.example .env          # заполнить ROUTEAI_API_KEY и пр.

# 2. ABOP Web API + фронт (StaticFiles отдаёт webapp/)
uvicorn server.web_api:app --reload --port 8091
# → http://127.0.0.1:8091  (фронт) · /api/health · /api/contracts/ingest

# 3. CLI APE (доступ к моделям)
pip install -e ./cli          # или без установки: python cli/ape.py <команда>
ape login                     # вход через GitHub (браузер)
ape code "напиши FastAPI-эндпоинт /health с тестом на pytest"
ape ask  "сравни подходы к очередям задач"
```

Модели (RouteAI): код → `qwen/qwen3.8-27b`, агентский движок → `qwen/qwen3-30b-a3b-instruct-2507`,
ресёрч → DeepSeek. Каскады настраиваются в `.env` (`ROUTEAI_*_CASCADE`).

## Тесты

```bash
pytest -q
```
