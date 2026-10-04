# -*- coding: utf-8 -*-
"""Блекборд: общая память человека и отдела, живущая дольше прогона.

Доска была только внутри запроса: выводы умирали вместе с прогоном, и соседний разговор о них не
знал. Владелец предложил сделать её общей — «дистилляция фактов из прогонов для общего контекста».

Два правила, без которых она стала бы свалкой, и оба проверяются здесь:
  · у факта есть автор, время, СРОК ЖИЗНИ и ссылка на прогон — утверждение без происхождения
    проверить нечем, а месячный факт, выданный за текущий, хуже отсутствия факта;
  · доступ: личная доска — только владельцу, доска отдела — тем, кому открыта семья. Доска не должна
    стать каналом в обход прав на данные.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

import pytest  # noqa: E402

from server import blackboard as bb  # noqa: E402
from server import web_api  # noqa: E402


def test_области_разбираются_по_владельцу():
    assert bb.user_scope("sub-1") == "usr-sub-1"
    assert bb.family_scope("analytics") == "fam-analytics"
    assert bb.scope_owner("usr-sub-1") == ("user", "sub-1")
    assert bb.scope_owner("fam-analytics") == ("family", "analytics")
    assert bb.scope_owner("run-123") == ("run", "run-123")
    assert bb.user_scope("") == "" and bb.family_scope("  ") == ""


def test_факт_несёт_автора_срок_и_прогон():
    async def flow():
        await bb.put_fact("usr-t1", "выручка_q3", {"значение": "12 млн"}, author="Иванов",
                          note="из управленческого отчёта", run_id="run-9", ttl_sec=3600)
        return await bb.load("usr-t1")
    rows = asyncio.run(flow())
    assert len(rows) == 1
    f = rows[0]
    assert f["key"] == "выручка_q3" and f["author"] == "Иванов"
    assert f["run_id"] == "run-9" and f["ttl_sec"] == 3600 and f["at"], f
    assert f["value"]["значение"] == "12 млн", "значение должно остаться дословным"


def test_просроченный_факт_не_выдаётся_за_текущий():
    async def flow():
        await bb.put_fact("usr-t2", "старое", {"a": 1}, author="кто-то", ttl_sec=1)
        # Ждать срок в тесте нельзя, поэтому сдвигаем время записи в хранилище: проверяем ПРАВИЛО
        # отсечки, а не скорость часов.
        bb._MEM["usr-t2"][0]["at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60))
        fresh = await bb.load("usr-t2")
        return fresh, await bb.load("usr-t2", include_stale=True)
    fresh, all_rows = asyncio.run(flow())
    assert fresh == [], "просроченный факт попал в работу"
    assert len(all_rows) == 1, "разбор должен видеть и просроченное"


def test_повтор_уточняет_а_не_спорит():
    """Один автор под одним ключом держит одну запись."""
    async def flow():
        await bb.put_fact("usr-t3", "ставка", {"v": 19}, author="Иванов")
        await bb.put_fact("usr-t3", "ставка", {"v": 21}, author="Иванов")
        await bb.put_fact("usr-t3", "ставка", {"v": 20}, author="Петров")
        return await bb.load("usr-t3")
    rows = asyncio.run(flow())
    assert len(rows) == 2, [r["author"] for r in rows]
    mine = [r for r in rows if r["author"] == "Иванов"][0]
    assert mine["value"]["v"] == 21, "повтор не уточнил запись"


def test_снятие_факта():
    async def flow():
        await bb.put_fact("usr-t4", "k", {"v": 1}, author="A")
        await bb.put_fact("usr-t4", "k", {"v": 2}, author="B")
        await bb.drop_fact("usr-t4", "k", "A")
        after_one = await bb.load("usr-t4")
        await bb.drop_fact("usr-t4", "k")
        return after_one, await bb.load("usr-t4")
    one, none_ = asyncio.run(flow())
    assert [r["author"] for r in one] == ["B"]
    assert none_ == []


def test_доска_отдела_требует_прав():
    """ABAC: аналитик не читает доску архитектуры — иначе доска обходит права на данные."""
    analyst = {"sub": "s1", "name": "Иванов", "department": "analytics", "level": "manager"}
    admin = {"sub": "s2", "name": "Админ", "department": "*", "level": "admin"}
    assert web_api._board_scope("family", analyst, "analytics") == "fam-analytics"
    assert web_api._board_scope("family", admin, "architecture") == "fam-architecture"
    with pytest.raises(Exception) as e:
        web_api._board_scope("family", analyst, "architecture")
    assert "403" in str(e.value) or "вне вашей области" in str(e.value)
    with pytest.raises(Exception):
        web_api._board_scope("family", admin, "")      # семья не названа — не угадываем


def test_личная_доска_своя_у_каждого():
    a = web_api._board_scope("user", {"sub": "s1"})
    b = web_api._board_scope("user", {"sub": "s2"})
    assert a != b and a.startswith("usr-")


def test_итоги_прогона_выносятся_фактами():
    """По одному факту на навык, со ссылкой на прогон и дословным значением."""
    run = {"run_id": "run-77", "agent_name": "Оценка идеи",
           "skill_outputs": [{"skill": "idea-scorer", "structured": {"вердикт": "доработать", "сумма": 17}},
                             {"skill": "report-editor", "structured": {"раскладка": []}}]}

    async def flow():
        scope = bb.user_scope("s9")
        for so in run["skill_outputs"]:
            if so.get("structured"):
                await bb.put_fact(scope, so["skill"], so["structured"],
                                  author=f"{run['agent_name']} · {so['skill']}", run_id=run["run_id"])
        return await bb.load(scope)
    rows = asyncio.run(flow())
    keys = {r["key"] for r in rows}
    assert "idea-scorer" in keys
    f = [r for r in rows if r["key"] == "idea-scorer"][0]
    assert f["run_id"] == "run-77" and f["value"]["вердикт"] == "доработать"
    assert "Оценка идеи" in f["author"], "по автору должно быть видно, чей это вывод"
