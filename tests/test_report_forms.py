"""Форма отчёта выбирается по предмету, а не только по форме результата.

Две показанные вертикали — аудит 1С и расследование — имели свои бланки, а остальные двадцать с
лишним навыков делили два общих. Получатель финансовой модели и получатель перечня задач читают
разные документы и ждут разных реквизитов, поэтому форма объявляет, чьи результаты оформляет.

Привязка хранится рядом с формой (в базе, сид из reports/index.json): иначе бланк живёт в одном
месте, а область его применения в другом, и расходятся они на первой же правке.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from server import report_store

REPORTS = Path(__file__).resolve().parents[1] / "reports"


def test_shipped_forms_declare_their_subject():
    """Каждая вертикальная форма называет, для каких навыков она: без этого её никто не выберет."""
    idx = json.loads((REPORTS / "index.json").read_text(encoding="utf-8"))
    files = report_store.load_files()
    for tid in ("letter", "tickets", "finance", "project"):
        assert tid in files, tid
        assert files[tid]["for_skills"], f"{tid}: не объявлена область применения"
        assert idx[tid]["name"], tid
    # общие формы остаются без привязки — их выбирают по форме результата
    for tid in ("default", "digest"):
        assert not files[tid].get("for_skills"), tid


def test_every_form_has_requisites_and_footer():
    """Любой бланк — служебный документ: без шапки и подвала его нельзя ни подшить, ни проверить."""
    for tid, spec in report_store.load_files().items():
        assert "{{requisites}}" in spec["html"], tid
        assert "{{footer}}" in spec["html"], tid


def test_no_heading_without_content():
    """Заголовок в бланке допустим только над плейсхолдером, который всегда что-то даёт.

    Прежний дайджест печатал «Разбор почты» и «План дня» над плейсхолдерами отдельных навыков —
    у агента без них оставались пустые строки с названиями.
    """
    import re
    for tid, spec in report_store.load_files().items():
        for head, after in re.findall(r"<h2>([^<]+)</h2>\s*\{\{(\w+)\}\}", spec["html"]):
            assert not after.startswith("skill_"), f"{tid}: «{head}» висит над отдельным навыком"


def test_specific_form_wins_over_general():
    """Если подходят две, берём ту, что объявлена для меньшего числа навыков: она точнее."""
    async def flow():
        await report_store.save("t-wide", {"name": "широкая", "html": "x",
                                           "for_skills": ["a", "b", "c", "d"]}, editor="seed")
        await report_store.save("t-narrow", {"name": "узкая", "html": "x", "for_skills": ["a"]}, editor="seed")
        return (await report_store.template_for_skills(["a"]),
                await report_store.template_for_skills(["d"]),
                await report_store.template_for_skills(["нет-такого"]),
                await report_store.template_for_skills([]))
    narrow, wide, none_, empty = asyncio.run(flow())
    assert narrow == "t-narrow"
    assert wide == "t-wide"
    assert none_ == "" and empty == "", "нет подходящей формы — решает форма результата"


def test_form_scope_survives_saving():
    """Область применения сохраняется вместе с формой, иначе привязка теряется при первой же правке."""
    async def flow():
        await report_store.save("t-scope", {"name": "n", "html": "h", "for_skills": ["x", "y"]}, editor="seed")
        return await report_store.get("t-scope")
    assert (asyncio.run(flow()) or {}).get("for_skills") == ["x", "y"]
