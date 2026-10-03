"""Границы шага объявлены, сужаются сверху вниз, и что шаг делал — записано.

«Полусвободное исполнение внутри объявленных границ» держится на двух условиях. Первое: границу
нельзя поднять изнутри — иначе она не граница. Узел уже мог задать число шагов инструментов, и это
значение перекрывало потолок среды: свобода расширялась там, где её объявляли.

Второе: свободу надо предъявлять. Вызовы инструментов писались в поле результата и не показывались
нигде — агент что-то делал, а чем именно, знал только журнал.
"""
from __future__ import annotations

from server import report_store, runner


def test_environment_is_the_ceiling_not_a_default():
    """Узел точнее навыка, навык точнее среды — но выше потолка среды не поднимается никто."""
    assert runner.effective_limits({"tool_steps": 3})["tool_steps"] == 3


def test_tool_usage_is_collected_per_skill_and_tool():
    """Сводка отвечает на вопрос «чем пользовался каждый шаг и во что это обошлось»."""
    base = {"findings": [
        {"skill": "researcher", "tool_calls": [
            {"tool": "web", "input_tokens": 10, "output_tokens": 5},
            {"tool": "web", "input_tokens": 4, "output_tokens": 1},
            {"tool": "calc", "input_tokens": 2, "output_tokens": 0}]},
        {"skill": "finance-report", "tool_calls": [{"tool": "calc", "input_tokens": 7, "output_tokens": 3}]}]}
    rows = runner.tool_usage(base["findings"])
    by = {(r["навык"], r["инструмент"]): r for r in rows}
    assert by[("researcher", "web")]["вызовов"] == 2
    assert by[("researcher", "web")]["токенов"] == 20
    assert by[("finance-report", "calc")]["токенов"] == 10
    assert len(rows) == 3


def test_failed_calls_are_counted_not_hidden():
    """Молчать об ошибке вызова значит выдавать неполную работу за полную."""
    rows = runner.tool_usage([{"skill": "s", "tool_calls": [
        {"tool": "web", "error": "таймаут"}, {"tool": "web", "input_tokens": 1, "output_tokens": 1}]}])
    assert rows[0]["вызовов"] == 2 and rows[0]["ошибок"] == 1


def test_no_tools_means_no_section():
    """Пустой раздел в отчёте — шум: шаг без инструментов ничего не добавляет."""
    assert runner.tool_usage([{"skill": "s", "text": "без инструментов"}]) == []


def test_every_form_shows_what_the_step_did():
    """Раздел доказательный, поэтому стоит во всех формах, а не только в общей."""
    for tid, spec in report_store.load_files().items():
        assert "{{tool_usage}}" in spec["html"], tid
