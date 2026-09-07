# ABOP — API-контур ядра

**Назначение:** контракт между ядром ABOP и веб-средой. Реализация принципа PRD v1.1 «единое ядро, много клиентов»: CLI `ape` и веб дёргают ОДНИ функции ядра — веб через этот REST/SSE-слой (`server/web_api.py`), CLI напрямую.
**Auth:** Keycloak JWT (тот же realm, что MCP-шлюз). Dev-режим без JWKS → `dev`-пользователь.
**Транспорт:** REST (JSON) + SSE для живого прогона. За Caddy (TLS), рядом с FastMCP-шлюзом.
**Синхронизация:** имена `/api/*` здесь = источник истины; `ABOP_SCREENS.md` ссылается на них.
**Статус:** v0.1 — READ реализованы (реальные вызовы ядра); RUN/STREAM/HITL — контракт зафиксирован, исполнение = следующий инкремент.

---

## 0. Архитектура слоя

```
┌────────┐   REST/SSE    ┌──────────────┐   import    ┌──────────────┐
│  Веб   │ ────────────▶ │ server/      │ ──────────▶ │ cli/ape.py   │
│ (среда)│   JWT         │ web_api.py   │  функции    │ (ЯДРО)       │
└────────┘               │ (FastAPI)    │             │ семьи/навыки/│
                         └──────────────┘             │ DataPlane/   │
┌────────┐   MCP JSON-RPC ┌──────────────┐            │ прогон/память│
│  CLI   │ ────────────▶ │ server/main  │ ──────────▶ │              │
│ (ape)  │               │ (FastMCP)    │  LLM/RAG    └──────────────┘
└────────┘               └──────────────┘
```
web_api НЕ дублирует логику — только выставляет функции ядра как HTTP. Тяжёлые LLM-вызовы (прогон) идут через тот же MCP-шлюз, что CLI.

---

## 1. Аутентификация

- Заголовок `Authorization: Bearer <JWT>` (Keycloak realm курса). Проверка подписи по JWKS, issuer/audience из env (`KEYCLOAK_JWKS_URI`/`KEYCLOAK_ISSUER`/`KEYCLOAK_AUDIENCE`).
- Без `KEYCLOAK_JWKS_URI` — **dev-режим**: все запросы как `dev`/роль `developer` (для локальной разработки фронта).
- Роли (realm_access.roles): `developer` (авторинг), `operator` (HITL), `viewer`, `admin`.
- Ошибки: `401` нет/битый токен.

---

## 2. READ-эндпоинты (реализованы, реальные данные ядра)

| Метод · Путь | Ядро | Ответ | Экран |
|---|---|---|---|
| `GET /api/health` | — | `{ok, families, skills, adapters}` | — |
| `GET /api/me` | JWT | `{user:{sub,name,roles,dev}}` | Shell |
| `GET /api/families` | `AGENT_FAMILIES` | `{families:[{id,title,profile,mission,kind,members:[{key,title,skills[]}]}]}` | §4/§6 палитра |
| `GET /api/skills` | `SKILLS`+`skill_safety` | `{count, skills:[{id,title,short,safety:{mode,egress,cite},scope,families[]}]}` | §4 каталог |
| `GET /api/skills/{id}` | `load_skill_body` | `{id,title,short,safety,scope,body}` (полное тело SKILL.md) | §4 drawer |
| `GET /api/data/adapters` | `SOURCE_ADAPTERS` | `{adapters:[csv,json,http,sqlite,computed]}` | §5 коннекторы |
| `GET /api/data/schema/{entity}` | `data_schema` | `{entity,known,required[],hint}` | §5 редактор |
| `GET /api/data/query/{entity}?limit` | `data_query` | `{entity, records[]}` (свежие, freshness-гейт) | §5 предпросмотр |
| `POST /api/plan` `{goal}` | `detect_families`/`route_family` | `{goal, families[], preview_waves[[{task,family}]]}` | §9.1 запуск |

`POST /api/plan` — детерминированная декомпозиция (без LLM): многоаспектная цель → задача-на-семью; для превью плана в UI до запуска.

---

