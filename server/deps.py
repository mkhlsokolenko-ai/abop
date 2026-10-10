# -*- coding: utf-8 -*-
"""Общие зависимости HTTP-слоя: кто пользователь и что ему можно.

Вынесено из `web_api.py` при расщеплении монолита (п.23 исследования 08.10). Модули маршрутов
обязаны брать аутентификацию и гейты ОТСЮДА, а не из `web_api`: иначе первый же вынесенный роутер
импортировал бы приложение целиком, и расщепление свелось бы к перестановке строк между файлами.

Здесь намеренно нет ни одного маршрута и ни одного стора — только разбор токена, уровни доступа и
ABAC по отделу. Это та часть, которую переиспользуют все: HTTP-ручки, MCP-сервер и фоновые задачи.
"""
from __future__ import annotations

import os
import time

import jwt as _pyjwt
from fastapi import HTTPException, Request

from . import tenancy
from .config import settings  # noqa: F401  (часть помощников читает настройки)

# ── Auth: Keycloak JWT. Dev-режим без JWKS. Multi-issuer: смычка с desktop (realm ai-product-engineer)
# требует принимать JWT ДВУХ realm — свой `abop` (demo-логин ABOP) и desktop-realm — оба ВЕРИФИЦИРУЯ
# по своему JWKS. ABOP_EXTRA_JWKS = "<issuer>|<jwks_url>,..." добавляет доп. доверенные issuer.
# См. docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 0, auth-фундамент).
def _jwks_url() -> str:
    return os.getenv("KEYCLOAK_JWKS_URI") or os.getenv("KEYCLOAK_JWKS_INTERNAL") or ""


def _extra_issuers() -> dict:
    """{issuer: jwks_url} доп. доверенных realm (напр. desktop ai-product-engineer)."""
    out = {}
    for pair in (os.getenv("ABOP_EXTRA_JWKS", "") or "").split(","):
        pair = pair.strip()
        if "|" in pair:
            iss, jwks = pair.split("|", 1)
            out[iss.strip()] = jwks.strip()
    return out


_jwk_clients: dict = {}   # jwks_url -> PyJWKClient (кэш по URL, чтобы не плодить клиентов)


def _client(jwks_url: str | None = None):
    from jwt import PyJWKClient
    url = jwks_url or _jwks_url()
    cli = _jwk_clients.get(url)
    if cli is None:
        cli = PyJWKClient(url)
        _jwk_clients[url] = cli
    return cli


# ── Кэш верифицированных JWT: verify (RS256) один раз, дальше запросы того же токена — из кэша ──
# Ключ = хэш токена (не храним сырой токен и не привязываем к юзеру → чужим токеном не прокатиться).
# TTL = min(остаток жизни токена, потолок) — НИКОГДА не отдаём после exp. Первый запрос токена всё
# равно полностью верифицирует подпись; в кэш попадает только успех. Потолок мал → окно на ротацию/
# отзыв ключей крошечное (revocation-list нет → паритет с текущим «принимаем до exp»). Per-worker.
import hashlib as _hashlib
import time as _t
_JWT_CACHE: dict = {}                                   # tokhash -> (identity, deadline_ts)
_JWT_CACHE_TTL = max(5, int(os.getenv("ABOP_JWT_CACHE_TTL", "60")))
_JWT_CACHE_MAX = max(256, int(os.getenv("ABOP_JWT_CACHE_MAX", "5000")))


def _jwt_cache_get(token: str):
    h = _hashlib.sha256(token.encode()).hexdigest()
    hit = _JWT_CACHE.get(h)
    if hit and _t.time() < hit[1]:                      # не отдаём после дедлайна (учитывает exp)
        return hit[0]
    if hit:
        _JWT_CACHE.pop(h, None)                         # протухло — выкидываем
    return None


def _jwt_cache_put(token: str, identity: dict, exp) -> None:
    if len(_JWT_CACHE) >= _JWT_CACHE_MAX:               # bounded: не растём бесконечно
        _JWT_CACHE.clear()
    deadline = _t.time() + _JWT_CACHE_TTL
    try:
        if exp:                                         # не пережить exp токена
            deadline = min(deadline, float(exp))
    except (TypeError, ValueError):
        pass
    _JWT_CACHE[_hashlib.sha256(token.encode()).hexdigest()] = (identity, deadline)


_LEVELS = ["analyst", "manager", "support", "admin"]  # по возрастанию прав (support/admin — сквозной доступ)


def _role_level(roles: list) -> str:
    """Уровень доступа (RBAC) из realm-ролей. admin>support>manager>analyst."""
    for lv in ("admin", "support", "manager", "analyst"):
        if lv in roles:
            return lv
    return "analyst"


def _identity(claims: dict, dev: bool = False) -> dict:
    roles = (claims.get("realm_access") or {}).get("roles") or []
    level = _role_level(roles)
    dept = claims.get("department")
    if isinstance(dept, list):
        dept = dept[0] if dept else None
    # admin/support видят всё (ABAC-область = *), даже если department не задан
    if level in ("admin", "support"):
        dept = "*"
    return {"sub": claims.get("sub", "dev"), "name": claims.get("preferred_username") or claims.get("name") or "dev",
            "roles": roles, "level": level, "department": dept or "*", "dev": dev,
            # Арендатор — из токена, а не выбор пользователя: работодатель у человека один.
            # Фильтруют по нему СТОРЫ (server/tenancy.py), поэтому ручка не может прочитать чужое,
            # даже если её автор о тенантах не думал.
            "tenant": tenancy.из_заявки(claims)}


