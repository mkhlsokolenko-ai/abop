# -*- coding: utf-8 -*-
"""Доставка, которую некому выполнить, — обещание без исполнителя.

Навык объявляет `delivery.{system,type}`; команду исполняет коннектор-воркер по реестру
обработчиков. Проверялось только, что тип непустой: опечатка «issue.created» прошла бы в поставку, и
агент собрался бы, а задача в трекере не появилась — отказ увидел бы пользователь, а не автор.

Реестр воркера не дублируем: читаем его ключи из `connector/worker.py`.
"""
from __future__ import annotations

import ast
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _worker_pairs() -> set[tuple[str, str]]:
    """Пары (система, тип) из реестра обработчиков воркера — без его импорта (нужен aiokafka)."""
    tree = ast.parse((ROOT / "connector" / "worker.py").read_text(encoding="utf-8"))
    pairs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k in node.keys:
            if (isinstance(k, ast.Tuple) and len(k.elts) == 2
                    and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in k.elts)):
                pairs.add((k.elts[0].value, k.elts[1].value))
    return pairs


def _declared_pairs() -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for p in sorted((ROOT / "skills").glob("*/template.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        d = t.get("delivery")
        for spec in (d if isinstance(d, list) else [d] if d else []):
            if not isinstance(spec, dict):
                continue
            out.setdefault((str(spec.get("system") or ""), str(spec.get("type") or "")), []).append(p.parent.name)
    return out


def test_реестр_воркера_прочитан():
    pairs = _worker_pairs()
    assert ("redmine", "issue.create") in pairs, f"не разобрали реестр обработчиков: {sorted(pairs)[:5]}"


def test_каждая_объявленная_доставка_имеет_исполнителя():
    known = _worker_pairs()
    bad = [f"{sys_}/{ty} ← {', '.join(who)}" for (sys_, ty), who in sorted(_declared_pairs().items())
           if (sys_, ty) not in known]
    assert not bad, ("навык объявляет доставку, которой нет в реестре коннектор-воркера:\n  "
                     + "\n  ".join(bad)
                     + "\n  доступны: " + ", ".join(f"{a}/{b}" for a, b in sorted(known)))
