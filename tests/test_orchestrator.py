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


def test_готовый_агент_берётся_когда_умеет_нужное():
    """Самый дешёвый путь: ни сборки, ни вызова модели — но только если агент делает именно это."""
    d = _d("разбери почту за день: задачи, сроки, что требует ответа",
           matches=[{"id": "a1", "name": "Почтовик", "score": 0.81, "skills": ["mail-triage"]},
                    {"id": "a2", "name": "Секретарь", "score": 0.4, "skills": ["mail-triage"]}])
    assert d.kind == "agent" and d.agent_id == "a1", d.why
    assert d.alternatives and d.alternatives[0]["name"] == "Секретарь"
    assert any("умеет то, что нужно" in w for w in d.why), d.why
    assert "Почтовик" in orchestrator.explain(d)


def test_похожий_но_не_умеющий_агент_не_выигрывает():
    """Коварный случай: агент похож по словам, а делает другое — раньше он выигрывал по порогу.

    Ровно это и случилось на проде: «проверь идею сервиса…» уходило к «Финаналитику», потому что его
    счёт подбора прошёл порог, хотя собрать нужно было оценку идеи.
    """
    d = _d("нарежь задачи в трекере по решениям сверки",
           matches=[{"id": "a1", "name": "Финаналитик", "score": 0.6, "skills": ["mail-triage"]}])
    assert d.kind == "build", d.why
    assert "to-tickets" in d.skills, d.skills
    assert any("делает другое" in w for w in d.why), d.why


def test_нечего_собирать_но_агент_подходит():
    """План пуст (исполнителя из навыков не выходит), а похожий агент есть — звать его."""
    d = _d("покажи, как у нас устроен процесс закупки от заявки до оплаты",
           matches=[{"id": "a9", "name": "Карта процессов", "score": 0.7, "skills": []}],
           plan={"ok": True, "steps": []})
    assert d.kind == "agent" and d.agent_id == "a9", d.why


def test_слабый_подбор_уходит_в_сборку_из_навыков():
    d = _d("нарежь задачи в трекере по решениям сверки",
           matches=[{"id": "a1", "name": "Почтовик", "score": 0.2, "skills": ["mail-triage"]}])
    assert d.kind == "build" and d.skills, d.why
    assert any("планки" in w for w in d.why), "не сказано, почему готовый агент не выбран"


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


# ── перевод чата на оркестратор ──────────────────────────────────────────────────────────────────

def test_чат_спрашивает_решение_у_оркестратора():
    """Одно решение вместо трёх вызовов по очереди — и прежний путь остаётся запасным.

    Решение принимается в `decideAndOffer`, и только по команде /work: подбор на каждое сообщение
    превращал переписку в поток предложений собрать агента.
    """
    chat = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
    i = chat.index("async function decideAndOffer")
    body = chat[i:chat.index("// карточка уточнения", i)]
    assert "const d = await decide(task)" in body, "чат не спрашивает решение"
    for kind in ('d.kind === "ask"', 'd.kind === "agent"', 'd.kind === "build"'):
        assert kind in body, f"не разобран вид решения: {kind}"
    assert "/match" in body and "/plan" in body, "нет запасного пути для старого сайдкара"
    assert 'api(M + "/orchestrate"' in chat
    assert "work|задача|агент" in chat, "команда /work не разбирается"


def test_решение_пересчитывается_перед_сборкой():
    """Карточка могла пролежать в чате долго: именно на устаревшей запустили прежний план."""
    chat = (ROOT / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
    i = chat.index("async function buildAndRunPlan")
    body = chat[i:i + 1200]
    assert "await decide(a.task)" in body, "перед сборкой решение не пересчитывается"
    assert "Подбор пересчитан" in body, "расхождение с карточкой не показано человеку"


def test_сайдкар_отдаёт_решение():
    mod = (ROOT / "desktop" / "sidecar" / "modules" / "chat" / "module.py").read_text(encoding="utf-8")
    cli = (ROOT / "desktop" / "sidecar" / "abop_client.py").read_text(encoding="utf-8")
    assert '@router.post("/orchestrate")' in mod and "def orchestrate" in cli
    assert "/api/orchestrate" in cli


# ── слабое совпадение навыка: спрашиваем, а не угадываем ─────────────────────────────────────────

def test_слабое_совпадение_навыка_превращается_в_вопрос():
    """На проде «проверь идею ниже — идея сервиса…» дало цепочку аудита 1С со счётом 0.2.

    Уверенно неверный ответ хуже вопроса: он выглядит как работа и стоит реального прогона. Поэтому
    при слабом лидере оркестратор спрашивает — теми же словами, что и при слабом подборе агента.
    """
    plan = {"ok": True, "report_template": "audit1c",
            "steps": [{"skill": "audit1c-extract", "title": "Извлечение 1С", "score": 0.2},
                      {"skill": "audit1c-checks", "title": "Проверки", "score": 0.18}]}
    d = _d("проверь идею ниже - идея сервиса - надстройка над гитхаб с рейтингами", plan=plan)
    assert d.kind == "ask", d.why
    assert d.questions and "слишком много навыков" in d.questions[0]
    assert d.facts["догадка"][:1] == ["audit1c-extract"], d.facts
    assert any("догадка" in w for w in d.why), d.why


def test_уверенный_лидер_собирается_как_прежде():
    plan = {"ok": True, "report_template": "tickets",
            "steps": [{"skill": "to-tickets", "title": "Задачи в трекер", "score": 0.9}]}
    d = _d("нарежь задачи в трекере по решениям сверки", plan=plan)
    assert d.kind == "build" and d.skills == ["to-tickets"]
    assert d.facts["совпадение_навыка"] == 0.9


def test_планировщик_отдаёт_счёт_шага():
    """Счёт нужен числом: по словам в «why» решение принимать нельзя."""
    from server import planner as P
    p = P.plan("нарежь задачи в трекере", CATALOG, entities=set(), slots=set(), max_steps=2)
    assert p["steps"] and p["steps"][0].get("score") is not None, p["steps"]


def test_веб_тоже_спрашивает_решение_у_оркестратора():
    """Один вопрос на все каналы: иначе веб предлагает собрать, а чат — запустить похожего агента."""
    web = (ROOT / "webapp" / "src" / "template.html").read_text(encoding="utf-8")
    assert "'/api/orchestrate'" in web, "веб всё ещё решает сам через планировщик"
    i = web.index("'/api/orchestrate'")
    body = web[i:i + 2600]
    for kind in ("d.kind === 'build'", "d.kind === 'ask'", "d.kind === 'agent'", "d.kind === 'chat'"):
        assert kind in body, f"веб не разбирает вид решения: {kind}"
    assert "planAuto: p" in body, "решение не приведено к виду плана — сборка по кнопке сломается"
    # Трасса решения показывается человеку: выбор должен быть проверяем, а не на слово.
    assert "(p.decision || {}).why" in web or "((p.decision || {}).why)" in web


def test_собранный_бандл_веба_содержит_оркестратор():
    """webapp/index.html — артефакт сборки: без пересборки правка не доедет до стенда."""
    idx = (ROOT / "webapp" / "index.html").read_text(encoding="utf-8")
    assert "/api/orchestrate" in idx
