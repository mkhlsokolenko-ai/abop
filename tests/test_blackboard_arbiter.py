"""Этап 7: общая память прогона, снимок данных и арбитраж расхождений.

До этого ветви обменивались результатами только по рёбрам графа, а расхождение между ними решалось
молча — в слиянии выживало значение последней записавшей ветви. Человек получал одну цифру и никакого
следа спора.
"""
from __future__ import annotations

import asyncio
import json

from server import arbiter, blackboard as bb, runner


# ── Доска: авторство, чтение по контракту, расхождения ───────────────────────────────────────

def test_board_keeps_author_and_reads_only_declared():
    b = bb.Board("r1")
    b.put("исполнение", [{"пункт": "RM-01"}], author="навык-а")
    b.put("риски", ["срыв срока"], author="навык-б")
    inputs = {"required": [{"from": "board", "key": "исполнение"}]}
    got = b.read(inputs)
    assert "исполнение" in got, "объявленный ключ читается"
    assert "риски" not in got, "необъявленное не отдаём: «всё всем» топит навык в чужих выводах"
    assert b.claims("исполнение")[0]["author"] == "навык-а"


def test_same_author_does_not_argue_with_itself():
    b = bb.Board()
    b.put("итог", "1", author="а")
    b.put("итог", "2", author="а")
    assert len(b.claims("итог")) == 1 and b.value("итог") == "2"
    assert not b.contradictions(), "повтор одного автора — не спор"


def test_contradiction_points_at_one_record_not_whole_list():
    """Ветви разошлись в одном пункте из двух — расхождение должно указывать именно на него."""
    b = bb.Board("grp")
    b.put("исполнение", [{"проект": "P1", "пункт": "RM-01", "статус": "просрочен"},
                         {"проект": "P1", "пункт": "RM-02", "статус": "выполнен"}], author="подрядчик")
    b.put("исполнение", [{"проект": "P1", "пункт": "RM-01", "статус": "в работе"},
                         {"проект": "P1", "пункт": "RM-02", "статус": "выполнен"}], author="учёт")
    c = b.contradictions()
    assert len(c) == 1, c
    assert c[0]["item"] == "P1 · RM-01"
    assert c[0]["fields"] == ["статус"]
    assert len(c[0]["variants"]) == 2


def test_record_without_identity_is_not_a_conflict():
    """Без идентификатора нельзя утверждать, что ветви говорят об одном и том же."""
    b = bb.Board()
    b.put("причины", [{"текст": "поставка задержана"}], author="а")
    b.put("причины", [{"текст": "подрядчик не вышел"}], author="б")
    assert b.contradictions() == []


def test_complementary_records_are_not_a_conflict():
    """Две стороны описывают один пункт разными полями — это дополнение, а не спор."""
    b = bb.Board()
    b.put("исполнение", [{"пункт": "RM-07", "статус": "просрочено", "причина_срыва": "нет доступа"}],
          author="подрядчик")
    b.put("исполнение", [{"пункт": "RM-07", "статус": "просрочено", "замечание": "работы не предъявлены"}],
          author="приёмка")
    assert b.contradictions() == [], "общие поля совпадают — расхождения нет"


def test_missing_board_key_blocks_skill():
    inputs = {"required": [{"from": "board", "key": "исполнение"}]}
    assert bb.missing_board(inputs, bb.Board()) == ["доска:исполнение"]
    b = bb.Board()
    b.put("исполнение", [{"пункт": "RM-01"}], author="а")
    assert bb.missing_board(inputs, b) == []


def test_board_block_shows_disagreement_to_the_skill():
    b = bb.Board()
    b.put("статус", "просрочен", author="а")
    b.put("статус", "в работе", author="б")
    txt = b.block({"optional": [{"from": "board", "key": "статус"}]})
    assert "ОБЩАЯ ПАМЯТЬ" in txt and "ветви разошлись" in txt


# ── Снимок данных ────────────────────────────────────────────────────────────────────────────

def test_snapshot_is_order_independent_and_catches_change():
    a = bb.snapshot({"doc1c": [{"id": 1}, {"id": 2}]})
    b = bb.snapshot({"doc1c": [{"id": 2}, {"id": 1}]})
    assert a["entities"]["doc1c"]["hash"] == b["entities"]["doc1c"]["hash"], "порядок строк не картина"
    assert bb.drift(a, {"doc1c": [{"id": 1}, {"id": 2}]}) == []
    d = bb.drift(a, {"doc1c": [{"id": 1}]})
    assert d and "состав изменился" in d[0]


# ── Арбитраж ─────────────────────────────────────────────────────────────────────────────────

def _conflict(field, va, vb, extra_a=None):
    a = {"пункт": "RM-01", field: va}
    a.update(extra_a or {})
    return {"key": "исполнение", "item": "RM-01", "fields": [field],
            "variants": [{"value": a, "authors": ["подрядчик"]},
                         {"value": {"пункт": "RM-01", field: vb}, "authors": ["учёт"]}]}


