# -*- coding: utf-8 -*-
"""Пришпиленный список роутов — страховка расщепления монолита (п.23 исследования 08.10).

`server/web_api.py` вырос до 8 300 строк и 191 ручки в одном `@app`, без единого `APIRouter`.
Шардировать сервис по домену, не расщепив файл, нельзя. Но расщепление — именно тот вид правки,
где легко потерять ручку и не заметить: тесты проверяют поведение известных эндпоинтов, а
исчезнувший никто не спросит.

Снимок `tests/fixtures/routes.json` фиксирует (метод, путь) всех маршрутов приложения. Переезд
ручки между модулями его не меняет — он описывает ВНЕШНЮЮ поверхность. Расхождение значит одно
из двух: ручку потеряли при переносе или добавили новую. Второе — нормально, нужно обновить
снимок осознанно и описать ручку в `docs/ABOP_API.md`.
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

from server import web_api  # noqa: E402

СНИМОК = КОРЕНЬ / "tests" / "fixtures" / "routes.json"


def _текущие() -> set[tuple[str, str]]:
    """Плоский список маршрутов.

    Включённый роутер этот FastAPI держит ОДНОЙ записью `_IncludedRouter`, а не раскрывает его
    ручки в `app.routes`. Поверхность при этом та же — но наивный обход её не увидит и решит,
    что при переносе домена ручки пропали. Разворачиваем вложенные роутеры рекурсивно.
    """
    out: set[tuple[str, str]] = set()

    def обойти(маршруты, префикс: str = "") -> None:
        for r in маршруты:
            исходный = getattr(r, "original_router", None)      # _IncludedRouter прячет ручки тут
            вложенные = (getattr(исходный, "routes", None)
                         or getattr(getattr(r, "router", None), "routes", None))
            if вложенные:
                обойти(вложенные, префикс + str(getattr(r, "prefix", "") or ""))
                continue
            путь = префикс + str(getattr(r, "path", ""))
            for m in (getattr(r, "methods", None) or {"MOUNT"}):
                out.add((m, путь))

    обойти(web_api.app.routes)
    return out


def test_поверхность_не_изменилась():
    """Единственная проверка, ради которой файл и заведён."""
    снимок = {(m, p) for m, p in json.loads(СНИМОК.read_text(encoding="utf-8"))}
    текущие = _текущие()
    пропали = sorted(снимок - текущие)
    появились = sorted(текущие - снимок)
    assert not пропали, (
        "ручки исчезли — при переносе между модулями этого быть не должно: " + str(пропали))
    assert not появились, (
        "новые ручки: " + str(появились) + ". Это нормально, но осознанно: опишите их в "
        "docs/ABOP_API.md и обновите tests/fixtures/routes.json")


def test_снимок_не_пустой():
    снимок = json.loads(СНИМОК.read_text(encoding="utf-8"))
    assert len(снимок) > 150, "снимок подозрительно мал — не затёрли ли его пустым?"
