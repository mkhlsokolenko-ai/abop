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
    """Любой бланк — служебный документ: происхождение цифр и подвал обязательны.

    У формы с раскладкой шапку задаёт сам документ (вид, объём, метод), поэтому реквизиты прогона
    идут служебной отметкой снизу — `{{service}}`. Два заголовка подряд спорили бы друг с другом.
    """
    for tid, spec in report_store.load_files().items():
        assert "{{requisites}}" in spec["html"] or "{{service}}" in spec["html"], tid
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


def test_every_skill_has_a_form():
    """Ни один навык не остаётся на общем бланке по недосмотру.

    Исключение одно и объявленное: вертикали аудита 1С и расследования выбираются по ВИДУ результата
    (находки A/B/C/D, цепочки-расследования), а не по навыку — там форму определяет не исполнитель,
    а то, что получилось.
    """
    from pathlib import Path as _P
    skills = sorted(p.parent.name for p in (_P(__file__).resolve().parents[1] / "skills").glob("*/template.json"))
    by_shape = {s for s in skills if s.startswith("audit1c-") or s.startswith("invest1c-")}
    taken = {s for spec in report_store.load_files().values() for s in (spec.get("for_skills") or [])}
    orphan = [s for s in skills if s not in taken and s not in by_shape]
    assert not orphan, f"без формы: {orphan}"


def test_forms_do_not_claim_the_same_skill_twice():
    """Навык принадлежит одной форме: две претендующие — это спор, который решит порядок строк."""
    seen: dict = {}
    for tid, spec in report_store.load_files().items():
        for sid in (spec.get("for_skills") or []):
            assert sid not in seen, f"«{sid}» объявлен и в «{seen.get(sid)}», и в «{tid}»"
            seen[sid] = tid


def test_responsibility_notes_are_present_where_the_document_decides():
    """Документ обязан сказать, чего он НЕ заменяет: там, где по нему принимают решение.

    Оговорка живёт либо в HTML формы, либо блоком `note` её раскладки — для получателя это одна и
    та же строка внизу документа.
    """
    files = report_store.load_files()
    for tid, must in (("credit", "не является офертой"),
                      ("model", "не оценка независимого оценщика"),
                      ("project", "не заменяет акт приёмки"),
                      ("letter", "только после подтверждения ответственным"),
                      ("close", "выполняет ответственный бухгалтер"),
                      ("audit1c", "не заменяет заключение аудитора")):
        spec = files[tid]
        notes = " ".join(str(b.get("text") or "") for b in (spec.get("layout") or []))
        assert must in spec["html"] or must in notes, tid


def test_diagram_is_printed_landscape():
    """Схему читают по ширине: в портрет она не ложится."""
    assert report_store.load_files()["diagram"]["pdf_options"].get("orientation") == "landscape"


def test_no_heading_can_hang_over_an_optional_block():
    """Заголовок допустим только над блоком, который всегда что-то даёт.

    Доставка бывает пустой (локальный файл, отменённая отправка), раздел навыка — отсутствующим.
    Заголовок над ними печатался всегда и читался как «данных нет», хотя их и не ждали.
    """
    import re
    # audit_cards не в списке: в форме аудита это обязательная часть — отчёт аудита без находок
    # не формируется, и заголовок над ними всегда со смыслом.
    optional = {"deliveries", "findings", "investigations", "charts", "tool_usage", "schema_notes"}
    for tid, spec in report_store.load_files().items():
        for head, key in re.findall(r"<h2>([^<]+)</h2>\s*\{\{(\w+)\}\}", spec["html"]):
            assert key not in optional and not key.startswith("skill_"), \
                f"{tid}: «{head}» висит над необязательным блоком {{{{{key}}}}}"


def test_снятая_форма_уходит_из_базы():
    """Вертикаль разделилась на виды документов — прежняя общая форма не должна остаться в списке.

    Иначе её можно поставить OUT-узлу, а бланка под ней уже нет: получатель увидит пустой документ.
    """
    async def flow():
        await report_store.save("architecture", {"name": "старая общая", "html": "x"},
                                editor="seed", builtin=True)
        await report_store.save("t-mine", {"name": "моя", "html": "x"}, editor="ivanov")
        await report_store.seed_if_empty()
        return await report_store.get("architecture"), await report_store.get("t-mine")
    gone, mine = asyncio.run(flow())
    assert gone is None, "посевная форма, убранная из поставки, осталась в базе"
    assert mine, "пользовательскую форму посев трогать не должен"
