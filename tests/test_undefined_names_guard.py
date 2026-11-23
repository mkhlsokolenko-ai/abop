# -*- coding: utf-8 -*-
"""Сторож неопределённых имён: в сервере, сайдкаре и скриптах не должно быть имени без определения.

22.11 на проде `/api/resolve/{entity}` отвечал 500: в routes/runs.py `_re` использовался на верхнем
уровне, а импорт остался внутри другой функции. Теста на сам маршрут не было, и регрессия доехала
до прода; в сайдкаре так же лежали два `_err` без определения — любой отказ платформы превращался
в 500 вместо причины. Такое ловится статически за долю секунды (Pyflakes, правило F821 в ruff)."""
import shutil
import subprocess
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
КАТАЛОГИ = ("server", "desktop/sidecar", "cli", "scripts", "connector")


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff не установлен — сторож пропущен")
def test_нет_имени_без_определения():
    r = subprocess.run(["ruff", "check", *КАТАЛОГИ, "--select", "F821", "--output-format", "concise"],
                       cwd=КОРЕНЬ, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "имя без определения:" + chr(10) + (r.stdout or r.stderr)
