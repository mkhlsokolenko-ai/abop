# -*- coding: utf-8 -*-
"""Руководство администратора и код подбора не должны расходиться.

Раздел 12 руководства — единственное место, где пороги подбора объяснены человеку: по нему
администратор решает, что крутить, когда «выбирается не тот агент». Числа там выписаны явно, и
молчаливое расхождение с кодом хуже их отсутствия: по документу подкрутят один порог, а сработает
другой. Поэтому каждое число раздела 12 проверяется против константы, из которой оно взято.

Тест намеренно сравнивает ТЕКСТ руководства с кодом, а не код с кодом: если константу меняют —
документ обязан измениться в том же коммите.
"""
from __future__ import annotations

import pathlib
import re

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
АДМИН = КОРЕНЬ / "docs" / "guide" / "RUKOVODSTVO_ADMINISTRATORA.html"
ПОЛЬЗ = КОРЕНЬ / "docs" / "guide" / "RUKOVODSTVO_POLZOVATELYA.html"


@pytest.fixture(scope="module")
def админ() -> str:
    return АДМИН.read_text(encoding="utf-8")


def _раздел12(текст: str) -> str:
    i = текст.index('id="a12"')
    j = текст.index('id="a13"')
    return текст[i:j]


def test_пороги_подбора_совпадают_с_кодом(админ):
    from server import choice_store, orchestrator, planner

    р = _раздел12(админ)
    ожидаем = {
        "AGENT_SURE": orchestrator.AGENT_SURE,
        "AGENT_FLOOR": orchestrator.AGENT_FLOOR,
        "AGENT_COVER": orchestrator.AGENT_COVER,
        "EDITOR_FROM": orchestrator.EDITOR_FROM,
        "SKILL_SURE": orchestrator.SKILL_SURE,
        "SKILL_LEAD": orchestrator.SKILL_LEAD,
        "SKILL_FLOOR": orchestrator.SKILL_FLOOR,
        "SEM_FLOOR": planner.SEM_FLOOR,
        "SEM_CAP": planner.SEM_CAP,
        "CONTINUE_MIN": planner.CONTINUE_MIN,
        "CONTINUE_MAX": planner.CONTINUE_MAX,
        "HINT_WEIGHT": planner.HINT_WEIGHT,
        "HINT_CAP": planner.HINT_CAP,
        "TARGET_BONUS": planner.TARGET_BONUS,
        "SOURCE_ROLE_PENALTY": planner.SOURCE_ROLE_PENALTY,
        "MAX_CANDIDATES": planner.MAX_CANDIDATES,
        "MIN_CANDIDATE": planner.MIN_CANDIDATE,
        "PREFER_CAP": choice_store.PREFER_CAP,
        "WINDOW": choice_store.WINDOW,
    }
    расхождения = []
    for имя, знач in ожидаем.items():
        assert f"<code>{имя}</code>" in р, f"раздел 12 не упоминает {имя}"
        # число ищем рядом с константой: в той же строке таблицы или в том же абзаце
        окно = р[р.index(f"<code>{имя}</code>"):][:400]
        варианты = {str(знач), f"{знач:g}"}
        if isinstance(знач, float):
            варианты.add(f"{знач:.2f}".rstrip("0").rstrip("."))
        if not any(v in окно for v in варианты):
            расхождения.append(f"{имя}: в коде {знач}, в тексте рядом не найдено")
    assert not расхождения, "руководство разошлось с кодом: " + "; ".join(расхождения)


def test_редактор_отчёта_назван_своим_идентификатором(админ):
    """Если навык-редактор переименуют, раздел 12.4 станет врать про исключение из ранжирования."""
    from server import planner

    assert f"<code>{planner.EDITOR_SKILL}</code>" in _раздел12(админ), (
        f"в разделе 12 не упомянут навык-редактор {planner.EDITOR_SKILL}")


def test_переменные_документов_по_ссылке_все_описаны(админ):
    """Приложение Б — справочник: переменная, которой там нет, не существует для администратора."""
    src = (КОРЕНЬ / "server" / "doclink.py").read_text(encoding="utf-8")
    переменные = set(re.findall(r'getenv\(\s*"(ABOP_DOC_[A-Z_]+)"', src))
    assert переменные, "в doclink.py не нашлось переменных окружения — тест потерял предмет"
    нет = sorted(v for v in переменные if v not in админ)
    assert not нет, f"не описаны в руководстве администратора: {', '.join(нет)}"


def test_в_руководстве_пользователя_нет_убранных_разделов_как_живых():
    """«Распознать» и «Источники» убраны из маршрута десктопа (HIDDEN_MODULES в core/app.js).
    Пока они описаны как пункты полосы слева, человек ищет их там и не находит."""
    ui = (КОРЕНЬ / "desktop" / "ui" / "core" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"HIDDEN_MODULES\s*=\s*new Set\(\[([^\]]*)\]", ui)
    assert m, "HIDDEN_MODULES больше не объявлен так — проверьте, что раздел 7 руководства актуален"
    скрытые = set(re.findall(r'"([a-z]+)"', m.group(1)))
    assert {"ocr", "findings"} <= скрытые, "состав скрытых разделов изменился — обновите раздел 7"
    текст = ПОЛЬЗ.read_text(encoding="utf-8")
    assert "7.5 Чего в маршруте больше нет" in текст, "раздел 7.5 должен объяснять, куда делись разделы"
    assert "<h3>7.5 Распознать</h3>" not in текст, "«Распознать» снова описан как раздел маршрута"
