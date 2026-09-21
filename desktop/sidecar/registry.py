"""Реестр модулей: авто-обнаружение папок в sidecar/modules/.

Каждый модуль = пакет modules/<id>/module.py, экспортирующий:
    MANIFEST: dict — {id, title, icon, ui, order, ...} (ui — id фронт-панели)
    router:  fastapi.APIRouter — монтируется на /api/modules/<id>

Добавить фичу = положить новую папку-модуль. Ядро НЕ правится.
"""
from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from . import modules as modules_pkg

# Явный список модулей — используется, если авто-обход не сработал (PyInstaller frozen:
# pkgutil.iter_modules по замороженному пакету может вернуть пусто). Новый модуль:
# добавь папку в modules/ И имя сюда — так он подхватится и в dev, и в собранном .exe.
_FALLBACK = ["chat", "agents", "graphlens", "opslens", "ocr", "connectors", "security", "cabinet"]


def _names() -> list[str]:
    names = [info.name for info in pkgutil.iter_modules(modules_pkg.__path__) if info.ispkg]
    # объединяем авто-обход и fallback (без дублей, порядок стабилен)
    seen, out = set(), []
    for n in names + _FALLBACK:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def discover() -> list[ModuleType]:
    found = []
    for name in _names():
        try:
            m = importlib.import_module(f"{modules_pkg.__name__}.{name}.module")
        except Exception as e:  # noqa: BLE001 — один битый модуль не роняет приложение
            print(f"[registry] модуль '{name}' не загружен: {e}")
            continue
        if hasattr(m, "MANIFEST") and hasattr(m, "router"):
            found.append(m)
    found.sort(key=lambda m: m.MANIFEST.get("order", 100))
    return found