def _применить_арендатора(request: Request, ident: dict) -> dict:
    """Поставить арендатора на контекст запроса — и пустить вендорский переход только явно.

    Ставится ОДИН раз и здесь: дальше по нему фильтруют сторы. Заголовок `X-ABOP-Tenant` работает
    только для вендора (admin/support) и пишется в журнал: «посмотреть у клиента» не должно быть
    тихой операцией.
    """
    просят = (request.headers.get("X-ABOP-Tenant") or "").strip() if request else ""
    действует = tenancy.для_пользователя(ident, просят or None)
    tenancy.установить(действует)
    if просят and действует != tenancy.нормализовать(ident.get("tenant")):
        try:    # журнал — не критичный путь: отказ записи не должен ронять запрос
            from . import observability as _obs
            _obs.log_event("warning", "tenant.switch", who=ident.get("name"),
                           level=ident.get("level"), from_tenant=ident.get("tenant"),
                           to_tenant=действует)
        except Exception:  # noqa: BLE001
            pass
    return {**ident, "tenant_active": действует}


def user(request: Request) -> dict:
    """Текущий пользователь из Bearer-JWT (roles + department → level + ABAC-область + арендатор).
    Bearer есть → декодируем (JWKS-верификация если настроена, иначе decode для демо-переключателя).
    Bearer нет: без JWKS — dev-admin; с JWKS — 401."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        import jwt
        cached = _jwt_cache_get(token) if _jwks_url() else None   # verify один раз на токен
        if cached is not None:
            return _применить_арендатора(request, cached)
        extra = _extra_issuers()
        try:
            if _jwks_url() or extra:  # строгая верификация подписи по JWKS (свой realm + доп. issuer)
                unv = jwt.decode(token, options={"verify_signature": False})  # issuer до верификации
                iss = unv.get("iss") or ""
                if iss in extra:                       # доп. доверенный realm (desktop) → его JWKS
                    jwks_url, exp_iss = extra[iss], iss
                else:                                   # свой realm ABOP
                    jwks_url, exp_iss = _jwks_url(), (os.getenv("KEYCLOAK_ISSUER") or None)
                if not jwks_url:
                    raise HTTPException(401, "issuer не доверен")
                key = _client(jwks_url).get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=["RS256"],
                                    audience=os.getenv("KEYCLOAK_AUDIENCE") or None,
                                    issuer=exp_iss,
                                    options={"verify_aud": bool(os.getenv("KEYCLOAK_AUDIENCE"))})
            else:  # демо/переключатель: decode без проверки подписи (токен от нашего Keycloak)
                claims = jwt.decode(token, options={"verify_signature": False})
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(401, "невалидный токен")
        ident = _identity(claims, dev=False)
        if _jwks_url() or extra:                          # кэшируем только верифицированный успех
            _jwt_cache_put(token, ident, claims.get("exp"))
        return _применить_арендатора(request, ident)
    if _jwks_url() or _extra_issuers():
        raise HTTPException(401, "нужен Bearer-JWT")
    # red-team #3: без Keycloak аноним = admin — только при явном ABOP_DEV_AUTH=1 (локальная разработка/CI);
    # иначе fail-closed, чтобы забытый env на стенде не открывал API всем.
    if os.getenv("ABOP_DEV_AUTH", "") != "1":
        raise HTTPException(401, "аутентификация не настроена: задайте KEYCLOAK_JWKS_URI (или ABOP_DEV_AUTH=1 для локальной разработки)")
    return _применить_арендатора(request, _identity(
        {"sub": "dev", "preferred_username": "dev (admin)",
         "realm_access": {"roles": ["admin"]}, "department": "*"}, dev=True))


def can_see_family(u: dict, family: str) -> bool:
    """ABAC: admin/support (область *) видят все семьи; иначе только свою (department == family)."""
    return u.get("department") in ("*", None) or u.get("department") == family


def require_level(u: dict, minimum: str) -> None:
    """RBAC-гейт мутаций: уровень пользователя ≥ minimum (иначе 403)."""
    if _LEVELS.index(u.get("level", "analyst")) < _LEVELS.index(minimum):
        raise HTTPException(403, f"нужен уровень доступа ≥ {minimum} (у вас {u.get('level')})")


def _sub(u: dict) -> str:
    """Устойчивый идентификатор человека: имя меняется, личная доска переживает переименование."""
    return str((u or {}).get("sub") or (u or {}).get("name") or "dev")

def _uid_of(u: dict) -> str:
    """Мастер-UID пользователя для сквозного ID: preferred_username/name/sub из JWT.

    Отличается от `_sub`: тот отвечает «кто это устойчиво внутри ABOP», а этот — «под каким именем
    его знают внешние системы» (почта, трекер, CRM). Живёт в шве, потому что нужен и домену
    сквозного ID, и прогонам, и доске.
    """
    return str(u.get("name") or u.get("preferred_username") or u.get("sub") or "").strip()
