---
name: docker-patterns
description: Паттерны Docker и Docker Compose для локальной разработки, безопасности контейнеров, сети, стратегий volume и оркестрации нескольких сервисов.
origin: ECC
---

# Docker-паттерны

Ты применяешь лучшие практики Docker и Docker Compose для контейнеризованной разработки: раскладка Compose, multi-stage сборки, сеть, volume, безопасность контейнеров и отладка. Цель — воспроизводимые сборки, минимальный и non-root образ, без секретов внутри слоёв.

## Когда применять

- Настраиваешь Docker Compose для локальной разработки.
- Проектируешь архитектуру из нескольких контейнеров.
- Разбираешь проблемы сети или volume контейнеров.
- Ревьюишь Dockerfile на безопасность и размер.
- Переходишь от локальной разработки к контейнеризованному рабочему потоку.

## Метод

### Docker Compose для локальной разработки

#### Стандартный стек веб-приложения

```yaml
# docker-compose.yml
services:
  app:
    build:
      context: .
      target: dev                     # Использует стадию dev из multi-stage Dockerfile
    ports:
      - "3000:3000"
    volumes:
      - .:/app                        # Bind mount для hot reload
      - /app/node_modules             # Анонимный volume — сохраняет зависимости контейнера
    environment:
      - DATABASE_URL=postgres://postgres:postgres@db:5432/app_dev
      - REDIS_URL=redis://redis:6379/0
      - NODE_ENV=development
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
    command: npm run dev

  db:
    image: postgres:16-alpine
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: app_dev
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./scripts/init-db.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redisdata:/data

  mailpit:                            # Локальное тестирование email
    image: axllent/mailpit
    ports:
      - "8025:8025"                   # Web UI
      - "1025:1025"                   # SMTP

volumes:
  pgdata:
  redisdata:
```

#### Dockerfile для разработки vs продакшена

```dockerfile
# Стадия: зависимости
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# Стадия: dev (hot reload, инструменты отладки)
FROM node:22-alpine AS dev
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]

# Стадия: build
FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build && npm prune --production

# Стадия: production (минимальный образ)
FROM node:22-alpine AS production
WORKDIR /app
RUN addgroup -g 1001 -S appgroup && adduser -S appuser -u 1001
USER appuser
COPY --from=build --chown=appuser:appgroup /app/dist ./dist
COPY --from=build --chown=appuser:appgroup /app/node_modules ./node_modules
COPY --from=build --chown=appuser:appgroup /app/package.json ./
ENV NODE_ENV=production
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://localhost:3000/health || exit 1
CMD ["node", "dist/server.js"]
```

#### Override-файлы

```yaml
# docker-compose.override.yml (загружается автоматически, только dev-настройки)
services:
  app:
    environment:
      - DEBUG=app:*
      - LOG_LEVEL=debug
    ports:
      - "9229:9229"                   # Node.js debugger

# docker-compose.prod.yml (для продакшена — явно)
services:
  app:
    build:
      target: production
    restart: always
    deploy:
      resources:
        limits:
          cpus: "1.0"
          memory: 512M
```

```bash
# Разработка (override загружается автоматически)
docker compose up

# Продакшен
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### Сеть (Networking)

#### Обнаружение сервисов

Сервисы в одной Compose-сети резолвятся по имени сервиса:
```
# Из контейнера "app":
postgres://postgres:postgres@db:5432/app_dev    # "db" резолвится в контейнер db
redis://redis:6379/0                             # "redis" резолвится в контейнер redis
```

#### Собственные сети

```yaml
services:
  frontend:
    networks:
      - frontend-net

  api:
    networks:
      - frontend-net
      - backend-net

  db:
    networks:
      - backend-net              # Доступен только из api, не из frontend

networks:
  frontend-net:
  backend-net:
```

#### Открывать только необходимое

```yaml
services:
  db:
    ports:
      - "127.0.0.1:5432:5432"   # Доступен только с host, не из сети
    # В продакшене убери порты полностью — доступ только внутри Docker-сети
```

### Стратегии volume

```yaml
volumes:
  # Именованный volume: переживает перезапуск контейнера, управляется Docker
  pgdata:

  # Bind mount: маппит директорию host в контейнер (для разработки)
  # - ./src:/app/src

  # Анонимный volume: сохраняет созданный контейнером контент от override bind mount
  # - /app/node_modules
```

#### Частые паттерны

```yaml
services:
  app:
    volumes:
      - .:/app                   # Исходный код (bind mount для hot reload)
      - /app/node_modules        # Защитить node_modules контейнера от host
      - /app/.next               # Сохранить build cache

  db:
    volumes:
      - pgdata:/var/lib/postgresql/data          # Постоянные данные
      - ./scripts/init.sql:/docker-entrypoint-initdb.d/init.sql  # Init-скрипты
```

### Безопасность контейнеров

#### Ужесточение Dockerfile

```dockerfile
# 1. Используйте конкретные теги (:latest — никогда)
FROM node:22.12-alpine3.20

