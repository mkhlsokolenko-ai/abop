# Шаблоны извлечения навыков (`skills/<sid>/template.json`)

Дата: 2026-09-27. Статус: на проде, 51 навык из 56 (4 демо-кейса + все бизнес-, финансовые и архитектурные навыки). Без шаблонов только code-навыки: conventional-commits, docker-patterns, fastapi-patterns, grill-me, test-writer (их результат — код/диалог, не структура).

## Зачем

До шаблонов все навыки отвечали по одной общей схеме «находки/итог»: `{находки:[{запись, наблюдение, сумма, норма}], итог}`.
Для ранжирования, первопричин, вердикта по инвестициям, DCF или письма клиенту это слишком узко — модель
либо теряла структуру (ранг, порог существенности, критерии приёмки), либо запихивала всё в «наблюдение».

Шаблон = подробная JSON Schema результата + инструкция-парсер. Модель отвечает строго по схеме
(vLLM xgrammar, `response_format=json_schema`), рантайм рендерит результат на доску прогона и кладёт
машиночитаемый объект в поле `structured` находки.

## Формат файла

```json
{
  "name": "Аудит 1С · ранжирование по существенности (rank)",
  "instruction": "Ранжируй находки … Суммы — только из данных …",
  "json_schema": { "type": "object", "additionalProperties": false, "required": [...], "properties": {...} },
  "max_tokens": 4000
}
```

- `json_schema` — только `type / properties / required / items / enum / additionalProperties:false`.
  Без `$ref`, `pattern`, `format`, `oneOf` (строгий режим vLLM их не понимает).
- Имена полей — русские, как в предметной области (`порог_существенности`, `критерии_приёмки`).
- Суммы и даты — строками с единицей («194 000,00 ₽», «2026-03»), чтобы модель не округляла и не «считала».
- `enum` для всего, где есть закрытый список (ранг, риск, уверенность, приоритет).
- `max_tokens` (необязательно) — лимит ответа для длинных схем. Без него берётся
  `ABOP_RUN_MAX_TOKENS_TEMPLATE` (3200). Explain — 7000, verdict/finance/dcf — 5000, bft/root-cause/rank/match-weak — 4000.

## Как это работает в рантайме

1. **Сид при старте** (`server/skill_templates.seed`): каждый `template.json` записывается в `schema_templates`
   как builtin с `id = <sid>`. В инструкцию добавляются маркеры `[repo:<fingerprint>]` и `[max_tokens:N]`;
   если файл в репо не менялся — запись не перезаписывается. Ручная правка шаблона в UI (builtin=false) имеет приоритет.
2. **Привязка** (`web_api._skill_schemas`): у навыка берётся явный `schema_template_id` из UI; если его нет или он равен
   общему `findings`, используется шаблон с id навыка. Если шаблона нет — общая схема находок.
3. **Промпт**: инструкция шаблона (без маркеров) + описание полей схемы (`describe_for_prompt`) попадает в системную часть промпта навыка.
4. **Лимит**: `max(ABOP_RUN_MAX_TOKENS, max_tokens шаблона, ABOP_RUN_MAX_TOKENS_TEMPLATE)`.
5. **Разбор**: `_extract_json`; если ответ обрезан по лимиту — `_repair_json` откатывается к последнему завершённому
   элементу, закрывает скобки и ставит маркер `_truncated` (на доске: «⚠ ответ модели обрезан…»).
6. **Рендер на доску** (`runner._render_struct`): скаляры «ключ: значение», списки объектов — маркированные строки
   (заголовок из первого осмысленного поля: название/причина/id…), вложенность — отступом. Общий рендер «находок»
   (`_render_findings`) применяется только если элементы несут поле `наблюдение` (дефолтная схема).

## Покрытие (51)

| Группа | Навыки |
|---|---|
| Аудитор 1С (демо) | audit1c-extract, graph-build, match-weak, checks, root-cause, rank, explain |
| Инвест-контроль 1С (демо) | invest1c-trace, invest1c-verdict |
| Почта / день менеджера (демо) | mail-triage, daily-plan, client-letter, email-draft, email-thread-reconstruct |
| Требования / БФТ (демо) | bft-draft, spec-reviewer, to-tickets, meeting-action-items, process-map, one-three-one |
| Финансы | dcf-valuation, unit-economics-checker, finance-report, budget-forecast, three-statement-model, variance_explanation, ledger_reconciliation, period_close_orchestration, cost-estimator |
| Кредитный конвейер | application_intake_validation, limit_policy_enforcement, disbursement_orchestration |
| Отчётность / PM | status-report, weekly-update, dashboard-builder |
| Продукт / идеи | market-research, idea-scorer, idea-selector, jtbd-formulator, devils-advocate, icp-interviewer, researcher |
| Архитектура / AI | adr-writer, api-design, c4-diagram, architecture-chooser, memory-architect, rag-architect, eval-generator, agent-design-writer, mlsdd-writer |