def test_status_conflict_resolves_towards_the_problem():
    d = arbiter.resolve(_conflict("статус", "просрочен", "в работе"))
    assert d["by"] == "rule:severity" and d["chosen"] == "просрочен"
    assert not d["needs_human"] and "в сторону проблемы" in d["reason"]


def test_numbers_resolve_by_the_larger_value():
    d = arbiter.resolve(_conflict("часы", 40, 25))
    assert d["by"] == "rule:edge" and d["chosen"] == 40


def test_majority_wins_before_other_rules():
    c = {"key": "итог", "item": "RM-01", "fields": ["оценка"],
         "variants": [{"value": {"пункт": "RM-01", "оценка": "низкая"}, "authors": ["а", "б"]},
                      {"value": {"пункт": "RM-01", "оценка": "высокая"}, "authors": ["в"]}]}
    d = arbiter.resolve(c)
    assert d["by"] == "rule:majority" and d["chosen"] == "низкая"


def test_deadline_conflict_goes_to_human():
    """Срок и деньги меняют смысл результата — правилом такое не закрывают."""
    d = arbiter.resolve(_conflict("срок", "2026-11-01", "2027-02-15"))
    assert d["needs_human"] and d["by"] == "human" and d["chosen"] is None


def test_unresolvable_conflict_stays_open():
    d = arbiter.resolve(_conflict("примечание", "поставка", "подрядчик"))
    assert d["needs_human"], "ни одно правило не различает варианты — это видно, а не спрятано"


def test_disagreement_in_many_fields_goes_to_human():
    c = {"key": "исполнение", "item": "RM-01", "fields": ["статус", "часы"],
         "variants": [{"value": {"статус": "просрочен", "часы": 40}, "authors": ["а"]},
                      {"value": {"статус": "в работе", "часы": 25}, "authors": ["б"]}]}
    assert arbiter.resolve(c)["needs_human"]


def test_decisions_land_in_rows_and_report():
    b = bb.Board("grp")
    b.put("исполнение", [{"проект": "P1", "пункт": "RM-01", "статус": "просрочен"}], author="подрядчик")
    b.put("исполнение", [{"проект": "P1", "пункт": "RM-01", "статус": "выполнен"}], author="учёт")
    dec = arbiter.resolve_all(b.contradictions())
    rows = [{"проект": "P1", "пункт": "RM-01", "статус": "выполнен"}]
    assert arbiter.apply_to_rows(rows, dec) == 1
    assert rows[0]["статус"] == "просрочен", "решение арбитра попадает в свод"
    assert rows[0]["_арбитраж"]["статус"]["кем"] == "rule:severity"
    rep = arbiter.report(dec)
    assert rep["total"] == 1 and rep["by_rule"] == 1 and rep["needs_human"] == 0


def test_open_conflict_is_visible_in_rows():
    dec = [arbiter.resolve(_conflict("срок", "2026-11-01", "2027-02-15"))]
    rows = [{"пункт": "RM-01", "срок": "2027-02-15"}]
    arbiter.apply_to_rows(rows, dec)
    assert rows[0]["_арбитраж"]["срок"]["решение"] == "ждёт человека"
    assert rows[0]["срок"] == "2027-02-15", "нерешённое расхождение не подчищаем молча"


def test_arbiter_skill_answer_must_match_a_variant():
    """Модель «примиряет» варианты новым значением — такого в результате быть не должно."""
    c = _conflict("примечание", "поставка", "подрядчик")

    async def invented(_task):
        return {"text": '{"вариант": "оба", "обоснование": "истина посередине"}'}

    d = asyncio.run(arbiter.resolve_by_skill(c, invented))
    assert d["needs_human"] and d["chosen"] is None

    async def picks(_task):
        return {"text": '{"вариант": 2, "обоснование": "подтверждено актом"}'}

    d2 = asyncio.run(arbiter.resolve_by_skill(c, picks))
    assert d2["by"].startswith("skill:") and d2["chosen"] == "подрядчик"


def test_arbiter_skill_failure_does_not_break_the_run():
    async def broken(_task):
        raise RuntimeError("модель недоступна")

    d = asyncio.run(arbiter.resolve_by_skill(_conflict("примечание", "a", "b"), broken))
    assert d["needs_human"] and "недоступен" in d["reason"]


# ── Прогон целиком ───────────────────────────────────────────────────────────────────────────

