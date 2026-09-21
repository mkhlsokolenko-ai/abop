# ADR + план: смычка APE Desktop ↔ ABOP (тонкий клиент → рантайм агентов)

> Статус: **draft** (2026-09-20). Как подключить desktop-клиент (`ape-desktop`, Electron + Python-сайдкар)
> к бэкенду ABOP, чтобы desktop стал точкой входа простого пользователя для запуска агентов.
> Решение владельца: **скопировать desktop в ABOP и адаптировать под него** (единый репозиторий/деплой).

---

## 1. Контекст (что есть сейчас — ground truth)

**ABOP** (этот репо): FastAPI `server/web_api.py`, Postgres. Агенты = контракт + граф узлов
(Триггер→Источник→Навык→HITL→OUT), прогон с governance/HITL/кэшем/observability. API: `/api/agents`,
`/api/demo/prepare`, `/api/runs`, `/api/hitl/*`, `/api/skills`, `/api/me`, `/api/data/*`. Auth —
Keycloak JWT (сейчас на проде демо-режим: JWKS пуст → decode без проверки подписи), ABAC по `department`.

**APE Desktop** (`ai-product-engineer/desktop`): Electron (`electron/main.js` спавнит сайдкар,
грузит UI) + Python-сайдкар FastAPI на `127.0.0.1:8799` (`sidecar/app.py`). Модули авто-обнаруживаются
(`registry.py`): `chat, agents, graphlens, opslens, connectors, security, cabinet` — **зеркало экранов
ABOP**. `gateway.py` ходит в **курсовой MCP** (`mcp.engineer-ai.pro`) и **portal** под JWT
пользователя. ADR desktop (`docs-thin-client-adr.md`, accepted): desktop = **тонкий клиент-рантайм
ABOP**, RBAC энфорсится на сервере, клиент лишь фильтрует каталог.

**Ключевые факты-корни смычки:**
1. **Два независимых каталога агентов.** Desktop держит СВОИ агенты в локальной SQLite сайдкара
   (`sidecar/modules/agents/module.py`: таблица `agents`, `/api/modules/agents/catalog`, `/skills`).
   ABOP держит СВОИ в Postgres (контракт+граф). Сейчас они не связаны.
2. **Разный upstream.** Desktop `gateway.py` → MCP/portal, НЕ → ABOP `/api/*`. Клиента ABOP нет.
3. **Разный realm.** Desktop: `auth.engineer-ai.pro/realms/ai-product-engineer`. ABOP: issuer
   `.../realms/abop`, JWKS пуст (демо). JWT одного не верифицируется другим строго.

---

## 2. Решение (ADR, Nygard)

**Контекст.** Нужна единая точка, откуда пользователь запускает агентов ABOP под своими правами, с
нескольких поверхностей (окно, хоткей, Office). Desktop уже задуман под это, но не подключён к ABOP.

**Решение.** **Внести APE Desktop в репозиторий ABOP** (папка `desktop/`) и **сделать сайдкар тонким
прокси к ABOP REST API**: сайдкар не хранит свой каталог агентов, а **проксирует** запросы к ABOP
`/api/*` под JWT пользователя. Desktop становится официальной клиент-поверхностью ABOP; ABOP —
единственный источник истины по агентам/прогонам/HITL/данным. RBAC/ABAC энфорсится ABOP (сервер),
desktop фильтрует каталог для UX.

**Почему копия в ABOP (а не два репо):** единый деплой и версия, контракт локального API живёт рядом
с сервером, в который встраиваются host-системы; нет рассинхрона каталогов и схем (главная текущая
болезнь). Desktop-специфика (Electron, PyInstaller-сборка) остаётся в `desktop/`, ABOP-ядро не трогаем.

**Альтернативы (отклонены):**
- *Оставить два репо, синхронизировать каталоги* — постоянный дрейф двух источников истины, дубли.
- *Desktop со своим каталогом + периодический импорт из ABOP* — двойное хранение, конфликты версий.
- *Толстый desktop со своими вызовами моделей* — обход governance/бюджета/RBAC (запрещено ADR desktop).

