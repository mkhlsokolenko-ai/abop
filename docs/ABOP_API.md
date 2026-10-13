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

- **Арендатор** (шаг 5): из заявки `tenant`/`org` токена; фильтруют по нему ХРАНИЛИЩА, не
  маршруты, поэтому ручка не может прочитать чужое. Чужое отвечает «нет такого», а не 403.
  Заголовок `X-ABOP-Tenant` открывает данные арендатора — только для `admin`/`support`, и
  переход пишется в журнал (`tenant.switch`). Подробно: `docs/ARENDATORY.md`, ADR-055.
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
| `GET /api/data/rules` | пакеты вертикалей + `check_rules` | `{наборы:[{id,название,предмет,правил,коды[],правка,версия_правки,кто,когда,валиден,ошибки[]}], предикатов}` | Данные → Правила |
| `GET /api/data/predicates` | `checks.ПРЕДИКАТЫ` | `{предикаты:[{имя,описание,параметры,по_набору}]}` | форма редактора правила |
| `GET /api/data/rules/{rid}` | стор + пакет | `{…сводка, набор, поставочный}` | карточка набора |
| `PUT /api/data/rules/{rid}` `{набор}` | `checks.проверить_набор` → `check_rules` | сводка с `версия_правки`; **422 со списком ошибок**, если набор невалиден | сохранить правку (manager+) |
| `POST /api/data/rules/{rid}/revert` | удалить правку | сводка поставочного набора + `снято` | вернуть версию пакета (manager+) |
| `GET /api/plan/terms?task=` | `choice_store.prefer_terms` | `{terms[], prefer_terms{навык:прибавка}, cap, поддержка_мин, решений_для_полного_веса}` | что подбор выучил на СЛОВАХ запроса |
| `POST /api/data/rules/{rid}/dryrun` `{набор?,лимит?}` | `прогнать_набор` на выборке | `{записей,находок,по_правилу{код:сколько},служебные{},ошибки[],находки[]}` | прогон черновика без сохранения |
| `POST /api/channels/{channel}/code` | `channel_tokens.выдать_код` | `{код,канал,живёт_минут}` — одноразовый, 15 минут | привязать бота; **из бота запрещено (403)** |
| `POST /api/channels/{channel}/exchange` `{code,external_id}` | код → узкий токен ABOP | `{ok,токен,id,кто,канал,арендатор}` | зовёт БОТ с заголовком `X-Bot-Secret`; предел 10 попыток в минуту |
| `GET /api/channels` | `channel_tokens.мои` | `{привязки:[{id,канал,куда,создана,последнее}]}` | свои привязки в кабинете |
| `DELETE /api/channels/{binding}` | отзыв своей привязки | `{ok}`; 404 если нет или уже отозвана | мгновенно, без Keycloak |
| `GET /api/control/feed?days=` | `control_feed.лента` по журналу решений | `{аномалии[],наружу[по людям],не_дошло[],разрешения[],подпись,чем_посчитано,авто_правил_нет}` | лента контроля руководителя (**manager+**); не согласование — смотреть и управлять полномочиями |
| `DELETE /api/control/permissions/{binding}` | `channel_tokens.снять` в пределах отдела | `{ok,снято{кто,канал},что_дальше}`; 404 — нет или чужой отдел | единственное действие ленты: снять постоянное разрешение; запись в аудит `control.revoke_permission` |
| `GET /api/verticals` | каталог `verticals/` + состояние системы | `{вертикали:[{id,название,версия,наборы[],рецепты[],требует,чего_нет,ошибки[],годен,установлена}]}` | Админка → Вертикали |
| `GET /api/verticals/{vid}` | манифест + сверка | та же карточка по одному пакету | карточка вертикали |
| `POST /api/verticals/{vid}/install` `{перезаписать_правки?}` | контракт → правила → рецепты → агент | `{…карточка, отчёт:[{что,id,итог,почему}], агент, audit_id}`; **422**, если манифест разошёлся с содержимым или в системе нет требуемого навыка/адаптера | поставить вертикаль (manager+) |

Вертикали (шаг 4): пакет `verticals/<id>/` — манифест плюс контракт, агент, наборы правил и
рецепты. Установка идемпотентна и отчитывается за каждое действие словом «поставлен / обновлён /
пропущен» с причиной. **Правку методолога установка не трогает** — снять её можно только явным
`перезаписать_правки`. Пакет НЕ несёт Python: навыки и адаптеры объявлены в `требует` и должны
быть в системе заранее. Агент ставится черновиком, посадка на участок карты — отдельно.

