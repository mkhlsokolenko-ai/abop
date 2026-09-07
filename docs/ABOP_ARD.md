# ABOP — Architecture Design (ARD / AOP)

> **ABOP = продуктизация LUDA v2** (рантайм исполнения агентов, эволюция VRATA — переименование +
> ребилд на паттернах **openclaw**: Gateway / Channel / Tool-Skill / pairing-approval HITL /
> security-first). Отдельный продукт и репозиторий, **шире учебного проекта**; работает в
> регулируемом периметре (ФСТЭК/ДСП/ФЗ-152). Python, максимум переиспользования кода VRATA.
> Каноны LUDA v2 — `Downloads/LUDA pivot/{PRD,ADR,TDR,AOP}.md`; план — `slAVA/docs/REBUILD_PLAN_ABOP_LUDA_v2.md`.
>
> **Учебный проект (ai-product-engineer + CLI `ape`) — безопасный полигон**, где мы обкатываем
> ПРИНЦИПЫ и АРХИТЕКТУРУ (агенты/волны/Blackboard/tools/Data Plane), затем переносим в ABOP.
> Статус: **v0.2, draft**.

## 0. Топология продуктов (3 продукта, 3 репозитория)
```
MIRA   →   LUDA v2   →   sLAVA        →   ABOP
портф.     диагноз       восполнение      исполнение
           (аудит        контекста        (агенты,
            готовности)   (RAG/норматив)    рантайм)
```
- **LUDA v2** — аудитор готовности процесса к ИИ-агенту: Пробы→Evidence→оси→автономия **A0–A4**→
  **3 контракта** (`CapabilityRequest` / `DeploymentContract` / `BaselineMeasurement`).
- **ABOP** — рантайм агентов (этот документ): Gateway/каналы/тулы/HITL/security.
- **sLAVA** — RAG по нормативке. **MIRA** — портфельный слой.
- Связь между репо — **только версионированные схемы контрактов** (CI `check-no-foreign-imports`).
- **Жёсткая зависимость (AOP §7.3):** ABOP **не может** дать агенту autonomy_level выше, чем в
  `DeploymentContract` от LUDA. Единственная обязательная связь.

---

## 1. Проблема и инсайт

Мы построили **слой агентов и харнесс** их взаимодействия (оркестрация, tool-calling,
Blackboard). Но главного нет: **готового под агентов слоя данных в машиночитаемом виде**.

**Ключевой тезис:** агент потребляет данные **не как человек**. Человеку системы отдают
данные «для глаз» — HTML-страницы, UI, PDF, письма, разрозненные API с разными форматами.
Агенту нужен **типизированный, самоописываемый, стабильный JSON** с известной схемой,
провенансом и свежестью. Скрейпить человеческие интерфейсы каждым агентом = хрупко, дорого,
недетерминированно.

**Вывод:** между «системами» и «агентами» нужен **Data Plane** — слой адаптеров, который
переводит данные из систем в **structured JSON input рецептов**.

---

## 2. Три плоскости ABOP

```
┌──────────────────────────────────────────────────────────────┐
│  AGENT PLANE  — агенты, харнесс, оркестрация                   │
│  • ReAct tool-calling · волны (fan-out/fan-in) · синтез        │
│  • Blackboard (event-sourced): facts / notes / claims / snap   │
│  → ГОТОВО в ape, портируется в ABOP                            │
├──────────────────────────────────────────────────────────────┤
│  TOOL PLANE  — примитивы и рецепты (под JWT студента)          │
│  • примитивы: run_python(docker), web_fetch, render_diagram,   │
│    doc_export, save, mail_fetch, tracker                       │
│  • рецепты = агент + примитивы + LLM (RFM, дайджест, отчёт…)   │
│  → проектируется                                              │
├──────────────────────────────────────────────────────────────┤
│  DATA PLANE  — агент-нативный слой данных (НЕДОСТАЮЩИЙ ПАЗЛ)   │
│  • Source Adapters → Canonical Schemas → Data Contracts       │
│  • выдача агенту: data.query(entity, filter) → JSON            │
│  → НОВОЕ, ядро этого документа                                │
└──────────────────────────────────────────────────────────────┘
        ↑ единый контракт вызова: session_id + JWT + cost_journal
```

