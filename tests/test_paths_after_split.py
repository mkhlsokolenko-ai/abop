# -*- coding: utf-8 -*-
"""Пути, считаемые от файла, обязаны пережить переезд файла.

При расщеплении монолита (ADR-052) код уехал на уровень глубже: `server/web_api.py` →
`server/routes/admin.py`. Выражение `Path(__file__).resolve().parents[1] / "demo"` при этом стало
указывать в `server/demo`, которого нет, и кнопка «Собрать и запустить» начала молча отвечать
«нет демо-пакета». Ни один тест этого не заметил: имена разрешались, маршрут был на месте, ответ
был корректным 404 — просто неправдой.

Статическая проверка имён такое не ловит в принципе, поэтому проверка здесь предметная: каталоги,
которые код вычисляет от своего расположения, должны существовать и содержать то, ради чего их
вычисляют.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"
os.environ["ABOP_RUN_WORKERS"] = "0"


def test_каталог_демо_пакетов_находится():
    from server.routes import admin

    assert admin._DEMO_DIR == КОРЕНЬ / "demo", f"каталог демо считается неверно: {admin._DEMO_DIR}"
    assert admin._DEMO_DIR.is_dir()


def test_каждый_демо_пакет_полон():
    """`/api/demo/prepare` требует пару контракт + тело агента; неполный пакет — это 404 в показе."""
    from server.routes import admin

    пакеты = [p for p in admin._DEMO_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")]
    assert пакеты, "демо-пакетов нет вовсе"
    полных = 0
    for p in пакеты:
        контракты = sorted(p.glob("contract*.json"))
        тело = p / "agent_body.json"
        if not контракты or not тело.is_file():
            continue                      # каталог без пакета (например, код демо-агента) — не ошибка
        полных += 1
        c = json.loads(контракты[0].read_text(encoding="utf-8"))
        b = json.loads(тело.read_text(encoding="utf-8"))
        audit_id = (c.get("capability_request") or {}).get("audit_id")
        assert audit_id, f"{p.name}: в контракте нет capability_request.audit_id"
        assert b.get("contract_audit_id") == audit_id, f"{p.name}: контракт и агент про разное"
    assert полных >= 2, "ожидали несколько готовых к показу пакетов"


def test_каталог_руководств_находится():
    """Те же грабли ждут любой путь от файла: руководства отдаются по /api/guides."""
    from server import web_api

    assert web_api._GUIDE_DIR == КОРЕНЬ / "docs" / "guide", web_api._GUIDE_DIR
    assert (web_api._GUIDE_DIR / "RUKOVODSTVO_POLZOVATELYA.html").is_file()


def test_в_доменах_нет_путей_от_файла_короче_нужного():
    """Прямой запрет на `parents[1]` в модулях маршрутов: они лежат на уровень глубже приложения.

    Модуль домена — `server/routes/<домен>.py`, поэтому корень репозитория у него `parents[2]`.
    `parents[1]` там почти наверняка ошибка переезда, и её лучше поймать здесь, чем в показе.
    """
    плохо = []
    for p in sorted((КОРЕНЬ / "server" / "routes").glob("*.py")):
        текст = p.read_text(encoding="utf-8")
        for номер, строка in enumerate(текст.splitlines(), 1):
            if "parents[1]" in строка and "__file__" in строка:
                плохо.append(f"{p.name}:{номер}")
    assert not плохо, "путь от файла в домене считается как в приложении: " + ", ".join(плохо)
