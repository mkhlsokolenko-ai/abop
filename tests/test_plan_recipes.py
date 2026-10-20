# -*- coding: utf-8 -*-
"""Рецепты демо-графика лежат в репозитории как данные, и их карта не теряет полей.

Грабля повторилась дважды: карта рецепта ОДДС однажды отбросила контрагента и статью ДДС, а карта
`plan_rules` — календарь и статусы ожидания. Поле есть в данных, в карте его нет — и до движка оно
не доезжает. Ошибки при этом нет: правило просто не срабатывает, и отчёт выглядит «чистым».

Тест сверяет три стороны: что лежит в демо-данных, что переносит рецепт и что ждёт движок.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))

РЕЦЕПТЫ = {r["recipe"]: r for r in
           json.load(io.open(КОРЕНЬ / "demo" / "plan" / "recipes.json", encoding="utf-8"))}


def _данные(имя):
    return json.load(io.open(КОРЕНЬ / "demo" / "plan" / имя, encoding="utf-8"))


def _карта(рецепт):
    return {str(m["field"]) for m in РЕЦЕПТЫ[рецепт]["map_rows"]}


def test_рецепты_демо_на_месте():
    assert set(РЕЦЕПТЫ) == {"plan_stages", "plan_rules", "plan_roadmap", "plan_roadmap_live",
                            "plan_roadmap_mpp", "plan_transcript", "plan_shift",
                            "plan_portfolio", "plan_history"}, sorted(РЕЦЕПТЫ)
    # Архив версий свода лежит в объектном хранилище, и рецепт привязан к системе реестра:
    # иначе чтение не пройдёт ABAC и не будет видно в происхождении.
    история = РЕЦЕПТЫ["plan_history"]
    assert история["entity"] == "portfolio_snapshot" and история["system_id"] == "minio"
    assert история["source"]["root"] == "проекты", "в файле архива проекты лежат под ключом"
    # Свод портфеля считается по готовым расчётам, а не лезет в источники заново.
    портфель = РЕЦЕПТЫ["plan_portfolio"]
    assert портфель["entity"] == "portfolio_line" and портфель["source"]["kind"] == "portfolio"
    # Три рецепта наполняют одну сущность: файл демо, живой проектный сервер и план MS Project.
    # Это осознанно (гоняем тот, у которого есть источник), и каждый объясняет себя заметкой.
    план = РЕЦЕПТЫ["plan_roadmap_mpp"]
    assert план["entity"] == "roadmap_item" and план["source"]["kind"] == "msproject"
    assert "заметка" in план and план["source"]["target"].endswith(".xml")
    assert set(план["source"]["связи"]) <= {"FS", "FF", "SF", "SS"}
    # Живой рецепт и файловый наполняют ОДНУ сущность: это осознанно и объяснено в самом рецепте.
    живой = РЕЦЕПТЫ["plan_roadmap_live"]
    assert живой["entity"] == РЕЦЕПТЫ["plan_roadmap"]["entity"] == "roadmap_item"
    assert "заметка" in живой and "без проектного сервера" in живой["заметка"]
    assert живой["source"]["kind"] == "openproject"
    # Соответствие полей заказчика — в рецепте, а не в коде адаптера.
    assert set(живой["source"]["поля"]) == {"стадия", "критично_для_ввода", "дата_входа",
                                            "возвратов"}
    assert "токен" not in живой["source"], "ключ API не должен лежать в рецепте открытым текстом"
    assert живой["source"].get("токен_файл") or живой["source"].get("токен_env"), (
        "рецепт должен сказать, ГДЕ взять ключ: файл или переменная среды")
    for имя, r in РЕЦЕПТЫ.items():
        assert r["status"] == "опубликован", имя
        assert (r.get("emit") or {}).get("schema") == r["entity"], имя
        assert r["map"] and len(r["map"]) == len(r["map_rows"]), (имя, "карта и форма разошлись")
    assert РЕЦЕПТЫ["plan_shift"]["source"]["kind"] == "plan", "расчёт приходит вычисляемым адаптером"
    assert РЕЦЕПТЫ["plan_transcript"]["source"]["kind"] == "transcript"


def test_карта_переносит_все_поля_данных():
    """Новое поле правила без поля в карте — молчаливая потеря: правило не сработает."""
    для = {"plan_rules": "rules.json", "plan_stages": "stages.json", "plan_roadmap": "roadmap.json"}
    for рецепт, файл in для.items():
        поля = {k for запись in _данные(файл) for k in запись}
        потеряно = поля - _карта(рецепт)
        assert not потеряно, f"{рецепт} не переносит: {', '.join(sorted(потеряно))}"


def test_параметры_правил_доезжают_до_движка():
    """Календарь, статусы ожидания и пороги — те самые поля, без которых проверки молчат."""
    карта = _карта("plan_rules")
    for поле in ("вид", "выходные", "праздники", "статусы_ожидания", "порог_дней",
                 "порог_возвратов", "значения_критичности", "буфер_дней", "основание"):
        assert поле in карта, f"параметр правила «{поле}» не переносится рецептом"


def test_карта_расчёта_переносит_то_что_отдаёт_движок():
    import ape
    from tests.test_plan_shift import _расчёт

    строки = ape._план_строки(_расчёт())
    поля = {k for с in строки for k in с}
    потеряно = поля - _карта("plan_shift")
    assert not потеряно, f"рецепт расчёта не переносит: {', '.join(sorted(потеряно))}"