Рецепт = **Agent Plane** (кто думает) + **Tool Plane** (чем делает) + **Data Plane**
(на каких данных). Вход рецепта — JSON-структура из Data Plane, а не сырой текст.

---

## 3. DATA PLANE — детально (ядро)

### 3.1 Назначение
Превратить разнородные источники в **канонические машиночитаемые сущности** с контрактом,
чтобы агенты и рецепты работали с предсказуемым JSON, а не парсили человеческие форматы.

### 3.2 Компоненты
1. **Source Adapter** (на источник): mail(IMAP), Jira, Confluence, GitHub, БД, web, файлы(OCR),
   CRM. Умеет `pull` (по запросу/расписанию) и/или `push` (webhook). Знает, как извлечь и
   нормализовать данные конкретной системы.
2. **Canonical Schema** (на сущность): единая типизированная модель — `Email`, `Issue`,
   `Customer`, `Transaction`, `Document`, `Meeting`… Версионируется.
3. **Data Contract** (на выдачу): что агент получит — поля, типы, обязательность, провенанс,
   свежесть. Стабильный интерфейс между Data Plane и рецептами.
4. **Data Tools** (в Tool Plane): `data_query(entity, filter, fields) → JSON[]`,
   `data_get(entity, id) → JSON`, `data_schema(entity) → schema`. Агент берёт данные ТОЛЬКО так.

### 3.3 Свойства каждой записи (обязательны)
- `schema` + `schema_version` — самоописание;
- `id` — стабильный идентификатор сущности;
- `provenance` — источник, адаптер, время извлечения, ссылка на оригинал;
- `freshness`/`ttl` — насколько свежо, когда протухает;
- нормализованные связи (`refs`) между сущностями (Email→Customer, Issue→Project).

### 3.4 Пример контракта (Email → canonical)
```json
{
  "schema": "email", "schema_version": "1.0",
  "id": "msg_8f2a...",
  "from": {"name": "Игорь Сидоров", "email": "i@acme.com", "customer_id": "cust_42"},
  "subject": "Счёт за март",
  "received_at": "2026-03-01T10:12:00Z",
  "body_text": "…нормализованный текст…",
  "attachments": [{"name": "invoice.pdf", "kind": "invoice", "doc_id": "doc_11"}],
  "labels": ["billing"], "thread_id": "thr_3",
  "provenance": {"source": "imap:acme", "adapter": "mail@1.2", "fetched_at": "…", "raw_ref": "…"},
  "ttl_sec": 86400
}
```
Агент получает это через `data_query("email", {"labels":"billing","since":"…"})` — и сразу
считает дайджест/отчёт, не парся IMAP и HTML.

### 3.5 Типы адаптеров
- **pull**: mail, Jira, БД, web (по запросу агента/рецепта);
- **push/webhook**: события систем → Data Plane (новое письмо, новый тикет);
- **file**: скан/PDF → OCR → structured (invoice/резюме/договор);
- **computed**: производные сущности (RFM из Transaction, воронка из событий) —
  фактически рецепт, чей выход снова кладётся в Data Plane как сущность.

### 3.5-bis. Data Plane УЖЕ ЧАСТИЧНО ЕСТЬ в LUDA (не изобретаем заново)
В репозитории LUDA это фактически стартовало — переиспользуем/формализуем, а не пишем с нуля:
- `luda/connectors/base.py` → **Source Adapter** (контракт коннектора; read-only метрики).
- `luda/canon/` (`normalize, align, contradictions, graph, morph, temporal, thresholds, codelist`)
  → движок **канонизации/нормализации** источник→canonical (ядро Data Plane).
- `luda/spec/process_agent_spec.schema.json` + `emit.py` + `validate.py` → **Data Contracts**
  (Process Agent Spec: CapabilityRequest / DeploymentContract / BaselineMeasurement).
