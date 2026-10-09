# -*- coding: utf-8 -*-
"""Агенты: сборка, версии, конверт, развёртывание, флот и приём контрактов.

Самый большой домен маршрутов (ADR-052, docs/RASKLADKA_SERVERA.md) — и поэтому он поехал
последним: сначала из монолита пришлось вынуть слой исполнения прогона (`server/run_exec.py`),
который зовут и агенты, и прогоны, и триггеры.

Что здесь: авторинг агента на канве и проверка его графа до сохранения, версии и откат,
развёртывание и пауза, конверт (автономия ≤ контракт, ADR-013), подбор агента по словам и смыслу,
флот и приём контрактов от LUDA.

Правило домена, которое нельзя потерять: конверт проверяется ПЕРЕД сохранением, а не при запуске.
Агента, нарушающего границы, среда не принимает — иначе «нельзя» превращается в «можно, но потом
накажем».

Зависимости наружу: `server/deps` (личность и гейты), сторы, ядро `ape` и `run_exec`;
`web_api` отсюда не импортируется.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from cli import ape

from .. import assembly, audit_store, clients, contract_store, families_store, hitl_store
from .. import ingress, lexicon, nlu
from .. import observability as obs
from .. import planner, run_queue, run_store, runner, schema_store, skill_contract
from .. import skill_store, skill_templates, triggers
from .. import agent_store
from ..deps import _sub, can_see_family, require_level, user
from ..run_exec import execute_agent_run
from ..vectors import cosine as _cosine

router = APIRouter()


# ═══════════════ ФЛОТ (линза «Эксплуатирую»): только реальные данные, без симуляции ═══════════════
_DEPLOY_STATUSES = ("draft", "deployed", "paused")


def _graph_titles(a: dict | None) -> list[str]:
    g = (a or {}).get("graph") or {}
    return [str(n.get("title") or n.get("id") or "") for n in (g.get("nodes") or []) if isinstance(n, dict)]


_AGENT_EMB_CACHE: dict = {}   # id → (doc_hash, embedding), чтобы не эмбедить агентов каждый раз


@router.get("/api/agents/spec")
def agent_spec(family: str, member: str = "", u: dict = Depends(user)) -> dict:
    """Спека агента (ADR-032): роль семьи + навыки + become-переходы + конверт + data-scope
    (резолв рецептов по entity). §6 авторинг узла-агента на канве."""
    try:
        return ape.build_agent_spec(family, member)
    except ValueError as ex:
        raise HTTPException(404, str(ex))


@router.post("/api/contracts/ingest")
async def contracts_ingest(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Принять handoff-бандл LUDA: валидация схем luda.*/1.0 → сохранение ContractSet.

    Тело — сам бандл {capability_request, deployment_contract, baseline_measurement,
    evidence_pack}. Невалидный → 422 со списком ошибок (SDD §4.3, не доверяем вслепую).
    """
    require_level(u, "manager")   # red-team #3: приём контракта — мутация среды
    res = ingress.validate(body)
    if not res.accepted:
        return JSONResponse(
            {"accepted": False, "errors": res.errors, "warnings": res.warnings},
            status_code=422,
        )
    saved = await contract_store.save(body, res.intake, ingested_by=u.get("name") or u.get("sub") or "dev")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "contract.ingest", saved["audit_id"],
                             {"family": res.intake.get("family"), "autonomy_ceiling": res.intake.get("autonomy_ceiling"),
                              "skills": len(res.intake.get("skills") or [])})
    return JSONResponse({
        "accepted": True, "warnings": res.warnings,
        "audit_id": saved["audit_id"], "intake": res.intake,
        "ingested_at": saved.get("ingested_at"),
    }, status_code=201)


@router.get("/api/contracts")
async def contracts_list(u: dict = Depends(user)) -> dict:
    """Список принятых ContractSet (краткие карточки). §4/§6 — источник для канвы. ABAC: только своя семья."""
    items = await contract_store.list_all()
    return {"contracts": [c for c in items if can_see_family(u, (c.get("family") or (c.get("intake") or {}).get("family")))]}


@router.get("/api/contracts/{audit_id}")
async def contracts_get(audit_id: str, u: dict = Depends(user)) -> dict:
    """Полный ContractSet по audit_id (бандл + intake) — для посева канвы."""
    cs = await contract_store.get(audit_id)
    if not cs:
        raise HTTPException(404, "нет такого ContractSet")
    if not can_see_family(u, (cs.get("family") or (cs.get("intake") or {}).get("family"))):
        raise HTTPException(403, "контракт другого отдела")
    return cs


def _resolve_role(fam: str, intake: dict) -> str:
    """Роль в семье по контракту: единственная — она; иначе — по макс. совпадению навыков intake."""
    if fam not in ape.AGENT_FAMILIES:
        return ""
    members = ape.AGENT_FAMILIES[fam]["members"]
    if len(members) == 1:
        return next(iter(members))
    want = set(intake.get("skills") or [])
    best, best_score = "", 0
    for rid, (_title, _sk) in members.items():
        score = len(want & set(_sk))
        if score > best_score:
            best, best_score = rid, score
    return best