def _run(chat_fn, *, board=None, snapshot=None, rows=None, schemas=None):
    agent = {"id": "ag-7", "name": "Доска", "family": "audit",
             "graph": {"nodes": [{"id": "n1", "kind": "skill", "skill": "s1"},
                                 {"id": "n2", "kind": "skill", "skill": "s2"}], "edges": []}}
    contract = {"audit_id": "t-7", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    return asyncio.run(runner.run_live(
        agent, contract, lambda sid: {"mode": "read", "egress": "internal", "cite": False},
        data_query=lambda e, **kw: list(rows if rows is not None else [{"id": "d1", "тип": "Реализация"}]),
        skill_sources=lambda sid: [{"entity": "doc1c"}],
        load_body=lambda sid: "методика " + sid,
        chat_fn=chat_fn, skill_schemas=schemas or {}, user_context="задача",
        board=board, data_snapshot=snapshot))


def test_run_puts_outputs_on_the_board_and_reports_it():
    async def chat(messages=None, **kw):
        return {"text": json.dumps({"находки": [{"id": "A"}], "итог": "ок"}, ensure_ascii=False),
                "model": "local/test", "input_tokens": 10, "output_tokens": 10}

    res = _run(chat)
    brd = (res.get("run_metrics") or {}).get("board") or {}
    assert brd.get("entries"), "выходы навыков попадают в общую память"
    assert set(brd.get("authors") or []) == {"s1", "s2"}
    assert res.get("board_entries"), "доска уезжает наружу вместе с прогоном"


def test_run_arbitrates_contradiction_between_skills():
    """Два навыка дали разный статус по одному пункту — в результате явное расхождение и решение."""
    async def chat(messages=None, **kw):
        txt = json.dumps({"исполнение": [{"проект": "P1", "пункт": "RM-01", "статус": "просрочен"}]},
                         ensure_ascii=False)
        body = "".join(str(m.get("content")) for m in (messages or []))
        if "s2" in body:
            txt = json.dumps({"исполнение": [{"проект": "P1", "пункт": "RM-01", "статус": "выполнен"}]},
                             ensure_ascii=False)
        return {"text": txt, "model": "local/test", "input_tokens": 10, "output_tokens": 10}

    res = _run(chat)
    arb = (res.get("run_metrics") or {}).get("arbitration") or {}
    assert arb.get("total") == 1, arb
    d = arb["decisions"][0]
    assert d["item"] == "P1 · RM-01" and d["chosen"] == "просрочен"
    assert any(b.get("kind") == "arbitration" for b in (res.get("board") or [])), \
        "расхождение видно на доске событий прогона, а не только в метриках"


def test_run_records_snapshot_and_notices_drift():
    async def chat(messages=None, **kw):
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    res = _run(chat, rows=[{"id": "d1"}, {"id": "d2"}])
    snap = (res.get("run_metrics") or {}).get("data_snapshot") or {}
    assert snap.get("rows_total") == 2 and not snap.get("drift")

    res2 = _run(chat, rows=[{"id": "d1"}], snapshot=snap)
    snap2 = (res2.get("run_metrics") or {}).get("data_snapshot") or {}
    assert snap2.get("inherited") and snap2.get("drift"), "ветвь считала по другому набору — это видно"
    assert any(b.get("kind") == "warning" for b in (res2.get("board") or []))


def test_skill_waits_for_a_board_key_it_declared():
    async def chat(messages=None, **kw):
        return {"text": json.dumps({"итог": "ок"}, ensure_ascii=False),
                "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    schemas = {"s1": {"inputs": {"required": [{"from": "board", "key": "исполнение"}]}}}
    res = _run(chat, schemas=schemas)
    skipped = [f for f in (res.get("findings") or []) if f.get("skipped")]
    assert any("доска:исполнение" in (f.get("text") or "") for f in skipped), \
        "без объявленного ключа навык не запускается, а не выдаёт общие слова"


def test_shared_board_carries_neighbour_conclusions_into_the_run():
    """Ветвь группы видит вывод соседней ветви, если объявила ключ."""
    shared = bb.Board("grp-1")
    shared.put("исполнение", [{"пункт": "RM-01", "статус": "просрочен"}], author="ветвь-P2")
    seen = []

    async def chat(messages=None, **kw):
        seen.append("".join(str(m.get("content")) for m in (messages or [])))
        return {"text": "{}", "model": "local/test", "input_tokens": 1, "output_tokens": 1}

    schemas = {"s1": {"inputs": {"optional": [{"from": "board", "key": "исполнение"}]}}}
    _run(chat, board=shared, schemas=schemas)
    withboard = [b for b in seen if "ОБЩАЯ ПАМЯТЬ" in b]
    assert len(withboard) == 1, "ключ объявил один навык — второй общей памяти не получает"
    assert "ветвь-P2" in withboard[0]


def test_board_store_roundtrip_without_postgres():
    async def flow():
        await bb.save("grp-x", [{"key": "итог", "author": "а", "value": [1, 2]}])
        await bb.save("grp-x", [{"key": "итог", "author": "б", "value": [3]}])
        await bb.save("grp-x", [{"key": "итог", "author": "а", "value": [9]}])   # повтор заменяет
        b = await bb.board_of("grp-x")
        assert len(b.entries) == 2, "повторная запись автора заменяет прежнюю, а не копится"
        assert {e["author"] for e in b.entries} == {"а", "б"}
        assert [e["value"] for e in b.claims("итог") if e["author"] == "а"] == [[9]]
    asyncio.run(flow())
