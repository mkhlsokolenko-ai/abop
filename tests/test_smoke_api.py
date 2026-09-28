# -*- coding: utf-8 -*-
"""Smoke-тесты API ABOP для CI (Блок 5): поднимаем FastAPI-приложение in-process без Postgres/Keycloak
(все сторы падают в память, пользователь — dev-admin) и проверяем, что ключевые ручки живы и
отвечают ожидаемой формой. Не проверяет LLM-прогоны (нет модели в CI)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "KEYCLOAK_JWKS_INTERNAL", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ.setdefault("ABOP_RUN_WORKERS", "0")
os.environ["ABOP_DEV_AUTH"] = "1"   # без Keycloak API закрыт (fail-closed); тесты — dev-admin явно


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from server import web_api
    with TestClient(web_api.app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200


def test_index_is_bundle(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert '__bundler/template' in body and '__bundler/manifest' in body
    assert 'data-theme' in body and '__abopA11y' in body       # тема + a11y-слой (Блок 2)


def test_me_dev_admin(client):
    r = client.get("/api/me")
    assert r.status_code == 200
    assert (r.json().get("user") or {}).get("level") == "admin"


def test_agents_and_families(client):
    assert client.get("/api/agents").status_code == 200
    assert "agents" in client.get("/api/agents").json()
    assert client.get("/api/families").status_code == 200


def test_fleet_shape(client):
    r = client.get("/api/fleet")
    assert r.status_code == 200
    d = r.json()
    for k in ("deployments", "live", "alerts", "series", "queue"):
        assert k in d
    assert len(d["series"]) == 8


def test_queue_and_jobs(client):
    assert client.get("/api/runs/queue").status_code == 200
    assert "jobs" in client.get("/api/runs/jobs").json()


def test_findings_journal_and_metrics(client):
    r = client.get("/api/findings")
    assert r.status_code == 200
    d = r.json()
    assert "items" in d and "metrics" in d
    assert d["metrics"]["thresholds"] == {"precision": 80, "cross_share": 50, "manual_miss": 3}


def test_norms_served(client):
    r = client.get("/api/audit1c/norms/01_nds_scheta_faktury.md")
    assert r.status_code == 200 and "Чем грозит" in r.text
    assert client.get("/api/audit1c/norms/../secret.md").status_code in (404, 422)


def test_findings_normalizer_on_fixture():
    from server import findings
    run = {"run_id": "r1", "agent_id": "a", "created_at": "2026-09-26T00:00:00",
           "findings": [{"id": "B1-РТ-0002", "класс": "B", "проверка": "Реализация без счёта-фактуры выданного", "серьёзность": "высокая",
                         "документ": {"uuid": "1-2", "тип": "РеализацияТоваровУслуг", "Номер": "РТ-0002", "Дата": "2024-02-15", "Контрагент": "ООО Т"},
                         "описание": "НДС не предъявлен", "доказательство": "нет СФ", "сумма": 194000.0}]}
    cards = findings.cards_for_run(run, {})
    assert len(cards) == 1
    c = cards[0]
    assert c["section_from"] == "Реализация" and c["section_to"] == "НДС" and c["cross"]
    assert c["amount_text"].startswith("194 000")
    assert any(l["broken"] for l in c["chain"])
    assert c["explain"]["action"] and c["norm"]["url"].endswith(".md")
    m = findings.pilot_metrics([dict(c, label={"decision": "confirmed", "manual_miss": True})])
    assert m["precision"] == 100 and m["manual_miss"] == 1 and m["cross_share"] == 100


def test_run_queue_memory_roundtrip():
    import asyncio
    from server import run_queue
    async def flow():
        job = await run_queue.enqueue(agent_id="a1", actor="dev", payload={"context": "x"}, kind="run")
        assert job["status"] == "queued"
        got = await run_queue.get(job["id"])
        assert got and got["id"] == job["id"]
        # прогресс прогона: фаза + навыки → в статусе задания (checkpoint.run, чекпоинт цепочки не трогаем)
        await run_queue.set_checkpoint(job["id"], {"step": 1, "steps_total": 2, "steps": []})
        await run_queue.set_progress(job["id"], {"phase": "навыки", "total": 3, "skills": {"audit1c-rank": {"state": "running"}}})
        pub = run_queue.public(await run_queue.get(job["id"]))
        assert pub["progress"]["run"]["phase"] == "навыки" and pub["progress"]["run"]["skills"]["audit1c-rank"]["state"] == "running"
        assert pub["progress"]["steps_total"] == 2   # чекпоинт цепочки сохранён
        assert await run_queue.cancel(job["id"], "dev") is not None
    asyncio.run(flow())


def test_skill_templates_load_and_strict_safe():
    import json
    from server import skill_templates as st
    t = st.load_all()
    for sid in ("audit1c-checks", "audit1c-explain", "invest1c-verdict", "mail-triage", "daily-plan", "client-letter", "bft-draft", "finance-report"):
        assert sid in t, sid
    for sid, tp in t.items():
        s = json.dumps(tp["json_schema"], ensure_ascii=False)
        assert '"$ref"' not in s and '"pattern"' not in s and '"format"' not in s, sid
        assert tp["json_schema"].get("type") == "object" and tp["json_schema"].get("required"), sid
        assert tp["instruction"], sid
    desc = st.describe_for_prompt(t["audit1c-explain"])
    assert "находки[].откуда.документы" in desc and "критично|существенно" in desc


def test_skill_templates_seed_and_default_binding():
    import asyncio
    from server import schema_store, skill_templates as st

    async def flow():
        await st.seed(schema_store)              # идемпотентно (приложение могло посеять при старте)
        ids = {t["id"] for t in await schema_store.all()}
        assert len([i for i in ids if i in st.load_all()]) >= 16
        n2 = await st.seed(schema_store)         # повторный посев без изменений — ничего не пишет
        assert n2 == 0
        tpl = await schema_store.get("audit1c-explain")
        assert tpl and tpl["builtin"] and "[repo:" in tpl["instruction"]
        rf = schema_store.response_format(tpl)
        assert rf["type"] == "json_schema" and rf["json_schema"]["strict"]
    asyncio.run(flow())


def test_render_struct_generic():
    from server import runner
    txt = runner._render_struct({"резюме": "ок", "находки_x": [{"заголовок": "РТ-0002 без СФ", "ранг": "критично", "откуда": {"документы": ["РеализацияТоваровУслуг № РТ-0002"]}}], "пусто": []})
    assert "резюме: ок" in txt and "• РТ-0002 без СФ" in txt and "ранг: критично" in txt and "документы" in txt
    assert "пусто" not in txt
    # схема находок по умолчанию — прежний рендер
    assert "• обс" in runner._render_struct({"находки": [{"наблюдение": "обс", "запись": "r1"}], "итог": "и"})


def test_schema_templates_persist_import_edit_reset(client):
    """Шаблоны — персистентны в БД: импорт через API (без пересборки), max_tokens сохраняется,
    ручная правка снимает builtin (посев не перекроет), reset возвращает версию репо."""
    from server import skill_templates as st
    repo = st.load_one("audit1c-rank")
    assert repo and repo["max_tokens"] == 4000
    # импорт «нового» шаблона с source=repo → builtin, max_tokens в карточке
    tpl = {"id": "zz-test-import", "name": "Тест импорта", "instruction": "Извлеки поля только из данных.",
           "json_schema": {"type": "object", "additionalProperties": False, "required": ["итог"],
                           "properties": {"итог": {"type": "string"}}}, "max_tokens": 2500}
    r = client.post("/api/schema-templates/import", json={"templates": [tpl], "source": "repo"})
    assert r.status_code == 200 and r.json()["imported"] == ["zz-test-import"], r.text
    got = client.get("/api/schema-templates/zz-test-import").json()
    assert got["builtin"] and got["max_tokens"] == 2500 and "[repo:" in got["instruction"]
    # невалидная схема (нет additionalProperties:false) → в errors, не пишется
    bad = dict(tpl, id="zz-bad", json_schema={"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}})
    r = client.post("/api/schema-templates/import", json={"templates": [bad]})
    assert r.json()["imported"] == [] and "zz-bad" in r.json()["errors"]
    # ручная правка → builtin False, маркеры вычищены, max_tokens меняется
    r = client.post("/api/schema-templates/zz-test-import", json={"instruction": "Правка руками.", "max_tokens": 3000})
    assert r.status_code == 200 and r.json()["builtin"] is False and r.json()["max_tokens"] == 3000
    assert "[repo:" not in r.json()["instruction"]
    # повторный импорт из репо без force ручную правку не трогает
    r = client.post("/api/schema-templates/import", json={"templates": [tpl], "source": "repo"})
    assert r.json()["skipped"] == ["zz-test-import"]
    # реальный навык: правка руками, потом reset → версия репо (builtin, max_tokens 4000)
    r = client.post("/api/schema-templates/audit1c-rank", json={"max_tokens": 2600})
    assert r.json()["builtin"] is False and r.json()["max_tokens"] == 2600
    r = client.post("/api/schema-templates/audit1c-rank/reset")
    assert r.status_code == 200 and r.json()["builtin"] and r.json()["max_tokens"] == 4000
    assert st.max_tokens_of(r.json()) == 4000
    assert client.post("/api/schema-templates/nope-no-such/reset").status_code == 404


def test_delivery_render_commands_from_template():
    """Секция delivery шаблона → команды коннектора из structured-ответа: фильтр по рангу, подстановки,
    списки маркерами, корень через $., превью без скриптов."""
    from server import delivery as dl, skill_templates as st
    spec = st.load_one("audit1c-explain")["delivery"]
    assert not dl.validate_delivery(spec)
    struct = {"резюме_для_главбуха": "две критичные", "итог": "и",
              "находки": [{"id": "A", "ранг": "критично", "заголовок": "РТ-0008 без себестоимости", "что_не_сходится": "нет Дт 90.02",
                           "откуда": {"документы": ["РТ-0008 от 2024-08-15"], "проводки": ["Дт 62 Кт 90.01 236 000,00 ₽"], "доказательство": "нет 90.02"},
                           "чем_грозит": "завышена прибыль", "норма": {"статья": "ФСБУ 5/2019 п.15", "цитата": "…", "источник": "ФСБУ"},
                           "последствия": ["уточнёнка"], "что_проверить": ["проводку"], "статус": "факт"},
                          {"id": "C", "ранг": "формально", "заголовок": "дубль", "что_не_сходится": "x", "откуда": {"документы": [], "проводки": [], "доказательство": ""},
                           "чем_грозит": "", "норма": {"статья": "", "цитата": "", "источник": ""}, "последствия": [], "что_проверить": [], "статус": "гипотеза"}]}
    cmds = dl.build_commands(spec, struct, skill="audit1c-explain")
    assert len(cmds) == 1 and cmds[0]["system"] == "redmine" and cmds[0]["type"] == "issue.create"
    p = cmds[0]["payload"]
    assert p["subject"] == "Аудит 1С · критично · A · РТ-0008 без себестоимости"
    assert "- РТ-0008 от 2024-08-15" in p["description"] and "- Дт 62 Кт 90.01 236 000,00 ₽" in p["description"]
    assert "Резюме для главбуха: две критичные" in p["description"] and "ФСБУ 5/2019 п.15" in p["description"]
    assert cmds[0]["source"] == {"skill": "audit1c-explain", "item": "A"}
    html = dl.preview_html(cmds[0])
    assert "redmine · issue.create" in html and "<pre" in html and "<script" not in html
    # rank: одна сводная команда (без each), список рейтинга маркерами
    rk = st.load_one("audit1c-rank")["delivery"]
    c2 = dl.build_commands(rk, {"порог_существенности": {"сумма": "100 000 ₽", "как_выведен": "медиана"},
                                "рейтинг": [{"место": 1, "id": "B", "ранг": "критично"}], "топ_3_действия": ["выставить СФ"], "итог": "и"})
    assert len(c2) == 1 and "порог 100 000 ₽" in c2[0]["payload"]["subject"] and "- выставить СФ" in c2[0]["payload"]["description"]
    assert dl.validate_delivery({"system": "redmine"}) and dl.validate_delivery({"system": "redmine", "type": "x", "payload": {}, "foo": 1})


def test_delivery_for_all_demo_cases():
    """Доставка по шаблонам покрывает четыре демо-кейса: аудит → задачи Redmine, инвест → страница вики,
    дайджест → письмо, БФТ → задача. Проверяем целевую систему, тип команды и что подстановки заполнены."""
    from server import delivery as dl, skill_templates as st
    want = {"audit1c-explain": ("redmine", "issue.create"), "audit1c-rank": ("redmine", "issue.create"),
            "invest1c-verdict": ("bookstack", "page.publish"), "client-letter": ("mailpit", "email.send"),
            "bft-draft": ("redmine", "issue.create")}
    for sid, (sys_, typ) in want.items():
        spec = (st.load_one(sid) or {}).get("delivery")
        assert spec and not dl.validate_delivery(spec), sid
        assert (spec["system"], spec["type"]) == (sys_, typ), sid
    # письмо: адрес и тема берутся из ответа навыка, тело собирается списками
    cmds = dl.build_commands(st.load_one("client-letter")["delivery"],
                             {"кому": "client@demo.local", "тема": "Статус", "приветствие": "Добрый день!",
                              "в_работе": [{"задача": "счёт", "статус": "в процессе"}], "план_на_сегодня": ["проверить"],
                              "сроки": [{"что": "оплата", "когда": "30.09"}], "нужно_от_вас": ["подтвердить"],
                              "подпись": "ABOP", "текст_письма": "Проверяем.", "требуется_подтверждение": True}, skill="client-letter")
    assert len(cmds) == 1 and cmds[0]["payload"]["to"] == "client@demo.local" and cmds[0]["payload"]["subject"] == "Статус"
    assert "- проверить" in cmds[0]["payload"]["body"] and "задача: счёт" in cmds[0]["payload"]["body"]
    # страница вики: book_id остаётся числом (не подстановка), заголовок и markdown — из полей
    c2 = dl.build_commands(st.load_one("invest1c-verdict")["delivery"],
                           {"заключения": [{"id": "INV-1", "существенность": "критично"}], "требуется_подтверждение": True, "итог": "разрыв"}, skill="invest1c-verdict")
    assert c2[0]["payload"]["book_id"] == 1 and "разрыв" in c2[0]["payload"]["title"] and "- id: INV-1" in c2[0]["payload"]["markdown"]


def test_delivery_templates_make_hitl_preview_and_take_connector_result(client):
    """Прогон → structured-ответ навыка → HITL-заявка «команда» с превью (наружу ничего) → approve публикует
    (шина в тестах не подключена → 503, честно) → ответ коннектора command.done ложится в заявку."""
    import asyncio
    from server import hitl_store, systems_store, web_api
    from server import delivery as dl

    async def flow():
        await systems_store.save("redmine", {"kind": "rest", "base_url": "http://x", "scope": []})
        agent = {"id": "ag-tpl", "name": "Аудитор", "family": "finance"}
        result = {"run_id": "run-1", "delivery": [], "skill_outputs": [{"skill": "audit1c-rank", "structured": {
            "порог_существенности": {"сумма": "100 000 ₽", "как_выведен": "медиана"},
            "рейтинг": [{"место": 1, "id": "B", "ранг": "критично", "сумма_влияния": "210 000 ₽", "риск": "налоговый", "охват": 1, "обоснование": "СФ"}],
            "топ_3_действия": ["выставить СФ"], "итог": "и"}}]}
        from server import skill_templates as st
        schemas = {"audit1c-rank": {"delivery": st.load_one("audit1c-rank")["delivery"]}}
        await web_api._deliver_templates(agent, result, "tester", schemas)
        d = result["delivery"]
        assert len(d) == 1 and d[0]["mode"] == "awaiting_hitl" and d[0]["channel"] == "redmine" and d[0]["hitl_id"]
        assert "порог 100 000 ₽" in d[0]["subject"]
        # «только в чат» → наружу ничего
        r2 = {"delivery": [], "skill_outputs": result["skill_outputs"]}
        await web_api._deliver_templates(agent, r2, "tester", schemas, deliver_filter="chat")
        assert r2["delivery"] == []
        return d[0]["hitl_id"]
    hid = asyncio.run(flow())
    it = client.get(f"/api/hitl/{hid}").json()
    assert it["kind"] == "command" and it["system"] == "redmine" and it["type"] == "issue.create"
    assert "порог 100 000 ₽" in it["subject"] and "<pre" in it["html"] and it["state"] == "pending"
    assert any(q["id"] == hid for q in client.get("/api/hitl/queue").json()["queue"])
    r = client.post(f"/api/hitl/{hid}/approve", json={"decision": "approve"})
    assert r.status_code == 503   # шина не подключена — команду некуда публиковать; заявка остаётся pending
    assert client.get(f"/api/hitl/{hid}").json()["state"] == "pending"

    async def feedback():
        await hitl_store.update_payload(hid, {"command_id": "cmd-42"})
        await web_api._on_command_result("redmine", {"type": "command.done", "payload": {"command_id": "cmd-42", "command_type": "issue.create",
                                                                                          "result": {"issue_id": 7, "url": "http://x/issues/7"}, "ms": 120}})
        await web_api._on_command_result("redmine", {"type": "command.done", "payload": {"command_id": "no-such"}})
    asyncio.run(feedback())
    it = client.get(f"/api/hitl/{hid}").json()
    assert it["result_state"] == "done" and it["result"]["url"] == "http://x/issues/7" and it["command_id"] == "cmd-42"
    r = client.post(f"/api/hitl/{hid}/approve", json={"decision": "reject"})
    assert r.status_code == 200 and r.json()["state"] == "rejected"


def test_report_templates_from_repo_and_run_report_html(client):
    """PDF/HTML-отчёт по шаблону: шаблоны из reports/ сеются в БД, структурированные ответы навыков
    попадают в {{skills}} и {{skill_<sid>}}, GET /api/runs/{id}/report отдаёт HTML по кейс-шаблону."""
    import asyncio
    from server import report_store, run_store
    files = report_store.load_files()
    assert {"default", "audit1c", "invest", "digest"} <= set(files) and "{{skill_audit1c_rank}}" in files["audit1c"]["html"]
    tpls = {t["id"]: t for t in client.get("/api/report-templates").json()["templates"]}
    assert tpls["audit1c"]["builtin"] and "{{skill_audit1c_explain}}" in tpls["audit1c"]["html"] and ".tbl" in tpls["audit1c"]["css"]
    html = report_store.struct_html({"порог_существенности": {"сумма": "100 000 ₽", "как_выведен": "медиана"},
                                     "рейтинг": [{"место": 1, "id": "B", "ранг": "критично", "сумма_влияния": "210 000 ₽"}],
                                     "топ_3_действия": ["выставить СФ", "проверить договор"], "итог": "и" * 130})
    assert "<table class='tbl'>" in html and "<th>ранг</th>" in html and "<li>выставить СФ</li>" in html and "class='lead'" in html
    assert "<script" not in report_store.struct_html({"x": "<script>alert(1)</script>"})

    async def mk():
        return await run_store.save({"agent_id": "ag-rep", "verdict": {"ok": True, "autonomy_used": "A1"}, "waves": [[]],
                                     "findings": [{"класс": "A", "проверка": "нет СФ", "описание": "РТ-0002"}],
                                     "findings_summary": {"total": 1, "by_class": {"A": 1, "B": 0, "C": 0, "D": 0}},
                                     "skill_outputs": [{"skill": "audit1c-rank", "structured": {"порог_существенности": {"сумма": "100 000 ₽", "как_выведен": "медиана"},
                                                                                                 "рейтинг": [{"место": 1, "id": "A", "ранг": "критично"}], "топ_3_действия": ["выставить СФ"], "итог": "ок"}}],
                                     "delivery": [{"channel": "redmine", "to": "redmine/issue.create", "mode": "awaiting_hitl"}]})
    saved = asyncio.run(mk())
    rid = saved.get("id") or saved.get("run_id")
    r = client.get(f"/api/runs/{rid}/report")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    body = r.text
    assert "Ранжирование по существенности" in body and "<th>ранг</th>" in body and "выставить СФ" in body and "нет СФ" in body
    assert "{{" not in body   # все плейсхолдеры подставлены (пустые → пусто)
    r2 = client.get(f"/api/runs/{rid}/report?template=digest")
    assert r2.status_code == 200 and "Задачи и сводка" in r2.text and "<th>ранг</th>" in r2.text
    assert client.get("/api/runs/no-such-run/report").status_code == 404


def test_bus_dlq_ack_and_replay(client):
    """Разбор DLQ: отметки «списано/повторено» живут отдельно от Kafka (dlq_acks); без шины повтор честно 503."""
    r = client.get("/api/bus/dlq")
    assert r.status_code == 200 and r.json()["count"] == 0 and r.json()["open"] == 0
    r = client.post("/api/bus/dlq/ack", json={"partition": 0, "offset": 17, "note": "ошибка коннектора разобрана руками"})
    assert r.status_code == 200 and r.json()["key_id"] == "0:17" and r.json()["ack"]["state"] == "acked"
    assert client.post("/api/bus/dlq/ack", json={"partition": "x"}).status_code == 422
    r = client.post("/api/bus/dlq/replay", json={"partition": 0, "offset": 17})
    assert r.status_code == 503   # PgBus в тестах: повторить некуда
    b = client.get("/api/bus").json()
    assert "systems" in b and "bus" in b


def test_llm_status_banner(client):
    """Состояние модели для баннера: активная модель, каскад, тариф, доступность своего бокса, откаты."""
    r = client.get("/api/llm/status")
    assert r.status_code == 200
    d = r.json()
    assert d["active"] and isinstance(d["cascade"], list) and "self_hosted" in d
    assert d["cost_per_1k_rub"] is not None and isinstance(d["free"], bool)
    assert "health" in d["local"] and "total" in d["fallback"]
    assert d["gpu"] is None or "credit" in d["gpu"]   # GPU-блок только если задан ключ в окружении
    from server import clients, observability as obs
    obs.inc("abop_llm_fallback_total", model="local/qwen3-30b-a3b")
    clients._LAST_FALLBACK.update({"at": "2026-09-27T12:00:00+00:00", "model": "local/qwen3-30b-a3b", "error": "ReadTimeout: "})
    d2 = client.get("/api/llm/status").json()
    assert d2["fallback"]["total"] >= 1 and d2["fallback"]["last"]["model"] == "local/qwen3-30b-a3b"
    assert obs.counter_total("abop_llm_fallback_total") >= 1


def test_runner_context_only_skill_runs_and_skips_are_reported():
    """Навык без объявленного data-scope (письмо/БФТ) выполняется по контексту задачи, а не выпадает молча;
    навык с источниками, но без данных в store, отмечается как «пропущен» в прогрессе."""
    import asyncio
    from server import runner

    agent = {"id": "ag-ctx", "name": "Тест", "family": "management", "graph": {"nodes": [
        {"id": "client-letter", "kind": "skill", "skill": "client-letter"},
        {"id": "mail-triage", "kind": "skill", "skill": "mail-triage"}], "edges": []}}
    contract = {"audit_id": "t-ctx", "autonomy_level": "A1", "criticality": "T3", "metrics": {}}
    seen: list = []

    async def chat_fn(**kw):
        return {"text": '{"кому":"c@x","тема":"Статус","приветствие":"Добрый день!","в_работе":[],"план_на_сегодня":["шаг"],'
                        '"сроки":[],"нужно_от_вас":[],"подпись":"ABOP","текст_письма":"Готово.","требуется_подтверждение":true}',
                "model": "local/test", "input_tokens": 10, "output_tokens": 20}

    async def flow():
        return await runner.run_live(
            agent, contract, lambda sid: {"mode": "write", "output": "structured"},
            data_query=lambda e, limit=0: [],                     # store пуст
            skill_sources=lambda sid: ([] if sid == "client-letter" else [{"entity": "email"}]),
            load_body=lambda sid: "методика", chat_fn=chat_fn,
            user_context="Собери дайджест и напиши письмо заказчику",
            on_progress=lambda sid, state, **kw: seen.append((sid, state)))
    res = asyncio.run(flow())
    got = {sid: st for sid, st in seen}
    assert got["client-letter"] in ("done", "error"), seen        # без данных, но с контекстом — выполнился
    assert got["mail-triage"] == "skipped", seen                  # объявил источник, данных нет — пропущен
    letters = [f for f in res["findings"] if f.get("skill") == "client-letter"]
    assert letters and isinstance(letters[0].get("structured"), dict) and letters[0]["structured"]["тема"] == "Статус"


def test_delivery_skips_command_with_empty_required_field():
    """Навык не дал адрес/тему — команда не создаётся (иначе коннектор упадёт «payload.to пустой» в DLQ),
    причина видна отдельной записью пропуска."""
    from server import delivery as dl, skill_templates as st
    spec = st.load_one("client-letter")["delivery"]
    empty = {"кому": "", "тема": "", "приветствие": "", "в_работе": [], "план_на_сегодня": [], "сроки": [],
             "нужно_от_вас": [], "подпись": "", "текст_письма": "", "требуется_подтверждение": False}
    cmds, skipped = dl.split_skipped(dl.build_commands(spec, empty, skill="client-letter"))
    assert cmds == [] and skipped and "to" in skipped[0]["reason"] and "subject" in skipped[0]["reason"]
    ok, sk2 = dl.split_skipped(dl.build_commands(spec, {**empty, "кому": "c@x", "тема": "Статус", "текст_письма": "ок"}, skill="client-letter"))
    assert len(ok) == 1 and ok[0]["payload"]["to"] == "c@x" and not sk2
    # явный require валидируется на несуществующие ключи
    assert dl.validate_delivery({"system": "mailpit", "type": "email.send", "payload": {"to": "x"}, "require": ["subject"]})
    assert not dl.validate_delivery({"system": "mailpit", "type": "email.send", "payload": {"to": "{{кому}}"}, "require": ["to"]})


def test_template_forces_structured_over_freeform_default():
    """Навык, помеченный в коде как «документ» (client-letter), при наличии шаблона извлечения отвечает
    по схеме — иначе доставка и секции отчёта не собираются. Явный freeform на узле шаблон не перебивает."""
    import asyncio
    from server import runner, skill_templates as st

    tpl = st.load_one("client-letter")
    schema = {"response_format": {"type": "json_schema", "json_schema": {"name": "t", "strict": True, "schema": tpl["json_schema"]}},
              "instruction": tpl["instruction"], "max_tokens": None, "delivery": tpl["delivery"], "force_struct": True}
    seen = []

    async def chat_fn(**kw):
        seen.append(bool(kw.get("response_format")))
        return {"text": '{"кому":"c@x","тема":"Т","приветствие":"","в_работе":[],"план_на_сегодня":[],"сроки":[],'
                        '"нужно_от_вас":[],"подпись":"","текст_письма":"Готово.","требуется_подтверждение":false}',
                "model": "local/test", "input_tokens": 5, "output_tokens": 5}

    def run(node_output=None):
        node = {"id": "client-letter", "kind": "skill", "skill": "client-letter"}
        if node_output:
            node["output"] = node_output
        agent = {"id": "ag-f", "name": "Т", "family": "management", "graph": {"nodes": [node], "edges": []}}
        return asyncio.run(runner.run_live(agent, {"audit_id": "t", "autonomy_level": "A1", "criticality": "T3", "metrics": {}},
                                          lambda sid: {"mode": "write", "output": "freeform"},   # дефолт кода — документ
                                          data_query=lambda e, limit=0: [], skill_sources=lambda sid: [],
                                          load_body=lambda sid: "методика", chat_fn=chat_fn,
                                          user_context="Письмо заказчику", skill_schemas={"client-letter": schema}))
    res = run()
    f = [x for x in res["findings"] if x.get("skill") == "client-letter"][0]
    assert isinstance(f.get("structured"), dict) and f["structured"]["кому"] == "c@x"   # шаблон применён
    assert seen == [True], seen          # схема ушла в модель (strict structured output)
    run(node_output="freeform")
    assert seen == [True, False], seen    # явный выбор «рассуждения» на узле: схему не навязываем


def test_run_diff_between_two_runs(client):
    """Сравнение прогонов: что в находках появилось, ушло и изменилось, плюс навыки и метрики.
    Без ?vs берётся предыдущий прогон того же агента — регресс после правки шаблонов виден сразу."""
    import asyncio
    from server import run_store

    def mk(findings, rub, skills):
        return {"agent_id": "ag-diff", "verdict": {"ok": True, "autonomy_used": "A1"}, "waves": [[]],
                "findings": findings, "skill_outputs": [{"skill": s, "structured": {}} for s in skills],
                "run_metrics": {"cost": {"rub": rub, "by_model": {"local/qwen3-30b-a3b": 1}}, "timings": {"total_ms": 1000}},
                "delivery": [{"mode": "awaiting_hitl"}]}

    base = asyncio.run(run_store.save(mk(
        [{"id": "A1", "класс": "A", "серьёзность": "высокая", "проверка": "нет себестоимости", "сумма": "236000"},
         {"id": "B2", "класс": "B", "серьёзность": "средняя", "проверка": "нет счёта-фактуры", "сумма": "194000"}],
        0.0, ["audit1c-checks", "audit1c-rank"])))
    cur = asyncio.run(run_store.save(mk(
        [{"id": "A1", "класс": "A", "серьёзность": "высокая", "проверка": "нет себестоимости", "сумма": "999000"},
         {"id": "C3", "класс": "C", "серьёзность": "низкая", "проверка": "дубль контрагента", "сумма": ""}],
        0.0, ["audit1c-checks", "audit1c-rank", "audit1c-explain"])))
    rid_b, rid_c = base.get("id") or base.get("run_id"), cur.get("id") or cur.get("run_id")

    d = client.get(f"/api/runs/{rid_c}/diff?vs={rid_b}").json()
    f = d["находки"]
    assert [x["id"] for x in f["добавились"]] == ["C3"] and [x["id"] for x in f["ушли"]] == ["B2"]
    assert len(f["изменились"]) == 1 and f["изменились"][0]["id"] == "A1" and f["изменились"][0]["поля"] == ["сумма"]
    assert f["всего_было"] == 2 and f["всего_стало"] == 2
    assert d["навыки"]["только_сейчас"] == ["audit1c-explain"] and "audit1c-checks" in d["навыки"]["общие"]
    assert d["метрики"]["стало"]["commands"] == 1 and d["метрики"]["было"]["rub"] == 0.0
    # без vs — предыдущий прогон того же агента
    d2 = client.get(f"/api/runs/{rid_c}/diff").json()
    assert d2["base_run_id"] == rid_b
    assert client.get(f"/api/runs/{rid_b}/diff?vs=no-such-run").status_code == 404


def test_report_includes_expert_labels(client):
    """Экспертная разметка находок попадает в отчёт: сводка метрик против порогов и статусы находок.
    Без разметки секция пустая (плейсхолдеры не рендерятся мусором)."""
    import asyncio
    from server import finding_store, run_store

    run = {"agent_id": "audit1c-holding-2026.v9", "verdict": {"ok": True, "autonomy_used": "A1"}, "waves": [[]],
           "findings": [{"id": "A1", "класс": "A", "серьёзность": "высокая", "проверка": "Реализация без списания себестоимости",
                         "описание": "РТ-0008", "сумма": 236000},
                        {"id": "B2", "класс": "B", "серьёзность": "средняя", "проверка": "Реализация без счёта-фактуры выданного",
                         "описание": "РТ-0002", "сумма": 194000}],
           "findings_summary": {"total": 2, "by_class": {"A": 1, "B": 1, "C": 0, "D": 0}}}
    saved = asyncio.run(run_store.save(run))
    rid = saved.get("id") or saved.get("run_id")
    html_no_labels = client.get(f"/api/runs/{rid}/report?template=audit1c").text
    assert "Размечено:" not in html_no_labels and "{{" not in html_no_labels

    asyncio.run(finding_store.set_label(rid, "A1", expert="эксперт", decision="confirmed", manual_miss=True, comment="вручную не увидели"))
    asyncio.run(finding_store.set_label(rid, "B2", expert="эксперт", decision="rejected", manual_miss=False, comment=""))
    html = client.get(f"/api/runs/{rid}/report?template=audit1c").text
    assert "Экспертная разметка (слепая проверка)" in html and "Размечено:</b> 2 из 2" in html
    assert "подтверждено 1" in html and "отклонено 1" in html
    assert "Точность значимых" in html and "«Вручную бы не нашли»" in html
    assert "подтверждена экспертом" in html and "отклонена экспертом" in html and "вручную бы не нашли" in html
    assert "{{" not in html
    # снятие разметки: ошибочная метка не остаётся в метриках пилота навсегда
    r = client.delete(f"/api/runs/{rid}/findings/A1/label")
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.delete(f"/api/runs/{rid}/findings/A1/label").json()["removed"] is False
    assert "Размечено:</b> 1 из 2" in client.get(f"/api/runs/{rid}/report?template=audit1c").text


def test_auth_login_returns_refresh_and_refresh_endpoint_exists(client, monkeypatch):
    """Вход отдаёт refresh_token и срок жизни, есть /api/auth/refresh: без этого сессия веба жила 5 минут
    и всё превращалось в 401 («сервер недоступен»)."""
    import httpx
    from server import web_api

    class _R:
        status_code = 200

        @staticmethod
        def json():
            # токен-заглушка с полезной нагрузкой {"preferred_username": "u"}
            return {"access_token": "eyJhbGciOiJub25lIn0.eyJwcmVmZXJyZWRfdXNlcm5hbWUiOiJ1In0.",
                    "refresh_token": "rt-1", "expires_in": 300, "refresh_expires_in": 1800}

    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            assert "openid-connect/token" in url
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", _C)
    r = client.post("/api/auth/login", json={"username": "u", "password": "p"})
    assert r.status_code == 200
    d = r.json()
    assert d["access_token"] and d["refresh_token"] == "rt-1" and d["expires_in"] == 300
    r2 = client.post("/api/auth/refresh", json={"refresh_token": "rt-1"})
    assert r2.status_code == 200 and r2.json()["access_token"] and r2.json()["refresh_token"] == "rt-1"
    assert client.post("/api/auth/refresh", json={}).status_code == 422
    assert web_api  # ссылка на модуль, чтобы не потерять импорт при рефакторинге


def test_nlu_stages_and_lexicon_matching():
    """Разбор фразы: нормализация лексем, этапы по маркерам и перечислению, триггер с окружением."""
    from server import lexicon, nlu

    assert nlu.stem("проверь") == nlu.stem("проверить")            # формы слова сходятся
    assert nlu.stem("отчёты") == nlu.stem("отчет") == "отчет"
    assert nlu.stem("1С".lower()) == "1с" and nlu.stem("НДС".lower()) == "ндс"   # короткие не режем

    sts = nlu.split_stages("Сначала проверь данные 1С за квартал, потом подготовь заключение для главбуха")
    assert [s.order for s in sts] == [1, 2] and sts[0].kind == "проверка" and sts[1].kind == "подготовка"
    assert "1с" in sts[0].after and "главбух" in sts[1].after
    assert "снача" not in sts[0].before and "потом" not in sts[1].before   # маркеры порядка — не лексемы
    assert sts[1].channel == "email"                                       # адресат после триггера

    multi = nlu.split_stages("Проверь 1С, найди расхождения, объясни их и подготовь заключение в вики")
    assert len(multi) == 4 and multi[-1].channel == "bookstack"
    assert [s.kind for s in multi] == ["проверка", "анализ", "объяснение", "подготовка"]
    one = nlu.split_stages("проверь 1С за квартал")                         # хвост без триггера не рвёт этап
    assert len(one) == 1 and "кварт" in one[0].after

    agent = {"id": "a1", "name": "Аудитор данных в 1С", "role": "auditor-1c", "family": "audit",
             "graph": {"nodes": [{"kind": "skill", "skill": "audit1c-checks"}, {"kind": "out", "out": {"channel": "email"}}]}}
    terms = lexicon.build(agent, skills={"audit1c-checks": ("Проверки аудита 1С", "расхождения по НДС и документам")})
    assert terms and lexicon.score(nlu.lexemes("проверь 1С"), terms) > 0.3
    assert lexicon.score(nlu.lexemes("напиши письмо клиенту"), terms) < 0.15   # чужая задача не липнет
    assert lexicon.doc_hash(agent) == lexicon.doc_hash(dict(agent))            # отпечаток стабилен


def test_pipeline_suggest_uses_stage_order(client, monkeypatch):
    """Цепочка собирается по этапам фразы: порядок шагов = порядок этапов, при низкой уверенности — предупреждение."""
    import asyncio
    from server import agent_store, lexicon, web_api

    async def mk():
        for name, role, skill, ch in (("Аудитор 1С", "auditor-1c", "audit1c-checks", "email"),
                                      ("Коммуникатор", "comms", "client-letter", "email")):
            a = await agent_store.save(name=name, audit_id="nlu-" + role, version=1, family="", role=role,
                                       autonomy_max="A1", graph={"nodes": [
                                           {"id": skill, "kind": "skill", "skill": skill},
                                           {"id": "out", "kind": "out", "out": {"channel": ch}}]})
            await web_api._refresh_lexicon(a)
    asyncio.run(mk())

    async def no_llm(**kw):
        raise RuntimeError("LLM отключён в тесте")
    monkeypatch.setattr(web_api.clients, "chat", no_llm)

    r = client.post("/api/pipelines/suggest", json={"q": "Сначала проверь 1С, потом напиши письмо клиенту"})
    d = r.json()
    assert r.status_code == 200 and len(d["steps"]) == 2
    assert [s["order"] for s in d["stages"]] == [1, 2]
    assert d["stages"][0]["kind"] == "проверка" and d["stages"][1]["kind"] == "письмо"
    assert "confidence" in d and isinstance(d["low_confidence"], bool)
    assert d["parse"]["multi_stage"] is True
    if d["low_confidence"]:
        assert d["warning"]
    rev = client.post("/api/pipelines/suggest", json={"q": "Сначала напиши письмо клиенту, потом проверь 1С"}).json()
    assert [s["kind"] for s in rev["stages"]] == ["письмо", "проверка"]      # порядок берётся из фразы


def test_nlu_management_api(client):
    """Управление подбором из UI: разбор фразы, пороги уверенности, словарь агента (добавить/запретить слово)."""
    import asyncio
    from server import agent_store, web_api

    p = client.get("/api/nlu/parse", params={"q": "Сначала проверь 1С, потом напиши письмо клиенту"}).json()
    assert p["multi_stage"] is True and len(p["stages"]) == 2 and "проверка" in p["triggers"]
    assert p["config"]["min_confidence"] == 0.35 and p["config"]["rerank"] is True

    r = client.post("/api/nlu/config", json={"min_confidence": 0.5, "rerank": False, "weak_stage": 0.1})
    assert r.status_code == 200 and r.json()["min_confidence"] == 0.5 and r.json()["rerank"] is False
    assert client.get("/api/nlu/config").json()["weak_stage"] == 0.1
    assert client.post("/api/nlu/config", json={"min_confidence": 7}).status_code == 422
    client.post("/api/nlu/config", json={"min_confidence": 0.35, "rerank": True, "weak_stage": 0.2})   # вернули дефолт

    async def mk():
        return await agent_store.save(name="Лексикон-агент", audit_id="lex-ui", version=1, family="", role="fin-analyst",
                                      autonomy_max="A1", graph={"nodes": [{"id": "dcf-valuation", "kind": "skill", "skill": "dcf-valuation"}]})
    ag = asyncio.run(mk())
    aid = ag["id"]
    asyncio.run(web_api._refresh_lexicon(ag))

    lx = client.get(f"/api/agents/{aid}/lexicon").json()
    assert lx["agent_id"] == aid and lx["seeded_count"] > 0
    assert all(t["source"] in ("навыки", "оператор") for t in lx["terms"])

    # слово оператора: агента начинает находить по нему
    before = client.post("/api/agents/match", json={"q": "оцени стоимость стартапа"}).json()["matches"]
    client.post(f"/api/agents/{aid}/lexicon", json={"add": ["стартап", "оценка стоимости"]})
    lx2 = client.get(f"/api/agents/{aid}/lexicon").json()
    assert "стартап" in lx2["manual"]
    assert lx2["terms"][0]["source"] == "оператор"          # ручные слова видны сразу, а не в хвосте
    assert {"стартап"} <= {t["term"] for t in lx2["terms"] if t["source"] == "оператор"}
    after = client.post("/api/agents/match", json={"q": "оцени стоимость стартапа"}).json()["matches"]
    rank = lambda ms: next((i for i, m in enumerate(ms) if m["id"] == aid), 99)  # noqa: E731
    assert rank(after) <= rank(before)

    # минус-слово: по нему агент больше не подбирается
    client.post(f"/api/agents/{aid}/lexicon", json={"ban": ["стартап"]})
    assert any(t["term"] == "стартап" and t["source"] == "запрещено" for t in client.get(f"/api/agents/{aid}/lexicon").json()["terms"])
    assert "стартап" not in (client.get(f"/api/agents/{aid}/lexicon").json()["manual"].keys() - {"стартап"})
    from server import lexicon
    assert asyncio.run(lexicon.get(aid)).get("стартап") is None
    client.post(f"/api/agents/{aid}/lexicon", json={"remove": ["стартап", "оценка стоимости"]})
    assert client.get(f"/api/agents/{aid}/lexicon").json()["manual"] == {}
    assert client.post(f"/api/agents/{aid}/lexicon/refresh").json()["seeded"] > 0
    assert client.get("/api/agents/no-such/lexicon").status_code == 404
