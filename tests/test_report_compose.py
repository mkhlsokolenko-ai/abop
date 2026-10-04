# -*- coding: utf-8 -*-
"""Прогон из разных вертикалей получает СШИТЫЙ документ, а не бланк одной из них.

Форма выбиралась правилом «та, что объявлена для наименьшего числа навыков». Для одного навыка это
верно, для смеси — произвол: прогон из статус-отчёта, ADR, письма и БФТ получал бланк ADR, а
остальные три навыка уходили в общий хвост. Владелец сказал: «отчёт выходит на каждый шаг, нужен
механизм объединения».

Сшивка детерминированная, без модели: раздел каждого навыка берётся из ЕГО бланка, порядок — порядок
выполнения, ничего не отбрасывается по важности.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "ABOP_BUS", "ABOP_KAFKA_BROKERS"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

import asyncio  # noqa: E402

from server import report_compose, report_form, report_store, web_api  # noqa: E402

# Без Postgres реестр форм живёт в памяти и пуст: засеваем его из reports/, иначе сшивать нечего и
# проверка молча проходила бы на пустом месте.
asyncio.run(report_store.seed_if_empty())

RUN = {"run_id": "r-mix", "started_by": "ivanov", "skill_outputs": [
    {"skill": "idea-scorer", "structured": {
        "идея": {"название": "Рубрикатор репозиториев", "кратко": "надстройка над GitHub"},
        "оценки": [{"критерий": "боль", "балл": 4, "обоснование": "поиск живого инструмента долгий"}],
        "сумма": 17, "вердикт": "доработать", "обоснование_вердикта": "нет проверки спроса"}},
    {"skill": "market-research", "structured": {
        "гипотеза": "разработчики платят за курированный каталог",
        "размер_рынка": [{"уровень": "SAM", "значение": "40 млн $", "формула": "ARPU × число команд",
                          "уверенность": "средняя"}],
        "вывод": {"вердикт": "идти", "куда_именно": "команды 10–50 человек", "обоснование": "белое пятно"}}},
]}


def _doc(run) -> str:
    tpl = asyncio.run(web_api._template_for_run(run))
    ctx = web_api._report_context({"name": "Проба"}, run)
    return tpl, report_store.render(tpl, ctx)


def test_смешанный_прогон_получает_сшитый_документ():
    tpl, html = _doc(RUN)
    assert tpl["id"] == "composed", f"взят бланк одной вертикали: {tpl['id']}"
    # разделы ОБОИХ навыков на месте, каждый со своим заголовком
    assert "Оценка идеи" in html and "Исследование рынка" in html
    assert "навык idea-scorer" in html and "навык market-research" in html
    # содержание из обоих: вердикт оценки и вывод исследования
    assert "доработать" in html and "белое пятно" in html
    # порядок — порядок выполнения
    assert html.index("навык idea-scorer") < html.index("навык market-research")


def test_разделы_не_подсматривают_друг_у_друга():
    """Ссылки пришпилены к своему навыку: иначе графа «вывод» тянула бы чужое значение."""
    tpl, _ = _doc(RUN)
    refs = [r for b in tpl["layout"] for r in report_form.used_refs([b])]
    assert refs, "в сшитой раскладке не осталось ссылок"
    assert all(":" in r.split("|")[0] for r in refs), [r for r in refs if ":" not in r]


def test_один_навык_получает_свой_бланк():
    """Когда вертикаль одна, сшивать нечего — берём согласованный бланк этой вертикали."""
    one = {"run_id": "r-one", "skill_outputs": [RUN["skill_outputs"][1]]}
    tpl, html = _doc(one)
    assert tpl["id"] == "research", tpl["id"]
    assert "Аналитическая записка" in html


def test_названная_форма_не_переигрывается():
    """OUT-узел или человек назвали форму — сшивка не вмешивается."""
    tpl = asyncio.run(web_api._template_for_run(RUN, forced="letter"))
    assert tpl["id"] == "letter"


def test_у_документа_одна_шапка_одна_оговорка_и_подписи():
    tpl, html = _doc(RUN)
    kinds = [b.get("t") for b in tpl["layout"]]
    assert kinds[0] == "head" and kinds.count("head") == 1, kinds
    assert kinds.count("sign") == 1 and kinds.count("note") == 1, kinds
    assert "Подготовил (агентный процесс ABOP)" in html


def test_пришпиливание_не_ломает_альтернативы_и_готовые_привязки():
    assert report_compose.pin("критичность|ценность", "x") == "x:критичность|x:ценность"
    assert report_compose.pin("other:вердикт", "x") == "other:вердикт"
    b = report_compose.pin_block({"t": "attrs", "rows": [["Поле", "a.b"], ["Другое", "c"]]}, "s")
    assert b["rows"] == [["Поле", "s:a.b"], ["Другое", "s:c"]]
