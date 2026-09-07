# ABOP — Agent Build & Operations Platform

Среда сборки, запуска и эксплуатации ИИ-агентов в управляемом периметре. Инженерная
половина связки **LUDA → ABOP**: LUDA (аудитор) выдаёт бизнес-вердикт готовности и три
контракта, ABOP по ним собирает, запускает и эксплуатирует агентов под жёстким конвертом
governance.

> Отделён из монорепо `ai-product-engineer` (учебный портал остаётся там). Переиспользуемые
> части (MCP-шлюз, навыки, CLI APE, Keycloak, инфра) склонированы в оба репозитория.
> Связь между продуктами — только через версионированные контракты, не через код (ADR-027).

## Архитектура (кратко)

- **Канва-центр** (ADR-030): один граф процесса + линзы Строю / Запускаю / Эксплуатирую / Конверт.
- **Contract Ingress** (ADR-029): приём бандла LUDA (`luda.*/1.0`) → `ContractSet` → посев канвы.
- **Конверт governance** (ADR-028): автономия агента ≤ `DeploymentContract` от LUDA (ADR-013), HITL, egress-политика, аудит.
- **Обратная петля** (SDD §4-bis): ABOP публикует `RunMetrics` → LUDA сверяет с baseline.

## Структура

| Путь | Что |
|---|---|
| `server/` | FastAPI/FastMCP: шлюз к моделям (RouteAI), `web_api.py` (ABOP API), `ingress.py` + `contract_store.py` (Contract Ingress), auth/db/clients/pricing/config |
| `webapp/` | Buildless-React фронт ABOP (dark-first дизайн-система, токены `tokens.css`, вендоренный React) |
| `cli/` | **APE** — CLI доступа к моделям (общий с учебным репо) |
| `skills/` | Декларативные навыки агентов (mode/egress/cite) |
| `bench/` | Выбор модели (гейт) + проверка RAG-цепочки и изоляции тенантов |
| `docs/` | ADR/SDD/PRD/API/SCREENS, `LUDA2_FUNNEL_SPEC`, дизайн-кит и прототип v3 |
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
