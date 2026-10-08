# -*- coding: utf-8 -*-
"""Карта процессов: редакции, посадки, подтверждение прогонами, импорт BPMN, чужие агенты.

Первый домен, вынесенный из монолита (п.23 исследования 08.10). Выбран не случайно: он самый
молодой и самый связный — семнадцать ручек, один стор, свои правила прав (область как единица
правки, ADR-045), и ни одна другая часть сервиса в него не заглядывает.

Зависимости наружу: `server/deps` (кто пользователь и что ему можно) и сторы. `web_api` отсюда
не импортируется — это вернуло бы монолит через чёрный ход.
"""
from __future__ import annotations

import asyncio as _asyncio
import json as _json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .. import agent_store, audit_store, bpmn_import, cachebus, clients, contract_store
from .. import reglament_store
from .. import observability as obs
from .. import process_deploy, process_store, run_store, slava
from ..deps import _sub, can_see_family, require_level, user


def _cosine(a, b) -> float:
    """Косинус двух векторов. Копия из web_api намеренно не делалась: функция чистая и
    крошечная, а тянуть ради неё приложение в модуль маршрутов значило бы вернуть монолит."""
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return (s / (na * nb)) if na and nb else 0.0

router = APIRouter()

# ── Карта процессов: редакции, посадки, синхронизация правок (ADR-042/043/045/046) ───────────────

def can_edit_area(u: dict, area: str) -> bool:
    """Правка карты: admin — везде; менеджер — только своя объявленная область. Аналитик читает,
    поддержка читает сквозь области, но процессы отдела не правит (ADR-045). Область «*» — верхняя
    рамка карты (организация и состав процессов первого уровня), её переставляет только admin."""
    level = u.get("level")
    if level == "admin":
        return True
    if level != "manager":
        return False
    return area != "*" and u.get("department") == area


def _area_of_change(tree: dict, ch: dict, overlays: dict | None = None) -> str:
    """Область правки берётся из САМОЙ карты по её цели: для add_node — область родителя, иначе —
    область узла. Присланный в теле `area` не используется, иначе его достаточно было бы подменить.

    Блок, созданный в ЭТОЙ ЖЕ редакции, в применённом дереве ещё не существует — по нему область
    выходила «*», и обычный сценарий «добавил шаг → посадил на него агента» падал с «правки
    затрагивают несколько областей». Поэтому неизвестную цель ищем в наложении открытых редакций.
    """
    цель = str((ch or {}).get("target") or "")
    if process_store.найти(tree, цель) is None:
        for area, дерево in (overlays or {}).items():
            if process_store.найти(дерево, цель) is not None:
                return area
    return process_store.area_of(tree, цель)


async def _overlays_for(u: dict, tree: dict) -> dict:
    """Наложения открытых редакций по областям, где человек вправе править: в них живут блоки,
    созданные в текущей редакции и ещё не попавшие в применённое дерево."""
    области = {process_store.area_of(tree, nid) for nid in process_store.поддерево(tree)}
    из: dict = {}
    for area in sorted(области):
        if not can_edit_area(u, area):
            continue
        черновик = await process_store.draft_of(area)
        if черновик and черновик.get("changes"):
            из[area] = (await process_store.projection(area, layer="to-be")).get("tree") or {}
    return из


def _require_area(u: dict, tree: dict, changes: list[dict], overlays: dict | None = None) -> str:
    """Одна область на пачку правок + проверка прав. Редакция живёт в одной области: иначе её
    применение затронуло бы чужой отдел."""
    по_областям: dict[str, list[str]] = {}
    for ch in changes or []:
        по_областям.setdefault(_area_of_change(tree, ch, overlays), []).append(str((ch or {}).get("target") or ""))
    if not по_областям:
        raise HTTPException(422, "нечего применять")
    if len(по_областям) > 1:
        описание = "; ".join(f"{a}: {', '.join(t)}" for a, t in sorted(по_областям.items()))
        raise HTTPException(422, "правки затрагивают несколько областей, редакция живёт в одной — " + описание)
    area = next(iter(по_областям))
    if not can_edit_area(u, area):
        чужие = ", ".join(по_областям[area])
        raise HTTPException(403, f"область «{area}» вам не принадлежит (вы {u.get('level')}"
                                 f"/{u.get('department')}): узлы {чужие}")
    return area


def _process_http(ex: Exception):
    """Отказ стора → HTTP с ПРИЧИНОЙ в теле: интерфейс её показывает, а не глотает (разбор, Р4)."""
    if isinstance(ex, process_store.Конфликт):
        return HTTPException(409, {"error": str(ex), "version": ex.версия, "rivals": ex.соперники})
    return HTTPException(422, str(ex))