**Последствия:**
- (+) Один источник истины (ABOP); desktop — тонкий; единый деплой/версия.
- (+) HITL/кэш/observability/ABAC ABOP работают в desktop «бесплатно».
- (−) Обязательно выровнять auth (единый realm + строгая верификация) — до продакшена, не «потом».
- (−) Нужен стабильный **локальный API-контракт** сайдкара (в него встраиваются Office/deep-link).

---

## 3. Целевая архитектура

```
 Поверхности (одна сессия / один JWT):
   окно APE · глобальный хоткей · Office-надстройка · deep-link ape://
        │  всё → локальный сайдкар :127.0.0.1:8799
        ▼
 ┌─────────────────────────────────────────┐
 │ APE Desktop сайдкар (в репо ABOP/desktop) │  держит JWT пользователя; ЛОКАЛЬНЫЙ API (контракт)
 │  = ТОНКИЙ ПРОКСИ к ABOP /api/*            │  фильтрует каталог для UX; НЕ хранит свой каталог
 └─────────────────────────────────────────┘
        │ Bearer JWT пользователя (тот же realm)
        ▼
 ┌─────────────────────────────────────────┐
 │ ABOP Web API (server/web_api.py)          │  ЕДИНЫЙ источник истины + энфорс ABAC/RBAC
 │  /api/agents · /api/demo/prepare ·         │  governance-конверт, HITL, кэш, observability
 │  /api/runs · /api/hitl/* · /api/skills ·   │
 │  /api/me · /api/data/*                      │
 └─────────────────────────────────────────┘
        ▼
 Модели (Qwen/DeepSeek каскад) · Data Plane (коннекторы) · Postgres
```

**Маппинг desktop-модулей на ABOP API (заменяем локальную SQLite на прокси):**
| Модуль desktop | Было (локально) | Станет (прокси в ABOP) |
|---|---|---|
| `agents` каталог | SQLite `agents` | `GET /api/agents` (ABAC по семье) |
| `agents` запуск | нет | `POST /api/demo/prepare` + `POST /api/runs` |
| `agents` навыки | `SKILLS` в модуле | `GET /api/skills` |
| HITL-очередь | нет | `GET /api/hitl/queue` + `POST /api/hitl/{id}/approve` |
| `opslens` прогоны | — | `GET /api/runs`, `/api/runs/{id}` |
| `graphlens` граф | — | граф из `/api/agents/{id}` |
| `connectors` данные | — | `/api/data/*` |
| `cabinet` профиль | `/api/auth/me` | `/api/me` (ABOP-идентичность/ABAC) |

---

## 4. Что критично решить ДО кода (блокеры)

1. **Единый realm + строгая верификация JWT.** Свести desktop и ABOP на один Keycloak-realm; на
   проде ABOP задать `KEYCLOAK_JWKS_URI` (сейчас пуст → демо-decode без подписи). Без этого смычка
   небезопасна. → отдельный шаг «realm-auth» (см. §6, Фаза 0).
2. **Локальный API-контракт сайдкара** (версионировать `/api/v1/...`): `/session` (кто вошёл),
   `/agents` (каталог по роли), `/run` (запуск с контекстом текст/файл/выделение), `/hitl`. В него
   встраиваются host-системы — это продукт, не внутренность.
3. **Аутентификация host-приложений к сайдкару** (Office-надстройка/deep-link стучатся в :8799):
   локальный токен/loopback-ограничение/CORS — чтобы не любой процесс дёргал сайдкар.
4. **Делегированный доступ к системам** (коннекторы от имени пользователя): token-exchange на шлюзе
   vs пер-юзер OAuth. Отдельное ADR (самый ответственный по безопасности) — не для первого среза.
5. **Дистрибуция сайдкара**: dev — системный Python; прод — PyInstaller `ape-sidecar.exe`. При копии
   в ABOP сайдкар импортирует ABOP-клиент — учесть в сборке.