@router.post("/api/agents")
async def agent_save(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Сохранить собранный на канве граф как AgentVersion (draft), привязав к ContractSet.

    Тело: {name, contract_audit_id, graph:{nodes[],edges[]}}. До сохранения — governance:
    автономия узла ≤ потолок (ADR-013), внешнее действие под HITL (ADR-014), покрытие (SDD §4.4).
    Жёсткие нарушения → 422; сохранение только чистого графа.
    """
    require_level(u, "manager")   # red-team #3: сохранение агента — мутация (аналитик — только чтение)
    name = str((body or {}).get("name", "")).strip()
    audit_id = str((body or {}).get("contract_audit_id", "")).strip()
    graph = (body or {}).get("graph") or {}
    if not name or not audit_id:
        raise HTTPException(422, "нужны name и contract_audit_id")
    cs = await contract_store.get(audit_id)
    if not cs:
        raise HTTPException(404, "нет ContractSet для привязки")

    check = assembly.check_graph(graph, cs.get("intake") or {}, ape.skill_safety, reads_data=await _skills_reading_data())
    if check["errors"]:
        return JSONResponse({"saved": False, "errors": check["errors"],
                             "warnings": check["warnings"]}, status_code=422)

    # семья/роль — из контракта LUDA (intake), чтобы агент корректно отражался в «Агентах»,
    # проходил ABAC (can_see_family) и обогащал журнал/Флот (ADR-032).
    intake = cs.get("intake") or {}
    fam = intake.get("family") or ""
    role = _resolve_role(fam, intake)
    # ADR-024: обычное сохранение/автосейв ПЕРЕЗАПИСЫВАЕТ draft (не плодит версии).
    # Новая версия — только при осознанном Пересмотре (revise=true) поверх НЕ-draft.
    revise = bool((body or {}).get("revise"))
    if revise:
        version = await agent_store.next_version(audit_id)
        saved = await agent_store.save(name=name, audit_id=audit_id, version=version, graph=graph,
                                       autonomy_max=check["autonomy_max"],
                                       created_by=u.get("name") or u.get("sub") or "dev",
                                       family=fam, role=role)
    else:
        saved = await agent_store.save_draft(name=name, audit_id=audit_id, graph=graph,
                                             autonomy_max=check["autonomy_max"],
                                             created_by=u.get("name") or u.get("sub") or "dev",
                                             family=fam, role=role)
    version = saved.get("version")
    await audit_store.record(u.get("name") or u.get("sub") or "dev",
                             "agent.revise" if revise else "agent.save", saved["id"],
                             {"family": fam, "role": role, "autonomy_max": check["autonomy_max"],
                              "hitl": check["hitl_count"]})
    return JSONResponse({"saved": True, "id": saved["id"], "version": version, "status": "draft",
                         "family": fam, "role": role,
                         "autonomy_max": check["autonomy_max"], "hitl_count": check["hitl_count"],
                         "warnings": check["warnings"]}, status_code=201)


def _edit_scope(agent: dict) -> str:
    """Кто вправе править агента (governance-класс):
    - 'user' — пользовательский агент (собран из чата/authored, менеджер настраивает под себя, напр. «Дайджест задач»);
    - 'methodologist' — методологический (по контракту LUDA, правит только методолог через ABOP, напр. «Аудитор 1С»).
    Эвристика по источнику: authored → user; contract → methodologist. Явное поле перекрывает."""
    ex = (agent or {}).get("edit_scope")
    if ex in ("user", "methodologist"):
        return ex
    # Пользовательские: собранные из чата (authored) ИЛИ менеджерская семья (management) — юзер правит под себя.
    # Методологические: специализированные семьи (audit/finance/analytics/credit/architecture) — по контракту LUDA.
    if (agent or {}).get("source") == "authored" or (agent or {}).get("family") == "management":
        return "user"
    return "methodologist"


@router.get("/api/agents")
async def agents_list(contract: str = "", archived: bool = False, u: dict = Depends(user)) -> dict:
    """Список AgentVersion. ABAC: пользователь видит только агентов своего отдела (family==department);
    admin/support (область *) — всех. archived=1 → Лимб (retired). §7/§8а."""
    items = await agent_store.list_for(contract or None, archived=archived)
    # Схлопываем до ПОСЛЕДНЕЙ версии на агента (contract_audit_id) — 1 агент = 1 запись, без визуальных
    # дублей версий (#8). У authored теперь стабильный per-agent id, поэтому ключ работает и для них.
    latest: dict = {}
    for a in items:
        cid = a.get("contract_audit_id") or a.get("id")
        if cid not in latest or (a.get("version") or 0) > (latest[cid].get("version") or 0):
            latest[cid] = a
    out = []
    for a in latest.values():
        if can_see_family(u, a.get("family")):
            a = dict(a)
            a["edit_scope"] = _edit_scope(a)          # governance-класс: user | methodologist
            a["owner"] = a.get("source") == "authored"  # «мой» агент (создан пользователем) vs общий (#9)
            out.append(a)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"agents": out}


def _dep_status_ru(agent: dict, trig_rows: list[dict]) -> str:
    """Статус развёртывания по-русски: актив / пауза / черновик / отозван."""
    st = agent.get("status") or "draft"
    if st == "retired":
        return "отозван"
    if st == "paused":
        return "пауза"
    if st == "deployed":
        return "актив"
    if trig_rows and any(t.get("enabled") for t in trig_rows):
        return "актив"          # черновик, но у него включённое расписание → фактически работает
    if trig_rows:
        return "пауза"          # расписания есть, все выключены
    return "черновик"


def _version_diff(cur: dict | None, prev: dict | None) -> list[dict]:
    """Честная разница версий по узлам графа (что уйдёт, что вернётся при откате)."""
    ct, pt = set(_graph_titles(cur)), set(_graph_titles(prev))
    out = [{"sign": "−", "text": "узел «" + t + "» уйдёт (есть только в текущей версии)"} for t in sorted(ct - pt)]
    out += [{"sign": "+", "text": "узел «" + t + "» вернётся (есть в предыдущей версии)"} for t in sorted(pt - ct)]
    same = len(ct & pt)
    if same:
        out.append({"sign": "·", "text": str(same) + " " + ("узел без изменений" if same == 1 else "узлов без изменений")})
    if (cur or {}).get("autonomy_max") != (prev or {}).get("autonomy_max"):
        out.append({"sign": "·", "text": "автономия " + str((cur or {}).get("autonomy_max")) + " → " + str((prev or {}).get("autonomy_max"))})
    return out or [{"sign": "·", "text": "графы версий совпадают"}]


async def _prev_version_of(a: dict) -> dict | None:
    """Предыдущая (не retired) версия того же агента — цель отката."""
    cid = a.get("contract_audit_id") or a.get("id")
    cands = [x for x in await agent_store.list_for(cid, limit=200) if (x.get("version") or 0) < (a.get("version") or 0)]
    cands.sort(key=lambda x: x.get("version") or 0, reverse=True)
    return cands[0] if cands else None


@router.post("/api/agents/{agent_id}/status")
async def agent_set_status(agent_id: str, body: dict, u: dict = Depends(user)) -> dict:
    """Статус развёртывания: deployed (актив — принимает триггеры), paused (планировщик не фаерит),
    draft. Реальный эффект: scheduler_loop пропускает paused. manager+."""
    require_level(u, "manager")
    st = str((body or {}).get("status") or "").strip()
    if st not in _DEPLOY_STATUSES:
        raise HTTPException(422, "status: draft | deployed | paused")
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, a.get("family")):
        raise HTTPException(403, "агент другого отдела")
    await agent_store.set_status(agent_id, st)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "agent.status", agent_id,
                             {"from": a.get("status"), "to": st})
    return {"id": agent_id, "status": st}


@router.get("/api/agents/{agent_id}/rollback")
async def agent_rollback_preview(agent_id: str, u: dict = Depends(user)) -> dict:
    """Что изменится при откате на предыдущую версию (реальный diff узлов графа)."""
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого агента")
    prev = await _prev_version_of(a)
    prev_full = await agent_store.get(prev["id"]) if prev else None
    return {"id": agent_id, "version": a.get("version"), "prev": (prev or {}).get("id"),
            "prev_version": (prev or {}).get("version"), "diff": _version_diff(a, prev_full) if prev else []}


@router.post("/api/agents/{agent_id}/rollback")
async def agent_rollback(agent_id: str, u: dict = Depends(user)) -> dict:
    """Откат: предыдущая версия становится deployed, текущая уходит в Лимб (retired, обратимо через restore).
    Планировщик берёт последнюю НЕ-retired версию → расписания начнут фаерить из предыдущей. manager+."""
    require_level(u, "manager")
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, a.get("family")):
        raise HTTPException(403, "агент другого отдела")
    prev = await _prev_version_of(a)
    if not prev:
        raise HTTPException(409, "предыдущей версии нет — откатывать некуда")
    await agent_store.set_status(prev["id"], "deployed")
    await agent_store.set_status(agent_id, "retired")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "agent.rollback", agent_id,
                             {"to": prev["id"], "from_version": a.get("version"), "to_version": prev.get("version")})
    return {"ok": True, "from": agent_id, "to": prev["id"], "to_version": prev.get("version")}


@router.get("/api/fleet")
async def fleet(u: dict = Depends(user)) -> dict:
    """Флот целиком из реальных источников (ABAC по семье): развёртывания = последние версии агентов
    (+ их триггеры, верификация, предыдущая версия), живые = задания очереди + недавние прогоны,
    алерты = ошибки/HITL/память/конверт, история 8 дней = ошибки/стоимость/DoD из журнала прогонов."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    actor = u.get("name") or u.get("sub") or "dev"
    all_ = u.get("level") in ("admin", "support")
    active = [a for a in await agent_store.list_for(None, limit=500) if can_see_family(u, a.get("family"))]
    limbo = [a for a in await agent_store.list_for(None, limit=500, archived=True) if can_see_family(u, a.get("family"))]
    fams = {f["id"]: f for f in await families_store.all()}
    trig_by: dict = {}
    for t in await triggers.list_triggers():
        trig_by.setdefault(t["agent_id"], []).append(t)
    # последняя версия на агента + наличие предыдущей
    latest: dict = {}
    versions: dict = {}
    for a in active:
        cid = a.get("contract_audit_id") or a.get("id")
        versions.setdefault(cid, []).append(a.get("version") or 0)
        if cid not in latest or (a.get("version") or 0) > (latest[cid].get("version") or 0):
            latest[cid] = a
    latest_ids = {a["id"] for a in latest.values()}
    retired_latest = {}
    for a in limbo:
        cid = a.get("contract_audit_id") or a.get("id")
        if cid in latest:
            continue
        if cid not in retired_latest or (a.get("version") or 0) > (retired_latest[cid].get("version") or 0):
            retired_latest[cid] = a

    def _trig_text(rows: list[dict]) -> tuple[str, str]:
        if not rows:
            return ("вручную / по запросу", "▶")
        r = rows[0]
        if r.get("type") == "schedule":
            return ("расписание " + str(r.get("cron") or ""), "⏱")
        if r.get("type") == "event":
            return ("событие " + str(r.get("source") or ""), "⚡")
        return (str(r.get("type") or "триггер"), "✉")

    def _dep(a: dict) -> dict:
        rows = trig_by.get(a["id"], [])
        ttext, ticon = _trig_text(rows)
        ver = (a.get("verification") or {}) if isinstance(a.get("verification"), dict) else {}
        # Чужой рантайм: графа нет, осматривать нечем. Показать «contract», как у агента по
        # контракту LUDA, значило бы выдать объявленный конверт за проверенный.
        verify = ("external" if a.get("source") == "external"
                  else "contract" if a.get("source") != "authored"
                  else ("ok" if ver.get("ok") else ("fail" if ver else "draft")))
        cid = a.get("contract_audit_id") or a.get("id")
        vs = sorted(versions.get(cid, []))
        return {"id": a["id"], "agent": a.get("name") or a["id"], "version": "v" + str(a.get("version") or 1),
                "prev": ("v" + str(vs[-2])) if len(vs) >= 2 else None,
                "area": (fams.get(a.get("family") or "") or {}).get("title") or a.get("family") or "—",
                "family": a.get("family"), "role": a.get("role"),
                "trigger": ttext, "tIcon": ticon, "triggers": len(rows),
                "trigger_enabled": any(t.get("enabled") for t in rows),
                "last_fire": max([t.get("last_fire") or "" for t in rows] or [""]) or None,
                "status": _dep_status_ru(a, rows), "raw_status": a.get("status"),
                "owner": a.get("created_by") or "—", "autonomy": str(a.get("autonomy_max") or "A1"),
                "since": (a.get("created_at") or "")[:10], "verify": verify}
    deployments = [_dep(a) for a in latest.values()] + [_dep(a) for a in retired_latest.values()]
    deployments.sort(key=lambda d: ({"актив": 0, "пауза": 1, "черновик": 2, "отозван": 3}.get(d["status"], 9), d["agent"]))

    # живые: задания очереди (мои / все для admin+support) + недавние прогоны
    names = {a["id"]: a.get("name") for a in active + limbo}
    fam_of = {a["id"]: a.get("family") for a in active + limbo}
    jobs = [j for j in await run_queue.list_jobs(actor=None if all_ else actor, limit=100)
            if can_see_family(u, fam_of.get(j.get("agent_id")))]
    runs = [r for r in await run_store.list_runs(limit=300) if can_see_family(u, fam_of.get(r.get("agent_id")))]
    run_by_id = {r["id"]: r for r in runs}

    def _secs(ts: str | None, end: str | None = None) -> int:
        try:
            t0 = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=_dt.timezone.utc)
            t1 = _dt.datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else now
            if t1.tzinfo is None:
                t1 = t1.replace(tzinfo=_dt.timezone.utc)
            return max(0, int((t1 - t0).total_seconds()))
        except Exception:  # noqa: BLE001
            return 0
    live, seen_runs = [], set()
    for j in jobs:
        st = j.get("status")
        cp = j.get("checkpoint") or {}
        status = {"queued": "ждёт", "running": "думает", "awaiting_hitl": "HITL", "done": "готово",
                  "failed": "ошибка", "cancelled": "отменён"}.get(st, st)
        run = run_by_id.get(j.get("run_id") or "")
        cost = float(((run or {}).get("cost") or {}).get("rub") or 0)
        step = ("цепочка · шаг " + str((cp.get("step") or 0) + 1) + "/" + str(cp.get("steps_total") or "?")) if (j.get("kind") == "pipeline") \
            else (("v" + str(j.get("version") or "")) if j.get("version") else "прогон")
        if st == "done" and j.get("kind") == "pipeline":
            step = "цепочка · " + str(cp.get("steps_total") or len(cp.get("steps") or [])) + " " + ("шаг" if (cp.get("steps_total") or 0) == 1 else "шага" if (cp.get("steps_total") or 0) in (2, 3, 4) else "шагов") + " · готово"
        elif st == "done" and run:
            step = ("вердикт пройден" if run.get("verdict_ok") else "есть замечания")
            status = "готово" if run.get("verdict_ok") else "внимание"
        if st == "failed":
            step = str(j.get("error") or "ошибка")[:80]
        live.append({"id": j["id"], "dep": j.get("agent_id"), "agent": names.get(j.get("agent_id")) or j.get("agent_id"),
                     "step": step, "init": "запустил " + str(j.get("actor") or "—"),
                     "dur": _secs(j.get("started_at") or j.get("created_at"), j.get("finished_at")),
                     "cost": cost, "status": status, "phase": "live" if st in ("queued", "running", "awaiting_hitl") else "done",
                     "run_id": j.get("run_id"), "when": (j.get("created_at") or "")[:16].replace("T", " ")})
        if j.get("run_id"):
            seen_runs.add(j["run_id"])
    for r in runs[:12]:
        if r["id"] in seen_runs:
            continue
        live.append({"id": r["id"], "dep": r.get("agent_id"), "agent": names.get(r.get("agent_id")) or r.get("agent_id"),
                     "step": "вердикт пройден" if r.get("verdict_ok") else "есть замечания",
                     "init": ("запустил " + str(r.get("started_by"))) if r.get("started_by") else "прогон",
                     "dur": 0, "cost": float((r.get("cost") or {}).get("rub") or 0),
                     "status": ("HITL" if (r.get("hitl_count") or 0) else ("готово" if r.get("verdict_ok") else "внимание")),
                     "phase": "done", "run_id": r["id"], "when": (r.get("created_at") or "")[:16].replace("T", " ")})
    order = {"live": 0, "done": 1}
    live.sort(key=lambda x: (order.get(x["phase"], 2), x.get("when") or ""), reverse=False)
    live = [x for x in live if x["phase"] == "live"] + sorted([x for x in live if x["phase"] != "live"], key=lambda x: x.get("when") or "", reverse=True)[:10]

    # история 8 дней: ошибки / стоимость / DoD
    days = [(now - _dt.timedelta(days=7 - k)).date() for k in range(8)]
    hist = {d: {"err": 0, "cost": 0.0, "ok": 0, "n": 0} for d in days}
    for r in runs:
        try:
            d = _dt.datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00")).date()
        except Exception:  # noqa: BLE001
            continue
        if d in hist:
            hist[d]["n"] += 1
            hist[d]["ok"] += 1 if r.get("verdict_ok") else 0
            hist[d]["err"] += 0 if r.get("verdict_ok") else 1
            hist[d]["cost"] += float((r.get("cost") or {}).get("rub") or 0)
    for j in jobs:
        if j.get("status") == "failed":
            try:
                d = _dt.datetime.fromisoformat(str(j.get("created_at")).replace("Z", "+00:00")).date()
                if d in hist:
                    hist[d]["err"] += 1
            except Exception:  # noqa: BLE001
                pass
    series = [{"day": d.isoformat(), "err": hist[d]["err"], "cost": round(hist[d]["cost"], 2),
               "n": hist[d]["n"], "ok": hist[d]["ok"],
               "dod": (round(100 * hist[d]["ok"] / hist[d]["n"]) if hist[d]["n"] else None)} for d in days]

    # Дрейф DoD: число в панели здоровья, которое само по себе ничего не объясняет. Отдаём разбор —
    # из чего сложено, на каких днях и какие прогоны его уронили, — чтобы интерфейс не пересказывал
    # график, а показывал причину. День без прогонов в расчёт не входит: это пробел, не ноль.
    _падения: list[dict] = []
    for r in runs:
        if r.get("verdict_ok"):
            continue
        try:
            d = _dt.datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00")).date()
        except Exception:  # noqa: BLE001
            continue
        if d not in hist:
            continue
        _падения.append({"at": str(r.get("created_at"))[:16].replace("T", " "),
                         "agent_id": r.get("agent_id"),
                         "agent": names.get(r.get("agent_id")) or r.get("agent_id") or "—",
                         "run_id": r.get("id"), "why": "вердикт прогона не закрыт"})
    for j in jobs:
        if j.get("status") != "failed":
            continue
        try:
            d = _dt.datetime.fromisoformat(str(j.get("created_at")).replace("Z", "+00:00")).date()
        except Exception:  # noqa: BLE001
            continue
        if d not in hist:
            continue
        _падения.append({"at": str(j.get("created_at"))[:16].replace("T", " "),
                         "agent_id": j.get("agent_id"),
                         "agent": names.get(j.get("agent_id")) or j.get("agent_id") or "—",
                         "run_id": j.get("run_id") or j.get("id"),
                         "why": str(j.get("error") or "")[:140] or "задание упало без текста ошибки"})
    _падения.sort(key=lambda x: x["at"], reverse=True)
    _живые_дни = [x for x in series if x["n"]]
    _ранее = _живые_дни[:-1]
    _сейчас = _живые_дни[-1]["dod"] if _живые_дни else None
    _база = round(sum(x["dod"] for x in _ранее) / len(_ранее)) if _ранее else None
    dod = {"now": _сейчас, "base": _база,
           "drift": (_сейчас - _база) if (_сейчас is not None and _база is not None) else None,
           "last_day": _живые_дни[-1]["day"] if _живые_дни else None,
           "days_total": len(days), "days_with_runs": len(_живые_дни),
           "runs": sum(x["n"] for x in series), "ok": sum(x["ok"] for x in series),
           "failed": _падения[:14], "failed_total": len(_падения)}

    # алерты — только реальные события
    alerts = []
    day_ago = now - _dt.timedelta(hours=24)
    for j in jobs:
        if j.get("status") == "failed" and _secs(j.get("created_at")) < 86400:
            alerts.append({"level": "crit", "kind": "runs", "agent_id": j.get("agent_id"),
                           "title": (names.get(j.get("agent_id")) or j.get("agent_id")) + ": прогон завершился ошибкой",
                           "note": str(j.get("error") or "")[:160] or "без текста ошибки", "action": "журнал"})
    pending = [h for h in await hitl_store.list_pending() if can_see_family(u, h.get("family"))]
    if pending:
        alerts.append({"level": "warn", "kind": "hitl", "title": str(len(pending)) + " " + ("решение ждёт" if len(pending) == 1 else "решения ждут") + " оператора",
                       "note": "Прогоны/цепочки не продолжатся, пока вы не подтвердите или не отклоните доставку.", "action": "к очереди"})
    for d in deployments:
        if d["verify"] == "fail":
            alerts.append({"level": "warn", "kind": "envelope", "agent_id": d["id"], "title": d["agent"] + " " + d["version"] + ": нарушение конверта",
                           "note": "Авто-верификация нашла узлы выше разрешённой автономии — см. вердикт в паспорте агента.", "action": "паспорт"})
        if d["trigger_enabled"] and d["status"] == "актив" and d.get("last_fire") and _secs(d["last_fire"]) > 2 * 86400 and "расписание" in d["trigger"]:
            alerts.append({"level": "soft", "kind": "trigger", "agent_id": d["id"], "title": d["agent"] + ": расписание не срабатывало 2 дня",
                           "note": "Последний запуск " + str(d["last_fire"])[:16].replace("T", " ") + ". Проверьте cron и статус агента.", "action": "развёртывания"})
    qstats = await run_queue.stats()
    if qstats.get("memory_pressure"):
        alerts.append({"level": "crit", "kind": "memory", "title": "Память процесса выше мягкого лимита",
                       "note": "RSS " + str(qstats.get("rss_mb")) + " МБ при лимите " + str(qstats.get("mem_soft_mb")) + " МБ — воркеры не берут новые задания до снижения.", "action": "очередь"})
    obs.inc("abop_http_fleet_total")
    return {"deployments": deployments, "live": live, "alerts": alerts, "series": series, "dod": dod,
            "queue": {"by_status": qstats.get("by_status") or {}, "workers": qstats.get("workers"),
                      "rss_mb": qstats.get("rss_mb"), "memory_pressure": bool(qstats.get("memory_pressure"))},
            "hitl_pending": len(pending), "generated_at": now.isoformat(timespec="seconds")}


