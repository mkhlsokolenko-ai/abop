"""Конфиг сайдкара desktop-оболочки. Один источник правды для эндпоинтов и путей.

Оболочка — тонкий клиент; движок = этот локальный FastAPI. Стек окна (Electron) можно
поменять, не трогая сайдкар: контракт — HTTP на 127.0.0.1.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ── курсовой шлюз (те же эндпоинты, что у CLI `ape`) ──
KC = "https://auth.engineer-ai.pro/realms/ai-product-engineer/protocol/openid-connect"
MCP = "https://mcp.engineer-ai.pro/mcp"
PORTAL = "https://engineer-ai.pro"
CLIENT = "portal"

# ── ABOP Web API (рантайм агентов) — сайдкар становится тонким прокси к нему под JWT пользователя ──
# Один источник истины по агентам/прогонам/HITL/данным (см. docs/ADR_DESKTOP_ABOP_SMYCHKA.md).
ABOP_ПО_УМОЛЧАНИЮ = "http://5.129.192.63:8091"


def адрес_abop() -> str:
    """Адрес установки ABOP. Три источника, от сильного к слабому.

    1. переменная среды `ABOP_BASE_URL` — ею пользуются разработка и CI, и она не должна уступать
       сохранённому значению;
    2. настройка из «Кабинета» (`config.json`, поле `abop_base_url`);
    3. умолчание.

    Почему функция, а не константа. Прежде адрес читался ОДИН раз при импорте, и сменить его можно
    было только переменной среды с перезапуском приложения — то есть доступом к машине человека.
    12.11.2026 владелец остался без ABOP из-за маршрута, и единственным выходом был `setx` и
    перезапуск. Теперь значение читается на каждый запрос: правка в «Кабинете» действует сразу.

    `config.ABOP` остаётся рабочим именем (девять мест кода) — его отдаёт `__getattr__` ниже.
    """
    из_среды = str(os.environ.get("ABOP_BASE_URL") or "").strip()
    if из_среды:
        return из_среды.rstrip("/")
    try:
        сохранено = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("abop_base_url")
    except Exception:  # noqa: BLE001 — нет файла или битый json: работаем умолчанием
        сохранено = None
    return str(сохранено or ABOP_ПО_УМОЛЧАНИЮ).strip().rstrip("/") or ABOP_ПО_УМОЛЧАНИЮ


def источник_адреса() -> str:
    """Откуда взят адрес: «переменная среды» | «настройка» | «умолчание».

    Экран обязан это показывать: переменная среды сильнее настройки, и поле, значение которого
    молча перекрыто, выглядит как сломанное.
    """
    if str(os.environ.get("ABOP_BASE_URL") or "").strip():
        return "переменная среды"
    try:
        if json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("abop_base_url"):
            return "настройка"
    except Exception:  # noqa: BLE001
        pass
    return "умолчание"


def __getattr__(name: str):
    """`config.ABOP` — вычисляемое имя, а не снимок на момент импорта (PEP 562)."""
    if name == "ABOP":
        return адрес_abop()
    raise AttributeError(name)

APP_NAME = "ABOP Desktop"
# Версия берётся из Electron (app.getVersion() → env APE_APP_VERSION) — всегда совпадает с .exe,
# без ручного бампа. Фолбэк для dev/запуска сайдкара отдельно. Раньше был захардкожен «1.0.1»,
# из-за чего шапка показывала 1.0.1 при любой сборке.
APP_VERSION = os.environ.get("APE_APP_VERSION") or "1.0.41"

# ── локальные данные приложения — ОТДЕЛЬНЫЙ каталог от курсовой версии (~/.ape-desktop),
#    чтобы БД/токены/состояние не пересекались (две версии ставятся и работают независимо). ──
DATA_DIR = Path(os.environ.get("ABOP_DESKTOP_HOME") or (Path.home() / ".abop-desktop"))
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "app.db"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
