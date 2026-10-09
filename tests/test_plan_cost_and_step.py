# -*- coding: utf-8 -*-
"""Долг подбора из разбора 05.10 — оставшиеся три пункта (п.22 исследования 08.10).

Разбор 05.10 назвал шесть слабостей. Три из них (продолжение цепочки по необязательным
контрактам, эмбеддинги навыков, обучение на исходе подбора) были закрыты сразу после него, и
исследование 08.10 ошибочно перечислило их как открытые — список скопировали, не перепроверив.
Здесь — настоящие три: предпросмотр стоимости, прогон одного шага и источники шага.

Общее у всех трёх: они отвечают на вопрос «что будет / что было», и соблазн в каждом один и тот
же — ответить правдоподобно вместо того, чтобы ответить точно или промолчать.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from server import cost_estimate as ce  # noqa: E402
from server import planner, web_api  # noqa: E402

ТАРИФ = (lambda model, i, o: round((i + o) / 1000 * 2.0, 4))


def _прогон(*шаги) -> dict:
    return {"findings": [{"skill": s, "model": "m", "input_tokens": i, "output_tokens": o}
                         for s, i, o in шаги]}


# ── предпросмотр стоимости ───────────────────────────────────────────────────────────────────────

def test_оценка_считается_по_собственным_прогонам():
    """Выдумывать коэффициенты нельзя: журнал знает, сколько навык ест на самом деле."""
    h = ce.наблюдения([_прогон(("a", 1000, 200)), _прогон(("a", 1400, 300))])
    assert h["a"] == {"input": 1200, "output": 250, "runs": 2}
    o = ce.оценить(steps=["a"], history=h, model="m", цена=ТАРИФ)
    assert o["rows"][0]["basis"] == "по прогонам" and o["rows"][0]["runs"] == 2
    assert o["partial"] is False
    assert "по собственным прогонам" in o["note"]


def test_навык_без_истории_помечен_а_не_выдуман():
    h = ce.наблюдения([_прогон(("a", 1000, 200))])
    o = ce.оценить(steps=["a", "новый"], history=h, model="m", цена=ТАРИФ)
    строка = [r for r in o["rows"] if r["skill"] == "новый"][0]
    assert строка["basis"] == "по каталогу" and строка["runs"] == 0
    assert o["partial"] is True, "оценка с догадкой обязана быть помечена неполной"
    assert "загрубее" in o["note"]


def test_без_истории_вовсе_оценка_не_выдаётся():
    """Молчание честнее правдоподобного числа — то же правило, что у DoD на пустом стенде."""
    o = ce.оценить(steps=["x", "y"], history={}, model="m", цена=ТАРИФ)
    assert all(r["rub"] is None and r["basis"] == "нет данных" for r in o["rows"])
    assert o["unknown"] == ["x", "y"] and o["partial"] is True
    assert "выдумывать" in o["note"]


def test_шаги_без_модели_не_занижают_медиану():
    """Пропущенный и детерминированный шаг ничего не стоят; в статистике им не место."""
    h = ce.наблюдения([{"findings": [{"skill": "a", "model": "", "input_tokens": 0},
                                     {"skill": "a", "model": "m", "input_tokens": 2000,
                                      "output_tokens": 400}]}])
    assert h["a"]["input"] == 2000 and h["a"]["runs"] == 1


def test_сумма_сходится_со_строками():
    h = ce.наблюдения([_прогон(("a", 1000, 100), ("b", 2000, 200))])
    o = ce.оценить(steps=["a", "b"], history=h, model="m", цена=ТАРИФ)
    assert o["input_tokens"] == 3000 and o["output_tokens"] == 300
    assert o["rub"] == ТАРИФ("m", 3000, 300)


def test_эндпоинт_оценки_существует():
    пути = {getattr(r, "path", "") for r in web_api.app.routes}
    assert "/api/plan/estimate" in пути


# ── прогон одного шага ───────────────────────────────────────────────────────────────────────────

def test_один_шаг_идёт_тем_же_исполнителем():
    """Второй путь исполнения разошёлся бы с основным, и отладка по нему стала бы враньём."""
    from http_layer import исходник
    исходник = исходник()
    блок = исходник[исходник.index("async def agent_run_step("):][:2600]
    assert "execute_agent_run(" in блок, "пробный шаг исполняется своим кодом"
    assert "use_cache=False" in блок, "пробный шаг может вернуть кэш — это не проверка"
    assert 'deliver_filter="none"' in блок, "пробный шаг способен отправить наружу"
    assert "step-test" in блок, "пробный шаг неотличим в журнале от настоящего"


def test_пробный_шаг_не_подтверждает_участок():
    """op_ids у него нет: иначе отладкой можно было бы «подтвердить» операцию карты."""
    from http_layer import исходник
    исходник = исходник()
    начало = исходник.index("async def agent_run_step(")
    # После докстринга: в нём op_ids упомянут как раз затем, чтобы объяснить, почему их нет.
    тело = исходник[исходник.index(chr(34) * 3, исходник.index(chr(34) * 3, начало) + 3) + 3:][:2400]
    assert "op_ids" not in тело, "пробный шаг пишет op_ids и подтверждает карту"


# ── источники шага ───────────────────────────────────────────────────────────────────────────────

def test_источники_собраны_по_шагам():
    """«А он вообще мой файл прочитал?» — вопрос, на который карточка прогона не отвечала."""
    прогон = {"findings": [{"skill": "a", "model": "m", "entities": ["doc1c", "ref1c"],
                            "input_tokens": 10, "output_tokens": 2},
                           {"skill": "b", "model": "", "skipped": True}],
              "run_metrics": {"documents": [{"name": "акт.pdf", "ok": True},
                                            {"name": "битый.pdf", "ok": False}],
                              "data_snapshot": {"doc1c": {"records": 107}}}}
    s = web_api.шаги_с_источниками(прогон)
    первый = s[0]
    assert первый["entities"] == ["doc1c", "ref1c"]
    assert первый["records"]["doc1c"] == 107, "не видно, сколько записей дошло"
    assert первый["documents"] == ["акт.pdf"], "битый документ показан как дошедший"
    assert s[1]["skipped"] is True and s[1]["documents"] == []


def test_источники_доезжают_до_карточки_прогона():
    from http_layer import исходник
    исходник = исходник()
    блок = исходник[исходник.index("async def run_get("):][:800]
    assert "step_sources" in блок, "карточка прогона снова без источников"


# ── три пункта, которые уже были закрыты: сторожим, чтобы не откатились ─────────────────────────

def test_цепочка_продолжается_по_необязательным_связям():
    """Главный долг подбора по разбору 05.10 — и он был закрыт сразу после него."""
    исходник = (КОРЕНЬ / "server" / "planner.py").read_text(encoding="utf-8")
    assert "_continuations" in исходник and "CONTINUE_MIN" in исходник
    assert "_accepts(" in исходник, "необязательные входы снова не участвуют в сборке"


def test_подбор_умеет_смысл_и_учится_на_исходе():
    assert "semantic" in planner.plan.__doc__ or "hints" in planner.plan.__doc__
    from http_layer import исходник
    исходник = исходник()
    assert "_SKILL_EMB_CACHE" in исходник, "эмбеддинги навыков пропали"
    assert "choice_store.prefer" in исходник, "обучение на исходе подбора пропало"
