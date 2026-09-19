# ABOP — разворачивание, восстановление, настройка (runbook)

> Актуально на 2026-09-18. Прод: **server-1 `5.129.192.63`**. Полный список эндпоинтов/портов — в [`API_ENDPOINTS_PORTS.md`](API_ENDPOINTS_PORTS.md). Инструкция для пользователя (как запускать агентов) — [`INSTRUKCIYA_ZAPUSK_AGENTA.md`](INSTRUKCIYA_ZAPUSK_AGENTA.md).

---

## 1. Что где работает (топология)

| Что | Где | Порт | Персистентность |
|---|---|---|---|
| **ABOP Web API** (`abop-webapi`) | server-1, docker, `--network host` | 8091 | код в образе; данные — в томе `abop_ape` + Postgres |
| Postgres | server-1 | 5433 | том `ape_pg` (agents/contracts/runs/skills/рецепты/…) |
| Keycloak | server-1 | 8811 | realm `abop` (JWT) |
| Prometheus | server-1, docker | 9091 | том `abop_prom_data` |
| Grafana | server-1, docker | 3300 | том `abop_grafana_data` |
| Демо-сервисы (Redmine/BookStack/Twenty/Mailpit/NocoDB/Gitea/Kroki/MinIO) | server-1 | см. API-док | свои тома |
| sLAVA (RAG) + Qdrant + Gotenberg + дамп 1С | **server-2 `201.51.5.24`** | 8000/6333/3050/8092 | — |

**Где живут данные (важно для восстановления):**
- **Postgres (`ape_pg`)**: агенты, контракты, прогоны, навыки-правки, **определения** рецептов/коннекторов (`dp_recipes`/`dp_connectors`), admin-config, RBAC, память агентов, конформанс.
- **Том `abop_ape` (`/root/.ape`)**: **эмитированные канонические данные** `data/*.jsonl` (doc1c/ref1c/transaction/…), файловые копии рецептов/коннекторов, сессии/память CLI. ⚠️ Эмитированные записи Data Plane живут ТОЛЬКО здесь — без тома пропадут при перенакате.

---

## 2. Разворачивание ABOP Web API (с нуля / обновление)

Каноничный скрипт: [`../ops/deploy-webapi.sh`](../ops/deploy-webapi.sh) — жёстко фиксирует том `abop_ape`, env-файл, health-check.

```bash
# на server-1
cd /opt/abop
# 1) выкатить код (из git или scp git archive)
git pull   # или: tar -xf _deploy.tar   (git archive HEAD cli server skills webapp demo Dockerfile.webapi ops)

# 2) собрать и перезапустить (ВСЕГДА с томом abop_ape!)
bash ops/deploy-webapi.sh
```

Что делает скрипт (эквивалент вручную):
```bash
docker build -q -f Dockerfile.webapi -t abop-webapi .
docker rm -f abop-webapi 2>/dev/null || true
docker run -d --name abop-webapi --network host --restart unless-stopped \
  --env-file /opt/abop/.env -v abop_ape:/root/.ape abop-webapi
curl -sf http://127.0.0.1:8091/api/health   # → {"ok":true,...}
```

> ⚠️ **Никогда не запускать без `-v abop_ape:/root/.ape`** — потеряете canonical store Data Plane.

### Деплой с локальной машины (как в разработке)
```bash
git archive HEAD cli server skills webapp demo Dockerfile.webapi ops -o _deploy.tar
scp _deploy.tar root@5.129.192.63:/opt/abop/
ssh root@5.129.192.63 'cd /opt/abop && tar -xf _deploy.tar && bash ops/deploy-webapi.sh'
```

---

## 3. Observability-стек (Prometheus + Grafana)

```bash
cd /opt/abop
docker compose -f ops/docker-compose.observability.yml up -d
```
- Prometheus `http://5.129.192.63:9091` — scrape-ит `abop-webapi:/metrics` (target `abop-webapi`).
- Grafana `http://5.129.192.63:3300` (admin/admin — сменить). Источник данных: Prometheus.
- Метрики ABOP: `abop_http_requests_total`, `abop_http_request_seconds`, `abop_runs_total`, `abop_run_seconds`, `abop_findings_total`, `abop_run_cost_rub_total`, `abop_deliveries_total`, `abop_run_soft_errors_total`.
- Сквозной `trace_id` — заголовок `X-Trace-Id` (генерится/пробрасывается); в прогоне поле `trace_id`.
- LLM-трейсы (langfuse) — каркас; включаются заданием `LANGFUSE_URL/PUBLIC_KEY/SECRET_KEY`.

