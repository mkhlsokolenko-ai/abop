"""Сквозной ключ: чем запись опознаётся и чем соединяется с чужими.

Аудит 30.09 нашёл 15 выходов, где `produces.key` называл поле, которого в схеме результата нет.
Последствие не косметическое: доска прогона и арбитраж сопоставляют записи разных ветвей именно по
этому полю — запись без ключа молча выпадает из сопоставления, и расхождение остаётся ненайденным.
Ещё четыре выхода объявляли сквозное понятие синонимом («расхождение» вместо «находка»), и один и
тот же предмет читался как два разных.

Проверки ниже сторожат каждый из этих случаев на всём каталоге, а не на демо-вертикали.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import ape
from server import skill_contract as sc

SKILLS = Path(__file__).resolve().parents[1] / "skills"
TEMPLATES = sorted(SKILLS.glob("*/template.json"))


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def item_fields(schema: dict, path: str) -> set[str]:
    """Поля ЗАПИСИ того списка, который навык объявил своим выходом."""
    node = ((schema or {}).get("properties") or {}).get(path) or {}
    if node.get("type") == "array":
        return set(((node.get("items") or {}).get("properties") or {}).keys())
    return set((node.get("properties") or {}).keys())


def test_templates_found():
    assert len(TEMPLATES) > 40, "шаблоны навыков не найдены"


@pytest.mark.parametrize("path", TEMPLATES, ids=[p.parent.name for p in TEMPLATES])
def test_produces_key_exists_in_schema(path: Path):
    """Ключ записи обязан существовать в схеме: иначе опознать запись нечем."""
    t = load(path)
    schema = t.get("json_schema") or {}
    for p in sc.produces_list(t.get("produces")):
        key, out = str(p.get("key") or ""), str(p.get("path") or "")
        if not key or not out:
            continue
        fields = item_fields(schema, out)
        if not fields:
            continue        # выход — не список записей (скаляр/объект): опознавать нечего
        assert key in fields, (
            f"{path.parent.name}.{out}: ключ «{key}» не найден среди полей записи "
            f"({', '.join(sorted(fields))})")


@pytest.mark.parametrize("path", TEMPLATES, ids=[p.parent.name for p in TEMPLATES])
def test_join_is_canonical_concept(path: Path):
    """Сквозное понятие называется каноническим именем, а не синонимом.

    Синоним formально сводится реестром, но в контракте он читается как другое понятие — и автор
    следующего навыка объявит своё, третье.
    """
    t = load(path)
    for p in sc.produces_list(t.get("produces")):
        join = str(p.get("join") or "")
        if not join:
            continue
        assert sc.canonical(join) == join, (
            f"{path.parent.name}.{p.get('path')}: join «{join}» — синоним, "
            f"каноническое имя «{sc.canonical(join)}»")


def test_every_used_entity_has_a_declared_contract():
    """Сущность, на которой работает навык, обязана иметь объявленный контракт.

    Без него `data_schema` отдаёт заглушку «required: id», и сопоставление записей идёт наугад: у
    пунктов плана, отчётов подрядчика и решений приёмки идентификаторы разные по построению, а
    говорят они об одном пункте одного проекта.
    """
    used: set[str] = set()
    for p in TEMPLATES:
        t = load(p)
        for bucket in ("required", "optional"):
            for it in ((t.get("inputs") or {}).get(bucket) or []):
                if isinstance(it, dict) and it.get("from") == "data" and it.get("entity"):
                    used.add(str(it["entity"]))
    missing = sorted(e for e in used if not ape.data_schema(e).get("known"))
    assert not missing, "нет контракта у сущностей: " + ", ".join(missing)


def test_join_key_declared_for_every_entity():
    """У каждой объявленной сущности сказано, чем её записи соединяются с чужими."""
    for entity, spec in ape.CANONICAL_SCHEMAS.items():
        join = ape.entity_join_key(entity)
        assert join, f"{entity}: не объявлен сквозной ключ"
        for f in join:
            assert f in (spec.get("required") or []) or f == "id", (
                f"{entity}: поле сквозного ключа «{f}» не входит в обязательные — "
                f"по нему нельзя соединять, его может не быть в записи")


def test_project_vertical_joins_by_project_and_item():
    """Проектная вертикаль соединяется парой «проект + пункт», а не идентификатором.

    Живой случай: id у трёх сущностей разный по построению («PRJ-2451/RM-02/ООО Стек-Интегро» против
    «PRJ-2451/RM-02/приёмка»), и сведение по id дало бы три отдельные записи об одном пункте.
    """
    for entity in ("roadmap_item", "contractor_report", "acceptance"):
        assert ape.entity_join_key(entity) == ["проект", "пункт"], entity
    assert ape.entity_join_key("project") == ["id"]


# ── Заливка шаблонов не должна терять контракт ────────────────────────────────────────────────

def test_push_script_sends_the_contract():
    """Скрипт заливки шлёт inputs/produces/slots, а не только схему.

    30.09 `push_templates.py --force` обнулил контракты всех 56 шаблонов на проде: скрипт отправлял
    пять полей, а хранилище записывало отсутствующие как пустые. План по контрактам, покрытие входов,
    доска и арбитраж держатся именно на них.
    """
    src = (Path(__file__).resolve().parents[1] / "scripts" / "push_templates.py").read_text(encoding="utf-8")
    for field in ("inputs", "produces", "slots", "tool_steps"):
        assert f'"{field}"' in src, f"скрипт заливки не отправляет «{field}»"


def test_import_endpoint_carries_the_contract():
    """Эндпоинт импорта кладёт контракт в карточку, а не теряет его по дороге."""
    src = (Path(__file__).resolve().parents[1] / "server" / "web_api.py").read_text(encoding="utf-8")
    i = src.index("async def schema_templates_import(")
    body = src[i:i + 3000]
    for field in ("inputs", "produces", "slots", "tool_steps"):
        assert f'"{field}":' in body, f"импорт не переносит «{field}» в карточку шаблона"
