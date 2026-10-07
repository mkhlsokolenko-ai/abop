# -*- coding: utf-8 -*-
"""Редакция карты процессов: предложение изменения — объект, который подписывают (ADR-042).

До этого слой AS-IS/TO-BE был фильтром по полю `layer` над одним деревом в `app_config`: правка на
AS-IS меняла и TO-BE, поле `layer` из интерфейса не менялось, поэтому план не мог стать фактом, а
удаление блока вычищало операцию из всех назначений, включая плановые.

Здесь проверяется то, ради чего заведена редакция: AS-IS не меняется, пока редакцию не применили;
применение атомарно; черновик на область один; отброс черновика ничего не стоит; применение растит
номер и пишет автора; удалённые идентификаторы не переиспользуются, иначе история рассыплется.
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


def _срез(area, layer):
    return asyncio.run(ps.projection(area, layer=layer))


def test_черновик_не_виден_на_as_is(карта):
    """AS-IS — факт. Пока редакцию не применили, у остальных прежняя картина."""
    asyncio.run(ps.append(карта, base_version=0, author="@pm",
                          changes=[_правка("add_seat", "o213", audit_id="audit-1c"),
                                   _правка("add_node", "s21", kind="task", label="Привязка к норме")]))
    как_есть = _срез(карта, "as-is")
    assert как_есть["seats"] == []
    assert len(ps.найти(как_есть["tree"], "s21")["children"]) == 2

    как_будет = _срез(карта, "to-be")
    assert [s["state"] for s in как_будет["seats"]] == ["planned"]
    assert len(ps.найти(как_будет["tree"], "s21")["children"]) == 3


def test_применение_атомарно(карта):
    """Негодная правка валит всю пачку: применять половину редакции нельзя."""
    asyncio.run(ps.append(карта, base_version=0, author="@pm",
                          changes=[_правка("add_seat", "o213", audit_id="audit-1c")]))
    with pytest.raises(ps.Отказ):
        asyncio.run(ps.append(карта, base_version=1, author="@pm", changes=[
            _правка("rename_node", "o212", label="Связи"),
            _правка("add_seat", "s21", audit_id="audit-1c"),   # не лист — вся пачка отбивается
        ]))
    черновик = asyncio.run(ps.draft_of(карта))
    assert черновик["version"] == 1, "в журнал попала часть пачки"
    assert ps.найти(_срез(карта, "to-be")["tree"], "o212")["label"] == "Связи документов"


def test_применение_растит_номер_и_пишет_автора(карта):
    asyncio.run(ps.append(карта, base_version=0, author="@pm",
                          changes=[_правка("add_seat", "o213", audit_id="audit-1c")]))
    итог = asyncio.run(ps.apply(карта, base_version=1, actor="@pm"))
    assert итог["revision"] == 2 and итог["seats_added"] == 1
    применено = asyncio.run(ps.map_of(ps.КАРТА))
    assert применено["revision"] == 2 and применено["applied_by"] == "@pm"
    assert [s["state"] for s in _срез(карта, "as-is")["seats"]] == ["actual"]
    assert asyncio.run(ps.draft_of(карта)) is None, "после применения черновик должен закрыться"


def test_применение_непрочитанной_редакции_отбивается(карта):
    """Строгий замок там, где он уместен: применять то, во что дописали после просмотра, нельзя."""
    asyncio.run(ps.append(карта, base_version=0, author="@pm",
                          changes=[_правка("add_seat", "o213", audit_id="audit-1c")]))
    asyncio.run(ps.append(карта, base_version=1, author="@ivanov",
                          changes=[_правка("rename_node", "o212", label="Связи")]))
    with pytest.raises(ps.Конфликт) as ex:
        asyncio.run(ps.apply(карта, base_version=1, actor="@pm"))
    assert ex.value.соперники[0]["author"] == "@ivanov"


def test_один_открытый_черновик_на_область(карта):
    """Вторая правка дописывается в тот же черновик, а не плодит второй."""
    первый = asyncio.run(ps.append(карта, base_version=0, author="@pm",
                                   changes=[_правка("rename_node", "o212", label="Раз")]))
    второй = asyncio.run(ps.append(карта, base_version=1, author="@ivanov",
                                   changes=[_правка("rename_node", "o213", label="Два")]))
    assert первый["revision_id"] == второй["revision_id"]


def test_отброс_черновика_не_меняет_as_is(карта):
    asyncio.run(ps.append(карта, base_version=0, author="@pm",
                          changes=[_правка("drop_node", "o213")]))
    assert asyncio.run(ps.drop_draft(карта, actor="@pm")) is True
    assert asyncio.run(ps.draft_of(карта)) is None
    assert ps.найти(_срез(карта, "as-is")["tree"], "o213") is not None
    assert asyncio.run(ps.map_of(ps.КАРТА))["revision"] == 1, "отброс не должен растить номер"


def test_идентификаторы_не_переиспользуются(карта):
    """Удалённый узел остаётся в прежних редакциях, и старые прогоны продолжают на него ссылаться."""
    asyncio.run(ps.append(карта, base_version=0, author="@pm", changes=[_правка("drop_node", "o213")]))
    asyncio.run(ps.apply(карта, base_version=1, actor="@pm"))
    итог = asyncio.run(ps.append(карта, base_version=0, author="@pm",
                                 changes=[_правка("add_node", "s21", kind="task", label="Новая")]))
    assert итог["applied"][0]["payload"]["id"] != "o213"
    прежняя = asyncio.run(ps.map_of(ps.КАРТА))
    assert ps.найти(прежняя["tree"], "o213") is None, "в применённой карте узла уже нет"
    assert ps.найти(ДЕРЕВО, "o213") is not None, "сид не должен мутировать: наложение копирует дерево"


def test_правки_отдаются_с_подписью_цели(карта):
    """Разбор редакции читает человек: «блок «Проверки A · B · C» удалён», а не «o213».
    Подпись берётся из ПРИМЕНЁННОГО дерева — в наложении удалённого блока уже нет."""
    asyncio.run(ps.append(карта, base_version=0, author="@pm",
                          changes=[_правка("drop_node", "o213"),
                                   _правка("rename_node", "o212", label="Связи и основания")]))
    правки = _срез(карта, "to-be")["changes"]
    подписи = {c["kind"]: c["target_label"] for c in правки}
    assert подписи["drop_node"] == "Проверки A · B · C"
    assert подписи["rename_node"] == "Связи документов"
