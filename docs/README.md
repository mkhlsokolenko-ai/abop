# Документация ABOP — индекс

> Актуально на 2026-09-27. Детали рантайма (очередь, шина, коннектор, шаблоны, LLM-бокс) описаны в «Актуальных» документах; исторические спецификации фиксируют замысел и решения (ADR), но не текущее устройство сервера.

## Актуальные (эксплуатация, продукт, сессии)

| Документ | О чём |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Runbook: топология server-1 / server-2 / GPU-бокс, env, раскатка кода и шаблонов, Redpanda и коннектор, LLM-бокс, восстановление Data Plane, частые проблемы |
| [API_ENDPOINTS_PORTS.md](API_ENDPOINTS_PORTS.md) | Все эндпоинты `server/web_api.py` по группам, порты сервисов на обоих серверах, список переменных окружения |
| [SKILL_TEMPLATES.md](SKILL_TEMPLATES.md) | Шаблоны извлечения навыков (`skills/<sid>/template.json`, 51/56), персистентность в БД, импорт без пересборки, доставка результата превью → HITL → коннектор |
| [CONNECTOR_WORKER.md](CONNECTOR_WORKER.md) | Коннектор-воркер на server-2: поток команд через шину, адаптеры, секреты, дедуп/ретраи/DLQ, метрики и Grafana |
| [CONCEPT_SCALING_OBSERVABILITY.md](CONCEPT_SCALING_OBSERVABILITY.md) | Концепция масштабирования по Хартии v3.5; §13–15 — реализованные Фаза 1 и Блоки 1/3 (очередь, цепочки/HITL через очередь, шина, tool-calling, нагрузка) |
| [SECURITY_FIXES_2026-09-26.md](SECURITY_FIXES_2026-09-26.md) | Разбор red-team #48: fail-closed auth, ABAC контрактов, санитизация промптов, origin-gate, аудит блокирующих вызовов |
| [RELEASE_DISCIPLINE.md](RELEASE_DISCIPLINE.md) | Дисциплина правок: веб (исходники `webapp/src` → бандл), десктоп (версия + тег владельца), smoke и CI-гейты |
| [PILOT_1C_FINDINGS.md](PILOT_1C_FINDINGS.md) | Продукт пилота «Аудитор данных в 1С»: журнал и карточки находок, слой объяснения, слепая разметка, метрики пилота |
| [INSTRUKCIYA_ZAPUSK_AGENTA.md](INSTRUKCIYA_ZAPUSK_AGENTA.md) | Пошаговая инструкция для не-технического пользователя: как собрать и запустить агента |
| [GUIDE_SBORKA_AGENTOV.md](GUIDE_SBORKA_AGENTOV.md) | Полный гайд: от пустого места до агента, приносящего ценность (навыки, данные, доставка) |
| [CONNECTORS_REAL_PO.md](CONNECTORS_REAL_PO.md) | Как перевести агента с демо-стенда на реальные системы заказчика (почта, трекер, вики, БД) |
| [DEMO_SCENARIY_2026-09-28.md](DEMO_SCENARIY_2026-09-28.md) | Сценарий демо на 20 минут: четыре кейса, тайминг, чек-лист, план Б |
| [DEMO_GID_2_MENEDZHERSKIH_KEISA.md](DEMO_GID_2_MENEDZHERSKIH_KEISA.md) | Гайд-сценарий демо: веб-расследование invest1c + два менеджерских кейса (десктоп → веб) |
| [DEMO_KEISY_ZADACHI.md](DEMO_KEISY_ZADACHI.md) | Два кейса на канве для показа: «Дайджест задач» и БФТ на реальных сервисах стенда |
| [PRODUCT_BACKLOG.md](PRODUCT_BACKLOG.md) | Ранжированный продуктовый бэклог; Tier 0–3 закрыты, остаток — XL «после PMF» |
| [UX_AUDIT_2026-09-25.md](UX_AUDIT_2026-09-25.md) | UX-аудит десктопа и веба со статусами исправлений |
| [SESSION_SUMMARY_2026-09-27.md](SESSION_SUMMARY_2026-09-27.md) | Итог сессий 26–27.09: Блоки 1–5, безопасность, шаблоны, доставка, LLM-инцидент, проверка на проде |
| [SESSION_SUMMARY_2026-09-25.md](SESSION_SUMMARY_2026-09-25.md) | Итог 25.09: бэклог Tier 0–3 закрыт, HITL в чате, демо готово, релиз десктопа 1.0.5 |
| [SESSION_SUMMARY_2026-09-24.md](SESSION_SUMMARY_2026-09-24.md) | Итог 24.09: отчёты по кейсам, цепочки, биллинг, tool-calling (графики/расчёты) |
| [SESSION_SUMMARY_2026-09-14.md](SESSION_SUMMARY_2026-09-14.md) | Итог 14.09: ЛК/RBAC, админ-панель, канва, флот |
| [ADR_DESKTOP_ABOP_SMYCHKA.md](ADR_DESKTOP_ABOP_SMYCHKA.md) | ADR смычки десктопа с ABOP: сайдкар-прокси, multi-issuer JWT, Путь А |
| [ДЕМО_ПРОГОН_1С.md](ДЕМО_ПРОГОН_1С.md), [КАК_СОБРАТЬ_АГЕНТА.md](КАК_СОБРАТЬ_АГЕНТА.md), [ГАЙД_ABOP_ПОЛНЫЙ.md](ГАЙД_ABOP_ПОЛНЫЙ.md) | Пользовательские гайды по экранам веба (сентябрь; экраны с тех пор дорабатывались — сверяться с UX-аудитом) |
| [DATA_RECIPES.md](DATA_RECIPES.md), [AGENT_FAMILIES_GUIDE.md](AGENT_FAMILIES_GUIDE.md), [APE_GUIDE.md](APE_GUIDE.md), [START.md](START.md) | Ядро `ape`: рецепты данных, семьи → роли → навыки, CLI |

