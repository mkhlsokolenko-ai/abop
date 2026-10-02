"""Результат уезжает в систему на её языке, а не склейкой «ключ: значение».

Жалоба владельца 02.10: «в нашем Redmine вместо задач стена текста вперемешку с json, в аналоге
Confluence неструктурированный вывод». Причина была одна: подстановка списка схлопывала КАЖДУЮ
запись в строку «- id: F-001; ранг: критично; сумма: 120000; обоснование: …». Для находок, рейтинга,
историй и ролей — а это всё списки объектов — такое нечитаемо.

Разметка — свойство приёмника, а не шаблона: шаблон описывает содержание, язык выбирает доставка.
"""
from __future__ import annotations

from server import delivery as dl

ROWS = [{"id": "F-001", "ранг": "критично", "сумма": 120000},
        {"id": "F-002", "ранг": "существенно", "сумма": 4300}]


def test_format_follows_the_receiving_system():
    """Трекер читает textile, вики — markdown, письмо остаётся текстом."""
    assert dl.delivery_format({"system": "redmine", "type": "issue.create"}) == "textile"
    assert dl.delivery_format({"system": "bookstack", "type": "page.publish"}) == "markdown"
    assert dl.delivery_format({"system": "mailpit", "type": "email.send"}) == "text"
    assert dl.delivery_format({"system": "неизвестная", "type": "x"}) == "text", "простой текст читается везде"
    assert dl.delivery_format({"system": "redmine", "type": "email.send"}) == "text", "письмо — всегда текст"


def test_records_become_a_table_in_the_tracker():
    """Redmine понимает textile: шапка через |_. и строки — иначе это дамп, а не задача."""
    out = dl.render("{{#находки}}", {"находки": ROWS}, {}, "textile")
    assert out.splitlines()[0] == "|_. id|_. ранг|_. сумма|"
    assert "| F-001 | критично | 120000 |" in out
    assert "id: F-001;" not in out, "прежняя склейка полей не должна вернуться"


def test_records_become_a_table_in_the_wiki():
    """Вики принимает markdown — страница читается как документ."""
    out = dl.render("{{#находки}}", {"находки": ROWS}, {}, "markdown")
    lines = out.splitlines()
    assert lines[0] == "| id | ранг | сумма |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| F-001 | критично | 120000 |"


def test_records_in_a_letter_are_labelled_lines():
    """Таблицы в почте не живут: запись — абзац с подписанными полями."""
    out = dl.render("{{#находки}}", {"находки": ROWS}, {}, "text")
    assert out.startswith("- F-001")
    assert "  ранг: критично" in out and "  сумма: 120000" in out


def test_plain_list_stays_a_list():
    """Список строк не превращается в таблицу из одной колонки."""
    for fmt in ("textile", "markdown", "text"):
        out = dl.render("{{#шаги}}", {"шаги": ["сверить", "подтвердить"]}, {}, fmt)
        assert out == "- сверить\n- подтвердить", fmt


def test_dict_is_rendered_by_fields():
    """Объект — подписанные строки, подчёркивания в именах полей человеку не нужны."""
    out = dl.render("{{#порог}}", {"порог": {"сумма": 50000, "как_выведен": "0.5% выручки"}}, {}, "markdown")
    assert out == "- сумма: 50000\n- как выведен: 0.5% выручки"


def test_wide_tables_are_trimmed_not_broken():
    """Шире шести колонок таблица не читается ни в трекере, ни в вики."""
    wide = [{f"поле{i}": i for i in range(12)}]
    out = dl.render("{{#x}}", {"x": wide}, {}, "markdown")
    assert out.splitlines()[0].count("|") == 7, out.splitlines()[0]


def test_long_tables_say_how_much_is_hidden():
    """Остаток не выбрасывается молча: получатель должен знать, что видит не всё."""
    many = [{"id": f"F-{i:03d}"} for i in range(60)]
    out = dl.render("{{#x}}", {"x": many}, {}, "markdown")
    assert "первые 40 из 60" in out


def test_pipe_in_data_does_not_break_the_table():
    """Вертикальная черта в значении рвала бы разметку таблицы у обеих систем."""
    out = dl.render("{{#x}}", {"x": [{"a": "лево|право", "b": "строка\nвторая"}]}, {}, "markdown")
    assert "лево/право" in out and "строка вторая" in out


def test_inline_substitution_is_unchanged():
    """Одиночная подстановка осталась прежней: меняли только списки."""
    assert dl.render("Ранг: {{ранг}}", {"ранг": "критично"}, {}) == "Ранг: критично"
    assert dl.render("{{$.итог}}", {}, {"итог": "не сходится"}) == "не сходится"