@router.post("/api/agents/{agent_id}/retire")
async def agent_retire(agent_id: str, u: dict = Depends(user)) -> dict:
    """Вывести агента из эксплуатации → Лимб (архив, status=retired; ADR-024). Обратимо через restore."""
    require_level(u, "manager")  # analyst — только чтение
    a = await agent_store.set_status(agent_id, "retired")
    if not a:
        raise HTTPException(404, "нет такого агента")
    return {"id": agent_id, "status": "retired", "archived": True}


@router.post("/api/agents/{agent_id}/restore")
async def agent_restore(agent_id: str, u: dict = Depends(user)) -> dict:
    """Вернуть агента из Лимба обратно в draft (ADR-024)."""
    require_level(u, "manager")
    a = await agent_store.set_status(agent_id, "draft")
    if not a:
        raise HTTPException(404, "нет такого агента")
    return {"id": agent_id, "status": "draft", "archived": False}


@router.delete("/api/agents/{agent_id}")
async def agent_delete(agent_id: str, u: dict = Depends(user)) -> dict:
    """Полное удаление агента (жёсткое, минуя Лимб). Идемпотентно: сносит ВСЕ версии этого агента
    (по contract_audit_id), чтобы «мой» агент исчезал целиком и везде; повторный вызов не падает (#8/#9)."""
    require_level(u, "manager")
    a = await agent_store.get(agent_id)
    cid = (a or {}).get("contract_audit_id")
    deleted = 0
    if cid:
        vers = await agent_store.list_for(cid) + await agent_store.list_for(cid, archived=True)
        for v in vers:
            if await agent_store.delete(v["id"]):
                deleted += 1
    if not deleted:                                   # запасной путь: снести хотя бы саму версию
        deleted = 1 if await agent_store.delete(agent_id) else 0
    if not deleted:
        raise HTTPException(404, "нет такого агента")   # UX-аудит: DELETE несуществующего отвечал «успех»
    actor = u.get("name") or u.get("sub") or "dev"
    await audit_store.record(actor, "agent.delete", agent_id, {"versions_removed": deleted}, severity="warn")
    return {"id": agent_id, "deleted": True, "versions_removed": deleted}