---

## 4. Восстановление Data Plane (если прогоны дают 0 находок)

Симптом: прогон отрабатывает <1с, `findings: 0`, LLM не звался. Диагностика по шагам:

```bash
# 1) том смонтирован?
docker inspect abop-webapi --format '{{json .Mounts}}' | grep abop_ape

# 2) файлы данных есть и непустые?
docker exec abop-webapi wc -l /root/.ape/data/*.jsonl
#   ожидаемо: doc1c.jsonl ~300+, ref1c.jsonl ~150+, transaction.jsonl ~15

# 3) data_query отдаёт записи?
docker exec abop-webapi python -c "import sys;sys.path.insert(0,'/app/cli');import ape;print('doc1c',len(ape.data_query('doc1c',limit=99999)))"
```

- **Файлы есть, но data_query=0** → это была бага бинарного TTL (исправлено 2026-09-18: версионная freshness — последняя версия не протухает по времени). Если снова всплывёт — проверить `_fresh`/`data_query` в `cli/ape.py`.
- **Файлы пусты** → прогнать рецепты наполнения:
```bash
TOKEN=$(curl -s -XPOST http://127.0.0.1:8091/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<pass>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
for r in audit1c_docs audit1c_refs invoices crm_deals payments redmine_issues; do
  curl -s -XPOST "http://127.0.0.1:8091/api/data/recipes/$r/run" -H "Authorization: Bearer $TOKEN"; echo
done
```
Рецепты тянут из источников (дамп 1С `201.51.5.24:8092`, CRM и т.п.) и пишут в `~/.ape/data/*.jsonl`.

**Версионная семантика (решение владельца 2026-09-18):** `data_query` держит ПОСЛЕДНЮЮ созданную версию записи всегда (не протухает по wall-clock); по TTL уходят только перекрытые старые версии (`_data_compact` при `data_run`). `include_stale=True` → вся история версий.

---

## 5. Запуск демо-агента (после разворачивания)

Три готовых сценария (кнопка «▶ Собрать и запустить» на канве, либо API):
```bash
# идемпотентно собрать агента из demo/<scenario>/ и запустить
curl -s -XPOST http://127.0.0.1:8091/api/demo/prepare -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"scenario":"audit1c"}'   # audit1c | invest1c | fin
curl -s -XPOST http://127.0.0.1:8091/api/runs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"agent_id":"audit1c-holding-2026.v1"}'
```
Демо-пакеты: `demo/audit1c/`, `demo/invest1c/`, `demo/fin/` (contract*.json + agent_body.json). Требуют наполненного Data Plane (§4).

---

## 6. Конфигурация (`.env` на server-1)

Полный список — в [`API_ENDPOINTS_PORTS.md` §3](API_ENDPOINTS_PORTS.md). Ключевое:
- **База/аутентификация**: `POSTGRES_DSN`, `KEYCLOAK_ISSUER/JWKS_URI/JWKS_INTERNAL/AUDIENCE`.
- **LLM/RAG**: `ROUTEAI_API_KEY`, `ROUTEAI_BASE_URL`, `SLAVA_API_BASE_URL`, `QDRANT_URL`, `EMBED_MODEL=baai/bge-m3`.
- **OUT-доставка (реальные API, опц.)**: `YOUGILE_TOKEN`/`YOUGILE_COLUMN_ID`, `YANDEX_SMTP_USER`/`YANDEX_SMTP_PASSWORD` (пароль приложения). Без них — dry_run.
- **Прогон/производительность**: `ABOP_RUN_LLM_CONCURRENCY` (по умолч. 3 — единая карта RouteAI троттлит при бо́льших; замер 2026-09-18), `ABOP_RUN_LLM_RETRIES`, `ABOP_RUN_LLM_BACKOFF`, `ABOP_RUN_LLM_TRUNCATE` (0 на проде).
- **Планировщик**: `ABOP_SCHEDULER` (0=выкл на N-1 репликах — leader-election нет), `ABOP_SCHEDULER_TICK`.
- **Observability**: `LANGFUSE_URL/PUBLIC_KEY/SECRET_KEY`, `LOG_LEVEL`.

