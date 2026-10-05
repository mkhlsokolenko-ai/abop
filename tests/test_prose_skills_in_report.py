# -*- coding: utf-8 -*-
"""Навык, ответивший прозой, попадает в отчёт — а не исчезает вместе со своей работой.

05.10 сквозная проверка кейса «оцени идею» показала беду яснее любых слов: прогон на шесть навыков,
85 тысяч потраченных токенов, 185 секунд — и в выходах прогона НОЛЬ разделов. Условие сборки было
«structured — словарь», поэтому навыки, которые отвечают прозой (разбор идеи, адвокат дьявола, ревью
спецификации, JTBD), выпадали целиком. Отчёт выходил пустым, и это ровно то, на что владелец
жаловался словами «отчёт ни о чём».

Теперь выход навыка — это либо структура, либо текст; в сшитый бланк текстовые навыки идут
отдельными разделами, перед оговоркой и подписями.
"""
from __future__ import annotations

from server import report_compose, report_form


def _result():
    return {"skill_outputs": [
        {"skill": "idea-scorer", "structured": {"итог": "идея жизнеспособна"}, "text": "разбор"},
        {"skill": "devils-advocate", "structured": None, "text": "Возражение первое.\n\nВозражение второе."},
        {"skill": "jtbd-formulator", "structured": None, "text": "Работа, которую нанимают делать…"},
        {"skill": "report-editor", "structured": None, "text": "служебный текст редактора"},
        {"skill": "пустой", "structured": None, "text": "   "},
    ]}


def test_прозаики_становятся_разделами():
    sec = report_compose.prose_sections(_result(), {"devils-advocate": "Адвокат дьявола"})
    ids = [s["title"] for s in sec]
    assert ids == ["Адвокат дьявола", "jtbd-formulator"], f"разделы собраны неверно: {ids}"
    assert all(s["t"] == "prose" for s in sec)


def test_редактор_и_структурные_не_дублируются():
    """У редактора своя роль — вид документа; навык со схемой уже имеет свой бланк."""
    sec = report_compose.prose_sections(_result())
    titles = [s["title"] for s in sec]
    assert "report-editor" not in titles, "редактор отчёта не должен идти разделом"
    assert "idea-scorer" not in titles, "навык со структурой дважды в документе не нужен"
    assert "пустой" not in titles, "пустой текст — не раздел"


def test_раздел_прозы_рисуется_из_своего_текста():
    html = report_form.render([{"t": "prose", "title": "Адвокат дьявола",
                                "text": "Первое возражение.\n\nВторое возражение."}], {}, {})
    assert "Адвокат дьявола" in html
    assert "Первое возражение." in html and "Второе возражение." in html
    assert html.count("<p>") == 2, "абзацы должны остаться абзацами"


def test_ссылочный_блок_прозы_работает_как_прежде():
    res = {"skill_outputs": [{"skill": "s1", "structured": {"вывод": "текст из структуры"}}]}
    html = report_form.render([{"t": "prose", "title": "Вывод", "src": "s1:вывод"}], res, {})
    assert "текст из структуры" in html, "ссылка в результат перестала работать"


def test_в_выходы_прогона_идут_все_навыки():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "server" / "web_api.py").read_text(encoding="utf-8")
    i = src.index('result["skill_outputs"] = [')
    body = src[i:i + 900]
    assert '"text": str(f.get("text")' in body, "текст навыка не сохраняется — отчёту нечем наполняться"
    assert 'str(f.get("text") or "").strip())' in body, "навык без структуры снова выпадает из выходов"
    assert '"sources_mode"' in body, "режим источников не доезжает до выходов прогона"
