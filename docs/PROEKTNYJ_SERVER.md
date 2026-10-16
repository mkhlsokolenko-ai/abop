# Проектный сервер: откуда агент берёт настоящий график

> Актуально на 16.10.2026. Сторожит `tests/test_openproject_adapter.py`.

## 1. Зачем он нужен

Агент «Управляющий графиком» строит справку о смещении сроков: план против факта, критический
путь, резервы, зависимости. Пока дорожная карта лежала файлом в `demo/plan/roadmap.json`, справка
была правдой про демо, а не про проект. Владелец сформулировал это прямо: «чтобы данные графиков
брались из реальных проектов, надо ставить на наш сервер аналог Microsoft Project».

На стенде поставлен **OpenProject 15** (`http://5.129.192.63:8096`, контейнер `openproject`,
данные в `/opt/openproject/pgdata`). Он занимает ~2 ГБ памяти; на server-1 под него добавлен своп.

## 2. Что знает код и что приходит данными

Адаптер `openproject` (`cli/ape.py`) знает **только формат ответа** OpenProject — HAL+JSON,
`_embedded.elements`, связи `relations` с типом `follows`. Он не знает ни одного поля заказчика.

| Что | Откуда берётся | Почему так |
|---|---|---|
| адрес, проект | рецепт, `source.base` / `source.проект` | у заказчика свой сервер и свой идентификатор |
| стадия, критичность, дата входа, возвратов | рецепт, `source.поля` — путь в ответе (`customField1.value`) | номера пользовательских полей у каждого заказчика свои; в коде их быть не должно |
| «кто кого ждёт» | связи `follows`: «2 follows 1» = «2 ждёт 1» | это ровно то, что движок смещения понимает под «ждёт» |
| сроки стадий, выходные, пороги, буфер | сущности `stage_rule` и `plan_rule` | правила — данные, см. [PROIZVODSTVENNYJ_PROCESS.md](PROIZVODSTVENNYJ_PROCESS.md) |

Два рецепта наполняют **одну** сущность `roadmap_item`: живой `plan_roadmap_live` и файловый
`plan_roadmap`. На стенде с проектным сервером гоняется только живой (с `reset`), файловый остаётся,
чтобы демо воспроизводилось без сервера. Это объяснено прямо в рецепте полем `заметка`.

## 3. Ключ API

Порядок поиска ключа в адаптере — по убыванию безопасности:

1. `source.токен_файл` — путь к файлу (на стенде `/app/secrets/openproject_token`, права 600).
   Ключа нет ни в данных, ни в `docker inspect`, ни в логах;
2. `source.токен_env` — имя переменной среды;
3. `source.токен` — значение прямо в рецепте: годится для локальной отладки, не для прода.

**Грабля, стоившая разбирательства.** Ключ, выписанный скриптом через
`User.where(admin: true).first`, достался **встроенному системному пользователю** OpenProject:
он `admin = true`, но членства в проектах у него нет, и API отвечает `403 MissingPermission`.
Ключ обязан принадлежать человеку, который видит проект (на стенде — учётная запись `admin`,
Мой аккаунт → Токены доступа → API). Поэтому адаптер различает отказы: `401` — «ключ не принят»,
`403` — «ключ принят, но у его владельца нет прав на проект».

## 4. Как перевести стенд на живой источник

Структура демо-проекта и ключ API заводятся двумя скриптами из репозитория — стенд
воспроизводится с нуля, а не «как-то руками»:

| Скрипт | Что делает |
|---|---|
| `demo/plan/openproject/seed.rb` | проект `prj-2451`, четыре пользовательских поля процесса, семь задач, связи `follows` |
| `demo/plan/openproject/issue_token.rb` | выписывает ключ API живому администратору и кладёт его в файл, не печатая значение |

```bash
# 1. структура проекта
docker cp demo/plan/openproject/seed.rb openproject:/tmp/seed.rb
docker exec openproject bash -lc 'cd /app && RAILS_ENV=production bundle exec rails runner /tmp/seed.rb'
# номера customField из вывода — в source.поля рецепта plan_roadmap_live

# 2. ключ: значение не выводится в терминал и не остаётся в /tmp
docker cp demo/plan/openproject/issue_token.rb openproject:/tmp/issue_token.rb
docker exec openproject bash -lc 'cd /app && RAILS_ENV=production bundle exec rails runner /tmp/issue_token.rb'
docker cp openproject:/tmp/op_token.txt /opt/abop/secrets/openproject_token
chmod 600 /opt/abop/secrets/openproject_token
docker exec openproject rm -f /tmp/op_token.txt /tmp/issue_token.rb
docker exec abop-webapi mkdir -p /app/secrets
docker cp /opt/abop/secrets/openproject_token abop-webapi:/app/secrets/openproject_token

# 3. ядро и рецепты в контейнер
docker cp cli/ape.py abop-webapi:/app/cli/ape.py
docker cp demo/plan/recipes.json abop-webapi:/app/demo/plan/recipes.json

# 4. живая карта вместо файловой и пересчёт смещения:
#    plan_roadmap_live (reset) → roadmap_item, затем plan_shift (reset) → plan_line
```

Проверка, что источник действительно живой: в `roadmap_item` поле `задача` ведёт на
`…/work_packages/<id>` проектного сервера, а `зависит_от` заполнено из связей `follows`, а не из
файла демо (у файлового источника `задача` выглядит как `PLAN-1`).
