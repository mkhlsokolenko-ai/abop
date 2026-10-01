"""Отчёт навыка — таблицами, а не стеной текста.

Письмо с результатом открывают ради одной строки: успеваем или нет, и где просрочка. Навык отдаёт
это структурой — сводку числами, построчное исполнение с планом и фактом, причины со сроками, — а в
отчёт всё уезжало одним куском `<pre>`: сорок строк «ключ: значение» подряд, и чтобы найти
просроченный пункт, надо было прочитать всё.

Форму берём из самих данных: перечислять разделы в коде отчёта значит разойтись с каталогом на
первом же новом навыке.
"""
from __future__ import annotations

import html as _html

from server import web_api

esc = lambda x: _html.escape(str(x if x is not None else ""))  # noqa: E731


def test_list_of_records_becomes_a_table_with_headers():
    """Однородные записи — таблица: колонку «отклонение» видно целиком, а не по строке на абзац."""
    h = web_api._structured_html(
        [{"пункт": "RM-05", "статус": "выполнено", "отклонение_дней": 7},
         {"пункт": "RM-06", "статус": "просрочено", "отклонение_дней": 35}], esc)
    assert "<table>" in h and "<thead>" in h
    assert "<th>пункт</th>" in h and "<th>отклонение дней</th>" in h, "подчёркивания в шапке не нужны"
    assert h.count("<tr>") == 3, "шапка и две записи"
    assert "RM-06" in h and "35" in h


def test_dict_becomes_two_columns():
    """Словарь — «поле / значение», а не склейка в строку."""
    h = web_api._structured_html({"просрочено": 1, "пунктов_всего": 37}, esc)
    assert "table class='kv'" in h
    assert "<th>просрочено</th><td>1</td>" in h


def test_nested_sections_get_their_own_headings():
    """Вложенный раздел получает заголовок: иначе непонятно, к чему относится таблица."""
    h = web_api._structured_html({"сводка": {"всего": 3}, "исполнение": [{"пункт": "RM-01"}]}, esc)
    assert "<h3>сводка</h3>" in h and "<h3>исполнение</h3>" in h
    assert h.index("<h3>сводка</h3>") < h.index("<h3>исполнение</h3>")


def test_empty_parts_are_not_rendered():
    """Пустой раздел в отчёте — шум: его не показываем вовсе."""
    assert web_api._structured_html({}, esc) == ""
    assert web_api._structured_html([], esc) == ""
    assert web_api._structured_html(None, esc) == ""


def test_structured_result_is_preferred_over_prose():
    """В отчёт идёт структура навыка, а его собственный текстовый дамп — только если схемы нет."""
    run = {"verdict": {"ok": True}, "waves": [["s"]],
           "skill_outputs": [{"skill": "roadmap-fact",
                              "structured": {"итог": "Не успеваем к вводу",
                                             "исполнение": [{"пункт": "RM-06", "статус": "просрочено"}]}}],
           "findings": [{"skill": "roadmap-fact", "text": "стена текста " * 50}]}
    h = web_api._build_report_html({"name": "Контролёр"}, run)
    assert "<table>" in h and "RM-06" in h
    assert "class='lead'" in h and "Не успеваем к вводу" in h, "итог — первой строкой"
    assert "стена текста" not in h, "прозаический дамп не должен дублировать структуру"


def test_prose_is_still_shown_when_there_is_no_structure():
    """Навык без схемы результата не должен исчезнуть из отчёта."""
    run = {"verdict": {"ok": True}, "waves": [],
           "findings": [{"skill": "researcher", "text": "вывод без схемы"}]}
    h = web_api._build_report_html({"name": "Ресёрч"}, run)
    assert "вывод без схемы" in h