@router.get("/api/process/map")
async def process_map(response: Response, layer: str = "as-is", area: str | None = None,
                      u: dict = Depends(user)) -> dict:
    """Карта процессов для отрисовки. `as-is` — применённая редакция (факт, только чтение),
    `to-be` — она же с наложенным черновиком области. Читать может любой вошедший: карта — про
    цепочку создания ценности, и свой процесс человек должен видеть между чужими (ADR-045)."""
    # Карта — живые данные: браузер отдавал повторный одинаковый GET из кэша, и после создания
    # карты экран продолжал показывать «карта не создана», хотя на сервере она уже была.
    response.headers["Cache-Control"] = "no-store"
    layer = "to-be" if str(layer).lower() == "to-be" else "as-is"
    область = (area or u.get("department") or "*")
    срез = await process_store.projection(область, layer=layer)
    дерево = срез.get("tree")
    области = {nid: process_store.area_of(дерево, nid)
               for nid in process_store.поддерево(дерево)} if дерево else {}
    редактируемые = sorted({a for a in области.values() if can_edit_area(u, a)})
    return dict(срез, areas=области, editable_areas=редактируемые,
                editors=await process_store.presence_of(область))


@router.post("/api/process/map")
async def process_map_seed(body: dict, u: dict = Depends(user)) -> dict:
    """Записать карту целиком новой применённой редакцией. Это вход для сида и миграции, а не для
    обычной правки: пользовательские изменения идут через редакцию (ADR-042), где они проверяются на
    конфликты и права по областям. Поэтому — только admin."""
    require_level(u, "admin")
    tree = (body or {}).get("tree")
    if not isinstance(tree, dict) or not tree.get("id"):
        raise HTTPException(422, "нужно дерево карты: {id, kind, label, children[]}")
    editor = u.get("name") or u.get("sub") or "dev"
    итог = await process_store.save_map(process_store.КАРТА, tree, editor=editor,
                                        note=str((body or {}).get("note") or ""))
    await audit_store.record(editor, "process.map.save", str(итог["revision"]),
                             {"note": итог.get("note")})
    # Отдаём сразу готовый срез: клиенту не нужен второй запрос за только что записанной картой.
    срез = await process_store.projection(process_store.КАРТА, layer="as-is")
    дерево = срез.get("tree") or {}
    области = {nid: process_store.area_of(дерево, nid) for nid in process_store.поддерево(дерево)}
    return dict(срез, areas=области,
                editable_areas=sorted({a for a in области.values() if can_edit_area(u, a)}),
                editors=[], note=итог.get("note"))


@router.post("/api/process/revision")
async def process_revision_append(body: dict, u: dict = Depends(user)) -> dict:
    """Дописать правки в черновик редакции своей области. Сливает с чужими правками, если зоны не
    пересеклись, и возвращает пропущенные — клиент догоняет без перезагрузки. Пересеклись — 409 с
    указанием, кто опередил (ADR-046)."""
    require_level(u, "manager")
    changes = (body or {}).get("changes") or []
    применено = await process_store.map_of(process_store.КАРТА)
    дерево = применено.get("tree")
    if not дерево:
        raise HTTPException(422, "карта процессов ещё не создана")
    area = _require_area(u, дерево, changes, await _overlays_for(u, дерево))
    author = u.get("name") or u.get("sub") or "dev"
    try:
        итог = await process_store.append(area, base_version=int((body or {}).get("base_version") or 0),
                                          changes=changes, author=author)
    except (process_store.Отказ, process_store.Конфликт) as ex:
        raise _process_http(ex) from ex
    await audit_store.record(author, "process.revision.append", итог["revision_id"],
                             {"area": area, "version": итог["version"],
                              "kinds": [c.get("kind") for c in changes]})
    return dict(итог, area=area)


@router.get("/api/process/revision/{rev_id}/changes")
async def process_revision_changes(response: Response, rev_id: str, since: int = 0,
                                   u: dict = Depends(user)) -> dict:
    """Разница по номеру: клиент берёт только то, чего не видел. Читать может любой вошедший."""
    response.headers["Cache-Control"] = "no-store"
    area = await _area_of_revision(rev_id)
    return {"revision_id": rev_id, "area": area, "since": since,
            "changes": await process_store.changes_since(area, since)}


