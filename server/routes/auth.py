# -*- coding: utf-8 -*-
"""Вход: обмен логина и пароля на токен и его обновление.

Своей базы паролей у ABOP нет — проверяет их каталог пользователей (Keycloak). Эти две
ручки нужны веб-клиенту и десктопу, чтобы не встраивать в них OIDC-поток целиком.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from ..deps import _identity

router = APIRouter()


@router.post("/api/auth/login")
async def auth_login(body: dict) -> dict:
    """Прокси-логин к Keycloak (realm abop) — Keycloak не публичный, поэтому вход идёт через ABOP.
    Тело: {username, password}. Возвращает {access_token, refresh_token, expires_in, user}: access у Keycloak живёт
    ~5 минут, поэтому фронт обязан обновлять сессию через /api/auth/refresh, иначе всё отваливается по 401
    и выглядит как «сервер недоступен»."""
    import httpx
    import jwt
    un = str((body or {}).get("username", "")).strip()
    pw = str((body or {}).get("password", ""))
    if not un or not pw:
        raise HTTPException(422, "нужны username и password")
    iss = os.getenv("KEYCLOAK_ISSUER") or "http://localhost:8811/realms/abop"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(iss + "/protocol/openid-connect/token",
                             data={"client_id": "abop-web", "username": un, "password": pw,
                                   "grant_type": "password"})
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"Keycloak недоступен: {ex}")
    if r.status_code != 200:
        raise HTTPException(401, "неверный логин или пароль")
    tokens = r.json()
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    return {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token"),
            "expires_in": int(tokens.get("expires_in") or 0),
            "refresh_expires_in": int(tokens.get("refresh_expires_in") or 0),
            "user": _identity(claims)}


@router.post("/api/auth/refresh")
async def auth_refresh(body: dict) -> dict:
    """Обновление сессии по refresh_token (Keycloak realm abop). Без этого веб жил 5 минут до первого 401."""
    import httpx
    import jwt
    rt = str((body or {}).get("refresh_token", "")).strip()
    if not rt:
        raise HTTPException(422, "нужен refresh_token")
    iss = os.getenv("KEYCLOAK_ISSUER") or "http://localhost:8811/realms/abop"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(iss + "/protocol/openid-connect/token",
                             data={"client_id": "abop-web", "grant_type": "refresh_token", "refresh_token": rt})
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"Keycloak недоступен: {ex}")
    if r.status_code != 200:
        raise HTTPException(401, "сессия истекла — войдите заново")
    tokens = r.json()
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    return {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or rt,
            "expires_in": int(tokens.get("expires_in") or 0), "user": _identity(claims)}
