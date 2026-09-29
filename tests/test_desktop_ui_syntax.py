"""Синтаксис интерфейса десктопа: каждый файл раздела должен разбираться как ES-модуль.

29.09 в чат уехала строка с настоящим переводом строки внутри литерала: раздел не грузился, а
приложение показывало «ABOP недоступен — проверьте сеть». Обычная проверка `node --check` этого не
ловила, потому что запускала файл как скрипт CommonJS, а не как модуль.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "desktop" / "ui"
FILES = sorted(UI.rglob("*.js"))


def test_ui_files_found():
    assert FILES, "файлы интерфейса десктопа не найдены"


@pytest.mark.skipif(not shutil.which("node"), reason="node недоступен")
@pytest.mark.parametrize("path", FILES, ids=[str(p.relative_to(UI)).replace("\\", "/") for p in FILES])
def test_parses_as_es_module(path: Path, tmp_path: Path):
    """Файл раздела грузится браузером как модуль — проверяем именно так."""
    copy = tmp_path / (path.stem + ".mjs")
    copy.write_bytes(path.read_bytes())
    r = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{path.name}: {(r.stderr or '').strip()[:400]}"