@router.delete("/api/process/revision/{rev_id}")
async def process_revision_drop(rev_id: str, u: dict = Depends(user)) -> dict:
    """Отбросить черновик целиком: применённая карта не менялась, значит терять нечего."""
    require_level(u, "manager")
    area = await _area_of_revision(rev_id)
    if not can_edit_area(u, area):
        raise HTTPException(403, f"область «{area}» вам не принадлежит")
    actor = u.get("name") or u.get("sub") or "dev"
    снято = await process_store.drop_draft(area, actor=actor)
    await audit_store.record(actor, "process.revision.drop", rev_id, {"area": area})
    return {"revision_id": rev_id, "area": area, "dropped": снято}


@router.post("/api/process/revision/{rev_id}/submit")
async def process_revision_submit(rev_id: str, body: dict | None = None,
                                  u: dict = Depends(user)) -> dict:
    """Отправить редакцию на согласование. Нужна, когда префлайт требует человека: закреплена
    версия агента на участке или агент ходит наружу (ADR-013/014). После отправки редакция не
    правится — подписант должен подписать ровно то, что прочёл."""
    require_level(u, "manager")
    area = await _area_of_revision(rev_id)
    if not can_edit_area(u, area):
        raise HTTPException(403, f"область «{area}» вам не принадлежит")
    actor = u.get("name") or u.get("sub") or "dev"
    try:
        итог = await process_store.submit(
            area, base_version=int(((body or {}).get("base_version")) or 0), actor=actor)
    except (process_store.Отказ, process_store.Конфликт) as ex:
        raise _process_http(ex) from ex
    await audit_store.record(actor, "process.revision.submit", rev_id, {"area": area})
    return итог


@router.post("/api/process/revision/{rev_id}/apply")
async def process_revision_apply(rev_id: str, body: dict, u: dict = Depends(user)) -> dict:
    """Применить редакцию: одна транзакция на дерево и посадки, либо ничего. `base_version` строгий —
    применять редакцию, в которую после вашего просмотра дописали, нельзя. Перед записью — префлайт
    посадок: агент существует и его автономия ≤ потолка контракта LUDA (ADR-013)."""
    require_level(u, "manager")
    area = await _area_of_revision(rev_id)
    if not can_edit_area(u, area):
        raise HTTPException(403, f"область «{area}» вам не принадлежит")
    actor = u.get("name") or u.get("sub") or "dev"
    черновик = await process_store.draft_of(area) or {}
    # Подписант — admin, и только по редакции, которую ему отправили: применять «за себя» то, что
    # требует согласования, нельзя даже администратору (ADR-042).
    подписано = (черновик.get("status") == "на согласовании" and u.get("level") == "admin")
    try:
        итог = await process_store.apply(area, base_version=int((body or {}).get("base_version") or 0),
                                         actor=actor, preflight=_seat_preflight,
                                         needs_approval=_seat_needs_approval, approved=подписано)
    except (process_store.Отказ, process_store.Конфликт) as ex:
        raise _process_http(ex) from ex
    # «Принять план» — это префлайт И деплой. Иначе карта утверждает, что агент работает участок,
    # а сам агент лежит черновиком и не принимает ни одного триггера (ADR-042).
    развёрнуты = await _deploy_seated(итог.get("seats") or [], actor=actor)
    await audit_store.record(actor, "process.revision.apply", rev_id,
                             {"area": area, "revision": итог["revision"],
                              "seats_added": итог["seats_added"], "seats_closed": итог["seats_closed"],
                              "deployed": развёрнуты})
    return dict(итог, revision_id=rev_id, deployed=развёрнуты)


@router.get("/api/process/liveness")
async def process_liveness(response: Response, window: str = "7d", u: dict = Depends(user)) -> dict:
    """Подтверждение AS-IS прогонами: по каждому участку — сколько прогонов за окно, когда последний
    и в каком он состоянии (подтверждён / тихо / осиротел), плюс агенты, которые работают, но ни на
    одной операции не сидят. Отдельно от /api/fleet: у них разная частота — флот тикает раз в десять
    секунд, подтверждение достаточно считать раз в минуту (ADR-044)."""
    response.headers["Cache-Control"] = "no-store"
    дней = {"24h": 1, "24ч": 1, "7d": 7, "7д": 7, "30d": 30, "30д": 30}.get(str(window).lower(), 7)
    посадки = await process_store.seats_of()
    прогоны = [{"agent_id": r.get("agent_id"), "created_at": r.get("created_at"),
                "verdict_ok": r.get("verdict_ok"), "op_ids": r.get("op_ids") or [],
                # Источник обязан доехать: отчёт чужого агента подтверждает участок слабее
                # собственного прогона, и карта должна это различать (ADR-028/044).
                "source": r.get("source") or "abop"}
               for r in await run_store.list_runs(limit=500)]
    в_лимбе = {str(a.get("contract_audit_id") or a["id"]).split(".v")[0]
               for a in await agent_store.list_for(None, limit=500, archived=True)}
    живые = {str(a.get("contract_audit_id") or a["id"]).split(".v")[0]
             for a in await agent_store.list_for(None, limit=500)}
    итог = process_store.liveness(seats=посадки, runs=прогоны, retired=(в_лимбе - живые),
                                  window_days=дней)
    имена = {str(a.get("contract_audit_id") or a["id"]).split(".v")[0]: a.get("name")
             for a in await agent_store.list_for(None, limit=500, archived=True)
             + await agent_store.list_for(None, limit=500)}
    for x in итог["outside"]:
        x["agent"] = имена.get(x["audit_id"]) or x["audit_id"]
    return итог


