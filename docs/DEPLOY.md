# Деплой на server-1 (sLAVA-пилот, 201.51.5.24)

MCP запускается **на том же сервере**, что sLAVA, и ходит к его сервисам по 127.0.0.1.
Наружу торчит только Caddy (443).

## Предпосылки
- Доступ по SSH-ключу (`slava@201.51.5.24`, см. память про пилот-сервер).
- На хосте уже живут: Qdrant (:6333), доступ к routerai.ru, Docker + Compose.
- Домен (A-запись → 201.51.5.24), например `mcp.<домен>` — для TLS Caddy.
- Keycloak с realm `ai-product-engineer` (можно поднять рядом или на отдельном хосте).

## Шаги

```bash
# 1) На сервере
ssh slava@201.51.5.24
git clone https://github.com/mkhlsokolenko-ai/ai-product-engineer.git
cd ai-product-engineer

# 2) Конфиг
cp .env.example .env
# заполни: ROUTEAI_API_KEY, KEYCLOAK_*, POSTGRES_DSN (пароль), домен в Caddyfile
nano .env
nano Caddyfile        # свой домен вместо mcp.example.ru

# 3) (опц.) self-hosted Qwen на арендованной RTX 6000
#    на GPU-хосте:
#    vllm serve Qwen/Qwen3.8-27B-AWQ --quantization awq --port 8001 \
#      --max-model-len 131072 --gpu-memory-utilization 0.92
#    затем в .env: LOCAL_LLM_BASE_URL=http://<gpu-host>:8001/v1

# 4) Подъём
docker compose up -d --build
docker compose logs -f mcp
```

## Проверка

```bash
# health/JWKS Keycloak доступен?
curl -s "$KEYCLOAK_JWKS_URI" | head -c 200

# MCP отвечает (изнутри сервера)
curl -s http://127.0.0.1:8787/  -H "Authorization: Bearer <тестовый JWT>"

# снаружи — через Caddy/TLS
curl -s https://mcp.<домен>/mcp -H "Authorization: Bearer <тестовый JWT>"
```

## Настройка OpenCode у студента

Клиент подключается к курсовому MCP по JWT. В конфиге OpenCode пропиши MCP-сервер:
`https://mcp.<домен>/mcp`, заголовок `Authorization: Bearer <JWT студента>`.
JWT студент получает через Keycloak (GitHub login). Прямого ключа провайдера у студента
нет — всё через шлюз.

## Обновление

```bash
cd ~/ai-product-engineer && git pull && docker compose up -d --build mcp
```

## Изоляция от sLAVA (важно)
- НЕ трогай коллекции Qdrant без префикса `ape_` — это корпус sLAVA.
- Postgres курса — отдельная БД `ape` (в compose свой контейнер), не лезь в БД sLAVA.
- При смене `.env` пересоздай контейнер: `docker compose up -d --force-recreate mcp`
  (иначе running-контейнер держит старое окружение — известная ловушка sLAVA).

## Безопасность
- `.env` в git не коммитится (`.gitignore`).
- Внутренние порты (8787, 5432, 6333) наружу не публикуй — только Caddy :443.
- В проде `cost_report` гейти по realm-role `lecturer` из JWT (см. `server/tools/admin.py`).