Лимиты: 3200 (4 компактных), 4000 (22), 5000 (16), 7000 (explain); 8 ранних шаблонов без поля — берут `ABOP_RUN_MAX_TOKENS_TEMPLATE`.

Проверка strict-совместимости всех шаблонов: `python scripts/check_templates.py skills/*/template.json` (все ключи из разрешённого набора,
`additionalProperties:false`, `required` = все свойства, у массивов есть `items`).

## Персистентность: источник правды — БД

Рантайм читает шаблоны только из таблицы `schema_templates` (Postgres). Репозиторий — это seed и «версия по умолчанию»,
а не то, что видит прогон. Поэтому:

- **Колонки:** `id, name, json_schema, instruction, builtin, editor, updated_at, max_tokens`. `max_tokens` — колонка
  (старые посевы держали маркер `[max_tokens:N]` в инструкции, он тоже понимается).
- **builtin=true** — шаблон управляется репо/импортом (маркер `[repo:<отпечаток>]` в инструкции делает посев идемпотентным).
  **builtin=false** — ручная правка: посев при старте и импорт без `force` её не трогают.
- **Ручная правка** (`POST /api/schema-templates/{id}` с `name/instruction/json_schema/max_tokens`) снимает builtin,
  валидирует схему на strict-совместимость (422 с перечнем ошибок), чистит служебные маркеры.
- **Сброс к репо:** `POST /api/schema-templates/{id}/reset` (берёт `skills/<id>/template.json` из образа).
- **Импорт без пересборки образа:** `POST /api/schema-templates/import` `{templates:[{id,name,instruction,json_schema,max_tokens}], source:"repo"|"manual", force}`
  → `{imported, skipped, errors}`. Admin-уровень, до 200 шаблонов за вызов, каждый проверяется.
- **CLI:** `python scripts/push_templates.py --base https://<abop> --user admin.abop --password '…' [--only a,b] [--force] [--dry-run]`
  — читает `skills/*/template.json`, проверяет локально, логинится, импортирует. Пароль можно дать через `ABOP_ADMIN_PASSWORD`.

## Доставка результата в систему: превью → HITL → коннектор

ABOP — платформа запуска: результат навыка живёт в целевой системе (задача в Redmine, страница в BookStack,
письмо), а не в счётчике на Обзоре. Поэтому шаблон описывает не только структуру ответа, но и куда она уходит —
секция `delivery` (персистентна в БД, колонка `delivery`):

```json
"delivery": {
  "system": "redmine", "type": "issue.create",
  "each": "находки", "where": {"ранг": ["критично", "существенно"]}, "limit": 10,
  "title": "Задача Redmine: {{заголовок}}",
  "payload": {"subject": "Аудит 1С · {{ранг}} · {{id}}", "description": "h2. Откуда
{{#откуда.документы}}
…{{$.резюме_для_главбуха}}"}
}
```

- `each` — массив ответа, по команде на элемент (без `each` — одна команда на весь ответ); `where` — фильтр по enum-полям; `limit` — потолок.
- Подстановки: `{{путь}}` из элемента, `{{$.путь}}` из корня ответа, `{{#путь}}` — список маркерами. Неизвестный путь → пусто, ничего не выдумывается.
- `payload` — поля команды коннектора (`connector/worker.py`: для `redmine/issue.create` это `subject`, `description`, `project`, `priority_id`).

Цепочка в рантайме (`web_api._deliver_templates`, после OUT-узлов):
1. Structured-ответы навыков снимаются в `result.skill_outputs` (до подмены `findings` детерминированными).
2. `delivery.build_commands` собирает команды; ABAC проверяет доступ семьи агента к системе (иначе `mode: denied`).
3. Каждая команда → **HITL-заявка канала `command`** с превью того, что уйдёт (`html`: система/действие, тема, описание).
   В карточке прогона — строка «redmine → redmine/issue.create · «тема» · ожидает вашего подтверждения».
   Наружу в этот момент не уходит ничего. `deliver_filter="chat"` («только в чат») отключает доставку.
