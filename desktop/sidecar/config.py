"""Конфиг сайдкара desktop-оболочки. Один источник правды для эндпоинтов и путей.

Оболочка — тонкий клиент; движок = этот локальный FastAPI. Стек окна (Electron) можно
поменять, не трогая сайдкар: контракт — HTTP на 127.0.0.1.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── курсовой шлюз (те же эндпоинты, что у CLI `ape`) ──
KC = "https://auth.engineer-ai.pro/realms/ai-product-engineer/protocol/openid-connect"
MCP = "https://mcp.engineer-ai.pro/mcp"
PORTAL = "https://engineer-ai.pro"
CLIENT = "portal"

# ── ABOP Web API (рантайм агентов) — сайдкар становится тонким прокси к нему под JWT пользователя ──
# Один источник истины по агентам/прогонам/HITL/данным (см. docs/ADR_DESKTOP_ABOP_SMYCHKA.md).
ABOP = os.environ.get("ABOP_BASE_URL", "http://5.129.192.63:8091")

APP_NAME = "ABOP Desktop"
# Версия берётся из Electron (app.getVersion() → env APE_APP_VERSION) — всегда совпадает с .exe,
# без ручного бампа. Фолбэк для dev/запуска сайдкара отдельно. Раньше был захардкожен «1.0.1»,
# из-за чего шапка показывала 1.0.1 при любой сборке.
APP_VERSION = os.environ.get("APE_APP_VERSION") or "1.0.7"

# ── локальные данные приложения — ОТДЕЛЬНЫЙ каталог от курсовой версии (~/.ape-desktop),
#    чтобы БД/токены/состояние не пересекались (две версии ставятся и работают независимо). ──
DATA_DIR = Path(os.environ.get("ABOP_DESKTOP_HOME") or (Path.home() / ".abop-desktop"))
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "app.db"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
