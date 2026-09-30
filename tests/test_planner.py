"""Этап 8: цепочка собирается по контрактам, а не по похожести слов.

Подбор по словарю отвечает на вопрос «кто похож», но не на вопрос «а сможет ли этот кто-то
выполниться». Навык может требовать сущность, которой в среде нет, или выход другого навыка, который
никто не поставит, — и это выясняется посреди прогона, когда деньги уже потрачены.
"""
from __future__ import annotations

from server import planner

CATALOG = {
    "roadmap-fact": {
        "title": "Сверка плана и факта", "short": "дорожная карта против отчётов подрядчиков",
        "body": "сверь дорожную карту проекта с отчётами подрядчиков: сроки, часы, причины срыва",
        "inputs": {"required": [{"from": "slot", "name": "проект"},
                                {"from": "data", "entity": "roadmap_item"},
                                {"from": "data", "entity": "contractor_report"}]},
        "produces": [{"path": "исполнение", "key": "пункт"}, {"path": "решения", "key": "что_сделать"}],
    },
    "acceptance-check": {
        "title": "Приёмка заказчика", "short": "решения комиссии по пунктам проекта",
        "body": "картина приёмки: принято, с замечаниями, не принято",
        "inputs": {"required": [{"from": "slot", "name": "проект"},
                                {"from": "data", "entity": "acceptance"}]},
        "produces": [{"path": "исполнение", "key": "пункт"}],
    },
    "to-tickets": {
        "title": "Задачи в трекер", "short": "решения превращаются в задачи",
        "body": "из решений сделай задачи трекера с исполнителем и сроком",
        "inputs": {"required": [{"from": "skill", "skill": "roadmap-fact", "path": "решения"}]},
        "produces": [{"path": "задачи", "key": "id"}],
    },
    "audit1c-checks": {
        "title": "Проверки 1С", "short": "детерминированные проверки проводок",
        "body": "проверь проводки, счета-фактуры, НСИ и внутригрупповые обороты",
        "inputs": {"required": [{"from": "data", "entity": "doc1c"}, {"from": "data", "entity": "ref1c"}]},
        "produces": [{"path": "находки", "key": "id"}],
    },
    "need-missing": {
        "title": "Навык на пустой сущности", "short": "требует данных, которых нет",
        "body": "проект отчёты сроки",
        "inputs": {"required": [{"from": "data", "entity": "нет_такой_сущности"}]},
        "produces": [{"path": "что-то", "key": "id"}],
    },
}
ENTS = {"roadmap_item", "contractor_report", "acceptance", "doc1c", "ref1c"}


def test_picks_executable_skill_for_the_task():
    p = planner.plan("сверь дорожную карту проекта с отчётами подрядчиков", CATALOG,
                     entities=ENTS, slots={"проект"})
    assert p["ok"]
    assert p["steps"][0]["skill"] == "roadmap-fact"
    assert p["waves"] and p["waves"][0] == ["roadmap-fact"]


def test_skill_without_data_is_not_taken():
    """Навык, которому нечем работать, в план не попадает — и это сказано вслух."""
    p = planner.plan("сверь пункты проекта с отчётами подрядчиков по срокам и часам", CATALOG,
                     entities={"roadmap_item"}, slots={"проект"})
    ids = [s["skill"] for s in p["steps"]]
    assert "need-missing" not in ids
    assert any("нет данных сущности" in m for m in p["missing"])


def test_dependency_is_built_before_the_step_that_needs_it():
    """Навык, требующий выход другого, тянет поставщика за собой и встаёт следующей волной."""
    p = planner.plan("нарежь тикеты по решениям сверки плана и факта", CATALOG,
                     entities=ENTS, slots={"проект"})
    ids = [s["skill"] for s in p["steps"]]
    assert "to-tickets" in ids and "roadmap-fact" in ids
    assert ids.index("roadmap-fact") < ids.index("to-tickets")
    assert p["waves"][0] == ["roadmap-fact"] and "to-tickets" in p["waves"][1]


def test_unknown_subject_is_asked_not_invented():
    p = planner.plan("сверь дорожную карту проекта с отчётами подрядчиков по срокам", CATALOG,
                     entities=ENTS, slots=set())
    assert p["ok"] and p["ask_slots"] == ["проект"]
    assert "уточнить предмет" in p["note"]


def test_no_executable_chain_is_said_honestly():
    """Подходящие навыки есть, но данных нет: план не собирается и объясняет почему."""
    p = planner.plan("сверь дорожную карту проекта с отчётами подрядчиков", CATALOG,
                     entities=set(), slots={"проект"})
    assert not p["ok"] and p["steps"] == []
    assert p["missing"] and "нет данных" in p["missing"][0]


