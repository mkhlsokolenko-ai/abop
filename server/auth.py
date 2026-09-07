"""Аутентификация студентов через Keycloak.

FastMCP валидирует Bearer-JWT по JWKS realm'а Keycloak (подпись, issuer, audience,
срок жизни). Внутри инструментов мы достаём claims токена, чтобы привязать каждый
LLM-вызов к конкретному студенту в cost-журнале.

Схема auth (см. docs/architecture.md):
    Студент → Keycloak: OIDC (JWT через GitHub login)
    Студент → FastMCP:  JWT в Authorization: Bearer <...>  (проверка по JWKS)
    FastMCP → RouteAI:  один курсовой API-key (спрятан на сервере)
"""
from __future__ import annotations

from dataclasses import dataclass

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

from .config import settings


def build_verifier() -> JWTVerifier:
    """Верификатор JWT под Keycloak-realm курса.

    JWKS тянется по jwks_uri и кешируется; ротация ключей Keycloak подхватывается
    автоматически. Токен без валидной подписи / с чужим issuer / просроченный —
    отвергается до вызова любого инструмента.
    """
    return JWTVerifier(
        jwks_uri=settings.kc_jwks_internal,  # внутренняя сеть; issuer проверяется по публичному
        issuer=settings.kc_issuer,
        audience=settings.kc_audience,
    )


@dataclass(frozen=True)
class Student:
    """Идентичность студента из проверенного JWT."""

    subject: str          # стабильный id (sub) — ключ в cost_journal
    username: str         # preferred_username (обычно github-логин)
    email: str | None

    @property
    def db_id(self) -> str:
        return self.subject


def current_student() -> Student:
    """Достаёт студента из уже провалидированного токена текущего запроса.

    Вызывается внутри инструмента. Если токена нет (что невозможно при включённом
    verifier), поднимает ошибку.
    """
    token = get_access_token()
    if token is None:
        raise PermissionError("Не аутентифицирован: отсутствует валидный JWT.")
    claims = token.claims
    return Student(
        subject=str(claims.get("sub", "unknown")),
        username=str(claims.get("preferred_username", claims.get("sub", "unknown"))),
        email=claims.get("email"),
    )