РАНТАЙМЫ = ("langgraph", "copilot-studio", "agentforce", "bedrock", "crewai", "autogen",
            "n8n", "другой")

# Конверт чужого агента ОБЪЯВЛЕН, а не проверен: графа у нас нет, узлы осмотреть нечем, и
# авто-верификация (ADR-013) к нему неприменима. Писать «ok» было бы ровно той подделкой, против
# которой заведено правило честности. Пишем прямо: кто объявил и что проверки не было.
ВЕРИФИКАЦИЯ_ЧУЖОГО = {
    "ok": None, "kind": "external",
    "note": "рантайм чужой: конверт объявлен владельцем агента и автоматически не проверяется",
}


@router.post("/api/agents/external")
async def agents_register_external(payload: dict, u: dict = Depends(user)) -> dict:
    """Зарегистрировать агента, которого ABOP не породил (ADR-028, п.21 исследования).

    Способность №1 по обзору рынка 2026 — реестр агентов независимо от того, где они развёрнуты.
    Посадочная модель к этому готова с самого начала: посадка ссылается на агента по
    `contract_audit_id` без версии (ADR-043), и рантайму там взяться неоткуда. Не хватало только
    записи в реестре и честного конверта.

    Что ОБЯЗАТЕЛЬНО остаётся прежним: агент принадлежит семье (ABAC), автономия не выше потолка,
    посадка проходит ту же редакцию с согласованием. Отличие ровно одно и оно видно насквозь:
    конверт объявлен, а не проверен.
    """
    require_level(u, "manager")
    имя = str(payload.get("name") or "").strip()
    рантайм = str(payload.get("runtime") or "").strip().lower()
    семья = str(payload.get("family") or u.get("department") or "").strip()
    автономия = str(payload.get("autonomy_max") or "A1").strip().upper()
    if not имя:
        raise HTTPException(422, "name: пусто")
    if рантайм not in РАНТАЙМЫ:
        raise HTTPException(422, "runtime: один из " + ", ".join(РАНТАЙМЫ))
    if not can_see_family(u, семья):
        raise HTTPException(403, f"семья «{семья}» вам не принадлежит")
    if автономия not in ("A0", "A1", "A2", "A3", "A4"):
        raise HTTPException(422, "autonomy_max: A0..A4")

    import re as _re
    audit_id = str(payload.get("audit_id") or "").strip() or ("ext-" + _re.sub(
        r"[^a-z0-9]+", "-", имя.lower()).strip("-"))[:60]
    прошлые = await agent_store.list_for(audit_id, limit=50)
    версия = max([int(a.get("version") or 0) for a in прошлые] or [0]) + 1
    агент = await agent_store.save(
        name=имя, audit_id=audit_id, version=версия,
        # Графа нет и быть не должно: исполняет чужой рантайм, а наша схема описывала бы то,
        # чего мы не контролируем. Вместо графа — паспорт внешнего исполнителя.
        graph={"external": {"runtime": рантайм,
                            "endpoint": str(payload.get("endpoint") or ""),
                            "owner": str(payload.get("owner") or u.get("name") or "")}},
        autonomy_max=автономия, created_by=u.get("name") or u.get("sub") or "dev",
        family=семья, role=str(payload.get("role") or ""), source="external",
        verification=ВЕРИФИКАЦИЯ_ЧУЖОГО)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "agent.register.external",
                             агент["id"], {"runtime": рантайм, "family": семья,
                                           "autonomy_max": автономия})
    краткий = agent_store._brief(агент) if hasattr(agent_store, "_brief") else dict(агент)
    return {"agent": {**краткий, "source": "external", "runtime": рантайм},
            "envelope": ВЕРИФИКАЦИЯ_ЧУЖОГО,
            "note": "агент в реестре. Посадка на операцию идёт обычной редакцией карты: "
                    "чужой рантайм не даёт права миновать согласование"}


