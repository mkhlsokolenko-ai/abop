"""Отчёт прогона — по форме служебного документа.

Письмо с результатом читает руководитель, и читает как корпоративный отчёт: сверху вниз и выборочно.
Раньше структурный результат навыка уезжал одним куском `<pre>` — сорок строк «ключ: значение»
подряд, обрезанных на двух тысячах знаков; чтобы найти просроченный пункт, надо было прочитать всё.

Порядок частей задан жёстко: шапка отвечает «кто и о чём», резюме — «что решать», показатели —
«насколько», разделы — доказательная часть, подвал — чем проверить. Проверяем именно это, а не
конкретные теги: вёрстку правят, обязательства остаются.
"""
from __future__ import annotations

import html as _html
import re

from server import web_api

esc = lambda x: _html.escape(str(x if x is not None else ""))  # noqa: E731

RUN = {
    "verdict": {"ok": True, "autonomy_used": "A2"}, "waves": [["roadmap-fact"]],
    "run_id": "run-demo-abc123", "started_by": "pm.manager", "created_at": "2026-10-01T13:46:20",
    "skill_outputs": [{"skill": "roadmap-fact", "structured": {
        "итог": "Не успеваем к вводу в эксплуатацию",
        "сводка": {"пунктов_всего": 37, "просрочено": 1, "перерасход_часов": 240},
        "исполнение": [{"пункт": "RM-05", "статус": "выполнено", "отклонение_дней": 7},
                       {"пункт": "RM-06", "статус": "просрочено", "отклонение_дней": 35}]}}],
    "findings": [{"skill": "roadmap-fact", "text": "стена текста " * 50}],
}


def text_of(h: str) -> str:
    return re.sub(r"<[^>]+>", " ", h)


# ── форма документа ──

def test_header_says_who_and_when():
    """Отчёт без «кто и когда» нельзя ни подшить, ни оспорить."""
    t = text_of(web_api._build_report_html({"name": "Контролёр проектов"}, RUN))
    for must in ("Контролёр проектов", "сформирован", "2026-10-01 13:46", "запустил", "pm.manager",
                 "навыки", "roadmap-fact"):
        assert must in t, must


def test_summary_comes_before_the_tables():
    """Решение принимают по резюме, остальное читают выборочно — значит оно идёт первым."""
    h = web_api._build_report_html({"name": "Контролёр"}, RUN)
    assert "Резюме" in text_of(h)
    # Сравниваем с первым РАЗДЕЛОМ, а не с первой таблицей: таблица реквизитов в шапке стоит выше
    # резюме по той же логике документа — сначала «кто и о чём», потом «что решать».
    assert h.index("Не успеваем к вводу") < h.index("<h2>"), "резюме обязано стоять до разделов"


def test_sections_are_numbered_for_references():
    """На разделы ссылаются в переписке: «см. п. 2» — значит у них есть номера."""
    t = text_of(web_api._build_report_html({"name": "Контролёр"}, RUN))
    # Раздел называется по-человечески, а не идентификатором навыка: документ служебный.
    assert "1. Сверка плана и факта" in t, t[:200]
    # В реквизитах шапки идентификатор уместен — это трассировка. В заголовке раздела нет:
    # документ служебный, и раздел называют так, как навык зовут люди.
    h2 = re.findall("<h2>(.*?)</h2>", web_api._build_report_html({"name": "Контролёр"}, RUN))
    assert h2 and "roadmap-fact" not in h2[0], h2


def test_footer_carries_traceability():
    """Подвал отвечает на «чем это проверить»: прогон, вердикт, автономия, этапы."""
    t = text_of(web_api._build_report_html({"name": "Контролёр"}, RUN))
    for must in ("run-demo-abc123", "вердикт", "пройден", "автономия", "A2", "этапов"):
        assert must in t, must


# ── форма данных ──

def test_list_of_records_becomes_a_table_with_headers():
    """Однородные записи — таблица: колонку «отклонение» видно целиком, а не по строке на абзац."""
    h = web_api._structured_html(
        [{"пункт": "RM-05", "статус": "выполнено", "отклонение_дней": 7},
         {"пункт": "RM-06", "статус": "просрочено", "отклонение_дней": 35}], esc)
    assert "<thead>" in h and h.count("<tr>") == 3, "шапка и две записи"
    assert "отклонение дней" in h, "подчёркивания в шапке не нужны"
    assert "строк: 2" in h, "счётчик строк — чтобы было видно, что таблица не обрезана"


def test_numeric_columns_are_right_aligned():
    """Цифры читаются столбиком только при выключке вправо — это форма, а не украшение."""
    h = web_api._structured_html([{"пункт": "RM-05", "часы": 90}, {"пункт": "RM-06", "часы": 705}], esc)
    assert "class=num" in h
    assert h.count("class=num") == 3, "колонка «часы»: шапка и две ячейки; «пункт» — текст"


def test_numeric_summary_becomes_key_figures():
    """Сводка из чисел — строка показателей: их переносят в отчёт выше по иерархии."""
    h = web_api._structured_html({"пунктов_всего": 37, "просрочено": 1, "перерасход_часов": 240}, esc)
    assert "kpis" in h and "перерасход часов" in h and "240" in h


def test_mixed_dict_stays_two_columns():
    """Словарь с текстом показателями не притворяется: длинное основание в плашку не влезет."""
    h = web_api._structured_html({"успеваем": "нет", "основание": "Пункт RM-06 просрочен на 35 дней",
                                  "прогноз_даты": "2027-01-15"}, esc)
    assert "table class='kv'" in h and "kpis" not in h


def test_nested_sections_get_their_own_headings():
    """Вложенный раздел получает заголовок: иначе непонятно, к чему относится таблица."""
    h = web_api._structured_html({"сводка": {"всего": 3}, "исполнение": [{"пункт": "RM-01"}]}, esc)
    assert ">сводка<" in h and ">исполнение<" in h
    assert h.index(">сводка<") < h.index(">исполнение<")


def test_empty_parts_are_not_rendered():
    """Пустой раздел в отчёте — шум: его не показываем вовсе."""
    assert web_api._structured_html({}, esc) == ""
    assert web_api._structured_html([], esc) == ""
    assert web_api._structured_html(None, esc) == ""


def test_structured_result_is_preferred_over_prose():
    """В отчёт идёт структура навыка, а его текстовый дамп — только если схемы нет."""
    h = web_api._build_report_html({"name": "Контролёр"}, RUN)
    assert "<table" in h and "RM-06" in h
    assert "стена текста" not in h, "прозаический дамп не должен дублировать структуру"


def test_prose_is_still_shown_when_there_is_no_structure():
    """Навык без схемы результата не должен исчезнуть из отчёта."""
    h = web_api._build_report_html({"name": "Ресёрч"}, {
        "verdict": {"ok": True}, "waves": [],
        "findings": [{"skill": "researcher", "text": "вывод без схемы"}]})
    assert "вывод без схемы" in h


def test_report_survives_as_plain_text_for_email_body():
    """Тело письма — текст: таблицы обязаны читаться и без вёрстки, а CSS в него попадать не должен."""
    t = web_api._html_to_text(web_api._build_report_html({"name": "Контролёр"}, RUN))
    assert "RM-06" in t and "просрочено" in t
    assert "{" not in t and "font-family" not in t, "стили в тело письма не уезжают"
