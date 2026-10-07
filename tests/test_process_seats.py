# -*- coding: utf-8 -*-
"""Посадка агента: (операция, агент) без версии, группа — производная (ADR-043).

Участок процесса раньше существовал в одном месте — строкой в заголовке триггера; TDR §13.1 так его и
задумал (`process_point: "приём заявки → скоринг"`). Назначение держало `dep: "d1".."d4"`, а
`/api/fleet` отдаёт `agent_id` вида `audit-1c.v2` — совпадения не бывало никогда, поэтому живые
экземпляры на карте не показывались ни при каких данных.

Отдельно проверяется то, что нашлось при разборе: `POST /api/agents/{id}/triggers` поднимает версию
агента (`next_version`), и посадка, привязанная к версии, рассинхронизировалась бы при каждом
добавлении расписания. Поэтому ссылка — на `audit_id`, а пин версии — осознанная опция.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from server import process_store as ps  # noqa: E402

ДЕРЕВО = {
    "id": "org", "kind": "org", "label": "Организация", "children": [
        {"id": "p2", "kind": "process", "label": "Учёт и отчётность", "area": "finance", "children": [
            {"id": "s21", "kind": "subprocess", "label": "Аудит данных в 1С", "children": [
                {"id": "o212", "kind": "task", "label": "Связи документов"},
                {"id": "o213", "kind": "task", "label": "Проверки A · B · C"},
                {"id": "o214", "kind": "task", "label": "Привязка к норме"},
            {"id": "o211", "kind": "event", "label": "Ночная выгрузка копии базы"},
            {"id": "o215", "kind": "hitl", "label": "Подтверждение главбуха"},
            ]},
        ]},
    ],
}


@pytest.fixture()
def карта():
    asyncio.run(ps._сброс_для_тестов())
    asyncio.run(ps.save_map(ps.КАРТА, ДЕРЕВО, editor="admin.abop", note="сид"))
    return "finance"


def _правка(вид, цель, **груз):
    return {"kind": вид, "target": цель, "payload": груз}


def _посадить(area, *, правки, base=0, автор="@pm"):
    итог = asyncio.run(ps.append(area, base_version=base, changes=правки, author=автор))
    return asyncio.run(ps.apply(area, base_version=итог["version"], actor=автор))


def test_группа_это_набор_посадок_по_операциям(карта):
    """`ops: [o212, o213, o214]` одной записью превращается в три строки — у каждой своё состояние."""
    _посадить(карта, правки=[_правка("add_seat", o, audit_id="audit-1c")
                             for o in ("o212", "o213", "o214")])
    посадки = asyncio.run(ps.seats_of(карта))
    assert sorted(s["op_id"] for s in посадки) == ["o212", "o213", "o214"]
    assert {s["audit_id"] for s in посадки} == {"audit-1c"}


def test_группа_выводится_а_не_хранится(карта):
    """Группа агента — это его посадки, собранные по `audit_id`, а не массив внутри записи."""
    _посадить(карта, правки=[_правка("add_seat", "o212", audit_id="audit-1c"),
                             _правка("add_seat", "o213", audit_id="audit-1c"),
                             _правка("add_seat", "o214", audit_id="mailtasks")])
    группа = asyncio.run(ps.seats_by_agent("audit-1c"))
    assert sorted(s["op_id"] for s in группа) == ["o212", "o213"]
    assert [s["op_id"] for s in asyncio.run(ps.seats_by_agent("mailtasks"))] == ["o214"]


def test_посадка_не_знает_версии_агента(карта):
    """Добавление расписания поднимает версию агента: посадка обязана это пережить."""
    _посадить(карта, правки=[_правка("add_seat", "o213", audit_id="audit-1c")])
    посадка = asyncio.run(ps.seats_of(карта))[0]
    assert посадка["audit_id"] == "audit-1c", "в посадке не должно быть .vN"
    assert посадка.get("pin_version") is None, "по умолчанию версия плывёт за действующей"


def test_пин_версии_задаётся_явно(карта):
    """Регулируемый участок: смена версии должна быть событием, а не побочным эффектом."""
    _посадить(карта, правки=[_правка("add_seat", "o213", audit_id="audit-1c", pin_version=2)])
    assert asyncio.run(ps.seats_of(карта))[0]["pin_version"] == 2


def test_снятие_это_until_а_не_удаление(карта):
    """История участка остаётся: кто там сидел и когда его сняли."""
    _посадить(карта, правки=[_правка("add_seat", "o213", audit_id="audit-1c")])
    итог = asyncio.run(ps.append(карта, base_version=0, author="@pm",
                                 changes=[_правка("drop_seat", "o213", audit_id="audit-1c")]))
    assert [s["state"] for s in asyncio.run(ps.projection(карта, layer="to-be"))["seats"]] == ["dropping"]
    asyncio.run(ps.apply(карта, base_version=итог["version"], actor="@pm"))
    assert asyncio.run(ps.seats_of(карта)) == []
    закрытые = asyncio.run(ps.seats_of(карта, include_closed=True))
    assert len(закрытые) == 1 and закрытые[0]["until"]


def test_удаление_операции_не_трогает_соседние_посадки(карта):
    """`removeBlock` раньше вычищал операцию из ВСЕХ назначений — соседи теряли агента заодно."""
    _посадить(карта, правки=[_правка("add_seat", o, audit_id="audit-1c") for o in ("o212", "o213")])
    итог = asyncio.run(ps.append(карта, base_version=0, author="@pm",
                                 changes=[_правка("drop_node", "o213")]))
    срез = asyncio.run(ps.projection(карта, layer="to-be"))
    assert [s["op_id"] for s in срез["seats"]] == ["o212"], "посадка на соседней операции уцелела"
    asyncio.run(ps.apply(карта, base_version=итог["version"], actor="@pm"))
    assert [s["op_id"] for s in asyncio.run(ps.seats_of(карта))] == ["o212"]
    закрытые = [s for s in asyncio.run(ps.seats_of(карта, include_closed=True)) if s["until"]]
    assert [s["op_id"] for s in закрытые] == ["o213"], (
        "посадка на удалённой операции должна закрыться с датой, а не остаться живой и невидимой"
    )


def test_префлайт_может_отбить_применение(карта):
    """Автономия ≤ контракт (ADR-013) проверяется до применения, а не после: причина — в отказе."""
    итог = asyncio.run(ps.append(карта, base_version=0, author="@pm",
                                 changes=[_правка("add_seat", "o213", audit_id="нет-такого")]))

    async def префлайт(новые):
        return [f"агента «{s['audit_id']}» нет в реестре" for s in новые]

    with pytest.raises(ps.Отказ) as ex:
        asyncio.run(ps.apply(карта, base_version=итог["version"], actor="@pm", preflight=префлайт))
    assert "нет-такого" in str(ex.value)
    assert asyncio.run(ps.seats_of(карта)) == [], "посадка не должна появиться при отказе префлайта"


def test_область_наследуется_от_процесса(карта):
    """Область объявляется на процессе и наследуется вниз; корень и необъявленное — «*» (ADR-045)."""
    assert ps.area_of(ДЕРЕВО, "o213") == "finance"
    assert ps.area_of(ДЕРЕВО, "s21") == "finance"
    assert ps.area_of(ДЕРЕВО, "p2") == "finance"
    assert ps.area_of(ДЕРЕВО, "org") == "*"


@pytest.mark.parametrize("узел,вид", [("o211", "событие"), ("o215", "человек")])
def test_агент_садится_только_на_задачу(карта, узел, вид):
    """Работу делает задача. Событие — триггер процесса, HITL — человек: сажать туда агента нельзя.

    Поймано на живом стенде: агент сел на «Заявка от поставщика» (event), и карточка участка
    показала агента там, где его быть не может. Интерфейс это уже знал — кнопка «＋ агент» рисуется
    только на задачах, — а сервер пускал любой лист.
    """
    with pytest.raises(ps.Отказ) as ex:
        asyncio.run(ps.append(карта, base_version=0, author="@pm",
                              changes=[_правка("add_seat", узел, audit_id="audit-1c")]))
    assert вид in str(ex.value).lower() or "задач" in str(ex.value).lower()
