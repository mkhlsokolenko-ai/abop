"""Контракт навыка: формат, канонические имена, покрытие входов.

Этап 0 плана перехода к мультиагентности. До контракта вход навыка был описан только прозой, поэтому
граф нечем было исполнять по порядку, а совместимость шагов приходилось угадывать по совпадению имён.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import skill_contract as sc

SKILLS = Path(__file__).resolve().parents[1] / "skills"
WITH_CONTRACT = sorted(
    p for p in SKILLS.glob("*/template.json")
    if "inputs" in json.loads(p.read_text(encoding="utf-8"))
)


def test_canonical_names():
    assert sc.canonical("дебет") == "счёт_дт"
    assert sc.canonical("счёт_дт") == "счёт_дт"
    assert sc.canonical("своё_поле") == "своё_поле"
    assert sc.same_field("серьёзность", "критичность")
    assert not sc.same_field("сумма", "срок")


def test_inputs_format_errors():
    assert sc.validate_inputs({"required": [{"from": "data"}]}), "вход из данных без сущности — ошибка"
    assert sc.validate_inputs({"required": [{"from": "неизвестно"}]}), "чужой источник — ошибка"
    assert sc.validate_inputs({"лишнее": []}), "лишний ключ — ошибка"
    assert not sc.validate_inputs({"required": [{"from": "data", "entity": "doc1c", "fields": ["id"]}]})
    assert not sc.validate_inputs(None)


def test_produces_errors():
    assert sc.validate_produces({"key": "id"}), "ключ без пути — ошибка"
    assert not sc.validate_produces({"path": "находки", "key": "id", "join": "находка"})


def test_contract_checks_schema_and_slots():
    tpl = {"json_schema": {"properties": {"находки": {"type": "array"}}},
           "produces": {"path": "выводы", "key": "id"}}
    assert any("не найден" in e for e in sc.validate_contract(tpl)), "путь выхода должен быть в схеме"
    tpl2 = {"json_schema": {"properties": {"находки": {}}},
            "inputs": {"required": [{"from": "slot", "name": "проект"}]}, "slots": []}
    assert any("слот" in e for e in sc.validate_contract(tpl2)), "вход из слота требует объявленного слота"


def test_coverage_reports_gaps():
    inputs = {"required": [
        {"from": "data", "entity": "roadmap_item"},
        {"from": "slot", "name": "проект"},
        {"from": "skill", "skill": "audit1c-checks", "path": "находки", "fields": ["id"]},
    ]}
    miss = sc.coverage(inputs, entities=set(), slots=set(), upstream={})
    assert len(miss) == 3, miss
    ok = sc.coverage(inputs, entities={"roadmap_item"}, slots={"проект"},
                     upstream={"audit1c-checks": {"path": "находки", "key": "id"}})
    assert ok == [], ok


def test_contracts_exist_for_audit_vertical():
    have = {p.parent.name for p in WITH_CONTRACT}
    for sid in ("audit1c-checks", "audit1c-explain", "audit1c-rank", "roadmap-fact"):
        assert sid in have, f"у навыка {sid} нет контракта"


@pytest.mark.parametrize("path", WITH_CONTRACT, ids=[p.parent.name for p in WITH_CONTRACT])
def test_declared_contracts_are_valid(path: Path):
    t = json.loads(path.read_text(encoding="utf-8"))
    errs = sc.validate_contract(t)
    assert not errs, f"{path.parent.name}: " + "; ".join(errs)


@pytest.mark.parametrize("path", WITH_CONTRACT, ids=[p.parent.name for p in WITH_CONTRACT])
def test_upstream_skills_exist(path: Path):
    """Вход из навыка должен ссылаться на существующий навык с объявленным выходом."""
    t = json.loads(path.read_text(encoding="utf-8"))
    for bucket in ("required", "optional"):
        for it in (t.get("inputs") or {}).get(bucket) or []:
            if it.get("from") != "skill":
                continue
            up = SKILLS / str(it.get("skill")) / "template.json"
            assert up.exists(), f"{path.parent.name}: нет навыка {it.get('skill')}"
            ut = json.loads(up.read_text(encoding="utf-8"))
            outs = sc.produces_list(ut.get("produces"))
            assert outs, f"{it.get('skill')} не объявил, какой список отдаёт"
            if it.get("path"):
                paths = [o.get("path") for o in outs]
                assert it["path"] in paths, (
                    f"{path.parent.name} ждёт «{it['path']}» от {it.get('skill')}, "
                    f"а тот отдаёт {paths}")