---

## 7. Производительность прогона (замеры 2026-09-18)

Оптимизация прогона audit1c (10 находок, снимок 1С):
| | до | дайджест данных | повтор |
|---|---|---|---|
| время | 357 с | **110 с** | 299 с (разброс латентности) |
| входные токены | 779 655 | **93 836** | 93 836 |
| стоимость | 15.35 ₽ | **2.80 ₽** | 2.76 ₽ |
| находки/нормы | 10 / 5 | **10 / 5** (без изменений) | 10 / 6 |

**Выводы:** реальный и СТАБИЛЬНЫЙ выигрыш — **дайджест данных** в LLM-промпт (счётчики по типам = полный scope + сэмпл, вместо полного дампа): −8× входных токенов, −5× стоимость, качество 1:1 (детекцию считает детерминированный код, не LLM). Разброс времени 110↔299с — это **вариативность латентности RouteAI** при одном и том же `ABOP_RUN_LLM_CONCURRENCY` (env перекрывает код-дефолт; был жёстко =2). Параллелизм на **одной** карте RouteAI упирается в троттлинг; поднимать выше 2-3 имеет смысл только с несколькими картами/своим vLLM. Дальнейшее ускорение — сокращение числа LLM-вызовов у детерминированных навыков (LLM там даёт лишь нарратив, детекция — код).

**Выделенный инстанс LLM (СДЕЛАНО 2026-09-18):** ABOP переключён на self-host **Qwen3-30B-A3B FP8** на Vast (`LOCAL_LLM_BASE_URL=http://95.3.33.46:44633/v1`, `LOCAL_LLM_MODEL=qwen3-30b-a3b`, `LOCAL_LLM_API_KEY=EMPTY`; `ROUTEAI_STANDARD_CASCADE=local/qwen3-30b-a3b,…` — local первым, RouteAI fallback). IP:port меняется при пересоздании бокса. На выделенной карте параллелизм заиграл (в отличие от RouteAI): **conc=8 → 77с**, а срез нарратива **max_tokens=1600 → ~50с** (2500→77с, 1200→42с с обрывом нарратива). Итог: **357с → ~50с (≈7×)**, находки/нормы 1:1, стоимость LLM ≈0. Ключи Vast/HF — в `Desktop/Робокасса.txt` (вне репо). **Второй Vast-инстанс = red-team.tech — НЕ ТРОГАТЬ.**

**Актуальные перф-настройки прода (env):** `ABOP_RUN_LLM_CONCURRENCY=8`, `ABOP_RUN_MAX_TOKENS=1600` (нарратив навыка; детекцию считает код), `ABOP_RUN_STRUCTURED=1` (глобальный kill-switch structured output).

**Формат вывода навыка — per-skill флаг `output` (structured|freeform).** Настраивается ТУМБЛЕРОМ в редакторе навыка (UI, раздел навыков) при заведении/правке. `structured` (по умолч.) — навык-детектор возвращает JSON по findings-схеме (минус «вода рассуждений»); `freeform` — навык-документ (письмо/план/БФТ/объяснение) возвращает свободный текст (иначе схема ломает форму). Хранится в PG (`skill_overrides.patch.output`), инжектится в `ape` (`set_skill_output_overrides`), инвалидируется между репликами (cachebus). Код-дефолты: детекторы structured; `daily-plan/client-letter/bft-draft` — freeform. Глобально можно выключить `ABOP_RUN_STRUCTURED=0`.

**Structured output + grounded объяснение (СДЕЛАНО 2026-09-18) — качество+скорость.** `run_live` просит СТРОГО JSON по findings-схеме через `response_format={"type":"json_schema",...}` (vLLM xgrammar запрещает прозу/markdown вне схемы → минус «вода»). Детекцию считает КОД (полные данные) ДО прогона и прокидывает в `run_live(findings_context=...)` → навык ОБЪЯСНЯЕТ реальные находки (grounded), не ищет заново на сэмпле (иначе LLM ложно писал «расхождений нет»). Поле `норма` — только статья закона (НК/ФСБУ/ПБУ), иначе пусто. Тумблеры: `ABOP_RUN_STRUCTURED=0` → свободный текст.

