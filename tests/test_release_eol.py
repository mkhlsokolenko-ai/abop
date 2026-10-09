# -*- coding: utf-8 -*-
"""Релизный архив не должен ломать скрипты переводом строки.

07.10 деплой на прод не стартовал: `git archive` с Windows (core.autocrlf=true) положил в архив
`ops/deploy-webapi.sh` с CRLF, и bash прочитал десятую строку как `set -euo pipefail\r` —
«invalid option name». Скрипт падал на первой же строке, контейнер оставался старый, а наружу это
выглядело как «изменений нет». Молчаливый отказ: ошибка ушла в nohup-лог, которого никто не читал.
"""
from __future__ import annotations

import pathlib

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]


def _исполняемые() -> list[pathlib.Path]:
    пути = sorted(КОРЕНЬ.glob("ops/**/*.sh")) + sorted(КОРЕНЬ.glob("*.sh"))
    пути += [p for p in КОРЕНЬ.glob("Dockerfile*") if p.is_file()]
    return [p for p in пути if p.is_file()]


def test_есть_что_проверять():
    assert _исполняемые(), "ни одного скрипта не нашлось — тест потерял цель"


def test_скрипты_без_возврата_каретки():
    """В рабочей копии — LF. Windows-клон без .gitattributes их бы испортил."""
    плохие = [str(p.relative_to(КОРЕНЬ)) for p in _исполняемые() if b"\r\n" in p.read_bytes()]
    assert not плохие, f"CRLF в файлах, которые исполняет Linux: {плохие}"


def test_gitattributes_закрепляет_lf():
    """Рабочая копия может быть чистой случайно — правило должно быть записано."""
    текст = (КОРЕНЬ / ".gitattributes").read_text(encoding="utf-8")
    for правило in ("*.sh text eol=lf", "Dockerfile* text eol=lf", "ops/** text eol=lf"):
        assert правило in текст, f"в .gitattributes нет правила «{правило}»"


def test_образ_несёт_ops_скрипты():
    """Руководство администратора зовёт скрипты как `docker exec abop-webapi python scripts/…`.

    Пока `scripts/` не копировались в образ, документ обещал то, чего там нет: перенос карты и
    чистку настроек выполнить было нечем, а узнавал об этом тот, кто в три часа ночи переносил
    стенд. Проверка дешевле.
    """
    import pathlib
    import re
    корень = pathlib.Path(__file__).resolve().parents[1]
    dockerfile = (корень / "Dockerfile.webapi").read_text(encoding="utf-8")
    assert re.search(r"^COPY\s+scripts\s", dockerfile, re.M), "образ не несёт scripts/"
    assert re.search(r"^COPY\s+ops\s", dockerfile, re.M), "образ не несёт ops/ (засев демо-стендов)"
    админ = (корень / "docs" / "guide" / "RUKOVODSTVO_ADMINISTRATORA.html").read_text(encoding="utf-8")
    for имя in re.findall(r"python (scripts/[\w./-]+\.py)", админ):
        assert (корень / имя).exists(), f"руководство зовёт {имя}, которого нет в репозитории"
