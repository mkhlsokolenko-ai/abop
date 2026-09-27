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