---

## 5. Локальный API сайдкара (черновик контракта)

```
GET  /api/v1/session            → {user, roles, department}          (из /api/me ABOP)
GET  /api/v1/agents             → [{id, name, family, ...}]          (из /api/agents, ABAC)
GET  /api/v1/agents/{id}        → {граф, навыки}                     (из /api/agents/{id})
POST /api/v1/run                → {run_id, ...}  тело {agent_id|scenario, context?}
                                   (demo/prepare при необходимости + runs; context = выделение/файл)
GET  /api/v1/runs               → журнал прогонов                    (из /api/runs)
GET  /api/v1/hitl               → очередь подтверждений              (из /api/hitl/queue)
POST /api/v1/hitl/{id}/approve  → подтвердить/отклонить              (из /api/hitl/{id}/approve)
```
Все вызовы наружу — с `Authorization: Bearer <JWT пользователя>`; сайдкар только проксирует и
адаптирует контекст (выделенный текст → вход прогона). RBAC решает ABOP.

---

## 6. План внедрения (фазы, низкий риск)

**Фаза 0 — перенос + auth-фундамент [✅ СДЕЛАНО 2026-09-21]:**
- ✅ `desktop/` скопирован в репо ABOP (Electron/сайдкар/ui, без node_modules/build; `.gitignore`).
- ✅ **Multi-issuer JWT** вместо «выровнять realm»: ABOP принимает JWT ДВУХ realm — свой `abop`
  (demo-логин) и desktop `ai-product-engineer` — оба ВЕРИФИЦИРУЯ по своему JWKS
  (`ABOP_EXTRA_JWKS="<issuer>|<jwks_url>"`). Не ломает прод (extra пуст → поведение прежнее).
  Включение строгого режима desktop-realm = задать env, когда desktop пойдёт в прод.
- ⏳ CORS/локальный токен сайдкара — при выходе на host-приложения (Фаза 2).

**Фаза 1 — тонкий прокси [✅ ВЕРТИКАЛЬНЫЙ СРЕЗ СДЕЛАН]:**
- ✅ `desktop/sidecar/abop_client.py` (base URL ABOP + проброс JWT; agents/skills/prepare/run/runs/hitl).
- ✅ Модуль `agents` сайдкара: SQLite-каталог заменён на прокси к ABOP (каталог/навыки/запуск/HITL).
- ✅ Проверено против прода: health/me/agents(9)/skills(55)/hitl(5) + запуск invest1c (cached, trace, 2
  расследования). Осталось: подключить UI-панель `ui/modules/agents/panel.js` к новым роутам сайдкара.

**Фаза 2 — поверхности вызова:**
- Глобальный хоткей (Ctrl+Space): выделил текст → выбрал агента → результат (`context` в /run).
- Office-надстройка (task-pane → :8799) — контекст документа как источник.
- deep-link `ape://agent/<id>`.

**Фаза 3 — делегированный доступ к системам** (отдельное ADR): token-exchange/пер-юзер OAuth,
коннекторы от имени пользователя.

---

## 7. Что НЕ трогаем

ABOP-ядро (`server/`, `cli/ape.py`, прогон, governance, кэш, HITL, observability) — desktop к нему
только обращается по REST. Логика агентов/навыков/данных не дублируется в desktop.

---

## 8. Открытые вопросы (в отдельные ADR)

- Механизм делегированного доступа к системам (token-exchange vs пер-юзер OAuth; хранение токенов).
- Модель видимости каталога агентов по ролям/группам (сейчас ABAC по `department`; нужны ли группы).
- Формат/версионирование локального API + аутентификация host-приложений к сайдкару.
- Дистрибуция сайдкара (PyInstaller с ABOP-клиентом) и Office-надстройки (sideload vs AppSource).
- Единый realm: мигрируем ABOP на `ai-product-engineer` или заводим общий?