@router.post("/api/runs/report")
async def runs_report_external(payload: dict, u: dict = Depends(user)) -> dict:
    """Чужой агент сообщает о выполненной работе на участке карты (ADR-044).

    AS-IS подтверждается прогонами. Прогон чужого агента мы не исполняли, поэтому принимаем
    ОТЧЁТ — и помечаем его как отчёт: подтверждение «со слов агента» слабее, чем запись
    собственного журнала, и эта разница доезжает до карты и отчёта о покрытии.

    Защита от накрутки: сообщить можно только про те операции, на которые агент посажен. Иначе
    любой держатель токена поднимал бы покрытие любого участка одним запросом.
    """
    audit_id = str(payload.get("audit_id") or "").split(".v")[0].strip()
    op_ids = [str(x) for x in (payload.get("op_ids") or []) if str(x).strip()]
    if not audit_id:
        raise HTTPException(422, "audit_id: пусто")
    if not op_ids:
        raise HTTPException(422, "op_ids: пусто — отчёт без участка ничего не подтверждает")

    версии = await agent_store.list_for(audit_id, limit=50)
    if not версии:
        raise HTTPException(404, f"агента {audit_id} нет в реестре")
    свежая = max(версии, key=lambda a: a.get("version") or 0)
    if not can_see_family(u, свежая.get("family")):
        raise HTTPException(403, "агент чужого отдела")
    if str(свежая.get("source") or "") != "external":
        raise HTTPException(422, "отчёт принимается только от внешнего агента: "
                                 "свои прогоны ABOP пишет сам")

    посажен = {s["op_id"] for s in await process_store.seats_by_agent(audit_id)}
    чужие = [o for o in op_ids if o not in посажен]
    if чужие:
        raise HTTPException(403, "агент не посажен на операции: " + ", ".join(sorted(чужие))
                            + " — отчёт о чужом участке не принимается")

    итог = await run_store.save({
        "agent_id": свежая["id"], "contract_audit_id": audit_id,
        "verdict": {"ok": bool(payload.get("ok", True)),
                    "autonomy_used": свежая.get("autonomy_max")},
        "op_ids": op_ids, "source": "external",
        "reported_by": u.get("name") or u.get("sub") or "dev",
        "started_by": "external:" + str((свежая.get("graph") or {}).get("external", {}).get("runtime") or "?"),
        "run_metrics": {"cost": payload.get("cost") or {}},
        "summary": str(payload.get("summary") or "")[:2000]})
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "run.report.external",
                             итог["id"], {"agent": свежая["id"], "op_ids": op_ids})
    return {"run_id": итог["id"], "op_ids": op_ids, "source": "external",
            "note": "принято как отчёт: в карте участок будет помечен «со слов агента»"}


@router.get("/api/process/history")
async def process_history(response: Response, limit: int = 50, u: dict = Depends(user)) -> dict:
    """Как менялся процесс: применённые редакции сверху вниз — кто, когда, сколько узлов стало.

    Карта отвечала «как сейчас» и не отвечала «когда это стало так и кто решил». Для процессной
    карты это половина смысла: решение принимают один раз, живут с ним годами, а спрашивают про
    него через полгода.
    """
    response.headers["Cache-Control"] = "no-store"
    return {"area": process_store.КАРТА,
            "revisions": await process_store.history(process_store.КАРТА, limit=limit)}


@router.get("/api/process/diff")
async def process_diff(response: Response, base: int = 0, target: int = 0,
                       u: dict = Depends(user)) -> dict:
    """Что изменилось между двумя применёнными редакциями — по сущностям, не построчно.

    `base=0` — предыдущая по отношению к `target`; `target=0` — текущая.
    """
    response.headers["Cache-Control"] = "no-store"
    история = await process_store.history(process_store.КАРТА, limit=500)
    if not история:
        return {"base": None, "target": None, "diff": process_store.разница({}, {})}
    номера = [r["revision"] for r in история]
    цель = int(target) or номера[0]
    если = [n for n in номера if n < цель]
    основа = int(base) or (если[0] if если else цель)
    a = await process_store.map_at(process_store.КАРТА, основа)
    b = await process_store.map_at(process_store.КАРТА, цель)
    if b is None:
        raise HTTPException(404, f"редакции {цель} нет")
    return {"base": основа, "target": цель,
            "applied_by": b.get("applied_by"), "applied_at": b.get("applied_at"),
            "diff": process_store.разница((a or {}).get("tree") or {}, b.get("tree") or {})}


