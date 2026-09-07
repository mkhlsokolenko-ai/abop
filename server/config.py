"""Конфигурация из окружения (.env). Единственный источник правды для эндпоинтов."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _split(csv: str) -> list[str]:
    return [x.strip() for x in csv.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # транспорт
    mcp_host: str = os.getenv("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(os.getenv("MCP_PORT", "8787"))

    # Keycloak
    kc_issuer: str = os.getenv("KEYCLOAK_ISSUER", "")
    kc_jwks_uri: str = os.getenv("KEYCLOAK_JWKS_URI", "")
    # JWKS тянем по ВНУТРЕННЕЙ сети (keycloak:8080), а не по публичному домену:
    # контейнер не может достучаться до собственного публичного IP хоста (hairpin).
    # issuer при этом проверяется по публичному kc_issuer.
    kc_jwks_internal: str = os.getenv(
        "KEYCLOAK_JWKS_INTERNAL",
        "http://keycloak:8080/realms/ai-product-engineer/protocol/openid-connect/certs",
    )
    kc_audience: str = os.getenv("KEYCLOAK_AUDIENCE", "course-mcp")

    # RouteAI
    routeai_base_url: str = os.getenv("ROUTEAI_BASE_URL", "https://routerai.ru/api/v1")
    routeai_api_key: str = os.getenv("ROUTEAI_API_KEY", "")
    # Каскады по профилю (все на RouteAI): код -> Qwen3.8-27B, агенты/standard ->
    # выбранная гейтом qwen3-30b-a3b-instruct-2507, исследования -> DeepSeek.
    code_cascade: list[str] = field(
        default_factory=lambda: _split(
            os.getenv("ROUTEAI_CODE_CASCADE", "qwen/qwen3.8-27b,deepseek/deepseek-v4-pro")
        )
    )
    research_cascade: list[str] = field(
        default_factory=lambda: _split(
            os.getenv("ROUTEAI_RESEARCH_CASCADE", "deepseek/deepseek-v4-pro,deepseek/deepseek-v4-flash")
        )
    )
    # standard = агентский движок: победитель гейта (bench/RESULTS.md) первым, DeepSeek запасным.
    standard_cascade: list[str] = field(
        default_factory=lambda: _split(
            os.getenv("ROUTEAI_STANDARD_CASCADE",
                      "qwen/qwen3-30b-a3b-instruct-2507,deepseek/deepseek-v4-flash")
        )
    )
    embed_model: str = os.getenv("ROUTEAI_EMBED_MODEL", "bge-m3")

    def cascade_for(self, profile: str) -> list[str]:
        """Каскад моделей под профиль задачи. code->Qwen, research->DeepSeek."""
        return {
            "code": self.code_cascade,
            "research": self.research_cascade,
            "standard": self.standard_cascade,
        }.get(profile, self.standard_cascade)

    # Локальный self-hosted LLM (vLLM, OpenAI-совместимый). Пусто = выключено.
    local_llm_base_url: str = os.getenv("LOCAL_LLM_BASE_URL", "")
    local_llm_model: str = os.getenv("LOCAL_LLM_MODEL", "")
    local_llm_api_key: str = os.getenv("LOCAL_LLM_API_KEY", "EMPTY")

    # Qdrant
    qdrant_url: str = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    collection_prefix: str = os.getenv("QDRANT_COLLECTION_PREFIX", "ape_")
    embed_dim: int = int(os.getenv("COURSE_EMBED_DIM", "1024"))

    # Reranker
    reranker_backend: str = os.getenv("RERANKER_BACKEND", "routerai")  # routerai | slava
    reranker_model: str = os.getenv("RERANKER_MODEL", "bge-reranker-v2-m3")
    slava_api_base_url: str = os.getenv("SLAVA_API_BASE_URL", "http://127.0.0.1:8000")

    # Postgres
    pg_dsn: str = os.getenv("POSTGRES_DSN", "")

    # квоты: 5M/сессия × 5 сессий = 25M токенов/неделю
    per_session_token_limit: int = int(os.getenv("PER_SESSION_TOKEN_LIMIT", "5000000"))
    sessions_per_week: int = int(os.getenv("SESSIONS_PER_WEEK", "5"))
    weekly_token_limit: int = int(os.getenv("WEEKLY_TOKEN_LIMIT", "25000000"))

    # MinIO (хранилище проектов). internal — для серверных операций, public —
    # хост для presigned-URL, который открывает браузер студента.
    minio_internal: str = os.getenv("MINIO_INTERNAL", "minio:9000")
    minio_public: str = os.getenv("MINIO_PUBLIC", "s3.engineer-ai.pro")
    minio_user: str = os.getenv("MINIO_ROOT_USER", "ape")
    minio_password: str = os.getenv("MINIO_ROOT_PASSWORD", "")
    minio_bucket: str = os.getenv("MINIO_BUCKET", "projects")
    storage_limit_bytes: int = int(os.getenv("STORAGE_LIMIT_BYTES", str(600 * 1024 * 1024)))

    # Курс: дата старта (для расчёта текущей недели и дедлайнов) и длительность.
    course_start_date: str = os.getenv("COURSE_START_DATE", "2026-09-01")
    course_weeks: int = int(os.getenv("COURSE_WEEKS", "15"))
    langfuse_url: str = os.getenv("LANGFUSE_URL", "")

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def collection(self, name: str) -> str:
        """Имя коллекции Qdrant с курсовым префиксом (изоляция от корпуса sLAVA)."""
        return f"{self.collection_prefix}{name}"


settings = Settings()
