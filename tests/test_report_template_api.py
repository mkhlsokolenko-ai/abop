# -*- coding: utf-8 -*-
"""Правка формы в интерфейсе не должна стирать устройство документа.

В редакторе шаблонов видно HTML, а у бланка вертикали в HTML всего одна строка `{{blank}}`: вид
задаёт раскладка. Пока сохранение не переносило раскладку, нажатие «Сохранить» в интерфейсе
уничтожало документ — оставался пустой лист. И превью: без данных прогона бланку нечего заполнять,
поэтому форма выглядела неизменившейся — ровно так это и выглядело у владельца.
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

from fastapi.testclient import TestClient  # noqa: E402

from server import report_store, web_api  # noqa: E402

CLIENT = TestClient(web_api.app)
LAYOUT = [{"t": "head", "title": "Проба", "note": "метод", "meta": [["Заявка", "заявка.id"]]},
          {"t": "attrs", "title": "Реквизиты", "rows": [["Заявка", "заявка.id"]]}]


def _seed(tid="t-api", layout=LAYOUT, skills=("limit_policy_enforcement",)):
    import asyncio
    asyncio.run(report_store.save(tid, {"name": "Проба", "html": "{{blank}}", "layout": layout,
                                       "for_skills": list(skills)}, editor="seed", builtin=True))


def test_сохранение_без_раскладки_её_не_стирает():
    _seed()
    r = CLIENT.post("/api/report-templates/t-api", json={"name": "Проба", "html": "{{blank}}"})
    assert r.status_code == 200, r.text
    assert len(r.json().get("layout") or []) == 2, "раскладка потеряна при сохранении из редактора"
    assert r.json().get("for_skills") == ["limit_policy_enforcement"], "область применения потеряна"


def test_раскладка_с_неизвестным_блоком_не_сохраняется():
    _seed()
    r = CLIENT.post("/api/report-templates/t-api",
                    json={"html": "{{blank}}", "layout": [{"t": "head", "title": "x"}, {"t": "таблица"}]})
    assert r.status_code == 422
    assert "таблица" in r.text
    r2 = CLIENT.post("/api/report-templates/t-api", json={"html": "{{blank}}", "layout": "не список"})
    assert r2.status_code == 422


def test_превью_бланка_показывает_документ_а_не_общий_вид():
    """Нет прогона — превью собирается по json_schema навыков формы и так и подписано."""
    _seed()
    r = CLIENT.post("/api/report-templates/t-api/preview", json={"context": {}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert "Проба" in d["html"] and "Реквизиты" in d["html"], "бланк в превью не собрался"
    assert "ОБРАЗЦЕ" in d["note"] or "прогоне" in d["note"], d["note"]


def test_превью_общей_формы_осталось_на_демо_строках():
    """У формы без раскладки превью как было: демо-находки, ничего не ломаем."""
    import asyncio
    asyncio.run(report_store.save("t-plain", {"name": "Общая", "html": "<body>{{findings}}</body>"},
                                  editor="seed", builtin=True))
    r = CLIENT.post("/api/report-templates/t-plain/preview", json={"context": {}})
    assert r.status_code == 200
    assert "НДС не сходится" in r.json()["html"]
    assert not r.json().get("note")


def test_план_предлагает_бланк_вертикали():
    """План в «Строю» ставит OUT-узлу форму: у навыка с бланком — его, а не общий дайджест.

    Правило по виду результата («есть путь „задачи“ → дайджест») не знает про бланки вертикалей, и
    собранный агент получал общую форму вместо кредитного заключения или протокола поручений.
    """
    import asyncio
    asyncio.run(report_store.save("t-vert", {"name": "Бланк для to-tickets", "html": "{{blank}}",
                                             "layout": LAYOUT, "for_skills": ["to-tickets"]},
                                  editor="seed", builtin=True))
    r = CLIENT.post("/api/plan/auto", json={"task": "нарежь задачи в трекере по решениям сверки плана и факта",
                                            "slots": {"проект": "PRJ-1"}})
    assert r.status_code == 200, r.text
    d = r.json()
    steps = [s.get("skill") for s in (d.get("steps") or [])]
    if "to-tickets" in steps:
        assert d.get("report_template") == "t-vert", (steps, d.get("report_template"))
    else:                      # подбор навыка — отдельный разговор; форму проверяем напрямую
        got = asyncio.run(report_store.template_for_skills(["to-tickets"]))
        assert got == "t-vert", got