@router.get("/api/process/coverage")
async def process_coverage(response: Response, window: str = "7d",
                           u: dict = Depends(user)) -> dict:
    """Где агенты и где дыры — главный отчёт процессной карты.

    Карта говорит, как устроен процесс. Этот отчёт отвечает на следующий вопрос, ради которого
    её и ведут: какая часть процесса закрыта агентами, какая держится на людях и где агент
    посажен, но на самом деле не работает. Связка «посадка + подтверждение прогонами» и
    отличает ABOP от схемы без факта и от реестра агентов без схемы.
    """
    response.headers["Cache-Control"] = "no-store"
    карта = await process_store.map_of(process_store.КАРТА)
    живое = await process_liveness(Response(), window, u)
    отчёт = process_store.покрытие(tree=карта.get("tree") or {},
                                   seats=await process_store.seats_of(), live=живое)
    имена = {str(a.get("contract_audit_id") or a["id"]).split(".v")[0]: a.get("name")
             for a in await agent_store.list_for(None, limit=500)}
    for строка in отчёт["rows"]:
        строка["agent_names"] = [имена.get(x) or x for x in строка["agents"]]
    return {"revision": карта.get("revision"), "window": window, **отчёт}


@router.post("/api/process/import/bpmn")
async def process_import_bpmn(payload: dict, u: dict = Depends(user)) -> dict:
    """Импорт BPMN 2.0 в редакцию карты. `{"xml": "...", "preview": true|false, "area": "..."}`.

    Организации, которым адресован продукт, рисуют процессы не у нас — в Camunda, Signavio, ARIS.
    Импорт превращает «нарисуйте свою организацию заново» в «покажите, что есть, и посадим
    агентов». Пишется он ОБЫЧНОЙ редакцией: иначе появился бы второй путь записи мимо
    согласования и мимо прав на область, а подписываемая редакция (ADR-042) и есть то, чем карта
    отличается от схемы в рисовалке.
    """
    xml = str(payload.get("xml") or "")
    if not xml.strip():
        raise HTTPException(422, "xml: пусто")
    try:
        разбор = bpmn_import.разобрать(xml)
    except bpmn_import.НеBPMN as ex:
        raise HTTPException(422, str(ex)) from ex
    if payload.get("preview", True):
        return {"preview": True, **разбор}

    область = str(payload.get("area") or u.get("department") or "")
    if not can_edit_area(u, область):
        raise HTTPException(403, f"область «{область}» вам не принадлежит")
    правки = bpmn_import.правки_импорта(разбор["tree"])
    черновик = await process_store.draft_of(область)
    база = (черновик or {}).get("version") or 0
    try:
        итог = await process_store.append(область, base_version=база, changes=правки,
                                          author=u.get("name") or u.get("sub") or "dev")
    except (process_store.Отказ, process_store.Конфликт) as ex:
        return _process_http(ex)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "process.import.bpmn",
                             область, {"nodes": разбор["stats"], "skipped": разбор["skipped"]})
    return {"preview": False, "area": область, "applied_to_draft": len(правки),
            "revision_id": итог.get("revision_id"), "version": итог.get("version"),
            "stats": разбор["stats"], "skipped": разбор["skipped"]}


@router.get("/api/process/stream")
async def process_stream(area: str | None = None, u: dict = Depends(user)):
    """Живое догоняние (SSE): при подключении — номер версии, дальше «появилось новое». Разницу клиент
    берёт через `/changes?since=`. Между репликами оповещение идёт существующим cachebus (ADR-046)."""
    import asyncio as _asyncio
    import json as _json
    from fastapi.responses import StreamingResponse
    область = (area or u.get("department") or "*")
    actor = u.get("name") or u.get("sub") or "dev"
    очередь = process_store.subscribe(область)

    async def gen():
        try:
            await process_store.presence_touch(область, actor)
            черновик = await process_store.draft_of(область)
            привет = {"type": "hello", "area": область,
                      "version": (черновик or {}).get("version") or 0,
                      "revision_id": (черновик or {}).get("id"),
                      "editors": await process_store.presence_of(область)}
            yield "data: " + _json.dumps(привет, ensure_ascii=False) + "\n\n"
            while True:
                try:
                    ев = await _asyncio.wait_for(очередь.get(), timeout=25)
                except _asyncio.TimeoutError:
                    await process_store.presence_touch(область, actor)
                    ев = {"type": "ping", "area": область,
                          "editors": await process_store.presence_of(область)}
                yield "data: " + _json.dumps(ев, ensure_ascii=False) + "\n\n"
        finally:
            process_store.unsubscribe(область, очередь)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _area_of_revision(rev_id: str) -> str:
    """Область редакции по её идентификатору: открытый черновик есть ровно один на область."""
    применено = await process_store.map_of(process_store.КАРТА)
    дерево = применено.get("tree") or {}
    области = {process_store.area_of(дерево, nid) for nid in process_store.поддерево(дерево)} | {"*"}
    for area in sorted(области):
        черновик = await process_store.draft_of(area)
        if черновик and черновик["id"] == rev_id:
            return area
    raise HTTPException(404, "нет открытой редакции с таким идентификатором")


