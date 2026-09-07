# Keycloak — realm `ai-product-engineer`

Аутентификация курса. Поднимается на server-1 (201.51.5.24) за Caddy, домен
**auth.engineer-ai.pro**. Realm импортируется из `realm-export.json` при старте.

## Что в realm
- Роли: **student** (по умолчанию), **lecturer** (админка, оценки, `cost_report`).
- Клиент **portal** — публичный (PKCE): вход в портал engineer-ai.pro и OpenCode CLI.
  В access-token добавляется `aud: course-mcp` (audience-mapper).
- Клиент **course-mcp** — bearer-only: аудитория, которую валидирует MCP по JWKS.

## Поднять (на server-1)
```bash
cd ~/ai-product-engineer
cp .env.example .env      # задай PG_PASSWORD, KC_ADMIN_PASSWORD (сильные!)
docker compose up -d postgres keycloak caddy
docker compose logs -f keycloak   # дождись "Imported realm ai-product-engineer"
```
Проверка: `https://auth.engineer-ai.pro/realms/ai-product-engineer/.well-known/openid-configuration`

## GitHub-вход (Identity Provider)
1. GitHub → Settings → Developer settings → **OAuth Apps** → New:
   - Homepage: `https://engineer-ai.pro`
   - Authorization callback URL (совпасть 1-в-1!):
     `https://auth.engineer-ai.pro/realms/ai-product-engineer/broker/github/endpoint`
2. Register → скопируй **Client ID**, Generate secret → скопируй **Client Secret**.

**Вариант A — через admin-консоль (удобно вживую на лекции):**
1. `https://auth.engineer-ai.pro/admin/` → войди `admin` → сверху выбери realm `ai-product-engineer`.
2. **Identity providers** → **Add provider** → **GitHub**.
3. Вставь **Client ID** / **Client Secret** → **Add**. Готово (роль `student` — по умолчанию).
4. Проверка: `.../realms/ai-product-engineer/account/` → Sign in → кнопка **GitHub**.

**Вариант B — через kcadm (creds не хранятся в git):**
3. Положи creds в `.env` (`KC_GITHUB_*`) и выполни:
```bash
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh config credentials \
  --server http://localhost:8080 --realm master \
  --user "$KC_ADMIN" --password "$KC_ADMIN_PASSWORD"
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh create identity-provider/instances \
  -r ai-product-engineer -s alias=github -s providerId=github -s enabled=true \
  -s "config.clientId=$KC_GITHUB_CLIENT_ID" -s "config.clientSecret=$KC_GITHUB_CLIENT_SECRET"
```

## Завести пользователя-лектора (себе)
```bash
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh create users \
  -r ai-product-engineer -s username=mikhail -s enabled=true \
  -s email=mkhlsokolenko@gmail.com
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh set-password \
  -r ai-product-engineer --username mikhail --new-password '<СИЛЬНЫЙ>'
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh add-roles \
  -r ai-product-engineer --uusername mikhail --rolename lecturer
```

## Связка с MCP
MCP уже читает JWKS этого realm (`server/auth.py`). В `.env` MCP:
```
KEYCLOAK_ISSUER=https://auth.engineer-ai.pro/realms/ai-product-engineer
KEYCLOAK_JWKS_URI=https://auth.engineer-ai.pro/realms/ai-product-engineer/protocol/openid-connect/certs
KEYCLOAK_AUDIENCE=course-mcp
```
