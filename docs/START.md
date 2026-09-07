# Как подключиться к моделям курса — за 3 минуты

Тебе не нужны ключи от LLM и ручное копирование токенов. Всё идёт через один
CLI курса — **`ape`**. Вход по GitHub, лимит один: 25M токенов в неделю.

Нужно: **Python 3.9+** и **git** (проверь: `python --version`, `git --version`).

---

## Шаг 1. Установи CLI

```bash
pip install "git+https://github.com/mkhlsokolenko-ai/ai-product-engineer#subdirectory=cli"
```

**Запускай через `python -m ape`** — так работает всегда, на Windows тоже:
```bash
python -m ape --help
```

> Почему `python -m ape`, а не просто `ape`? На Windows pip кладёт `ape.exe` в папку
> `Scripts`, которой обычно нет в PATH → `ape` не распознаётся. `python -m ape`
> обходит это и работает везде. (На macOS/Linux короткая `ape …` тоже сработает.)
> Если `python` не находится — попробуй `py -m ape` (Windows) или `python3 -m ape`.

**Нет git?** Скачай одним файлом (зависимостей нет):
```bash
curl -O https://s3.engineer-ai.pro/materials/ape.py
python ape.py login          # дальше вместо "ape" пиши "python ape.py"
```

---

## Шаг 2. Войди через GitHub

```bash
python -m ape login
```

Откроется браузер → **Continue with GitHub** → подтверди доступ. Вкладка скажет
«вход выполнен», можно закрыть. Токен сохранится в `~/.ape/` и будет обновляться сам.

Проверка входа:
```bash
python -m ape whoami
```
Должно показать твой логин и роль `student`.

---

## Шаг 3. Первый запрос

```bash
python -m ape code "напиши функцию проверки строки на палиндром + тест pytest"
```

Пойдёт на self-host **Qwen3.8-27B** — это бесплатно из твоей квоты.

Ресёрч и рассуждения — на **DeepSeek**:
```bash
python -m ape ask "когда RAG лучше дообучения модели? коротко"
```

---

## Команды

| Команда | Что делает |
|---|---|
| `python -m ape login` | вход через GitHub (токен сохраняется и обновляется сам) |
| `python -m ape code "…"` | код → Qwen3.8-27B (быстро, бесплатно из квоты, чистый вывод) |
| `python -m ape ask "…"` | ресёрч/рассуждения → DeepSeek |
| `python -m ape rag index ./docs/*.md` | загрузить документы в свою RAG-коллекцию |
| `python -m ape rag search "…"` | поиск по своей коллекции |
| `python -m ape usage` | сколько токенов осталось за неделю |
| `python -m ape whoami` / `... logout` | кто я / выйти |

Приложить файл к запросу: `python -m ape code "добавь обработку ошибок" -f app.py`
Через pipe: `git diff | python -m ape code "напиши сообщение коммита" --stdin`

---

## Если что-то не работает

- **`ape` не распознаётся / command not found** (частое на Windows) — pip положил `ape.exe`
  в папку `Scripts` вне PATH. Просто пиши `python -m ape …` (или `py -m ape …`). Это норма.
- **`python -m ape login` не открыл браузер** — скопируй ссылку, которую CLI напечатал, и открой вручную.
- **`Сессия истекла` / 401** — просто выполни `ape login` заново.
- **`SSL: UNEXPECTED_EOF` / «TLS-соединение сброшено»** — соединение рвётся не на нашей
  стороне (сервер проверен), а на твоей: провайдер/DPI, антивирус с проверкой HTTPS или
  корпоративная сеть. Что делать: **повтори команду** (часто пробивается со 2–3 раза — CLI
  уже ретраит сам), временно **выключи HTTPS-сканирование антивируса или включи/выключи VPN**,
  или зайди с **мобильного интернета** (точка доступа). Свежая версия CLI показывает эту
  подсказку вместо длинного трейсбека — обнови: `pip install -U "git+https://github.com/mkhlsokolenko-ai/ai-product-engineer#subdirectory=cli"`.
- **`quota_exceeded`** — израсходована недельная квота (25M токенов). Сбросится в понедельник;
  `ape usage` покажет остаток.
- **`pip install engineer-ai-cli` даёт ошибку** — такого пакета нет на PyPI. Ставь командой из
  Шага 1 (через `git+https://…`) или качай один файл `ape.py`.

---

## Продвинутый режим (позже, к блоку про агентов)

Тот же аккаунт можно подключить как **MCP-сервер** к агенту (Claude Code / Cursor / OpenCode):

- Endpoint: `https://mcp.engineer-ai.pro/mcp`
- Заголовок: `Authorization: Bearer <твой токен>` — токен возьми командой `ape whoami`
  показывает, что ты вошёл; сам токен лежит в `~/.ape/config.json` (поле `access_token`).
- Инструменты сервера: `chat`, `rag_index`, `rag_search`, `my_usage`.

Токен живёт 1 час — если агент начал получать 401, обнови токен (`ape login`) и вставь заново.
Для старта проще `ape`: он обновляет токен сам.