## 3. RUN — запуск и стрим прогона (контракт; исполнение — инкремент)

Прогон = LLM-оркестрация волнами (долгий) → фоновая задача + SSE. Требует выноса `cmd_agents` в фон с эмиссией событий.

### `POST /api/runs` → `202 {run_id, plan}`
Тело: `{goal: str, agent_id?: str}`. Запускает прогон, возвращает id. *(сейчас 501 + контракт)*

### `GET /api/runs/{run_id}/stream` → `text/event-stream`
SSE-события (по мере исполнения):

| event | data | UI (§3 Пульт) |
|---|---|---|
| `wave_start` | `{wave, tasks:[{task,family,member}]}` | новая волна на canvas |
| `agent_step` | `{agent,family,member,skill,status}` | статус узла (маскот-состояние) |
| `board_event` | `{type,text,agent,ts}` | лента доски |
| `handoff` | `{from,to_family,to_member,task}` | анимация «Эстафета» |
| `audit_verdict` | `{passed,dod[],gaps[]}` | вердикт аудитора |
| `hitl_request` | `{item_id,skill,preview}` | HITL-полоса |
| `cost` | `{tokens,rub,remaining}` | расход |
| `run_done` | `{synthesis,verdict}` | итог, маскот «DoD✓» |

### `GET /api/runs/{run_id}` → состояние прогона (для переоткрытия/поллинга-фолбэка).

---

## 4. HITL — подтверждения оператора (контракт)

| Метод · Путь | Ответ |
|---|---|
| `GET /api/hitl/queue` | `{queue:[{item_id,run_id,skill,preview,ts}]}` — action-навыки в dry_run |
| `POST /api/hitl/{item_id}/approve` `{decision:approve\|reject, reason?}` | `{applied}` — исполнить/отклонить действие *(501 сейчас)* |

Инвариант: ничего `action`/`external` не уходит наружу без `approve` (ADR-014).

---

## 5. AUTHORING — рецепты и канва (контракт; следующие инкременты)

| Метод · Путь | Назначение | Ядро |
|---|---|---|
| `GET /api/data/recipes` | список рецептов | `data_recipes` |
| `POST /api/data/recipe/preview` `{recipe}` | dry-прогон на выборке → canonical + счётчики | `data_run` (нужен dry-режим) |
| `POST /api/data/recipe` `{recipe}` | сохранить рецепт | файл рецепта |
| `POST /api/data/connectors` `{kind,config}` | подключить источник | `register_adapter` (серверные адаптеры) |
| `POST /api/flow/draft` `{description}` | LLM-черновик потока (канва) | планировщик |
| `POST /api/flow/save` / `GET /api/agents` | сохранить/список агентов | store |
| `GET /api/agents/{id}/passport` | конверт агента (роли/навыки/безопасность/автономия≤контракт) | сборка из семей+safety+contracts |

---

## 6. Инкременты реализации

1. **✅ READ** (families/skills/dataplane/plan) — готово, реальные вызовы ядра.
2. **RUN/STREAM** — вынести `cmd_agents` в фоновую задачу с колбэком-эмиттером событий → SSE. Ключевое: эмиссия `wave_start`/`agent_step`/`board_event`/`audit_verdict` из оркестратора (сейчас они печатаются в терминал — заменить на event-bus).
3. **HITL** — очередь dry_run-действий в общем сторе + approve исполняет отложенное.
4. **AUTHORING** — dry-режим `data_run` (preview без записи в store), сохранение рецептов/потоков, паспорт.

---

## 7. Запуск

```bash
# dev (без Keycloak — dev-user):
uvicorn server.web_api:app --reload --port 8091
# прод: за Caddy, env KEYCLOAK_JWKS_URI/ISSUER/AUDIENCE заданы
```

CORS открыт для dev; в проде сузить до домена среды.

---

## Приложение
- Реализация — `server/web_api.py`. Ядро — `cli/ape.py`.
- Экраны, потребляющие эти эндпоинты — `ABOP_SCREENS.md`.
- Продукт/принцип «единое ядро» — `ABOP_PRD.md` v1.1 (FR-I1).
