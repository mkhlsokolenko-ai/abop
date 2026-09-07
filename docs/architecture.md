# Архитектура курсового контура

## Схема (server-1 = sLAVA-пилот, 201.51.5.24)

```
Студент (OpenCode CLI / агент)
   │  1) OIDC login (GitHub) ──────────────► Keycloak (realm ai-product-engineer)
   │  ◄──────────────── JWT ────────────────┘
   │
   │  2) MCP tools, Authorization: Bearer <JWT>
   ▼
Caddy (TLS, :443)  ──►  FastMCP (:8787, JWT проверяется по JWKS Keycloak)
                              │
        ┌─────────────────────┼──────────────────────────────┐
        ▼                     ▼                                ▼
   RouteAI (routerai.ru)   Qdrant (:6333, свои коллекции)   Postgres (cost_journal
   LLM + embeddings        BGE-M3 embed / BGE-rerank          + квоты 5M/5/25M)
        │
        ├─ profile=code     ──► local/qwen3.8-27b (vLLM, RTX 6000) → fallback DeepSeek
        ├─ profile=research ──► DeepSeek V4-Pro → V4-Flash
        └─ profile=standard ──► Qwen-Plus → DeepSeek
```

## Auth-схемы (неделя 8 курса)

- Студент → Keycloak: **OIDC** (JWT через GitHub login)
- Студент → FastMCP: **JWT** в `Authorization` (валидируется по JWKS Keycloak)
- FastMCP → RouteAI: **API-key** (один курсовой ключ, спрятан на сервере)
- FastMCP → local vLLM / Qdrant / Postgres: по 127.0.0.1 на server-1

Три свойства, которые это даёт (из v0.5):
1. **Контроль расходов** — у студента нет ключа провайдера, слить бюджет нельзя.
2. **Учёт по пользователям** — каждый вызов в cost_journal с привязкой к `sub`.
3. **Cascade fallback** — упала модель → идёт следующая в каскаде профиля.

## Компоненты

| Компонент | Где | Назначение |
|---|---|---|
| FastMCP (`server/`) | server-1 :8787 | шлюз, инструменты, квоты, cost-лог |
| Keycloak | твой хост | OIDC/JWT, realm студентов |
| Caddy | server-1 :443 | TLS-фронт |
| Postgres | server-1 :5432 | cost_journal + квоты |
| Qdrant | server-1 :6333 (sLAVA) | vector search, коллекции `ape_*` |
| RouteAI | routerai.ru | LLM + embeddings |
| vLLM | RTX 6000 :8001 | self-hosted Qwen3.8-27B (код) |

## Инструменты MCP

| Tool | Кто | Что |
|---|---|---|
| `chat` | студент | LLM через профиль, с квотой и cost-логом |
| `my_usage` | студент | личный расход: неделя/сессии/текущая сессия |
| `rag_index` | студент | индексация чанков в свою коллекцию Qdrant |
| `rag_search` | студент | vector search + BGE-rerank по своей коллекции |
| `cost_report` | лектор | сводка по группе (командный центр) |

## Изоляция от sLAVA

MCP переиспользует живые сервисы sLAVA (Qdrant, RouteAI, при желании reranker), но:
- коллекции Qdrant — с префиксом `ape_` + хеш студента (корпус sLAVA не трогаем);
- Postgres — отдельная БД `ape`;
- том Qdrant общий по хосту, индексы курса живут рядом, но в своих коллекциях.

## Two-contour (из v0.5)

- **Студенческий контур** — этот, на server-1, RouteAI + self-host Qwen.
- **Bootcamp/корпоративный** — тот же код, но LLM через их прокси/inference, Keycloak
  federation с их IdP, реальные данные под NDA → **self-host обязателен** (данные не
  уходят наружу). Один git-репо, разные `.env` / compose.
