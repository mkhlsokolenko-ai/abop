# Шаблоны отчётов (PDF/HTML) — `reports/<id>.html`

Отчёт прогона рендерится по шаблону: HTML с плейсхолдерами `{{…}}` + общий `_base.css`. Файлы здесь — посев;
в рантайме источник правды — таблица `report_templates` в БД (веб-редактор с превью, `/api/report-templates`).
Правка в UI имеет приоритет над посевом; builtin-шаблон с `editor=seed` обновляется при старте, если файл изменился.

Плейсхолдеры (готовит `web_api._report_context`): `title`, `agent`, `date`, `verdict`, `findings_total`, `investigations_total`,
`by_class`, `charts`, `findings`, `investigations`, `deliveries`, `skills` (структурированные ответы всех навыков по их
шаблонам извлечения — таблицы/списки) и `skill_<sid>` для конкретного навыка (дефисы → подчёркивания:
`{{skill_audit1c_rank}}`, `{{skill_invest1c_verdict}}`). Пустой плейсхолдер рендерится как пусто.

Где используется: `GET /api/runs/{run_id}/report?template=<id>&format=html|pdf` (кнопка «Отчёт PDF» в десктопе),
OUT-узлы каналов pdf/email/bookstack (`report_template_id` узла, иначе авто-выбор по форме результата).
`index.json` — имена и pdf_options по id.
