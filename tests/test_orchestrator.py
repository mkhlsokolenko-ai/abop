# -*- coding: utf-8 -*-
"""Оркестратор: одно решение «кого звать и что собрать» — и трасса, почему именно так.

Цепочка решений была размазана по каналам: достаточность описания спрашивал один код, подбор агента —
другой, сборку из навыков — третий. Каналы расходились: карточка в чате держала план, собранный до
правки подбора, и человек запустил устаревший.

Трасса решения — не украшение. За два дня подбор дважды выбрал не тот навык (статус-отчёт вместо
оценки идеи, разбор почты вместо нарезки тикетов), и оба раза это заметили только потому, что было
видно, кто отработал. Поэтому каждое решение обязано говорить, что выбрано, с каким счётом и что
отброшено.
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

from fastapi.testclient import TestClient  # noqa: E402

from server import audit_store, orchestrator, web_api  # noqa: E402

CLIENT = TestClient(web_api.app)

CATALOG = {
    "to-tickets": {"title": "To Tickets", "short": "план → тикеты",
                   "body": "из решений и плана сделай задачи трекера с исполнителем и сроком",
                   "inputs": {"required": [{"from": "context"}]},
                   "produces": {"path": "тикеты", "key": "id", "join": "задача"}},
    "mail-triage": {"title": "Разбор почты", "short": "письма → задачи",
                    "body": "разбери почту: задачи, сроки, что требует ответа",
                    "inputs": {"required": [{"from": "context"}]},
                    "produces": [{"path": "задачи", "key": "задача"}]},
}


def _d(task, **kw):
    kw.setdefault("catalog", CATALOG)
    kw.setdefault("entities", set())
    kw.setdefault("slots", set())
    kw.setdefault("matches", [])
    return orchestrator.decide(task, **kw)


def test_уверенный_подбор_запускает_готового_агента():
    """Самый дешёвый путь: ни сборки, ни вызова модели — то, что уже собрано и проверено."""
    d = _d("разбери почту за день", matches=[{"id": "a1", "name": "Почтовик", "score": 0.81},
                                             {"id": "a2", "name": "Секретарь", "score": 0.4}])
    assert d.kind == "agent" and d.agent_id == "a1"
    assert d.alternatives and d.alternatives[0]["name"] == "Секретарь"
    assert any("0.81" in w for w in d.why), d.why
    assert "Почтовик" in orchestrator.explain(d)


def test_слабый_подбор_уходит_в_сборку_из_навыков():
    d = _d("нарежь задачи в трекере по решениям сверки", matches=[{"id": "a1", "name": "Почтовик", "score": 0.2}])
    assert d.kind == "build" and d.skills, d.why
    assert any("порога" in w for w in d.why), "не сказано, почему готовый агент не выбран"


def test_короткая_задача_возвращает_вопросы():
    """По двум словам исполнителя не выбирают: спрашиваем ровно недостающее."""
    d = _d("сделай отчёт")
    assert d.kind == "ask" and d.questions, d.why


def test_нет_исполнителя_это_тоже_ответ():
    d = _d("забронируй переговорку на троих в четверг после обеда")
    assert d.kind in ("chat", "ask"), d.kind
    if d.kind == "chat":
        assert any("каталоге нет" in w or "нет" in w for w in d.why), d.why


def test_редактор_при_нескольких_навыках_и_его_отсутствие_при_кэше():
    plan = {"ok": True, "steps": [{"skill": "mail-triage"}, {"skill": "to-tickets"}],
            "report_template": "tickets"}
    added = _d("разбери почту и нарежь задачи в трекере", plan=plan)
    assert added.kind == "build" and added.editor == "added"
    assert any("редактор отчёта" in w for w in added.why)
    cached = _d("разбери почту и нарежь задачи в трекере", plan=plan, cached_layout=True)
    assert cached.editor == "cached"
    assert any("уже известна" in w for w in cached.why)


def test_один_навык_редактора_не_требует():
    plan = {"ok": True, "steps": [{"skill": "mail-triage"}], "report_template": "digest"}
    d = _d("разбери почту", plan=plan)
    assert d.editor == "" and d.form == "digest"


def test_решение_целиком_сериализуемо():
    """Оно уходит в журнал и в ответ по сети — питоновских объектов там быть не должно."""
    import json
    json.dumps(_d("нарежь задачи в трекере").as_dict(), ensure_ascii=False)


def test_эндпоинт_решает_и_пишет_в_журнал():
    before = len(asyncio.run(audit_store.list_events(limit=200)) or [])
    r = CLIENT.post("/api/orchestrate", json={"task": "нарежь задачи в трекере по решениям сверки плана и факта"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] in ("agent", "build", "ask", "chat")
    assert d["итог"] and d["why"], d
    rows = asyncio.run(audit_store.list_events(limit=200)) or []
    assert len(rows) > before
    rec = [x for x in rows if x.get("action") == "orchestrator.decide"]
    assert rec, "решение не попало в журнал"
    assert rec[0]["detail"].get("why"), "в журнале нет трассы решения"


def test_оркестратор_решает_но_не_действует():
    """У действия своя проверка прав и свой журнал: решение их не подменяет."""
    r = CLIENT.post("/api/orchestrate", json={"task": "разбери почту и нарежь задачи в трекере"})
    body = r.json()
    assert "agent_id" in body and "skills" in body
    assert "run_id" not in body and "pipeline" not in body, "оркестратор что-то запустил сам"