async def _seat_ops(agent: dict) -> list[str]:
    """Операции, которые закрывает агент: его действующие посадки. Прогон запоминает их, иначе
    подтвердить AS-IS нечем — ни одна таблица прогонов операции не знала (ADR-044)."""
    audit_id = str(agent.get("contract_audit_id") or str(agent.get("id") or "").split(".v")[0])
    if not audit_id:
        return []
    try:
        return sorted({s["op_id"] for s in await process_store.seats_by_agent(audit_id)})
    except Exception as ex:  # noqa: BLE001 — прогон из-за карты падать не должен
        obs.log_event("warn", "process.seat_ops_fail", agent=agent.get("id"),
                      error=f"{type(ex).__name__}: {ex}")
        return []


async def _deploy_seated(посадки: list[dict], *, actor: str) -> list[str]:
    """Тот же хук, что у переноса со старого стенда, — см. server/process_deploy.py."""
    return await process_deploy.deploy_seated(посадки, actor=actor)


async def _seat_needs_approval(новые: list[dict]) -> list[str]:
    """Когда посадка требует подписи человека, а не только зелёного префлайта (ADR-013/014):
    закреплена версия (регулируемый участок — смена версии должна быть событием) или агент ходит
    наружу (внешнее действие под подтверждением)."""
    причины: list[str] = []
    for s in новые:
        audit_id = str(s.get("audit_id") or "")
        if s.get("pin_version"):
            причины.append(f"«{audit_id}»: версия закреплена v{s['pin_version']} — нужна ре-аттестация")
            continue
        версии = await agent_store.list_for(audit_id, limit=50)
        if not версии:
            continue
        свежая = max(версии, key=lambda a: a.get("version") or 0)
        узлы = ((свежая.get("graph") or {}).get("nodes") or [])
        наружу = [n for n in узлы if n.get("kind") == "out"
                  or ((n.get("safety") or {}).get("egress") == "external")]
        if наружу:
            причины.append(f"«{audit_id}»: агент действует наружу — посадка под подтверждением")
    return причины


async def _seat_preflight(новые: list[dict]) -> list[str]:
    """Префлайт посадок перед применением: агент есть в реестре и его автономия ≤ потолка контракта
    LUDA (ADR-013). Отказ приходит причинами, а не словом «нельзя»."""
    причины: list[str] = []
    порядок = ["A1", "A2", "A3", "A4"]
    for s in новые:
        audit_id = str(s.get("audit_id") or "")
        версии = [a for a in await agent_store.list_for(audit_id, limit=50)]
        if not версии:
            причины.append(f"агента «{audit_id}» нет в реестре")
            continue
        свежая = max(версии, key=lambda a: a.get("version") or 0)
        if s.get("pin_version") and not any(a.get("version") == s["pin_version"] for a in версии):
            причины.append(f"у «{audit_id}» нет версии v{s['pin_version']} для пина")
            continue
        contract = await contract_store.get(audit_id) or {}
        потолок = ((contract.get("intake") or {}).get("autonomy_ceiling")) or ""
        хочет = str(свежая.get("autonomy_max") or "A1")
        if потолок in порядок and хочет in порядок and порядок.index(хочет) > порядок.index(потолок):
            причины.append(f"автономия «{audit_id}» {хочет} выше потолка контракта {потолок}")
    return причины


@router.get("/api/process/graph")
async def process_graph(tenant: str = "default", u: dict = Depends(user)) -> dict:
    """Граф-слой регламента: узлы (операции/нормы с ключом НСИ) + рёбра (contains-иерархия +
    entails/action-кросс-цитирование норм). Для карты и трассировки последствий. §граф-слой."""
    chunks = await reglament_store.all_for(tenant)
    edges = await reglament_store.edges_for(tenant)
    nodes = [{"nsi_key": c["nsi_key"], "op": c["op"], "process": c["process"], "subprocess": c["subprocess"]}
             for c in chunks]
    return {"tenant": tenant, "nodes": nodes, "edges": edges,
            "summary": {"nodes": len(nodes), "contains": sum(1 for e in edges if e["relation"] == "contains"),
                        "entails": sum(1 for e in edges if e["relation"] == "entails"),
                        "action": sum(1 for e in edges if e["relation"] == "action")}}