# 2. Запуск от non-root пользователя
RUN addgroup -g 1001 -S app && adduser -S app -u 1001
USER app

# 3. Сбросить capabilities (в compose)
# 4. По возможности — read-only корневая файловая система
# 5. Никаких секретов в слоях образа
```

#### Безопасность Compose

```yaml
services:
  app:
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp
      - /app/.cache
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE          # Только для bind на порты < 1024
```

#### Управление секретами

```yaml
# ХОРОШО: используйте переменные окружения (инжектятся в runtime)
services:
  app:
    env_file:
      - .env                     # .env никогда не коммитить в git
    environment:
      - API_KEY                  # Наследуется из окружения host

# ХОРОШО: Docker secrets (режим Swarm)
secrets:
  db_password:
    file: ./secrets/db_password.txt

services:
  db:
    secrets:
      - db_password

# ПЛОХО: хардкод в образе
# ENV API_KEY=sk-proj-xxxxx      # НИКОГДА ТАК НЕ ДЕЛАЙТЕ
```

### .dockerignore

```
node_modules
.git
.env
.env.*
dist
coverage
*.log
.next
.cache
docker-compose*.yml
Dockerfile*
README.md
tests/
```

### Отладка

#### Частые команды

```bash
# Просмотр логов
docker compose logs -f app           # Следить за логами app
docker compose logs --tail=50 db     # Последние 50 строк из db

# Выполнить команду в работающем контейнере
docker compose exec app sh           # Войти в app через shell
docker compose exec db psql -U postgres  # Подключиться к postgres

# Инспекция
docker compose ps                     # Работающие сервисы
docker compose top                    # Процессы в каждом контейнере
docker stats                          # Использование ресурсов

# Пересборка
docker compose up --build             # Пересобрать образы
docker compose build --no-cache app   # Форсировать полную пересборку

# Очистка
docker compose down                   # Остановить и удалить контейнеры
docker compose down -v                # Удалить и volume (РАЗРУШИТЕЛЬНО)
docker system prune                   # Удалить неиспользуемые образы/контейнеры
```

#### Отладка проблем сети

```bash
# Проверить разрешение DNS внутри контейнера
docker compose exec app nslookup db

# Проверить связность
docker compose exec app wget -qO- http://api:3000/health

# Инспекция сети
docker network ls
docker network inspect <project>_default
```

## Definition of Done
- [ ] Образ multi-stage: финальная стадия минимальна, dev-инструменты/исходники в неё не попадают.
- [ ] Контейнер запускается от **non-root** пользователя (`USER`), а не от root.
- [ ] Теги образов зафиксированы (конкретная версия), `:latest` не используется.
- [ ] Capabilities сброшены (`cap_drop: [ALL]`), добавлены только необходимые; `no-new-privileges:true`.
- [ ] По возможности `read_only: true` + `tmpfs` для записываемых путей.
- [ ] Секретов нет в слоях образа: только переменные окружения / Docker secrets; `.env` в `.dockerignore` и не в git.
- [ ] Постоянные данные — на именованных volume; порты наружу открыты только необходимые (в проде — минимум).
- [ ] Есть `HEALTHCHECK`; зависимости через `depends_on` с `condition`.

## Анти-паттерны
- Docker Compose в продакшене без оркестрации — для прод-нагрузок бери Kubernetes, ECS или Docker Swarm.
- Хранение данных в контейнере без volume — контейнеры эфемерны, данные теряются при перезапуске.
- Запуск от root вместо выделенного non-root пользователя.
- Тег `:latest` вместо фиксированной версии — сборка невоспроизводима.
- Один «dev-контейнер» со всеми сервисами — разделяй ответственность: один процесс на контейнер.
- Секреты в `docker-compose.yml` вместо `.env` (gitignore) или Docker secrets.

## Безопасность
- `mode=read`, `egress=internal`, `cite=False`.
- Навык проектирует/ревьюит контейнерную конфигурацию внутри периметра, наружу сам не ходит.
- **Non-root:** каждый образ запускается от выделенного непривилегированного пользователя.
- **Минимум capabilities:** `cap_drop: [ALL]` + только необходимые `cap_add`; `no-new-privileges`; read-only FS где возможно.
- **Секреты не в образ:** ключи и пароли не попадают в слои/`ENV` Dockerfile — только runtime-инъекция через env/Docker secrets; `.env` в `.dockerignore`.

## Интеграция
- `data_query`: на входе сверься с существующей инфраструктурой/контрактами сервисов (порты, сети, имена), чтобы Compose был консистентен.
- `remember(criticality)`: пометь критичность контейнеров с egress-доступом или монтированием чувствительных данных — вход для гейта конверта.
- `handoff`: границы сервисов получай из `c4-diagram`; секретами и их хранилищем — согласуйся с ИБ-навыком; код приложения — из `fastapi-patterns`.
