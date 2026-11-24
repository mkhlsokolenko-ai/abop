# -*- coding: utf-8 -*-
"""Ни один __init__.py не должен игнорироваться git-ом.

24.11.2026 первый же полный прогон набора на CI упал на свежем клоне: тест «папка модуля не
пакет — importlib его не найдёт». Правило `_*` в .gitignore (черновики вида `_video`) съедало
все файлы, начинающиеся с подчёркивания, ВО ВСЁМ репозитории — из двадцати `__init__.py` в git
доехал один. На машинах, где репо старше правила, файлы лежали локально и всё работало; свежий
клон держался на namespace packages — пока тест не спросил пакет явно.

Правило сужено до корня (`/_*`). Сторож не даст ему расползти обратно.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]


def test_git_не_игнорирует_пакеты():
    inits = [p for p in КОРЕНЬ.rglob("__init__.py")
             if "node_modules" not in p.parts and "__pycache__" not in p.parts]
    assert len(inits) >= 15, f"init-файлов стало {len(inits)} — сторож их потерял"
    r = subprocess.run(["git", "check-ignore", "--stdin"], cwd=КОРЕНЬ,
                       input="\n".join(str(p.relative_to(КОРЕНЬ)) for p in inits),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode in (0, 1), r.stderr[-300:]
    съедено = [x for x in r.stdout.splitlines() if x.strip()]
    assert not съедено, (
        "git игнорирует пакеты — свежий клон их не получит (так `_ *` в .gitignore съел все "
        "__init__.py разом, поймано полным прогоном на CI 24.11):\n  " + "\n  ".join(съедено))
