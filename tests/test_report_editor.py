# -*- coding: utf-8 -*-
"""Навык-редактор отчёта: объявляет вид документа, но не его содержание.

Владелец предложил агента-редактора: смотрит на все навыки цепочки и их отчёты, по входной задаче
отбирает результат и собирает один документ из готовых блоков, одновременно сверяя задачу с
результатом. Здесь он как навык — его можно положить на холст, и он появляется в плане как обычный
шаг.

Главное ограничение: редактор отдаёт РАСКЛАДКУ (какие блоки, в каком порядке, с какими ссылками), а
значения в документ подставляет движок — дословно из ответов навыков. Иначе отчёт перестаёт быть
документом записи: два прогона на одних данных давали бы два разных документа.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "ABOP_BUS", "ABOP_KAFKA_BROKERS"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

import asyncio  # noqa: E402

from cli import ape  # noqa: E402
from server import report_compose, report_form, report_store, runner, skill_contract, web_api  # noqa: E402

asyncio.run(report_store.seed_if_empty())

GOOD = [{"t": "part", "title": "Оценка идеи", "sub": "навык idea-scorer"},
        {"t": "verdict", "title": "Решение", "src": "idea-scorer:вердикт",
         "why": "idea-scorer:обоснование_вердикта"},
        {"t": "register", "title": "Оценки", "src": "idea-scorer:оценки",
         "cols": [["Критерий", "критерий"], ["Балл", "балл"]]}]

RUN = {"run_id": "r-ed", "skill_outputs": [
    {"skill": "idea-scorer", "structured": {"вердикт": "доработать",
                                            "обоснование_вердикта": "нет проверки спроса",
                                            "оценки": [{"критерий": "боль", "балл": 4}]}},
    {"skill": "report-editor", "structured": {"раскладка": GOOD,
                                              "сверка_с_задачей": {"покрыто": [], "не_покрыто": [],
                                                                   "вывод": "закрыта частично"},
                                              "итог": "записка по идее"}},
]}


def test_навык_объявлен_в_каталоге_и_только_читает():
    assert "report-editor" in ape.SKILLS
    assert "report-editor" in ape.GLOBAL_SKILLS, "редактор нужен агенту любой семьи"
    sf = ape.skill_safety("report-editor")
    assert sf.get("mode") == "read" and sf.get("egress") == "internal"
    assert ape.skill_datasources("report-editor") == [], "редактор не должен сам ходить в данные"


def test_привилегия_чтения_объявлена_контрактом():
    """«Весь прогон» — единственное чтение без поимённого объявления, и оно видно в контракте."""
    tpl = json.loads((ROOT / "skills" / "report-editor" / "template.json").read_text(encoding="utf-8"))
    froms = [i.get("from") for i in (tpl["inputs"]["required"] or [])]
    assert "run" in froms and "run" in skill_contract.SOURCES
    assert runner._whole_run_block(tpl["inputs"], {"idea-scorer": {"вердикт": "x"}})
    assert runner._whole_run_block({"required": [{"from": "context"}]}, {"idea-scorer": {"a": 1}}) == "", \
        "весь прогон достался навыку, который его не объявлял"


def test_раскладка_редактора_становится_документом():
    tpl = asyncio.run(web_api._template_for_run(RUN))
    assert tpl["id"] == "edited", tpl["id"]
    html = report_store.render(tpl, web_api._report_context({"name": "Проба"}, RUN))
    assert "Оценка идеи" in html and "доработать" in html and "нет проверки спроса" in html
    assert "Подготовил (агентный процесс ABOP)" in html, "рамка документа своя, не от модели"


def test_негодная_раскладка_отбрасывается_и_об_этом_сказано():
    """Придуманный блок, непришпиленная ссылка, ссылка на чужой навык — документ собирается сшивкой."""
    for lay, why in (
        ([{"t": "таблица"}], "неизвестные блоки"),
        ([{"t": "verdict", "src": "вердикт"}], "без имени навыка"),
        ([{"t": "verdict", "src": "кто-то-другой:вердикт"}], "вне прогона"),
        ("не список", "не вернул раскладку"),
    ):
        # Набор навыков здесь свой: у набора из других тестов раскладка уже в кэше, и документ
        # законно собрался бы по кэшу — проверять надо именно отказ от негодной раскладки.
        run = {"skill_outputs": [{"skill": "devils-advocate", "structured": {"вердикт": {"статус": "рискованно"}}},
                                 {"skill": "report-editor", "structured": {"раскладка": lay}}]}
        got, reason = report_compose.editor_layout(run)
        assert got is None and why in reason, (lay, reason)
        tpl = asyncio.run(web_api._template_for_run(dict(run)))
        assert tpl["id"] != "edited", "негодная раскладка всё равно ушла в документ"


def test_отброс_виден_в_отчёте():
    run = {"skill_outputs": [RUN["skill_outputs"][0],
                             {"skill": "report-editor", "structured": {"раскладка": [{"t": "таблица"}]}}]}
    asyncio.run(web_api._template_for_run(run))
    html = report_store.render(asyncio.run(report_store.get("default")),
                               web_api._report_context({"name": "Проба"}, run))
    assert "раскладка редактора отброшена" in html


def test_редактор_не_становится_разделом_документа():
    """Его результат — устройство документа, а не содержание: собственного раздела у него нет."""
    run = {"skill_outputs": [
        RUN["skill_outputs"][0],
        {"skill": "market-research", "structured": {"гипотеза": "спрос есть"}},
        {"skill": "report-editor", "structured": {"раскладка": "мусор"}},
    ]}
    tpl = asyncio.run(web_api._template_for_run(run))
    assert tpl["id"] == "composed"
    parts = [b.get("sub") for b in tpl["layout"] if b.get("t") == "part"]
    assert not any("report-editor" in str(p) for p in parts), parts


def test_блоки_схемы_навыка_совпадают_с_движком():
    """Модель должна выбирать из тех же блоков, которые умеет рендерить движок."""
    tpl = json.loads((ROOT / "skills" / "report-editor" / "template.json").read_text(encoding="utf-8"))
    enum = set(tpl["json_schema"]["properties"]["раскладка"]["items"]["properties"]["t"]["enum"])
    assert enum <= set(report_form.BLOCK_NAMES), enum - set(report_form.BLOCK_NAMES)


def test_блок_канвы_кладёт_навык():
    """На холсте веба блок «Сборка отчёта» — это узел-навык report-editor, а не новый вид узла."""
    src = (ROOT / "webapp" / "src" / "template.html").read_text(encoding="utf-8")
    i = src.index("title: 'Выходы'")
    block = src[i:i + 700]
    assert "skill: 'report-editor'" in block and "Сборка отчёта" in block
    assert "kindDef.skill || kindDef.label" in src, "идентификатор навыка не доедет до узла"


def test_раскладка_кэшируется_по_набору_навыков():
    """Вызов модели нужен на новое сочетание, а не на каждый запуск."""
    run = {"run_id": "r-cache", "skill_outputs": [
        {"skill": "cost-estimator", "structured": {"итого_per_flow": {"стоимость": "12 ₽"}}},
        {"skill": "report-editor", "structured": {"раскладка": [
            {"t": "attrs", "title": "Стоимость", "rows": [["Per flow", "cost-estimator:итого_per_flow.стоимость"]]}]}},
    ]}
    cid = report_compose.cache_id(["cost-estimator"])
    assert cid.startswith("auto-")
    assert asyncio.run(report_store.get(cid)) is None, "кэш уже заполнен — тест потерял смысл"
    assert asyncio.run(web_api._template_for_run(run))["id"] == "edited"
    saved = asyncio.run(report_store.get(cid))
    assert saved and saved.get("layout"), "раскладка не попала в кэш"
    # следующий прогон того же набора — уже без редактора в графе
    again = {"run_id": "r-cache-2", "skill_outputs": [run["skill_outputs"][0]]}
    tpl = asyncio.run(web_api._template_for_run(again))
    assert tpl["id"] == "edited", "кэш не применился, документ собрался заново"


def test_кэш_не_спорит_с_бланками_вертикалей():
    """Форма-кэш сохранена под сочетание навыков и в подборе по навыку участвовать не должна."""
    asyncio.run(report_store.save("auto-пример", {"name": "кэш", "html": "x", "layout": [{"t": "note", "text": "н"}],
                                                  "for_skills": ["idea-scorer"]}, editor="report-editor"))
    assert asyncio.run(report_store.template_for_skills(["idea-scorer"])) == "decision"