## Исторические / архитектурные (сентябрь, до масштабирования)

Замысел, решения и спецификации экранов. Терминология и ADR действуют, но **детали рантайма** (очередь и воркеры, шина Redpanda и коннектор, шаблоны навыков в БД, self-host LLM, fail-closed auth) смотреть в актуальных документах выше.

| Документ | О чём |
|---|---|
| [ABOP_ADR.md](ABOP_ADR.md) | Реестр архитектурных решений (ADR-013 конверт, 024 Лимб, 027 два репо, 028–032 канва/ингресс/спека) |
| [ABOP_SDD.md](ABOP_SDD.md), [ABOP_PRD.md](ABOP_PRD.md), [ABOP_TDR.md](ABOP_TDR.md), [ABOP_ARD.md](ABOP_ARD.md), [ABOP_AOP.md](ABOP_AOP.md) | Системный дизайн, продуктовые требования, технический/архитектурный разбор, операционный план |
| [ABOP_API.md](ABOP_API.md) | Первоначальный API-контур ядра (единое ядро, много клиентов) — актуальные маршруты в API_ENDPOINTS_PORTS.md |
| [ABOP_RUNTIME_ARCHITECTURE.md](ABOP_RUNTIME_ARCHITECTURE.md) | Замысел локального рантайма (иерархия моделей, GraphRAG, кеши) |
| [ABOP_LUDA_ROLE_SPLIT.md](ABOP_LUDA_ROLE_SPLIT.md), [LUDA2_FUNNEL_SPEC.md](LUDA2_FUNNEL_SPEC.md) | Разделение ролей LUDA 2 ↔ ABOP, воронка бизнес-заказчика |
| [ABOP_SCREENS.md](ABOP_SCREENS.md), [ABOP_CANVAS_FIXSPEC.md](ABOP_CANVAS_FIXSPEC.md) | Спецификации экранов и канвы |
| [ABOP_FLEET_OPS.md](ABOP_FLEET_OPS.md), [ABOP_OPERATIONS_MAP.md](ABOP_OPERATIONS_MAP.md) | Замысел слоя операций / флота (реализация — `/api/fleet`, экран «Флот») |
| [DESIGN.md](DESIGN.md), [ABOP_DESIGN_BRIEF.md](ABOP_DESIGN_BRIEF.md), [ABOP_DESIGN_KIT.md](ABOP_DESIGN_KIT.md), [ABOP_DESIGN_HANDOFF.md](ABOP_DESIGN_HANDOFF.md), [ABOP_DESIGN_GAPS.md](ABOP_DESIGN_GAPS.md), `brand/` | Дизайн-система, бриф, кит, handoff, бренд |
| [ABOP_UX_FIXES.md](ABOP_UX_FIXES.md), [ABOP_UX_REFINEMENT.md](ABOP_UX_REFINEMENT.md), [ABOP_2MIN_TEST.md](ABOP_2MIN_TEST.md) | Ранние UX-ревью и тест «2 минуты нового пользователя» (актуальный аудит — UX_AUDIT_2026-09-25.md) |
| [architecture.md](architecture.md), [architecture.png](architecture.png), [ABOP_Architecture.html](ABOP_Architecture.html), `ABOP Prototype (standalone).html` | Схемы курсового контура и прототип |
| [alternatives.md](alternatives.md), [cost-analysis.md](cost-analysis.md) | Выбор моделей и cost-анализ (август; сейчас основной LLM — self-host Qwen за 0 ₽) |
