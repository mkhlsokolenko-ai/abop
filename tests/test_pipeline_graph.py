"""Этап 4: ветвление цепочек, условия запуска и слияние ветвей.

До этого цепочка была плоским списком, между шагами ехала проза с обрезкой, а одна упавшая ветвь
обнуляла бы результат, если бы ветви вообще существовали.
"""
from __future__ import annotations

from server import pipeline_graph as pg


def test_flat_pipeline_stays_linear():
    """Плоский список без зависимостей исполняется как раньше: шаг за шагом."""
    flat = [{"agent_id": "a"}, {"agent_id": "b"}, {"agent_id": "c"}]
    assert [[s["id"] for s in w] for w in pg.waves(flat)] == [["s1"], ["s2"], ["s3"]]
    assert pg.validate(flat) == []


def test_branch_and_join_waves():
    """Две ветви идут одной волной, слияние — следующей."""
    g = [{"id": "s1", "agent_id": "a"},
         {"id": "s2", "agent_id": "b", "after": ["s1"]},
         {"id": "s3", "agent_id": "c", "after": ["s1"]},
         {"id": "m", "join": {"policy": "all", "from": ["s2", "s3"]}, "after": ["s2", "s3"]}]
    assert [[s["id"] for s in w] for w in pg.waves(g)] == [["s1"], ["s2", "s3"], ["m"]]
    assert pg.validate(g) == []


def test_validation_catches_broken_graph():
    assert any("несуществующий" in e for e in pg.validate(
        [{"id": "s1", "agent_id": "a"}, {"id": "s2", "agent_id": "b", "after": ["нет-такого"]}]))
    assert any("цикл" in e for e in pg.validate(
        [{"id": "s1", "agent_id": "a", "after": ["s2"]}, {"id": "s2", "agent_id": "b", "after": ["s1"]}]))
    assert any("политика" in e for e in pg.validate(
        [{"id": "s1", "agent_id": "a"}, {"id": "m", "join": {"policy": "как-нибудь", "from": ["s1"]}, "after": ["s1"]}]))
    assert any("ключ" in e or "key" in e for e in pg.validate(
        [{"id": "s1", "agent_id": "a"}, {"id": "m", "join": {"policy": "by_key", "from": ["s1"]}, "after": ["s1"]}]))


def test_conditions():
    res = {"s1": {"data": {"находки": [{"id": "A"}, {"id": "B"}], "итог": "есть"}}}
    assert pg.should_run({"when": {"has": "s1.находки"}}, res)[0]
    assert not pg.should_run({"when": {"empty": "s1.находки"}}, res)[0]
    assert pg.should_run({"when": {"min_count": {"path": "s1.находки", "n": 2}}}, res)[0]
    ok, why = pg.should_run({"when": {"min_count": {"path": "s1.находки", "n": 5}}}, res)
    assert not ok and "нужно 5" in why
    assert pg.should_run({"when": {"eq": {"path": "s1.итог", "value": "есть"}}}, res)[0]
    assert not pg.should_run({"when": {"eq": {"path": "s1.итог", "value": "нет"}}}, res)[0]
    assert pg.should_run({}, res)[0], "без условия шаг запускается"


def test_merge_all_marks_branch():
    res = {"s2": {"data": {"пункты": [{"id": "RM-01"}]}}, "s3": {"data": {"пункты": [{"id": "RM-02"}]}}}
    m = pg.merge({"policy": "all", "from": ["s2", "s3"]}, res)
    rows = m["data"]["пункты"]
    assert len(rows) == 2
    assert {r["_ветвь"] for r in rows} == {"s2", "s3"}, "видно, из какой ветви запись"


def test_merge_by_key_joins_records():
    res = {"s2": {"data": {"исполнение": [{"пункт": "RM-05", "статус": "просрочено"}]}},
           "s3": {"data": {"исполнение": [{"пункт": "RM-05", "часы_факт": 690}, {"пункт": "RM-07"}]}}}
    m = pg.merge({"policy": "by_key", "from": ["s2", "s3"], "key": "пункт"}, res)
    rows = {r["пункт"]: r for r in m["data"]["сведено"]}
    assert set(rows) == {"RM-05", "RM-07"}
    assert rows["RM-05"]["статус"] == "просрочено" and rows["RM-05"]["часы_факт"] == 690, "поля ветвей объединены"
    assert rows["RM-05"]["_ветви"] == ["s2", "s3"]


def test_merge_vote_shows_disagreement():
    res = {"s2": {"data": {"вывод": [{"успеваем": "нет"}]}},
           "s3": {"data": {"вывод": [{"успеваем": "да"}]}}}
    m = pg.merge({"policy": "vote", "from": ["s2", "s3"], "key": "успеваем"}, res)
    assert "расхождение" in m["note"]
    assert {r["значение"] for r in m["data"]["голосование"]} == {"нет", "да"}


def test_failed_branch_does_not_break_merge():
    """Упавшая ветвь не обнуляет слияние, а попадает в список пропущенных."""
    res = {"s2": {"data": {"пункты": [{"id": "RM-01"}]}}}     # s3 упал, результата нет
    m = pg.merge({"policy": "all", "from": ["s2", "s3"]}, res)
    assert m["missing"] == ["s3"]
    assert len(m["data"]["пункты"]) == 1


def test_step_input_passes_structure_not_prose():
    res = {"s1": {"data": {"находки": [{"id": "A1", "сумма": 100}]}, "agent_name": "Аудитор"},
           "s2": {"data": {"чужое": [1]}}}
    body = pg.step_input({"id": "s3", "after": ["s1"]}, res)
    assert "РЕЗУЛЬТАТ ШАГА «s1»" in body and "Аудитор" in body
    assert "A1" in body and '"сумма": 100' in body, "структура едет как есть, а не пересказом"
    assert "чужое" not in body, "берём только шаги из after"
