# -*- coding: utf-8 -*-
"""Сборка из плана: один агент на все навыки, и готовый вместо однофамильца.

Каждый шаг плана заводил нового агента, а повторная сборка того же шага — новую версию того же
агента. После нескольких проб «Мои агенты» зарастали карточками, которые отличаются только номером
версии: владелец попросил снести четыре такие после одной проверки.

Переиспользуем только ПРОСТОГО агента: ровно один узел-навык и никакого вывода во внешний канал.
Иначе шаг цепочки начал бы отправлять письма или заводить задачи лишь потому, что когда-то такого
агента собрали с доставкой.
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

import asyncio  # noqa: E402

from server import agent_store, web_api  # noqa: E402

# department="*" — область admin/support: сборка агента гейтится по отделу (ABAC), и без области
# тест падал бы на 403 вместо проверки переиспользования.
U = {"name": "dev", "sub": "dev", "level": "admin", "roles": ["admin"], "department": "*"}
STEPS = [{"skill": "idea-scorer", "title": "Оценка идеи"}]


def _node(skill):
    return {"id": skill, "kind": "skill", "skill": skill, "autonomy": "A2"}


async def _save(audit_id, name, nodes):
    return await agent_store.save_draft(name=name, audit_id=audit_id, graph={"nodes": nodes, "edges": []},
                                        autonomy_max="A2", created_by="dev", family="analytics",
                                        role="analyst", source="authored")


def test_повторная_сборка_не_создаёт_второго_агента():
    async def flow():
        first = await web_api.plan_auto_build({"steps": STEPS, "name": "Проба 1"}, U)
        second = await web_api.plan_auto_build({"steps": STEPS, "name": "Проба 2"}, U)
        return first, second
    first, second = asyncio.run(flow())
    assert first["created"] == 1 and first["reused"] == 0, first
    assert second["created"] == 0 and second["reused"] == 1, "второй раз завёл агента заново"
    assert first["agents"][0]["agent_id"] == second["agents"][0]["agent_id"], "цепочка смотрит на другого агента"


def test_агент_с_внешней_отправкой_не_переиспользуется():
    """Иначе шаг цепочки неожиданно начнёт писать письма."""
    async def flow():
        await _save("authored-outward-mail", "С отправкой", [
            _node("email-draft"), {"id": "out1", "kind": "out", "out": {"channel": "email", "to": "x@y.z"}}])
        return await web_api._agent_for_skills(["email-draft"], U)
    assert asyncio.run(flow()) is None


def test_агент_из_нескольких_навыков_не_переиспользуется():
    """Нужен исполнитель одного шага, а не чужая цепочка внутри одного агента."""
    async def flow():
        await _save("authored-multi", "Двухнавыковый", [_node("market-research"), _node("idea-selector")])
        return await web_api._agent_for_skills(["market-research"], U)
    assert asyncio.run(flow()) is None


def test_локальный_вывод_переиспользованию_не_мешает():
    """PDF и чат наружу ничего не отправляют — такой агент как исполнитель шага годится."""
    async def flow():
        await _save("authored-local-pdf", "С PDF", [
            _node("devils-advocate"), {"id": "o", "kind": "out", "out": {"channel": "pdf"}}])
        got = await web_api._agent_for_skills(["devils-advocate"], U)
        return got
    got = asyncio.run(flow())
    assert got and got.get("id", "").startswith("authored-local-pdf")


def test_переиспользование_можно_отключить():
    """Явный reuse=false — когда нужен именно свежий агент (например, другой набор лимитов)."""
    async def flow():
        await web_api.plan_auto_build({"steps": STEPS, "name": "Проба 3"}, U)
        return await web_api.plan_auto_build({"steps": STEPS, "name": "Проба 4", "reuse": False}, U)
    r = asyncio.run(flow())
    assert r["created"] == 1 and r["reused"] == 0


MULTI = [{"skill": "market-research", "title": "Рынок"}, {"skill": "idea-scorer", "title": "Оценка идеи"}]


def test_план_из_нескольких_навыков_собирается_в_одного_агента():
    """Так передача между навыками идёт по контрактам (доска прогона), а отчёт получается один.

    Цепочка агентов передавала результат ТЕКСТОМ в промпт следующего шага и давала отчёт на каждый
    шаг — владелец сказал про это «навыки работают в вакууме».
    """
    r = asyncio.run(web_api.plan_auto_build({"steps": MULTI, "name": "Идея и рынок"}, U))
    assert r["pipeline"] is None, "снова собралась цепочка"
    # К навыкам плана добавляется редактор отчёта — он и складывает разделы в один документ.
    assert r["agent_id"] and set(r["skills"]) == {"market-research", "idea-scorer", "report-editor"}
    assert "отчёт будет один" in r["note"]

    async def graph_of():
        ag = await agent_store.get(r["agent_id"])
        return [n.get("skill") for n in ((ag or {}).get("graph") or {}).get("nodes") or []
                if n.get("kind") == "skill"], ((ag or {}).get("graph") or {}).get("edges") or []
    skills, edges = asyncio.run(graph_of())
    assert set(skills) == {"market-research", "idea-scorer", "report-editor"}, skills
    assert edges, "навыки в графе не связаны — они пойдут одной волной и не увидят друг друга"


def test_повторная_сборка_того_же_набора_берёт_готового():
    first = asyncio.run(web_api.plan_auto_build({"steps": MULTI, "name": "Набор 1"}, U))
    second = asyncio.run(web_api.plan_auto_build({"steps": MULTI, "name": "Набор 2"}, U))
    assert second["reused"] == 1 and second["created"] == 0, second["note"]
    assert second["agent_id"] == first["agent_id"]


def test_цепочка_остаётся_по_явной_просьбе():
    """Она нужна там, где у шагов разная доставка или своё подтверждение."""
    r = asyncio.run(web_api.plan_auto_build({"steps": MULTI, "name": "Цепочкой", "as_chain": True}, U))
    assert r["pipeline"], "явная просьба о цепочке проигнорирована"
    assert len(r["agents"]) == 2


def test_редактор_добавляется_сам_и_уходит_при_кэше():
    """Навыков больше одного — нужен один документ; раскладка уже известна — модель не зовём."""
    from server import report_compose, report_store
    pair = [{"skill": "process-map", "title": "Карта процесса"}, {"skill": "c4-diagram", "title": "Схема"}]
    first = asyncio.run(web_api.plan_auto_build({"steps": pair, "name": "Процесс и схема"}, U))
    assert first["editor"] == "added" and report_compose.EDITOR_SKILL in first["skills"]
    assert "редактор отчёта" in first["note"].lower()

    # кладём раскладку в кэш — следующая сборка редактора уже не добавляет
    cid = report_compose.cache_id(["process-map", "c4-diagram"])
    asyncio.run(report_store.save(cid, {"name": "кэш", "html": "x", "layout": [{"t": "note", "text": "н"}],
                                        "for_skills": []}, editor="report-editor"))
    second = asyncio.run(web_api.plan_auto_build({"steps": pair, "name": "Процесс и схема 2"}, U))
    assert second["editor"] == "cached"
    assert report_compose.EDITOR_SKILL not in second["skills"], "редактор добавлен впустую"


def test_один_навык_редактора_не_получает():
    """Сшивать нечего — звать модель незачем."""
    r = asyncio.run(web_api.plan_auto_build({"steps": [{"skill": "idea-scorer"}], "name": "Одна идея"}, U))
    assert not r.get("editor") and "report-editor" not in r["skills"]


def test_редактор_идёт_последним_в_графе():
    """Он читает весь прогон: в первой волне он увидел бы пустоту."""
    pair = [{"skill": "market-research"}, {"skill": "jtbd-formulator"}]
    r = asyncio.run(web_api.plan_auto_build({"steps": pair, "name": "Рынок и работы"}, U))

    async def order():
        ag = await agent_store.get(r["agent_id"])
        return [n.get("skill") for n in ((ag or {}).get("graph") or {}).get("nodes") or []
                if n.get("kind") == "skill"]
    seq = asyncio.run(order())
    assert seq and seq[-1] == "report-editor", seq