4. Оператор смотрит превью (десктоп: «Посмотреть и подтвердить», веб: очередь HITL) и подтверждает → команда публикуется
   в `abop.<система>.commands`, в заявку пишется `command_id`.
5. Коннектор исполняет и публикует `command.done|failed` → `_on_command_result` кладёт результат (`issue_id`, `url` или ошибку)
   в заявку (`result_state`, `result`); десктоп опрашивает заявку до 90 с и показывает «✓ выполнено: http://…/issues/N» в карточке.

Покрыты все четыре демо-кейса:

| Навык | Куда уходит | Что создаётся |
|---|---|---|
| `audit1c-explain` | redmine · issue.create | задача на каждую находку ранга критично/существенно (что не сходится, документы, проводки, норма, последствия, что проверить) |
| `audit1c-rank` | redmine · issue.create | одна сводная задача: порог существенности, рейтинг, топ-3 действия |
| `invest1c-verdict` | bookstack · page.publish | страница заключения по расследованию (заключения с нормами, срок, требуется ли подтверждение) |
| `client-letter` | mailpit · email.send | письмо заказчику: адрес и тема из ответа навыка, тело из «в работе / план / сроки / нужно от вас» |
| `bft-draft` | redmine · issue.create | задача «согласовать БФТ»: цель, контекст, роли, требования, критерии приёмки, границы, открытые вопросы |

Числовые поля payload (например `book_id`) остаются числами — подстановка идёт только в строках.

**Когда доставка не сработает (по конструкции):** команды собираются из структурированного ответа навыка, поэтому
навык должен выполниться. Навык с объявленным data-scope, но без данных в store, пропускается (в прогрессе прогона —
«пропущено: N»); навык без своего data-scope (письмо, БФТ, отчёт) выполняется по контексту — находки прогона, задача
пользователя, контекст цепочки — и пропускается, только если контекста нет вовсе. Внутри одного агента навыки идут
параллельно и не видят выход друг друга: связка «дайджест → письмо» делается цепочкой агентов (выход первого приходит
контекстом второму) либо OUT-узлом канала email с отчётом по шаблону. Проверка: `python scripts/check_templates.py` валидирует секцию; тесты
`test_delivery_render_commands_from_template`, `test_delivery_templates_make_hitl_preview_and_take_connector_result`.

## PDF-отчёт по шаблону

Второй выход результата — отчёт (HTML/PDF) по шаблону из `reports/<id>.html` (посев в `report_templates`, правится в веб-редакторе
с превью, источник правды — БД). В контекст отчёта попадают структурированные ответы навыков по их шаблонам извлечения:
`{{skills}}` — все, `{{skill_<sid>}}` — конкретный (дефисы → подчёркивания; объекты — строки «ключ: значение», списки объектов —
таблицы, списки строк — маркеры). Кейс-шаблоны: `audit1c` (пояснения, рейтинг, первопричины, детерминированные находки),
`invest` (вердикт, расследования, трассировка), `digest` (разбор почты, план дня, письмо), `default`.

- По требованию: `GET /api/runs/{run_id}/report?template=<id>&format=html|pdf` (PDF рендерит Gotenberg ABOP);
  в десктопе — кнопка «📄 Отчёт PDF» на карточке прогона (файл в «Загрузки»).
- В доставке: OUT-узел канала `pdf`/`email`/`bookstack` с `report_template_id` рендерит тот же шаблон (иначе авто-выбор по форме результата).
- Подробнее: `reports/README.md`.

## Как добавить или поменять шаблон

1. Создать/править `skills/<sid>/template.json` по формату выше.
2. Проверить: `python scripts/check_templates.py skills/<sid>/template.json` (та же логика, что в API).
3. Тесты: `tests/test_smoke_api.py` (загрузка, посев, импорт/правка/сброс, рендер).
4. Залить на стенд **без пересборки**: `python scripts/push_templates.py --base … --only <sid>`. Посев при следующей
   пересборке образа ничего не перепишет (отпечаток совпадёт).
5. Проверить на реальном прогоне: поле `structured` в карточке находки и текст на доске.

## Проверено на проде (2026-09-27)

Прогон `audit1c-holding-2026.v9` на локальном Qwen3-30B-A3B (`local/qwen3-30b-a3b`, 0 ₽): все 7 навыков распарсены,
rank отдаёт порог существенности + рейтинг + топ-3 действия, explain — по всем 10 детерминированным находкам.