@router.post("/api/agents/check")
async def agent_check(body: dict, u: dict = Depends(user)) -> dict:
    """Dry-run governance-проверка графа против контракта БЕЗ сохранения (ADR-013/014, SDD §4.4).

    Тело: {contract_audit_id, graph}. Возвращает {ok, errors[], warnings[], autonomy_max, hitl_count}.
    Питает кнопку «Проверить» в канве настоящим вердиктом конверта.
    """
    audit_id = str((body or {}).get("contract_audit_id", "")).strip()
    graph = (body or {}).get("graph") or {}
    cs = await contract_store.get(audit_id)
    if not cs:
        raise HTTPException(404, "нет ContractSet для проверки")
    check = assembly.check_graph(graph, cs.get("intake") or {}, ape.skill_safety, reads_data=await _skills_reading_data())
    gaps = await _input_gaps(graph)
    return {"ok": not check["errors"], "errors": check["errors"],
            "warnings": check["warnings"] + [g["text"] for g in gaps],
            "input_gaps": gaps,
            "autonomy_max": check["autonomy_max"], "hitl_count": check["hitl_count"]}


async def _skills_reading_data() -> set:
    """Навыки, читающие данные НАПРЯМУЮ (вход `from: data` в контракте).

    Нужны governance-проверке узла вывода: действующий шаг, который сам ходит в данные, нельзя
    оставлять без человека — он один стоит между внешним текстом и необратимой отправкой.
    """
    out: set = set()
    try:
        rows = {t["id"]: t for t in (await schema_store.all() or [])}
    except Exception:  # noqa: BLE001 — без хранилища карантин просто не доказан
        return out
    for sid, t in rows.items():
        ins = (t or {}).get("inputs") or {}
        for bucket in ("required", "optional"):
            for it in (ins.get(bucket) or []):
                if isinstance(it, dict) and it.get("from") == "data":
                    out.add(str(sid))
                    break
    return out