Наборы правил (шаг 3 «правила — данные»): набор приезжает с пакетом вертикали (`demo/*/rules/*.json`)
— это поставочная, неизменяемая часть; правка методолога ложится поверх отдельной записью
`check_rules`, поэтому «вернуть версию пакета» возможно всегда. Сохранение **валидирует** набор и
отказывает в 422: неизвестный предикат или неразрешимая ссылка на справочник означают, что правила
молча не сработают, а записи окажутся «чистыми». `dryrun` считает по НЕсохранённому набору и
возвращает счёт по каждому правилу, включая нули — нуль у правила такой же результат, как сотня.

`POST /api/plan` — детерминированная декомпозиция (без LLM): многоаспектная цель → задача-на-семью; для превью плана в UI до запуска.

---

## 3. RUN — запуск и стрим прогона (контракт; исполнение — инкремент)

Прогон = LLM-оркестрация волнами (долгий) → фоновая задача + SSE. Требует выноса `cmd_agents` в фон с эмиссией событий.

### `POST /api/runs` → `202 {run_id, plan}`
Тело: `{goal: str, agent_id?: str}`. Ставит прогон заданием в очередь, возвращает `202 + job_id`; ход — по SSE `GET /api/runs/{id}/stream` (события `hello/queue/wave_start/agent_step/board_event/hitl_request/run_done/ping`).

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
| `POST /api/hitl/{item_id}/approve` `{decision:approve\|reject, reason?}` | `{applied}` — исполнить/отклонить действие; approve делает РЕАЛЬНУЮ доставку и возвращает задание в очередь с чекпоинта |

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

## 5а. Карта процессов (линза «Эксплуатирую»)

Реализовано сегодня — граф-слой регламента и сверка с ним:

| Метод · Путь | Ядро | Ответ | Экран |
|---|---|---|---|
| `GET /api/process/graph` | `reglament_store` | `{tenant, nodes:[{nsi_key,op,process,subprocess}], edges, summary}` | карта · трассировка последствий |
| `POST /api/process/conformance` | `reglament_store` + эмбеддинги либо sLAVA | `{report:[{nsi_key,label,status,sim}], summary}` | сверка собранного процесса с регламентом |

Редакции карты и посадки агентов (ADR-042/043/045/046) — реализовано этапом 1:

| Метод · Путь | Что делает |
|---|---|
| `GET /api/process/map?layer=as-is\|to-be&area=` | `{tree, seats, revision, version, draft, changes, areas, editable_areas, editors}`; на `to-be` дерево уже с наложенным черновиком области |
| `POST /api/process/revision` `{base_version, changes[]}` | дописать правки в черновик своей области. Сливает с чужими, если зоны не пересеклись, и возвращает `missed` — клиент догоняет без перезагрузки |
| `GET /api/process/revision/{rev_id}/changes?since=` | разница по номеру |
| `DELETE /api/process/revision/{rev_id}` | отбросить черновик целиком |
| `POST /api/process/revision/{rev_id}/apply` `{base_version}` | применить: строгая проверка `base_version`, префлайт посадок, одна транзакция на дерево и посадки |
| `POST /api/process/revision/{rev_id}/submit` `{base_version}` | на согласование. Нужна, когда префлайт требует человека: закреплена версия агента на участке (ре-аттестация) или агент действует наружу (ADR-013/014). После отправки редакция не правится — подписант подпишет ровно то, что прочёл; применяет её `admin` |
| `GET /api/process/stream?area=` | SSE: `hello` с номером версии и списком `editors`, затем `change`/`applied`/`poke`/`ping`. Heartbeat пишет присутствие |
| `GET /api/process/liveness?window=24h\|7d\|30d` | подтверждение AS-IS прогонами: `{window_days, ops:{op_id:{runs,ok,last_at,silent_days,state}}, outside:[{audit_id,agent,runs,last_at}]}`. `state` — `подтверждён` / `тихо` / `осиротел`; `outside` — агенты, которые работают, но ни на одной операции не сидят |
| `GET /api/process/history?limit=` | как менялся процесс: применённые редакции сверху вниз — `{revisions:[{revision,note,applied_by,applied_at,nodes}]}`. История уже лежала в `process_map` (ключ `area,revision`), не хватало чтения |
| `GET /api/process/diff?base=&target=` | что изменилось между двумя применёнными редакциями — ПО СУЩНОСТЯМ, не построчно: `{added,removed,renamed,moved,edges_added,edges_removed,total}`. `base=0` — предыдущая, `target=0` — текущая |
| `GET /api/process/coverage?window=` | где агенты и где дыры: `{operations, confirmed, coverage_pct, by_state, rows:[{op_id,label,agents,agent_names,state,runs}], gaps:[{op_id,label,why}], outside}`. Главный отчёт карты — отвечает на вопрос, ради которого её ведут |
| `POST /api/agents/external` `{name, runtime, family, autonomy_max, endpoint?}` | зарегистрировать агента, которого ABOP не породил (ADR-028). `runtime` — langgraph/copilot-studio/agentforce/bedrock/crewai/autogen/n8n/другой. Конверт **объявлен, а не проверен**: графа нет, авто-верификация неприменима — в ответе `envelope.ok = null`, во флоте `verify: external`. Посадка на операцию идёт обычной редакцией карты: чужой рантайм не даёт права миновать согласование |
| `POST /api/runs/report` `{audit_id, op_ids, ok?, summary?, cost?}` | чужой агент сообщает о работе на участке. Принимается **только** от агента с `source=external` и **только** по операциям, на которые он посажен — иначе покрытие любого участка накручивалось бы одним запросом. Прогон помечается `source=external`; в `/coverage` участок получает `evidence: «со слов агента»`, и такие участки отдельно считаются в `confirmed_reported_only` |
| `POST /api/process/import/bpmn` `{xml, preview?, area?}` | импорт BPMN 2.0. `preview:true` (по умолчанию) — разбор без записи: `{tree, stats, skipped}`. Иначе правки ложатся в ЧЕРНОВИК области обычной редакцией (ADR-042), права на область проверяются. Переносится скелет: процессы, подпроцессы, задачи (userTask → `hitl`), события (таймер → `wait`), шлюзы, потоки (с условием → `cond`). Пулы, дорожки и исполнительная часть отбрасываются и перечисляются в `skipped` |
| `POST /api/process/map` `{tree, note}` | записать карту целиком новой применённой редакцией — вход для сида и миграции, **только admin** |