**ИТОГ оптимизации прогона: 357с → ~40с (≈9×), out-токены 18338→~9000, стоимость LLM ≈0, находки 10 (A1 B6 C1 D2) 1:1, нарратив объясняет реальные находки (док+проводки+статьи закона), читаемо.** Этапы: дайджест данных (357→110с) → local Qwen conc=8 (77с) → structured output (28с, но нарратив «0») → grounded (объяснение реальных находок, ~40с).

---

## 7а. Горизонтальное масштабирование (2+ реплики API)

Разблокировано (2026-09-18): реплики безопасно работают параллельно.
- **Инвалидация кэшей между репликами** (`server/cachebus.py`): Postgres `LISTEN/NOTIFY` канал `abop_cache`. Писатель делает локальный refresh И `notify(topic)` → все реплики перечитывают из PG (топики `skills`, `dataplane` — инжектятся в `ape`). Без этого реплика B держала бы устаревший кэш навык-источников/рецептов. Проверка: `docker logs abop-webapi | grep cachebus` → `cachebus.listening`/`cachebus.invalidated`.
- **Leader-election планировщика** (`triggers.scheduler_loop`): `pg_try_advisory_lock(0x41424F50)` — триггеры фаерит ТОЛЬКО реплика-лидер, остальные тикают вхолостую. При падении лидера Postgres освобождает lock → другая реплика перехватывает. Проверка: `SELECT * FROM pg_locks WHERE locktype='advisory' AND objid=1094864720` (держится 1 реплика); лог `scheduler.leader_acquired`.
- **Admission control к LLM** (`server/clients.chat`): ГЛОБАЛЬНЫЙ семафор `ABOP_LLM_MAX_INFLIGHT` (по умолч. 12) на все прогоны/запросы — при многих юзерах бокс не захлёбывается (семафор в runner был пер-прогон). Метрика `abop_llm_inflight`/`_max` в `/metrics`.
- **Per-user rate-limit** (`run_start`): скользящее окно `ABOP_USER_RATE_MAX` (30) / `ABOP_USER_RATE_WINDOW` (300с) на пользователя → 429 при превышении. Метрика `abop_rate_limited_total`.
- **Идемпотентность прогонов**: per-key lock (юзер+агент+`idempotency_key`) сериализует двойные сабмиты → второй берёт результат из version-aware кэша (двойной клик не запускает двойной тяжёлый прогон). Проверено: 2 параллельных → 1 реальный + 1 cached.
- **Что уже общее (PG):** agents/contracts/runs/skills/рецепты/admin/RBAC/память. **Canonical Data Plane** — в томе `abop_ape` (при нескольких хостах нужен общий том/перенос эмита в PG — TODO для мульти-хоста). NB: rate-limit и idempotency-lock — per-replica (в памяти); для строгой мульти-репличности вынести в PG/Redis.
- **Запуск 2-й реплики:** приложение на `--network host :8091` → второй реплике нужен свой порт + LB/Caddy upstream (по готовности). Механизм координации (кэш/лидер) уже готов.

## 8. Частые проблемы

| Симптом | Причина / решение |
|---|---|
| Прогон 0 находок, <1с | Data Plane пуст/протух — §4 |
| «Собрать и запустить» → «нет демо-пакета» | сценарий без `demo/<key>/` (готовы: audit1c, invest1c, fin) |
| «нет доступа к семье» | ABAC: у пользователя нет прав на семью — войти админом |
| OUT не отправился реально | у OUT-узла нет `✓ Реальная отправка` / стоит `Под HITL` / нет токенов в env |
| Фронт не обновился после деплоя | index.html no-cache, но браузер закешировал — hard refresh (Ctrl+F5) |
| Двойной запуск триггера | планировщик in-process без leader-election — `ABOP_SCHEDULER=0` на лишних репликах |

---

## 9. Что дальше (см. `CONCEPT_SCALING_OBSERVABILITY.md`)

Async-исполнение (`202`+Notification) и чат-оркестратор — **на холде** (точка входа простого пользователя = внешний менеджерский интерфейс, смычка позже). Observability Фаза 0 — сделана. Следующий инфра-шаг — по готовности внешнего UI.