async def _input_gaps(graph: dict) -> list[dict]:
    """Каким навыкам графа не хватает входа: нет данных сущности, не задан предмет, нет навыка-поставщика.

    Считается по контрактам навыков, волнами: навык видит только выходы тех, кто стоит раньше.
    Это ловит разрыв цепочки на сборке, а не на прогоне, где он выглядит пустым результатом."""
    nodes = [n for n in (graph or {}).get("nodes") or [] if n.get("skill")]
    if not nodes:
        return []
    ov = await skill_store.all()
    tpl_of: dict[str, dict] = {}
    for n in nodes:
        sid = n["skill"]
        tid = ((ov.get(sid) or {}).get("patch") or {}).get("schema_template_id") or sid
        tpl_of[sid] = await schema_store.get(tid) or await schema_store.get(sid) or {}

    entities: set[str] = set()
    for n in nodes:
        try:
            entities |= {d.get("entity") for d in (ape.skill_datasources_resolved(n["skill"]) or []) if d.get("entity")}
        except Exception:  # noqa: BLE001 — область данных опциональна
            pass
    slots = {str(sl.get("name")) for t in tpl_of.values() for sl in (t.get("slots") or []) if isinstance(sl, dict)}

    waves = runner._waves(nodes, (graph or {}).get("edges") or [])
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for wave in waves:
        for n in wave:
            sid = n.get("skill")
            if not sid:
                continue
            miss = skill_contract.coverage((tpl_of.get(sid) or {}).get("inputs"),
                                           entities=entities, slots=slots, upstream=seen)
            for m in miss:
                out.append({"skill": sid, "node": n.get("id"), "text": f"навык «{sid}»: {m}"})
        for n in wave:
            if n.get("skill"):
                seen[n["skill"]] = (tpl_of.get(n["skill"]) or {}).get("produces") or {}
    return out


async def _refresh_lexicon(agent: dict) -> dict:
    """Словарь лексем агента из описаний его навыков (шаблон + карточка + тело SKILL.md)."""
    try:
        tpls = {}
        for n in (agent.get("graph") or {}).get("nodes") or []:
            sid = n.get("skill")
            if sid and sid not in tpls:
                t = await schema_store.get(sid)
                if t:
                    tpls[sid] = {"name": t.get("name"), "instruction": skill_templates.strip_markers(t.get("instruction") or "")}
        return await lexicon.refresh(agent, skills=ape.SKILLS, body_of=ape.load_skill_body, templates=tpls)
    except Exception as _ex:  # noqa: BLE001 — подбор не должен падать из-за словаря
        obs.log_event("warning", "lexicon.refresh_failed", agent=agent.get("id"), error=str(_ex)[:160])
        return {}


def _agent_match_doc(a: dict) -> str:
    """Текст-документ агента для матча: имя + семья + роль + названия/описания его навыков."""
    parts = [a.get("name") or "", a.get("family") or "", a.get("role") or ""]
    for n in (a.get("graph") or {}).get("nodes") or []:
        sid = n.get("skill")
        if sid and sid in ape.SKILLS:
            t = ape.SKILLS[sid]
            parts.append(f"{t[0]} {t[1]}")   # заголовок + краткое
    return " · ".join(p for p in parts if p)


@router.post("/api/agents/match")
async def agents_match(body: dict, u: dict = Depends(user)) -> dict:
    """Подбор агента под задачу пользователя: ЛЕКСИКА (пересечение слов, надёжно/мгновенно) + СЕМАНТИКА
    (эмбеддинги BGE-M3, best-effort). Возвращает ранжированный топ агентов (ABAC по отделу) + их каналы
    доставки (из OUT-узлов графа). Питает дерево решений чата: «это задача для агента X, куда результат?»."""
    import re
    q = str((body or {}).get("q") or "").strip()
    if not q:
        return {"matches": []}
    # Правило достаточности: по двум словам подбирать нельзя. Под «сделай отчёт» подходит десяток
    # навыков, и уверенный выбор из них — обман. Возвращаем не пустоту, а конкретные вопросы,
    # ответы на которые делают запрос рабочим.
    if not bool((body or {}).get("force")):
        _enough = planner.sufficiency(q, {sid: {"title": (ape.SKILLS[sid] or ("",))[0],
                                                "body": ape.load_skill_body(sid) or ""}
                                          for sid in list(ape.SKILLS)})
        if not _enough["ok"]:
            return {"matches": [], "need_more": True, "sufficiency": _enough,
                    "note": _enough["почему"]}
    briefs = await agent_store.list_for(None)
    agents = []
    for b in briefs:
        if not can_see_family(u, b.get("family")):
            continue
        full = await agent_store.get(b["id"])
        if full:
            agents.append(full)
    # дедуп до ПОСЛЕДНЕЙ версии на агента — иначе подсказки/цепочки дублируют один агент (#8)
    _latest: dict = {}
    for a in agents:
        cid = a.get("contract_audit_id") or a.get("id")
        if cid not in _latest or (a.get("version") or 0) > (_latest[cid].get("version") or 0):
            _latest[cid] = a
    agents = list(_latest.values())
    if not agents:
        return {"matches": []}
    ql = q.lower()
    qterms = nlu.lexemes(q)          # нормализованные лексемы: «проверь» и «проверка» — одна основа
    docs = {a["id"]: _agent_match_doc(a) for a in agents}
    # лексика: совпадение со СЛОВАРЁМ агента (засеян из описаний его навыков) + бонус за имя;
    # словарь пустой (новый агент) → фолбэк на слова документа, чтобы подбор не молчал
    lexmap = await lexicon.all_terms()
    lex = {}
    for a in agents:
        terms = lexmap.get(a["id"]) or {}
        if not terms:
            terms = lexicon.build(a, skills=ape.SKILLS, templates={})
        sc = lexicon.score(qterms, terms) * 4.0
        name_hit = 2 if (a.get("name") or "").lower() in ql else 0
        lex[a["id"]] = sc + name_hit
    # семантика (best-effort): эмбеддим запрос + документы агентов (кэш по документу)
    sem = {a["id"]: 0.0 for a in agents}
    try:
        import hashlib as _h
        need_emb, need_ids = [], []
        for a in agents:
            dh = _h.md5(docs[a["id"]].encode()).hexdigest()
            cached = _AGENT_EMB_CACHE.get(a["id"])
            if not cached or cached[0] != dh:
                need_emb.append(docs[a["id"]]); need_ids.append((a["id"], dh))
        if need_emb:
            embs = await clients.embed(need_emb)
            for (aid, dh), e in zip(need_ids, embs):
                _AGENT_EMB_CACHE[aid] = (dh, e)
        qemb = (await clients.embed([q]))[0]
        for a in agents:
            c = _AGENT_EMB_CACHE.get(a["id"])
            if c:
                sem[a["id"]] = _cosine(qemb, c[1])
    except Exception:  # noqa: BLE001 — семантика опциональна, лексики достаточно
        pass
    # итоговый скор: лексика (норм.) + семантика
    maxlex = max(lex.values()) or 1
    scored = []
    for a in agents:
        score = 0.55 * (lex[a["id"]] / maxlex) + 0.45 * sem[a["id"]]
        channels = [n.get("channel") or (n.get("out") or {}).get("channel")
                    for n in (a.get("graph") or {}).get("nodes") or [] if n.get("kind") in ("output", "out")]
        channels = [c for c in channels if c]
        scored.append({"id": a["id"], "name": a.get("name"), "family": a.get("family"),
                       "role": a.get("role"), "score": round(score, 3),
                       "lex": lex[a["id"]], "sem": round(sem[a["id"]], 3), "channels": channels})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"matches": scored[:5], "query": q}