Коды: `409` — конфликт правок, тело `{error, version, rivals:[{target,kind,author,seq}]}`; `422` —
отказ по состоянию карты с причиной текстом; `403` — чужая область с перечислением узлов. Причина
всегда в теле: интерфейс её показывает, а не глотает.

Мутации — `manager+` **и** область узла ∈ области пользователя (`can_edit_area`). Область правки
берётся из самой карты по цели правки, присланный в теле `area` не используется.



---

## 5б. Оценка цепочки и пробный шаг (ADR-051)

| Метод · Путь | Что делает |
|---|---|
| `POST /api/plan/estimate` `{steps:[skill], model?, price_per_1k?}` | во что обойдётся цепочка ДО запуска: `{rows:[{skill,input_tokens,output_tokens,rub,basis,runs}], input_tokens, output_tokens, rub, partial, note}`. `basis` — **откуда взято число**: `по прогонам` (медиана по собственной истории навыка), `по каталогу` (объявленный в навыке ориентир), `нет данных` (тогда `rub = null`). `partial:true` — часть шагов оценить нечем, итог занижен, и в `note` это сказано словами. Правдоподобного числа вместо честного «оценить нечем» не выдаётся (ADR-016) |
| `POST /api/agents/{agent_id}/step` `{skill, input?}` | прогнать ОДИН шаг на графе агента: `{ok, skill, text, input_tokens, output_tokens, model, sources:{entities,documents,records}}`. Исполняется тем же `execute_agent_run`, что и обычный прогон, на временном агенте из одного узла — второй путь исполнения со временем разошёлся бы с настоящим. Кэш выключен, выход наружу выключен, `op_ids` не пишется: пробный шаг не вправе подтверждать участок карты. В журнале виден как `step-test` |
| `GET /api/runs/{run_id}` → `step_sources[]` | что именно дошло до каждого навыка: `[{skill, entities, records:{entity:count}, documents, input_tokens, output_tokens, skipped}]`. Собирается на сервере из `findings` и `run_metrics` — данные лежали в записи прогона и раньше, но были разбросаны |

---

## 5в. MCP-сервер — ABOP как среда для чужих агентов (ADR-048)

Включается переменной `ABOP_MCP=1`, монтируется на `/mcp` (streamable-http). Выключен по
умолчанию: это новая сетевая поверхность поверх всех данных, и открываться она обязана осознанно.

Аутентификация — тот же `Bearer`, тот же `server/deps.user`: multi-issuer JWKS, кэш, fail-closed.
Внешний агент **не получает того, чего не получает человек с тем же токеном**; гейты уровней
(`require_level`) применяются внутри вызываемых функций, а не дублируются в слое MCP.

| Инструмент | Что отдаёт |
|---|---|
| `data_query`, `data_sources`, `data_entities`, `catalog_fields`, `resolve_entity` | те же пять инструментов, что видят внутренние агенты — список берётся из `ape.AGENT_TOOLS`, чтобы два описания одного инструмента не разошлись |
| `process_map`, `process_liveness`, `process_coverage`, `agents_list` | карта процессов, подтверждение прогонами, покрытие и флот — ровно то же, что отдают одноимённые HTTP-ручки |
| `register_agent`, `report_run` | две записи, и обе — в СОБСТВЕННЫЕ записи ABOP (см. 5а). Наружу MCP не действует: письмо, задача, публикация идут через OUT-узел с подтверждением человека (ADR-014) |

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