def test_task_outside_catalog_gets_no_plan():
    p = planner.plan("свари кофе покрепче и вынеси мусор во двор пожалуйста", CATALOG,
                     entities=ENTS, slots=set())
    assert not p["ok"] and "исполнителя" in p["note"]


def test_report_template_follows_the_result_shape():
    audit = planner.plan("проверь проводки и счета-фактуры в 1С на расхождения", CATALOG,
                       entities=ENTS, slots=set())
    assert planner.report_template(audit["steps"], CATALOG) == "audit1c"
    tickets = planner.plan("нарежь тикеты по решениям сверки плана и факта", CATALOG,
                           entities=ENTS, slots={"проект"})
    assert planner.report_template(tickets["steps"], CATALOG) == "digest"


def test_plan_is_bounded():
    """План не разрастается: шагов не больше заданного, глубина достройки ограничена."""
    p = planner.plan("сверь пункты проекта с отчётами подрядчиков, проверь приёмку и сделай задачи",
                     CATALOG, entities=ENTS, slots={"проект"}, max_steps=2)
    assert len(p["steps"]) <= 3   # два по похожести плюс возможный поставщик зависимости


# ── Правило достаточности запроса ────────────────────────────────────────────────────────────

def test_short_phrase_is_not_guessed():
    """По двум словам подходит десяток навыков: уверенный выбор из них — обман."""
    r = planner.sufficiency("сделай отчёт", CATALOG)
    assert not r["ok"] and r["вопросы"]
    p = planner.plan("сделай отчёт", CATALOG, entities=ENTS, slots={"проект"})
    assert not p["ok"] and p.get("need_more") and p["sufficiency"]["вопросы"]


def test_questions_point_at_what_is_missing():
    """Спрашиваем ровно о недостающем, а не «уточните запрос»."""
    no_action = planner.sufficiency("отчёт по проекту", CATALOG)
    assert any("что нужно сделать" in q.lower() for q in no_action["вопросы"])
    no_subject = planner.sufficiency("сверь и покажи", CATALOG)
    assert any("по чему работаем" in q.lower() for q in no_subject["вопросы"])


def test_long_phrase_with_known_words_is_enough():
    """Длинная фраза с узнаваемыми словами каталога проходит даже без формальных пяти слов."""
    long_task = ("нужно посмотреть дорожную карту и отчёты подрядчиков по проекту, "
                 "чтобы понять положение дел со сроками на сегодня")
    assert planner.sufficiency(long_task, CATALOG)["ok"]


def test_normal_phrase_passes():
    assert planner.sufficiency("сверь дорожную карту проекта с отчётами подрядчиков", CATALOG)["ok"]


# ── Названный источник данных ───────────────────────────────────────────────────────────────

def test_named_source_outweighs_word_similarity():
    """«в 1С» ведёт к навыку, который читает 1С, даже если по словам похож другой.

    Живой случай: «проверь проводки и счета-фактуры в 1С на расхождения» давало сверку регистров —
    по словам методики она тоже про проводки и расхождения, но читает другую сущность.
    """
    cat = dict(CATALOG)
    cat["ledger-recon"] = {
        "title": "Сверка регистров", "short": "проводки и расхождения",
        "body": "сверь проводки, счета, суммы и расхождения по регистрам учёта, счета-фактуры тоже",
        "inputs": {"required": [{"from": "data", "entity": "transaction"}]},
        "produces": [{"path": "расхождения", "key": "id"}],
    }
    ents = ENTS | {"transaction"}
    p = planner.plan("проверь проводки и счета-фактуры в 1С на расхождения", cat, entities=ents, slots=set())
    assert p["ok"] and p["steps"][0]["skill"] == "audit1c-checks", [s["skill"] for s in p["steps"]]

    # без упоминания источника выигрывает тот, кто похож по словам — прибавка не должна быть вечной
    p2 = planner.plan("сверь проводки и счета по регистрам, покажи расхождения", cat, entities=ents, slots=set())
    assert p2["steps"][0]["skill"] == "ledger-recon", [s["skill"] for s in p2["steps"]]


def test_named_source_needs_the_data_to_exist():
    """Названный источник без данных навык не спасает: план не берёт неисполнимое."""
    p = planner.plan("проверь проводки и счета-фактуры в 1С на расхождения", CATALOG,
                     entities={"roadmap_item"}, slots=set())
    assert "audit1c-checks" not in [s["skill"] for s in p.get("steps") or []]
