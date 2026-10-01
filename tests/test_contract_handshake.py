"""Стыковка контрактов: просит ли навык то, что поставщик действительно отдаёт.

Прежние проверки сторожили форму контракта и ключ выхода. Сквозная проверка 01.10 вскрыла другое:
форма верна, а стык не сходится. Пять случаев на каталог, причём один — в середине главной демо-цепочки:

* `audit1c-graph-build` просил у `audit1c-extract` поля `id/тип` по пути «сущности», а там лежит
  ОПИСЬ выгрузки (`сущность/записей/типы`), а не сами документы;
* три навыка аудита просили у `doc1c` поле `ДокументОснование` — это колонка 1С, а рецепт кладёт её
  в поле `Основание_uuid`;
* `daily-plan` просил у `mail-triage` «критичность» и «ответственного», которых тот не отдаёт.

Расходятся такие контракты не на сборке, а в прогоне, когда деньги уже потрачены: coverage()
проверяет НАЛИЧИЕ сущности и поставщика, но не состав полей. Тесты ниже закрывают именно состав.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import ape
from server import skill_contract as sc

SKILLS = Path(__file__).resolve().parents[1] / "skills"
TEMPLATES = {p.parent.name: json.loads(p.read_text(encoding="utf-8"))
             for p in sorted(SKILLS.glob("*/template.json"))}


def produced_fields(sid: str, path: str) -> set[str] | None:
    """Поля записи, которые навык отдаёт по этому пути. None — пути в схеме результата нет."""
    props = ((TEMPLATES.get(sid) or {}).get("json_schema") or {}).get("properties") or {}
    node = props.get(path)
    if node is None:
        return None
    if node.get("type") == "array":
        node = node.get("items") or {}
    return set((node.get("properties") or {}).keys())


def skill_inputs(t: dict) -> list[tuple[str, dict]]:
    ins = t.get("inputs") or {}
    return [(b, it) for b in ("required", "optional") for it in (ins.get(b) or [])
            if isinstance(it, dict) and it.get("from") == "skill"]


@pytest.mark.parametrize("sid", sorted(TEMPLATES))
def test_upstream_skill_exists_and_declares_path(sid: str) -> None:
    """Поставщик есть в каталоге и объявляет тот путь, который у него просят."""
    for bucket, it in skill_inputs(TEMPLATES[sid]):
        up = str(it.get("skill") or "")
        assert up in TEMPLATES, f"{sid} [{bucket}]: поставщика «{up}» нет в каталоге"
        path = str(it.get("path") or "")
        if not path:
            continue
        declared = {str(p.get("path") or "") for p in sc.produces_list(TEMPLATES[up].get("produces"))}
        assert path in declared, (
            f"{sid} [{bucket}]: «{up}» не объявляет выход «{path}» (объявлено: {sorted(declared) or '—'})")
        assert produced_fields(up, path) is not None, (
            f"{sid} [{bucket}]: путь «{path}» у «{up}» объявлен в produces, но его нет в json_schema")


@pytest.mark.parametrize("sid", sorted(TEMPLATES))
def test_upstream_actually_gives_requested_fields(sid: str) -> None:
    """Каждое запрошенное поле поставщик действительно отдаёт (с учётом синонимов реестра)."""
    for bucket, it in skill_inputs(TEMPLATES[sid]):
        up, path = str(it.get("skill") or ""), str(it.get("path") or "")
        want = [str(f) for f in (it.get("fields") or [])]
        if not (want and path and up in TEMPLATES):
            continue
        have = produced_fields(up, path)
        if have is None:
            continue                      # отловлено предыдущим тестом
        canon_have = {sc.canonical(f) for f in have}
        miss = [f for f in want if f not in have and sc.canonical(f) not in canon_have]
        assert not miss, f"{sid} [{bucket}]: «{up}.{path}» не отдаёт {miss} (отдаёт: {sorted(have)})"


@pytest.mark.parametrize("sid", sorted(TEMPLATES))
def test_data_entity_is_declared(sid: str) -> None:
    """Сущность, у которой навык просит данные, описана контрактом данных.

    Произвольная сущность технически допустима, но навык каталога, завязанный на неописанную
    сущность, — это обещание без схемы: проверить его состав полей нечем.
    """
    ins = TEMPLATES[sid].get("inputs") or {}
    for bucket in ("required", "optional"):
        for it in (ins.get(bucket) or []):
            if not isinstance(it, dict) or it.get("from") != "data":
                continue
            ent = str(it.get("entity") or "")
            assert ent in ape.CANONICAL_SCHEMAS, (
                f"{sid} [{bucket}]: сущность «{ent}» не описана в CANONICAL_SCHEMAS")