- `rules-engine` (декларативный YAML: ФЗ-152, reversibility) → декларативные правила поверх данных.
В ABOP/учебном проекте мы даём **упрощённый аналог** этого слоя; в проде — код LUDA.

### 3.6-bis. РЕЦЕПТЫ ДАННЫХ пишет ЧЕЛОВЕК (ключевое требование, self-serve)
Адаптер источник→canonical должен создаваться **не только программистом**. Доменный эксперт
описывает **декларативный «рецепт данных»** — без исполняемого кода — который переводит источник
в каноническую сущность. Это ровно паттерн LUDA **VerticalPack** (манифест + пробы + `mapping` +
`rules YAML` + метрики, «без исполняемого кода», ведёт эксперт, **строгий YAML/JSON-валидатор**;
ADR-023).

**Data Recipe (декларативный, версионируемый):**
```yaml
recipe: acme-invoices        # id рецепта данных
version: 1
source: {kind: imap, ref: acme, scope: "label:billing"}   # откуда (адаптер + фильтр)
entity: invoice              # какую каноническую сущность производим
map:                         # маппинг полей источника → canonical (без кода)
  id:        "$.message_id"
  amount:    {from: "$.attachments[?(@.kind=='invoice')].total", cast: money}
  customer:  {lookup: customer, by: "$.from.email"}
  due_date:  {from: "$.body", extract: date}   # extract = встроенный экстрактор
rules:                       # декларативные правила/валидация (как rules-engine LUDA)
  - assert: "amount > 0"
  - pii_mask: ["$.from.email"]
emit: {schema: invoice, schema_version: "1.0", ttl_sec: 86400}
```
Свойства:
- **Декларативно** (YAML/JSON), **валидируется схемой** (нельзя сломать канон);
- **версионируется** и хранится как артефакт (переживает, ревьюится, откатывается);
- **два вида рецептов, не путать:** *Data Recipe* (этот — источник→canonical, пишет эксперт) vs
  *Agent Recipe* (плейбук: агент+tools+LLM, напр. дайджест/RFM — §4);
- редактор рецептов данных (no-code UI + предпросмотр результата на реальной выборке) — часть
  фронта ABOP.

### 3.6 Хранение и связь с Blackboard
- Канонические сущности — в **хранилище Data Plane** (Postgres/JSONB + опц. RAG-индекс для
  семантического поиска). Адаптер = ETL источник → canonical store.
- **Blackboard остаётся рабочей памятью прогона** (event-sourced: факты/claims/крошки),
  Data Plane — **долговременный типизированный слой источников**. Разделение: Blackboard = «что
  агенты уже надумали в этой задаче», Data Plane = «что есть в системах в машиночитаемом виде».
- Агент в рецепте: `data_query` (взять факты из источников) → работа/`run_python` →
  `bb_post` (промежуточное на доску) → `save`/`data_write` (результат).

---

## 4. TOOL PLANE (сводка, детали — в отдельном tool-каталоге)

- **Примитивы** (атомарные, под JWT, cost_journal): `run_python`(эфемерный Docker, sandbox),
  `web_fetch`, `render_diagram`, `doc_export`, `save`/`save_cloud`, `mail_fetch`, `tracker`,
  `data_query`/`data_get`/`data_schema`.
- **Рецепты** (композиции LLM+примитивы+данные): RFM, дайджест, отчёт, отрисовка архитектуры,
  заведение/трекинг тасок. Их пишут пользователи/студенты — платформа даёт примитивы и эталоны.
- **Бэкенды исполнения**: gateway (в процессе), docker (allowlist, без сети, лимиты, `--rm`,
  изоляция — не трогать sLAVA/qwen), integration (creds в .env / BYO).
- **Guardrails**: action-tools → `dry_run` + подтверждение + фильтр prompt-injection.

---