@router.post("/api/process/conformance")
async def process_conformance(body: dict, u: dict = Depends(user)) -> dict:
    """Сверка собранного процесса ABOP с регламентом по ключу НСИ: структурный drift (нет в регламенте /
    не собрано) + смысловой (cosine эмбеддинга операции ABOP vs чанк регламента, порог). §регламент-
    конформанс. Тело: {ops:[{nsi_key,label}], tenant?}."""
    ops = [o for o in ((body or {}).get("ops") or []) if o.get("nsi_key") or o.get("label")]
    tenant = str((body or {}).get("tenant") or "default").strip() or "default"
    # sLAVA-режим: сверка против РЕАЛЬНОГО регламент-корпуса (slava_reglament_ru/audit1c_norms) —
    # для каждой операции ABOP vector-поиск в sLAVA → лучший чанк+score → статус. Прямое направление
    # (ABOP-операция vs регламент); «не собрано» тут не считаем (корпус 22k чанков не перечислить).
    if str((body or {}).get("source") or "pg").strip() == "slava":
        collection = str((body or {}).get("collection") or "slava_reglament_ru").strip()
        report = []
        for o in ops:
            label = o.get("label") or ""
            try:
                srcs = (await slava.query(collection, label, top_k=1, tenant="abop")).get("sources") or []
            except Exception:  # noqa: BLE001
                srcs = []
            if not srcs:
                report.append({"nsi_key": o.get("nsi_key"), "label": label, "status": "нет в регламенте", "sim": None})
            else:
                sc = float(srcs[0].get("score") or 0)
                status = "соответствует" if sc >= 0.7 else ("расходится по сути" if sc >= 0.5 else "сильно расходится")
                report.append({"nsi_key": o.get("nsi_key"), "label": label, "status": status,
                               "sim": round(sc, 3), "reglament_op": (srcs[0].get("text") or "")[:90]})
        summary = {"total": len(report), "ok": sum(1 for x in report if x["status"] == "соответствует"),
                   "drift": sum(1 for x in report if x["status"] != "соответствует")}
        return {"collection": collection, "source": "slava", "report": report, "summary": summary}
    reg = await reglament_store.all_for(tenant)
    op_embs = await clients.embed([o.get("label") or "" for o in ops]) if ops else []
    # match='label': сопоставление по ЛУЧШЕМУ cosine (без обязательного nsi_key на узлах) — для карты
    # прогона, где узлы = навыки без ключей НСИ. Матчим операцию сборки к чанку регламента по смыслу.
    if str((body or {}).get("match") or "").strip() == "label":
        report = []
        for o, emb in zip(ops, op_embs):
            best, best_sim = None, 0.0
            for r in reg:
                sim = _cosine(emb, r.get("embedding") or [])
                if sim > best_sim:
                    best, best_sim = r, sim
            if not best or best_sim < 0.4:
                report.append({"nsi_key": o.get("nsi_key"), "label": o.get("label"), "status": "нет в регламенте", "sim": round(best_sim, 3)})
            else:
                status = "соответствует" if best_sim >= 0.7 else "расходится по сути"
                report.append({"nsi_key": best.get("nsi_key"), "label": o.get("label"),
                               "reglament_op": best.get("op"), "status": status, "sim": round(best_sim, 3)})
        summary = {"total": len(report), "ok": sum(1 for x in report if x["status"] == "соответствует"),
                   "drift": sum(1 for x in report if x["status"] != "соответствует")}
        return {"tenant": tenant, "report": report, "summary": summary, "match": "label"}
    reg_by_key: dict = {}
    for r in reg:
        reg_by_key.setdefault(r["nsi_key"], r)
    report, seen = [], set()
    for o, emb in zip(ops, op_embs):
        k = o.get("nsi_key")
        seen.add(k)
        r = reg_by_key.get(k)
        if not r:
            report.append({"nsi_key": k, "label": o.get("label"), "status": "нет в регламенте", "sim": None})
        else:
            sim = _cosine(emb, r.get("embedding") or [])
            status = "соответствует" if sim >= 0.75 else ("расходится по сути" if sim >= 0.5 else "сильно расходится")
            report.append({"nsi_key": k, "label": o.get("label"), "reglament_op": r.get("op"),
                           "status": status, "sim": round(sim, 3)})
    for k, r in reg_by_key.items():
        if k not in seen:
            report.append({"nsi_key": k, "label": None, "reglament_op": r.get("op"),
                           "status": "не собрано (есть в регламенте)", "sim": None})
    summary = {"total": len(report), "ok": sum(1 for x in report if x["status"] == "соответствует"),
               "drift": sum(1 for x in report if x["status"] != "соответствует")}
    return {"tenant": tenant, "report": report, "summary": summary}