def _verify_envelope(graph: dict, autonomy_max: str) -> dict:
    """Авто-верификация authored-агента против ПРОИЗВОДНОГО конверта (не контракт LUDA): та же
    governance-проверка, что и `/api/agents/check`, но интейк выводится из графа (потолок = автономия
    агента, требуемые навыки = навыки графа). Ловит нарушения ADR-013/014 (автономия>потолок,
    внешнее действие без HITL). verified=true → конверт соблюдён; для прод-деплоя всё равно нужен контракт."""
    import datetime as _datetime
    skills = [n.get("skill") for n in (graph or {}).get("nodes") or [] if n.get("kind") == "skill" and n.get("skill")]
    intake = {"autonomy_ceiling": autonomy_max or "A2", "skills": skills}
    try:
        res = assembly.check_graph(graph, intake, ape.skill_safety)
    except Exception as ex:  # noqa: BLE001 — верификация опциональна, не валим сборку
        return {"verified": False, "mode": "auto-envelope", "error": str(ex)}
    errs = res.get("errors") or []
    return {"verified": not errs, "mode": "auto-envelope", "against": "производный конверт (не контракт LUDA)",
            "autonomy_max": res.get("autonomy_max"), "hitl_count": res.get("hitl_count"),
            "errors": errs, "warnings": res.get("warnings") or [],
            "checked_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat()}


async def _order_by_contract(nodes: list[dict]) -> list[dict]:
    """Поставщик объявленного входа — раньше приёмника.

    Человек кликает навыки в том порядке, в каком думает, и это не всегда порядок исполнения:
    «объяснить находки» можно выбрать раньше «проверить». Контракт знает, кто кого кормит, —
    пользуемся этим вместо того, чтобы молча собрать неисполнимую цепочку.
    """
    ids = [n.get("skill") for n in nodes]
    need: dict[str, set] = {}
    whole: set = set()          # кто объявил чтение всего прогона — такому место в последней волне
    for sid in ids:
        tpl = await schema_store.get(sid) or {}
        req = ((tpl.get("inputs") or {}).get("required") or [])
        need[sid] = {str(it.get("skill")) for it in req
                     if isinstance(it, dict) and it.get("from") == "skill" and it.get("skill") in ids}
        if any(isinstance(it, dict) and it.get("from") == "run" for it in req):
            whole.add(str(sid))
    # «Весь прогон» — это зависимость от ВСЕХ остальных, просто объявленная одной строкой. Без этого
    # редактор отчёта, у которого поимённых входов нет, попадал бы в первую волну и видел пустоту.
    for sid in whole:
        need[sid] |= {str(x) for x in ids if str(x) != str(sid)}
    out: list[dict] = []
    placed: set = set()
    rest = list(nodes)
    guard = 0
    while rest and guard <= len(nodes) + 1:
        guard += 1
        ready = [n for n in rest if need.get(n.get("skill"), set()) <= placed]
        if not ready:          # взаимная зависимость — оставляем порядок человека, он не хуже
            out += rest
            break
        out += ready
        placed |= {n.get("skill") for n in ready}
        rest = [n for n in rest if n not in ready]
    return out


def _envelope_reason(env: dict) -> str:
    """Почему у личного агента именно такой потолок автономии.

    Конверт личного агента не объявляется человеком, а выводится из навыков по строжайшему:
    действие наружу или выход во внешнюю сеть — потолок A1 (без подтверждения ничего не
    уйдёт), иначе A2. Выше A2 личный агент не поднимается: A3/A4 даёт только контракт.
    Человеку это надо СКАЗАТЬ, иначе правило выглядит произволом системы."""
    if env.get("mode") == "action":
        return "A1: среди навыков есть действие наружу — каждое уйдёт только после вашего подтверждения"
    if env.get("egress") == "external":
        return "A1: навык ходит во внешнюю сеть — запросы наружу под подтверждением"
    return "A2: все навыки только читают — агент работает сам, наружу ничего не отправляет"


def _graph_from_canvas(canvas: dict, skills: list[dict], autonomy_max: str, on: dict) -> dict:
    """Граф с канвы — как его нарисовал человек, но governance наш.

    Сохраняем ВСЕ узлы, а не только навыки: вывод, подтверждение оператора, триггер и источники —
    это и есть то, чем агент отличается от списка навыков. Три вещи навязываем независимо от
    рисунка: автономия узла не выше выведенного конверта, навык-действие наружу всегда под
    подтверждением (ADR-014), а ребро в несуществующий узел отбрасываем — иначе волны считаются по
    призракам.
    """
    byid = {str(s.get("id")): s for s in (skills or [])}
    keep: list[dict] = []
    for n in (canvas.get("nodes") or []):
        if not isinstance(n, dict) or not n.get("id"):
            continue
        m = dict(n)
        if m.get("kind") == "skill":
            sk = str(m.get("skill") or m.get("title") or "")
            if sk not in ape.SKILLS:
                continue
            m["skill"] = sk
            m["autonomy"] = autonomy_max
            m.update(on or {})
            if ((byid.get(sk) or {}).get("safety") or {}).get("mode") == "action":
                m["hitl"] = True
        keep.append(m)
    ids = {str(n["id"]) for n in keep}
    edges = [e for e in (canvas.get("edges") or [])
             if isinstance(e, dict) and str(e.get("from")) in ids and str(e.get("to")) in ids]
    return {"nodes": keep, "edges": edges}


@router.post("/api/agents/author")
async def agent_author(body: dict, u: dict = Depends(user)) -> JSONResponse:
    """Сохранить агента, собранного авторингом БЕЗ контракта (ADR-024 draft, ADR-032):
    роль семьи + навыки → AgentVersion. Конверт/автономия — из навыков (консервативно);
    прод-развёртывание всё равно потребует контракт LUDA (ADR-029).

    Тело: {family, member, skills?, name?, graph?}. `graph` — готовый граф с канвы: его узлы и рёбра
    сохраняются как есть. Без него граф строится из навыков по порядку контрактов. Граф нужен затем,
    что на канве к навыкам добавляют вывод, подтверждение оператора, триггер и источники — без них
    агент считает, но никуда не отдаёт и ничьего «да» не спрашивает."""
    require_level(u, "manager")  # сборка агента — не для analyst (read-only)
    family = str((body or {}).get("family", "")).strip()
    member = str((body or {}).get("member", "")).strip()
    if family and not can_see_family(u, family):  # ABAC: только свой отдел (кроме admin/support)
        raise HTTPException(403, f"нельзя собирать агента вне своего отдела ({u.get('department')})")
    skills = (body or {}).get("skills")
    # Граф с канвы: навыки для конверта берём из него, чтобы потолок автономии считался по тому, что
    # реально исполняется, а не по отдельно присланному списку.
    canvas = (body or {}).get("graph") if isinstance((body or {}).get("graph"), dict) else None
    if canvas:
        cn = [n for n in (canvas.get("nodes") or []) if isinstance(n, dict)]
        from_graph = [str(n.get("skill") or n.get("title") or "") for n in cn if n.get("kind") == "skill"]
        from_graph = [x for x in from_graph if x in ape.SKILLS]
        if not from_graph:
            raise HTTPException(422, "в графе нет ни одного известного навыка")
        skills = from_graph
        # Семью канве знать неоткуда: она рисует навыки, а к какому отделу они приписаны — знает
        # реестр. Выводим по первому навыку графа, как это делает сборка из плана.
        if not family:
            _f = [f for f, spec0 in ape.AGENT_FAMILIES.items()
                  if any(from_graph[0] in (m[1] or []) for m in (spec0.get("members") or {}).values())]
            family = _f[0] if _f else "management"
    if not family:
        raise HTTPException(422, "нужна family")
    try:
        spec = ape.build_agent_spec(family, member, skills)
    except ValueError as ex:
        raise HTTPException(404, str(ex))
    env = spec["envelope"]
    # output-режим из тумблера конструктора (structured|freeform) → пишем на узлы; runner прочитает его
    # поверх дефолта навыка и подберёт лимит токенов (freeform ⇒ больше, чтобы не резать рассуждения).
    # Пусто = наследовать режим от самого навыка.
    _out = str((body or {}).get("output", "")).strip()
    _on = {"output": _out} if _out in ("structured", "freeform") else {}
    nodes = [{"id": s["id"], "kind": "skill", "skill": s["id"], "autonomy": env["autonomy_max"],
              "hitl": s["safety"]["mode"] == "action", **_on} for s in spec["skills"]]
    # Порядок, обещанный конструктором («#1 → #2 → #3»), должен стать рёбрами: без них
    # топологическая раскладка кладёт все навыки в одну волну, и ни один не видит результата
    # соседа — объявленный вход от предыдущего навыка не выполним в принципе.
    if canvas:
        graph = _graph_from_canvas(canvas, spec["skills"], env["autonomy_max"], _on)
    else:
        nodes = await _order_by_contract(nodes)
        graph = {"nodes": nodes,
                 "edges": [{"from": nodes[k]["id"], "to": nodes[k + 1]["id"]} for k in range(len(nodes) - 1)]}
    name = str((body or {}).get("name", "")).strip() or f"{spec['family_title']} · {spec['role_title']}"
    # Стабильный per-agent id для «моего» агента: пересборка тем же пользователем того же агента →
    # НОВАЯ ВЕРСИЯ того же агента (next_version), а не новый экземпляр «authored.vN» (#8/#9).
    import hashlib as _hl2, re as _re2
    _uid = str(u.get("sub") or u.get("name") or "dev")
    _slug = _re2.sub(r"[^a-z0-9а-яё]+", "-", name.lower()).strip("-")[:40] or "agent"
    audit_id = f"authored-{_hl2.md5(_uid.encode('utf-8')).hexdigest()[:6]}-{_slug}"
    # Переименование не должно плодить агентов. Канва помнит, какого агента она правит, и присылает
    # его audit_id — тогда сохранение даёт НОВУЮ ВЕРСИЮ того же агента, а не однофамильца рядом.
    # Чужой id не примем: префикс authored-<хеш автора> проверяем на совпадение со своим.
    _keep = str((body or {}).get("audit_id") or "").strip()
    _mine = f"authored-{_hl2.md5(_uid.encode('utf-8')).hexdigest()[:6]}-"
    if _keep.startswith(_mine):
        audit_id = _keep
    verdict = _verify_envelope(graph, env["autonomy_max"])   # авто-верификация против производного конверта
    # Та же проверка покрытия входов, что и на канве: навык без данных, без предмета или без
    # поставщика объявленного входа отработает вхолостую. Для личного агента это
    # предупреждение, а не запрет: его собирают в том числе чтобы попробовать.
    gaps = await _input_gaps(graph)
    # ADR-024: сохранение с канвы ПЕРЕЗАПИСЫВАЕТ черновик. Автосейв срабатывает через секунду
    # после каждого движения мышью — на next_version это давало по версии на движение.
    saved = await agent_store.save_draft(name=name, audit_id=audit_id, graph=graph,
                                         autonomy_max=env["autonomy_max"], created_by=u.get("name") or "dev",
                                         family=family, role=spec["role"], transitions=spec["transitions"],
                                         source="authored", verification=verdict)
    version = saved.get("version")
    await audit_store.record(u.get("name") or "dev", "agent.author", saved["id"],
                             {"family": family, "role": spec["role"], "autonomy_max": env["autonomy_max"],
                              "verified": verdict.get("verified")})
    return JSONResponse({"saved": True, "id": saved["id"], "version": version, "status": "draft",
                         "family": family, "role": spec["role"], "autonomy_max": env["autonomy_max"],
                         "verification": verdict, "input_gaps": gaps,
                         "envelope": {**env, "autonomy_max": env["autonomy_max"],
                                      "почему": _envelope_reason(env)},
                         "skills": [s["id"] for s in spec["skills"]], "data_scope": spec["data_scope"]},
                        status_code=201)


@router.get("/api/agents/{agent_id}")
async def agent_get(agent_id: str, u: dict = Depends(user)) -> dict:
    """Полный AgentVersion (граф + метаданные) — для паспорта/повторного открытия в канве."""
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого AgentVersion")
    a = dict(a)
    a["edit_scope"] = _edit_scope(a)
    # can_edit: пользователь вправе править агента? user-агента правит его владелец/manager+;
    # methodologist-агента — только admin/support (методолог через ABOP).
    lvl = (u or {}).get("level")
    a["can_edit"] = True if a["edit_scope"] == "user" else (u.get("department") in ("*", None) or lvl == "admin")
    return a


@router.post("/api/agents/{agent_id}/triggers")
async def agent_add_trigger(agent_id: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сделать задачу агента РЕГУЛЯРНОЙ: добавить триггер-узел «расписание» в граф агента и сохранить
    НОВОЙ версией (версия меняется). Узел виден на канве «Строю», планировщик (scheduler_loop) фаерит
    его по cron. Тело: {cron, title?, enabled?, hitl?}. cron: 'HH:MM' (ежедневно) | '*/N' (каждые N мин).
    Внешняя доставка всё равно под HITL узлов агента. manager+ (создание триггера = мутация среды)."""
    require_level(u, "manager")
    import uuid as _uuid
    import re as _re
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого AgentVersion")
    cron = str((body or {}).get("cron") or "").strip()
    if not (_re.match(r"^\d{1,2}:\d{2}$", cron) or _re.match(r"^\*/\d+$", cron)
            or len(cron.split()) == 5):
        raise HTTPException(422, "cron: ожидается 'HH:MM' (ежедневно) или '*/N' (каждые N минут)")
    graph = dict(a.get("graph") or {})
    nodes = list(graph.get("nodes") or [])
    tid = "trg-" + _uuid.uuid4().hex[:8]
    title = str((body or {}).get("title") or f"Расписание {cron}").strip()
    deliver = str((body or {}).get("deliver") or "chat").strip()  # chat | system
    node = {"id": tid, "kind": "trigger", "title": title,
            "trig": {"type": "schedule", "cron": cron,
                     "enabled": bool((body or {}).get("enabled", True)),
                     "source": "chat.recurring", "deliver": deliver if deliver in ("chat", "system") else "chat",
                     "owner": (u.get("name") or u.get("sub") or "dev"),
                     "spawn": {"autonomy_max": a.get("autonomy_max") or "A1",
                               "hitl": bool((body or {}).get("hitl", False))}}}
    nodes.append(node)
    graph["nodes"] = nodes
    audit_id = a.get("contract_audit_id") or agent_id.split(".v")[0]
    version = await agent_store.next_version(audit_id)
    _verif = a.get("verification") if a.get("verification") is not None else \
        (_verify_envelope(graph, a.get("autonomy_max") or "A1") if a.get("source") == "authored" else None)
    saved = await agent_store.save(
        name=a.get("name") or "Агент", audit_id=audit_id, version=version, graph=graph,
        autonomy_max=a.get("autonomy_max") or "A1", created_by=(u.get("name") or u.get("sub") or "dev"),
        family=a.get("family") or "", role=a.get("role") or "",
        transitions=a.get("transitions") or [], source=a.get("source") or "contract", verification=_verif)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "agent.trigger.add", saved["id"],
                             {"cron": cron, "from_version": a.get("version"), "trigger_id": tid})
    return {"ok": True, "agent_id": saved["id"], "version": saved.get("version"),
            "trigger_id": tid, "cron": cron, "title": title}


@router.delete("/api/agents/{agent_id}/triggers/{trigger_id}")
async def agent_del_trigger(agent_id: str, trigger_id: str, u: dict = Depends(user)) -> dict:
    """Убрать расписание: сохранить НОВУЮ версию агента без этого триггер-узла (версия меняется)."""
    require_level(u, "manager")
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого AgentVersion")
    graph = dict(a.get("graph") or {})
    nodes = [n for n in (graph.get("nodes") or []) if not (n.get("kind") == "trigger" and n.get("id") == trigger_id)]
    graph["nodes"] = nodes
    audit_id = a.get("contract_audit_id") or agent_id.split(".v")[0]
    version = await agent_store.next_version(audit_id)
    saved = await agent_store.save(
        name=a.get("name") or "Агент", audit_id=audit_id, version=version, graph=graph,
        autonomy_max=a.get("autonomy_max") or "A1", created_by=(u.get("name") or u.get("sub") or "dev"),
        family=a.get("family") or "", role=a.get("role") or "",
        transitions=a.get("transitions") or [], source=a.get("source") or "contract",
        verification=a.get("verification"))
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "agent.trigger.del", saved["id"],
                             {"trigger_id": trigger_id})
    return {"ok": True, "agent_id": saved["id"], "version": saved.get("version")}


@router.get("/api/agents/{agent_id}/lexicon")
async def agent_lexicon_get(agent_id: str, u: dict = Depends(user)) -> dict:
    """Слова, по которым агента находят: засеянные из навыков и ручные правки оператора."""
    ag = await agent_store.get(agent_id)
    if not ag:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, ag.get("family")):
        raise HTTPException(403, "агент другого отдела")
    seeded, manual = await lexicon.parts(agent_id)
    if not seeded:
        seeded = await _refresh_lexicon(ag)
    merged = lexicon.merge(seeded, manual)
    # ручные слова всегда сверху и всегда видимы: оператор должен видеть свою правку, а не искать её в хвосте
    rows = [{"term": t, "weight": round(float(w), 2), "source": "оператор"}
            for t, w in sorted(manual.items(), key=lambda kv: -kv[1]) if float(w) > 0]
    banned = [{"term": t, "weight": 0.0, "source": "запрещено"} for t, w in manual.items() if float(w) <= 0]
    seen = {r["term"] for r in rows} | {r["term"] for r in banned}
    rows += banned
    rows += [{"term": t, "weight": round(float(w), 2), "source": "навыки"}
             for t, w in sorted(merged.items(), key=lambda kv: -kv[1]) if t not in seen][:60]
    return {"agent_id": agent_id, "agent_name": ag.get("name"), "terms": rows,
            "manual": manual, "seeded_count": len(seeded), "total": len(merged)}


@router.post("/api/agents/{agent_id}/lexicon")
async def agent_lexicon_set(agent_id: str, body: dict, u: dict = Depends(user)) -> dict:
    """Правка словаря из UI: {add:["слово",…], ban:["слово",…], remove:["слово",…]} — добавить, запретить, снять правку.
    Слова нормализуются тем же стеммером, что и запрос, иначе правка не сработает. manager+."""
    require_level(u, "manager")
    ag = await agent_store.get(agent_id)
    if not ag:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, ag.get("family")):
        raise HTTPException(403, "агент другого отдела")
    _, manual = await lexicon.parts(agent_id)
    manual = dict(manual)
    for w in ((body or {}).get("add") or []):
        for t in nlu.tokens(str(w)):
            manual[t] = lexicon.MANUAL_WEIGHT
    for w in ((body or {}).get("ban") or []):
        for t in nlu.tokens(str(w)):
            manual[t] = lexicon.MANUAL_BAN
    for w in ((body or {}).get("remove") or []):
        for t in nlu.tokens(str(w)):
            manual.pop(t, None)
    if len(manual) > 200:
        raise HTTPException(422, "слишком много ручных слов (максимум 200)")
    merged = await lexicon.set_manual(agent_id, manual)
    actor = u.get("name") or u.get("sub") or "dev"
    await audit_store.record(actor, "agent.lexicon", agent_id, {"manual": len(manual)})
    return {"agent_id": agent_id, "manual": manual, "total": len(merged)}


@router.post("/api/agents/{agent_id}/lexicon/refresh")
async def agent_lexicon_refresh(agent_id: str, u: dict = Depends(user)) -> dict:
    """Пересобрать словарь из описаний навыков (после правки навыка или шаблона). manager+."""
    require_level(u, "manager")
    ag = await agent_store.get(agent_id)
    if not ag:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, ag.get("family")):
        raise HTTPException(403, "агент другого отдела")
    await lexicon.save(agent_id, {}, "")        # сбрасываем отпечаток, чтобы пересчёт точно прошёл
    terms = await _refresh_lexicon(ag)
    return {"agent_id": agent_id, "seeded": len(terms)}


@router.get("/api/agents/{agent_id}/slots")
async def agent_slots(agent_id: str, u: dict = Depends(user)) -> dict:
    """Слоты всех навыков агента: что спросить у человека до запуска."""
    a = await agent_store.get(agent_id)
    if not a:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, a.get("family")):
        raise HTTPException(403, "нет доступа к семье агента")
    out, seen = [], set()
    ov = await skill_store.all()
    for n in (a.get("graph") or {}).get("nodes") or []:
        sid = n.get("skill")
        if not sid:
            continue
        tid = ((ov.get(sid) or {}).get("patch") or {}).get("schema_template_id") or sid
        tpl = await schema_store.get(tid) or await schema_store.get(sid) or {}
        for sl in (tpl.get("slots") or []):
            key = str(sl.get("name") or "")
            if key and key not in seen:
                seen.add(key)
                out.append({**sl, "skill": sid})
    return {"agent_id": agent_id, "slots": out}


@router.post("/api/agents/{agent_id:path}/step")
async def agent_run_step(agent_id: str, payload: dict, u: dict = Depends(user)) -> dict:
    """Прогнать ОДИН навык агента и показать вход-выход (пункт 3 разбора 05.10).

    Запуск был «всё или ничего»: на цепочке из шести навыков это четыре–шесть минут до первой
    обратной связи, и если первый же шаг собрал не те данные, человек узнавал об этом в конце. В
    Dify и n8n один узел выполняется отдельно — у нас такого не было.

    Сделано БЕЗ второго пути исполнения: собираем временного агента из одного узла и гоняем его
    тем же `execute_agent_run`, что и обычный прогон. Иначе «один шаг» со временем начал бы вести
    себя не так, как тот же шаг внутри цепочки, — и отладка по нему стала бы враньём.

    Прогон журналируется (он правда тратит токены), но помечается `started_by: step-test` и
    уходит БЕЗ `op_ids`: пробный шаг не вправе подтверждать участок карты.
    """
    навык = str(payload.get("skill") or "").strip()
    if not навык:
        raise HTTPException(422, "skill: пусто")
    агент = await agent_store.get(agent_id)
    if not агент:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, агент.get("family")):
        raise HTTPException(403, "агент чужого отдела")
    require_level(u, "manager")

    граф = агент.get("graph") or {}
    узлы = [n for n in (граф.get("nodes") or []) if n.get("kind") == "skill"]
    узел = next((n for n in узлы if (n.get("skill") or n.get("id")) == навык), None)
    if узел is None:
        raise HTTPException(404, f"в графе агента нет навыка «{навык}»: "
                                 + ", ".join(sorted({str(n.get("skill") or n.get("id")) for n in узлы})))

    # Узлы-источники оставляем: без них навык не получит данных и шаг проверит не то. Выходы
    # наружу убираем всегда — пробный шаг не должен ничего отправлять, даже в превью.
    оставить = [n for n in (граф.get("nodes") or [])
                if n is узел or n.get("kind") in ("source", "trigger", "input", "doc")]
    один = {**агент, "id": агент["id"] + "#step",
            "graph": {**граф, "nodes": оставить,
                      "edges": [e for e in (граф.get("edges") or [])
                                if e.get("to") == узел.get("id")]}}

    контракт = await contract_store.get(агент.get("contract_audit_id") or "") or {}
    итог = await execute_agent_run(
        один, контракт, "step-test", started_by_sub=_sub(u),
        use_cache=False,                       # пробный шаг обязан пойти в модель, иначе это не проверка
        user_context=str(payload.get("context") or ""),
        deliver_filter="none",                 # наружу ничего
        limits=payload.get("limits") or None)
    результат = итог.get("result") or {}
    шаги = [f for f in (результат.get("findings") or []) if f.get("skill") == навык]
    ш = шаги[0] if шаги else {}
    return {"agent": агент["id"], "skill": навык,
            "run_id": (итог.get("saved") or {}).get("id"),
            "ok": bool((результат.get("verdict") or {}).get("ok")),
            "sources": {"entities": ш.get("entities") or [],
                        "documents": [d.get("name") or d.get("path")
                                      for d in ((результат.get("run_metrics") or {}).get("documents") or [])]},
            "model": ш.get("model"), "input_tokens": ш.get("input_tokens"),
            "output_tokens": ш.get("output_tokens"),
            "text": str(ш.get("text") or "")[:8000],
            "note": "пробный шаг: в журнале помечен step-test, участок карты не подтверждает"}