## 5. AGENT PLANE (уже реализовано в `ape`, наследуется ABOP)
- ReAct tool-calling; **оркестрация волнами** (fan-out/fan-in, барьер, следующая волна видит доску);
- **Blackboard** event-sourced: append-only, состояние=свёртка, snapshot-компакция, claim
  (нет дублей), нет конфликтов записи/потерь/«кто чистит»;
- память диалога с дистилляцией в конспект; прерывание (Esc); лимиты/квоты через шлюз.

---

## 6. Единый контракт (сквозной для ape и ABOP)
Любой вызов (tool или data) идёт через MCP-шлюз: `session_id` + **JWT студента** (авторизация,
изоляция, мультитенантность) → запись в `cost_journal` → структурный результат `{data|text,
artifacts[], provenance}`. Один слой обслуживает и CLI `ape`, и фронт ABOP.

---

## 7. Фазы
- **Ф0 (готово):** Agent Plane в `ape` (агенты/волны/Blackboard/клиентские tools).
- **Ф1:** Tool Plane на шлюз — `run_python` + `web_fetch` под JWT (эталон вызова + артефакты).
- **Ф2:** Data Plane MVP — 1 адаптер (mail или Jira) → canonical schema → `data_query`;
  1 рецепт на нём (дайджест/отчёт).
- **Ф3:** каталог адаптеров (Confluence/БД/CRM/файлы-OCR) + computed-сущности (RFM);
  фронт ABOP как «палитра источников + инструментов + запуск рецептов».
- **Ф4:** action-tools (write в Jira/почту) с подтверждениями; расписания/вебхуки.

---

## 8. Открытые вопросы
Решено:
- ✅ **Скоуп ABOP** — продуктизация LUDA v2 (рантайм VRATA на паттернах openclaw), отдельный
  продукт/репо, регулируемый периметр. Учебный проект — полигон принципов.
- ✅ **Рецепты данных пишет человек** — декларативно (VerticalPack-стиль), no-code, валидируется схемой.
- ✅ **Data Plane частично есть** в LUDA (`canon/`, `connectors/`, `spec/`, rules-engine).

Осталось:
1. Хранилище Data Plane в проде: Postgres+JSONB (как в LUDA) vs отдельный движок; RAG-слой поверх — где.
2. Первый источник/вертикаль для Data Plane (в LUDA стартовая — **1С month-end**, `vp-1c-monthend`;
   в учебном — что берём: почта/Jira/CSV?).
3. Как физически переиспользуем шлюз/Keycloak/Qwen из учебного контура в ABOP-периметре
   (регуляторка: `external_egress` через прокси sLAVA, `local_llm` — внутренний, ПДн-маскирование).
4. Мультитенантность источников: общие vs BYO-креды.
5. Формат редактора Data Recipe (no-code UI) на фронте ABOP.

---

## 9. Связанные материалы

**Комплект документов ABOP (как у LUDA v2):**
- [`ABOP_PRD.md`](ABOP_PRD.md) — что и зачем (продукт, проблема, границы, пользователи, концепции).
- [`ABOP_ADR.md`](ABOP_ADR.md) — 21 архитектурное решение (Nygard); канон ссылок ADR-0NN.
- [`ABOP_AOP.md`](ABOP_AOP.md) — Agent Operating Protocol: роли, дисциплина, фазы прогона, стык с LUDA.
- [`ABOP_TDR.md`](ABOP_TDR.md) — техническое устройство (топология, компоненты, модель данных, инварианты).
- [`AGENT_FAMILIES_GUIDE.md`](AGENT_FAMILIES_GUIDE.md) — как назначать задачи семьям/ролям (для пользователя).
- Этот документ (`ABOP_ARD.md`) — архитектурный обзор (3 плоскости, Data Plane).

**Реализация и полигон:**
- Принципы Agent/Blackboard/волны/tools/семьи — реализованы в `cli/ape.py` (см. [`../cli/README.md`]).
- Дизайн-система/портал — [`DESIGN.md`](DESIGN.md), [`DESIGN_SPEC.md`](DESIGN_SPEC.md).
