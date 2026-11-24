# -*- coding: utf-8 -*-
"""В JS десктопа не должно быть имени без определения.

20–23.11.2026 четыре падения «… is not defined» дошли до прода, и ни один сторож их не увидел:
`пользаБлок` на пульте (20.11), `Живое` в «Прогонах» (21.11), `org` в карточке группы находок и
`лим` в строке бюджета (обе найдены аудитом 23.11 — docs/AUDIT_DESKTOP_2026-11-23.md, п. 1).
Сторожа интерфейса строковые: они проверяют, что в файле есть нужный текст, а не что имена в нём
определены. Имя без определения ловится статически за секунды — ESLint, правило no-undef.

ESLint стоит отдельным пакетом в tools/jslint (не в зависимостях Electron-приложения):
    npm ci --prefix tools/jslint
Без него сторож пропущен — поэтому CI ставит его явно (job smoke).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
ESLINT = КОРЕНЬ / "tools" / "jslint" / "node_modules" / "eslint" / "bin" / "eslint.js"
CONFIG = КОРЕНЬ / "tools" / "jslint" / "eslint.config.mjs"


@pytest.mark.skipif(not ESLINT.exists(), reason="ESLint не установлен: npm ci --prefix tools/jslint")
def test_в_js_десктопа_нет_имени_без_определения():
    r = subprocess.run(["node", str(ESLINT), "-c", str(CONFIG), "desktop/ui", "desktop/electron", "--format", "json"],
                       cwd=КОРЕНЬ, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode in (0, 1), "ESLint не смог отработать:\n" + r.stderr[-600:]
    files = json.loads(r.stdout)
    assert len(files) >= 20, f"ESLint проверил {len(files)} файлов — конфиг потерял пути"
    undef = [(f["filePath"].replace("\\", "/").split("/abop/")[-1], m["line"], m["message"])
             for f in files for m in f["messages"] if m.get("ruleId") == "no-undef"]
    assert not undef, "имя без определения — упадёт в рантайме с «… is not defined»:\n" + "\n".join(
        f"  {p}:{n}  {msg}" for p, n, msg in undef)
