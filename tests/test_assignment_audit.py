# -*- coding: utf-8 -*-
"""Аудитор назначения — шаг 1 плана «Отделов агентов» (docs/OTDELY_AGENTOV.md §11.1).

Главный сценарий — реальный случай с прода: у контракта audit1c-holding-2026 версия v11
«Аудитор данных в 1С» несла граф следователя (extract → graph-build → invest1c-trace →
invest1c-verdict) без audit1c-checks. Прогоны были зелёными, вердикт «ok», находок ноль —
и неделю никто не замечал, потому что никто не сверял агента с тем, чем он назван.

Три уровня защиты, каждый проверяется здесь: предупреждение при сохранении, врата на
развёртывании (включая откат и прямую посадку картой), алерт и отчёт по уже работающим.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / "cli"))   # `ape` импортирует `checks` по имени

АДМИН = {"sub": "u1", "name": "админ", "roles": ["admin"], "level": "admin", "department": "*"}

АУДИТОР = ["audit1c-extract", "audit1c-graph-build", "audit1c-match-weak", "audit1c-checks",
           "audit1c-root-cause", "audit1c-rank", "audit1c-explain", "sverka-report"]
СЛЕДОВАТЕЛЬ = ["audit1c-extract", "audit1c-graph-build", "invest1c-trace", "invest1c-verdict"]


def _граф(*skills: str) -> dict:
    return {"nodes": [{"id": "n-" + s, "kind": "skill", "skill": s} for s in skills]}


def _семья_audit() -> dict:
    """Настоящий ростер семьи audit из кода: тест ловит расхождение графа с семантикой семьи."""
    from cli import ape
    return dict(ape.AGENT_FAMILIES["audit"]["members"])


def _проверка(граф: dict, роль: str, члены=None) -> dict:
    from server.assignment_audit import проверка_по_ростеру
    return проверка_по_ростеру(граф, роль, _семья_audit() if члены is None else члены)


def _агент(**kw) -> dict:
    """Слепок с прода: v11 «Аудитор данных в 1С» с графом следователя."""
    база = {"id": "audit1c-holding-2026.v11", "name": "Аудитор данных в 1С",
            "contract_audit_id": "audit1c-holding-2026", "version": 11,
            "status": "draft", "family": "audit", "role": "auditor-1c",
            "graph": _граф(*СЛЕДОВАТЕЛЬ), "source": "contract", "created_by": "владелец"}
    база.update(kw)
    return база


# ═══════════════ чистая функция вердикта ═══════════════

def test_v11_случай_ловится():
    v = _проверка(_граф(*СЛЕДОВАТЕЛЬ), "auditor-1c")
    assert v["серьёзность"] == "crit" and not v["ok"]
    assert v["фактическая_роль"] == "investigator-1c"
    assert "investigator-1c" in v["причина"]


def test_полный_аудитор_ок():
    assert _проверка(_граф(*АУДИТОР), "auditor-1c")["серьёзность"] == "ok"


def test_нехватка_навыков_роли_предупреждение():
    граф = _граф("audit1c-extract", "audit1c-graph-build", "audit1c-match-weak",
                 "audit1c-root-cause", "audit1c-rank", "audit1c-explain")
    v = _проверка(граф, "auditor-1c")
    assert v["серьёзность"] == "warn"
    assert "audit1c-checks" in v["отсутствуют"]


def test_чужой_граф_без_пересечений_крит():
    v = _проверка(_граф("mail-triage", "daily-plan"), "auditor-1c")
    assert v["серьёзность"] == "crit" and v["фактическая_роль"] is None


def test_кастомная_семья_без_ростера_не_кричит():
    v = _проверка(_граф("mail-triage"), "любой", члены={"любой": ("Любой", [])})
    assert v["серьёзность"] == "ok"


def test_роль_не_в_ростере_предупреждение():
    v = _проверка(_граф(*АУДИТОР), "несуществующая")
    assert v["серьёзность"] == "warn" and v["фактическая_роль"] == "auditor-1c"


def test_полусобранный_черновик_не_кричит():
    """Черновик, где пока только общие навыки, — не крит: он ещё строится, но уже warn."""
    assert _проверка(_граф("audit1c-extract", "audit1c-graph-build"), "auditor-1c")["серьёзность"] == "warn"


def test_проверка_падает_на_ростер_кода_если_реестра_нет():
    from server import families_store
    from server.assignment_audit import проверка

    async def flow():
        families_store._MEM.pop("audit", None)   # реестра нет → запасной эталон из кода
        return await проверка(_граф(*СЛЕДОВАТЕЛЬ), "audit", "auditor-1c")

    assert asyncio.run(flow())["серьёзность"] == "crit"


# ═══════════════ врата развёртывания ═══════════════

def test_врата_не_пускают_крит_на_деплой(monkeypatch):
    from server import agent_store
    from server.routes import agents as A
    агент = _агент()
    статусы: list = []

    async def _get(aid):
        return агент

    async def _set(aid, st):
        статусы.append((aid, st))

    monkeypatch.setattr(agent_store, "get", _get)
    monkeypatch.setattr(agent_store, "set_status", _set)
    ответ = asyncio.run(A.agent_set_status("audit1c-holding-2026.v11", {"status": "deployed"}, АДМИН))
    assert ответ.status_code == 422
    тело = json.loads(ответ.body)
    assert тело["assignment"]["серьёзность"] == "crit"
    assert статусы == []   # статус не сменился


def test_врата_пропускают_с_force_и_пишут_override(monkeypatch):
    from server import agent_store, audit_store
    from server.routes import agents as A
    агент = _агент()
    события: list = []

    async def _get(aid):
        return агент

    async def _set(aid, st):
        assert st == "deployed"

    async def _rec(актор, событие, цель, мета=None):
        события.append((событие, цель))

    monkeypatch.setattr(agent_store, "get", _get)
    monkeypatch.setattr(agent_store, "set_status", _set)
    monkeypatch.setattr(audit_store, "record", _rec)
    ответ = asyncio.run(A.agent_set_status("audit1c-holding-2026.v11",
                                           {"status": "deployed", "force_assignment": True}, АДМИН))
    assert ответ == {"id": "audit1c-holding-2026.v11", "status": "deployed"}
    assert ("agent.assignment.override", "audit1c-holding-2026.v11") in события


def test_врата_пропускают_честного_агента_без_шума(monkeypatch):
    from server import agent_store, audit_store
    from server.routes import agents as A
    агент = _агент(graph=_граф(*АУДИТОР))
    события: list = []

    async def _get(aid):
        return агент

    async def _set(aid, st):
        assert st == "deployed"

    async def _rec(актор, событие, цель, мета=None):
        события.append(событие)

    monkeypatch.setattr(agent_store, "get", _get)
    monkeypatch.setattr(agent_store, "set_status", _set)
    monkeypatch.setattr(audit_store, "record", _rec)
    ответ = asyncio.run(A.agent_set_status("audit1c-holding-2026.v11", {"status": "deployed"}, АДМИН))
    assert ответ["status"] == "deployed"
    assert "agent.assignment.override" not in события


def test_откат_критичной_версии_запрещён(monkeypatch):
    from server import agent_store
    from server.routes import agents as A
    import pytest
    текущая = _агент(graph=_граф(*АУДИТОР))
    предыдущая = _агент(id="audit1c-holding-2026.v10", version=10)

    async def _prev(a):
        return {"id": предыдущая["id"]}

    async def _get(aid):
        return предыдущая if aid == предыдущая["id"] else текущая

    async def _set(aid, st):
        raise AssertionError("откат не должен был пройти")

    monkeypatch.setattr(A, "_prev_version_of", _prev)
    monkeypatch.setattr(agent_store, "get", _get)
    monkeypatch.setattr(agent_store, "set_status", _set)
    with pytest.raises(Exception):
        asyncio.run(A.agent_rollback("audit1c-holding-2026.v11", АДМИН))


# ═══════════════ сохранение: предупреждения ═══════════════

def _спасти(контракт_навыки: list, граф: list, monkeypatch) -> dict:
    """Сохранить графа как это делает канва, вернув тело ответа."""
    from server import agent_store, contract_store
    from server.routes import agents as A

    cs = {"intake": {"family": "audit", "skills": контракт_навыки}}
    сохранено: list = []

    async def _cs(aid):
        return cs

    async def _чтение():
        return set()

    async def _latest(aid):
        # v10 в проде — аудитор; пересмотр поверх неё должен увидеть смену роли, если была
        return {"id": "audit1c-holding-2026.v10", "family": "audit", "role": "auditor-1c"}

    async def _draft(**kw):
        сохранено.append(kw)
        return {"id": "audit1c-holding-2026.v12", "version": 12}

    async def _rec(*a, **kw):
        pass

    monkeypatch.setattr(contract_store, "get", _cs)
    monkeypatch.setattr(A, "_skills_reading_data", _чтение)
    monkeypatch.setattr(agent_store, "latest", _latest)
    monkeypatch.setattr(agent_store, "save_draft", _draft)
    monkeypatch.setattr(A.audit_store, "record", _rec)
    resp = asyncio.run(A.agent_save({"name": "Аудитор данных в 1С",
                                     "contract_audit_id": "audit1c-holding-2026",
                                     "graph": _граф(*граф)}, АДМИН))
    assert сохранено, "агент должен был сохраниться (предупреждение, не запрет)"
    return json.loads(resp.body)


def test_сохранение_графа_чужой_роли_предупреждает(monkeypatch):
    """Контракт объявляет аудиторские навыки, а на канве собрали следователя (случай v11)."""
    тело = _спасти(АУДИТОР, СЛЕДОВАТЕЛЬ, monkeypatch)
    assert any("другой роли" in w for w in тело["warnings"])
    assert тело["assignment"]["серьёзность"] == "crit"


def test_сохранение_смены_роли_внутри_контракта_предупреждает(monkeypatch):
    """Контракт пересобрали под следователя целиком: crit нет, но смена роли внутри контракта
    видна — роль резолвилась в investigator-1c, а была auditor-1c у v10."""
    тело = _спасти(СЛЕДОВАТЕЛЬ, СЛЕДОВАТЕЛЬ, monkeypatch)
    assert any("роль сменилась" in w for w in тело["warnings"])


def test_сохранение_честного_агента_без_предупреждений(monkeypatch):
    тело = _спасти(АУДИТОР, АУДИТОР, monkeypatch)
    assert тело["warnings"] == []
    assert тело["assignment"]["серьёзность"] == "ok"


# ═══════════════ флот и отчёт ═══════════════

def test_алерт_флота_только_для_активных():
    from server.routes.agents import _assignment_alerts
    asg = {"x.v11": {"серьёзность": "crit", "причина": "граф делает работу другой роли"}}
    deployments = [{"id": "x.v11", "agent": "Аудитор данных в 1С", "version": "v11",
                    "status": "актив", "assignment": "crit"},
                   {"id": "y.v1", "agent": "Черновик", "version": "v1",
                    "status": "черновик", "assignment": "crit"}]
    out = _assignment_alerts(asg, deployments)
    assert len(out) == 1 and out[0]["agent_id"] == "x.v11"
    assert out[0]["level"] == "crit" and "v11" in out[0]["title"]


def test_отчёт_показывает_провалы(monkeypatch):
    from server import agent_store
    from server.routes import agents as A
    полный = [_агент(),
              _агент(id="ok.v1", contract_audit_id="ok", version=1, name="Честный",
                     graph=_граф(*АУДИТОР))]
    брифы = [{k: v for k, v in a.items() if k != "graph"} for a in полный]

    async def _list_for(audit_id=None, limit=100, archived=False):
        return брифы

    async def _get(aid):
        return next(a for a in полный if a["id"] == aid)

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(agent_store, "get", _get)
    rep = asyncio.run(A.assignment_audit_report(АДМИН))
    assert rep["агентов"] == 2 and rep["провалов"] == 1
    assert rep["строки"][0]["id"] == "audit1c-holding-2026.v11"
    assert rep["строки"][0]["серьёзность"] == "crit"


def test_прямая_посадка_картой_мимо_врат_не_проходит(monkeypatch):
    from server import agent_store, audit_store, process_deploy
    бриф = {k: v for k, v in _агент().items() if k != "graph"}
    развёрнуты: list = []

    async def _list_for(audit_id=None, limit=100, archived=False):
        return [бриф]

    async def _get(aid):
        return _агент()

    async def _set(aid, st):
        развёрнуты.append(aid)

    async def _rec(актор, событие, цель, мета=None):
        assert событие == "agent.assignment.block"

    monkeypatch.setattr(agent_store, "list_for", _list_for)
    monkeypatch.setattr(agent_store, "get", _get)
    monkeypatch.setattr(agent_store, "set_status", _set)
    monkeypatch.setattr(audit_store, "record", _rec)
    out = asyncio.run(process_deploy.deploy_seated(
        [{"audit_id": "audit1c-holding-2026", "op_id": "op-1"}], actor="карта"))
    assert out == [] and развёрнуты == []
