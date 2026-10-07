# -*- coding: utf-8 -*-
"""Область процесса как единица прав на карте: видеть всё, править своё (ADR-045).

Карта лежала в `app_config`, где правка требует уровня `manager`, но в интерфейсе прав не проверял
никто, а `saveConfig` глотал ответ сервера. Аналитик правил карту, видел «Добавлен блок», на сервер
не уходило ничего. На одно действие «посадить агента» приходилось три несовпадающих гейта, и ни один
не совпадал с серверным.

Правила, которые тут закреплены:

* читать карту может любой вошедший — иначе теряется смысл цепочки ценности, где процесс отдела стоит
  между чужими;
* править — только свою область, и область берётся из САМОЙ карты по цели правки, а не из тела
  запроса: иначе достаточно было бы прислать чужой `area`;
* верхняя рамка (организация и состав процессов первого уровня) — область «*», правит только admin;
* поддержка смотрит сквозь области, но процессы отдела не правит;
* отказ приходит с причиной, которую можно показать человеку.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import jwt
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "KEYCLOAK_JWKS_INTERNAL", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL",
          "ABOP_PG_DSN"):
    os.environ.pop(k, None)
os.environ.setdefault("ABOP_RUN_WORKERS", "0")
os.environ["ABOP_DEV_AUTH"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from server import process_store as ps, web_api  # noqa: E402

ДЕРЕВО = {
    "id": "org", "kind": "org", "label": "Организация", "children": [
        {"id": "p1", "kind": "process", "label": "Закупки", "area": "management", "children": [
            {"id": "s11", "kind": "subprocess", "label": "Проверка подрядчика", "children": [
                {"id": "o112", "kind": "task", "label": "Сбор данных о подрядчике"},
            ]},
        ]},
        {"id": "p2", "kind": "process", "label": "Учёт и отчётность", "area": "finance", "children": [
            {"id": "s21", "kind": "subprocess", "label": "Аудит данных в 1С", "children": [
                {"id": "o213", "kind": "task", "label": "Проверки A · B · C"},
            ]},
        ]},
    ],
}


def _кто(level: str, department: str = "*", имя: str = "") -> dict:
    токен = jwt.encode({"sub": имя or level, "preferred_username": имя or level,
                        "realm_access": {"roles": [level]}, "department": department},
                       "демо", algorithm="HS256")
    return {"Authorization": "Bearer " + токен}


АДМИН = _кто("admin", "*", "admin.abop")
БУХ_МЕНЕДЖЕР = _кто("manager", "finance", "buh.manager")
УПР_МЕНЕДЖЕР = _кто("manager", "management", "pm.manager")
БУХ_АНАЛИТИК = _кто("analyst", "finance", "buh.analyst")
ПОДДЕРЖКА = _кто("support", "*", "support.abop")


@pytest.fixture()
def клиент():
    import asyncio
    asyncio.run(ps._сброс_для_тестов())
    asyncio.run(ps.save_map(ps.КАРТА, ДЕРЕВО, editor="admin.abop", note="сид теста"))
    with TestClient(web_api.app) as c:
        yield c


def _правка(c, кто, *, base=0, changes):
    return c.post("/api/process/revision", json={"base_version": base, "changes": changes}, headers=кто)


def test_карта_видна_целиком_всем(клиент):
    """Аналитик бухгалтерии видит и чужие ветки: карта — про цепочку, а не про свой кусок."""
    r = клиент.get("/api/process/map", headers=БУХ_АНАЛИТИК)
    assert r.status_code == 200
    дерево = r.json()["tree"]
    assert [c["id"] for c in дерево["children"]] == ["p1", "p2"]
    assert r.json()["areas"]["o213"] == "finance"


def test_аналитик_не_правит_ничего(клиент):
    """Чтение — да, правка — нет, и причина должна быть читаемой."""
    r = _правка(клиент, БУХ_АНАЛИТИК,
                changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Проверки"}}])
    assert r.status_code == 403
    assert "analyst" in r.json()["detail"] or "менеджер" in r.json()["detail"]


def test_менеджер_правит_свою_область(клиент):
    r = _правка(клиент, БУХ_МЕНЕДЖЕР,
                changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Проверки A–D"}}])
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1 and r.json()["area"] == "finance"


def test_правка_чужой_области_отбивается_с_перечислением(клиент):
    """Менеджер управления не правит бухгалтерию — и видит, какие именно узлы чужие."""
    r = _правка(клиент, УПР_МЕНЕДЖЕР,
                changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Моё"}}])
    assert r.status_code == 403
    assert "o213" in r.json()["detail"] and "finance" in r.json()["detail"]


def test_область_берётся_из_карты_а_не_из_тела(клиент):
    """Присланный `area` ничего не значит: иначе достаточно было бы подменить поле в запросе."""
    r = клиент.post("/api/process/revision",
                    json={"area": "management", "base_version": 0,
                          "changes": [{"kind": "rename_node", "target": "o213",
                                       "payload": {"label": "Обход"}}]},
                    headers=УПР_МЕНЕДЖЕР)
    assert r.status_code == 403


def test_правки_через_две_области_не_принимаются(клиент):
    """Редакция живёт в одной области: применять её иначе означало бы применять за чужой отдел."""
    r = _правка(клиент, АДМИН, changes=[
        {"kind": "rename_node", "target": "o213", "payload": {"label": "Раз"}},
        {"kind": "rename_node", "target": "o112", "payload": {"label": "Два"}},
    ])
    assert r.status_code == 422
    assert "finance" in r.json()["detail"] and "management" in r.json()["detail"]


def test_верхняя_рамка_только_админ(клиент):
    """Состав процессов первого уровня — цепочка ценности целиком, её не переставляет отдел."""
    отказ = _правка(клиент, БУХ_МЕНЕДЖЕР,
                    changes=[{"kind": "add_node", "target": "org",
                              "payload": {"kind": "process", "label": "Новый процесс"}}])
    assert отказ.status_code == 403
    можно = _правка(клиент, АДМИН,
                    changes=[{"kind": "add_node", "target": "org",
                              "payload": {"kind": "process", "label": "Новый процесс"}}])
    assert можно.status_code == 200, можно.text


def test_поддержка_смотрит_но_не_правит(клиент):
    """Сквозное чтение — часть работы поддержки; правка процессов отдела — нет."""
    assert клиент.get("/api/process/map", headers=ПОДДЕРЖКА).status_code == 200
    r = _правка(клиент, ПОДДЕРЖКА,
                changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Правлю"}}])
    assert r.status_code == 403


def test_конфликт_приходит_с_именем_соперника(клиент):
    """409 несёт версию и того, кто опередил: интерфейсу надо что показать (ADR-046)."""
    assert _правка(клиент, БУХ_МЕНЕДЖЕР,
                   changes=[{"kind": "rename_node", "target": "o213",
                             "payload": {"label": "Раз"}}]).status_code == 200
    r = _правка(клиент, АДМИН, base=0,
                changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Два"}}])
    assert r.status_code == 409
    тело = r.json()["detail"]
    assert тело["version"] == 1 and тело["rivals"][0]["author"] == "buh.manager"


def test_отказ_по_состоянию_приходит_с_причиной(клиент):
    """422 объясняет, почему правка невозможна: «не операция, а subprocess», а не «ошибка»."""
    r = _правка(клиент, БУХ_МЕНЕДЖЕР,
                changes=[{"kind": "add_seat", "target": "s21", "payload": {"audit_id": "audit-1c"}}])
    assert r.status_code == 422
    assert "операц" in r.json()["detail"]


def test_применение_и_отброс_под_теми_же_правами(клиент):
    """Применить и отбросить редакцию может тот, кто вправе её править."""
    r = _правка(клиент, БУХ_МЕНЕДЖЕР,
                changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Проверки A–D"}}])
    rid = r.json()["revision_id"]
    чужой = клиент.post(f"/api/process/revision/{rid}/apply", json={"base_version": 1},
                        headers=УПР_МЕНЕДЖЕР)
    assert чужой.status_code == 403
    свой = клиент.post(f"/api/process/revision/{rid}/apply", json={"base_version": 1},
                       headers=БУХ_МЕНЕДЖЕР)
    assert свой.status_code == 200, свой.text
    assert свой.json()["revision"] == 2


def test_разница_и_присутствие_читаются(клиент):
    """Клиент догоняет по номеру, а «кто ещё здесь» отвечает на вопрос, почему карта шевелится."""
    r = _правка(клиент, БУХ_МЕНЕДЖЕР,
                changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Раз"}}])
    rid = r.json()["revision_id"]
    d = клиент.get(f"/api/process/revision/{rid}/changes?since=0", headers=БУХ_АНАЛИТИК)
    assert d.status_code == 200 and [c["seq"] for c in d.json()["changes"]] == [1]
    assert клиент.get(f"/api/process/revision/{rid}/changes?since=1",
                      headers=БУХ_АНАЛИТИК).json()["changes"] == []


def test_применение_разворачивает_посаженных_агентов(клиент, monkeypatch):
    """«Принять план» — это префлайт И деплой: иначе карта утверждает, что агент работает участок,
    а сам агент лежит черновиком и ни одного триггера не принимает (ADR-042)."""
    import asyncio

    from server import agent_store

    asyncio.run(agent_store.save(name="Аудитор 1С", audit_id="audit-1c", version=1, graph={},
                                 autonomy_max="A1", created_by="admin.abop", family="finance"))
    assert asyncio.run(agent_store.get("audit-1c.v1"))["status"] == "draft"

    r = _правка(клиент, БУХ_МЕНЕДЖЕР,
                changes=[{"kind": "add_seat", "target": "o213", "payload": {"audit_id": "audit-1c"}}])
    assert r.status_code == 200, r.text
    rid, версия = r.json()["revision_id"], r.json()["version"]
    ap = клиент.post(f"/api/process/revision/{rid}/apply", json={"base_version": версия},
                     headers=БУХ_МЕНЕДЖЕР)
    assert ap.status_code == 200, ap.text
    assert ap.json()["deployed"] == ["audit-1c.v1"], ap.json()
    assert asyncio.run(agent_store.get("audit-1c.v1"))["status"] == "deployed"


def test_карта_не_кэшируется(клиент):
    """Карта — живые данные. Браузер отдавал повторный одинаковый GET из кэша, и после создания
    карты экран продолжал показывать «карта не создана», хотя на сервере она уже была."""
    r = клиент.get("/api/process/map", headers=БУХ_АНАЛИТИК)
    assert r.headers.get("cache-control") == "no-store", r.headers


def test_посадка_с_пином_уходит_на_согласование(клиент):
    """Закреплённая версия — регулируемый участок: смена версии должна быть событием, а не
    побочным эффектом. Менеджер отправляет редакцию, применяет подписант-admin (ADR-042)."""
    import asyncio

    from server import agent_store
    asyncio.run(agent_store.save(name="Аудитор 1С", audit_id="audit-1c", version=1, graph={},
                                 autonomy_max="A1", created_by="admin.abop", family="finance"))
    r = _правка(клиент, БУХ_МЕНЕДЖЕР, changes=[
        {"kind": "add_seat", "target": "o213", "payload": {"audit_id": "audit-1c", "pin_version": 1}}])
    rid, версия = r.json()["revision_id"], r.json()["version"]

    сразу = клиент.post(f"/api/process/revision/{rid}/apply", json={"base_version": версия},
                        headers=БУХ_МЕНЕДЖЕР)
    assert сразу.status_code == 422 and "согласован" in сразу.json()["detail"]

    assert клиент.post(f"/api/process/revision/{rid}/submit", json={"base_version": версия},
                       headers=БУХ_МЕНЕДЖЕР).status_code == 200
    # после отправки редакция не правится
    поздно = _правка(клиент, БУХ_МЕНЕДЖЕР, base=версия,
                     changes=[{"kind": "rename_node", "target": "o213", "payload": {"label": "Поздно"}}])
    assert поздно.status_code == 422 and "согласован" in поздно.json()["detail"]

    свой = клиент.post(f"/api/process/revision/{rid}/apply", json={"base_version": версия},
                       headers=БУХ_МЕНЕДЖЕР)
    assert свой.status_code == 422, "менеджер не может подписать сам себе"
    админ = клиент.post(f"/api/process/revision/{rid}/apply", json={"base_version": версия},
                        headers=АДМИН)
    assert админ.status_code == 200, админ.text
    assert админ.json()["seats_added"] == 1


def test_админ_не_подписывает_не_отправленную_редакцию(клиент):
    """Применять «за себя» то, что требует согласования, нельзя даже администратору."""
    import asyncio

    from server import agent_store
    asyncio.run(agent_store.save(name="Аудитор 1С", audit_id="audit-1c", version=1, graph={},
                                 autonomy_max="A1", created_by="admin.abop", family="finance"))
    r = _правка(клиент, АДМИН, changes=[
        {"kind": "add_seat", "target": "o213", "payload": {"audit_id": "audit-1c", "pin_version": 1}}])
    ap = клиент.post(f"/api/process/revision/{r.json()['revision_id']}/apply",
                     json={"base_version": r.json()["version"]}, headers=АДМИН)
    assert ap.status_code == 422 and "согласован" in ap.json()["detail"]
