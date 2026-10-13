# -*- coding: utf-8 -*-
"""Прогоны: запуск, очередь, группы и веер, доска, находки, отчёт и стрим.

Домен вынесен из монолита последним (ADR-052, docs/RASKLADKA_SERVERA.md) вместе с агентами: оба
держались за `execute_agent_run`, и до выноса слоя исполнения (`server/run_exec.py`) переехать не
могли.

Что здесь: постановка задания в очередь и его состояние, группы и веер по предметам, доска
прогона, находки и их разметка, журнал и поиск, сравнение двух прогонов, отчёт по шаблону и
SSE-стрим. Само исполнение живёт в `run_exec` — домен только принимает запрос, проверяет права и
отдаёт результат.

Правило домена: прогон виден тому, кто его запускал, и его отделу (`_run_visible`). Журнал —
не общий поток: чужой прогон не показывается даже по прямой ссылке.

Зависимости наружу: `server/deps`, сторы, `run_exec`, контекст отчёта и соседний домен агентов
(слоты участка). `web_api` отсюда не импортируется.
"""
from __future__ import annotations

import asyncio as _aio
import asyncio as _asyncio
import hashlib as _hashlib
import os
import time as _t
import time as _time

from cli import ape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import agent_store, arbiter, assembly, audit_store, blackboard, channel_tokens, contract_store, finding_store
from .. import findings, hitl_store, identity_store, levels
from .. import observability as obs
from .. import pipeline_graph, report_store, run_bus, run_queue, run_store, runner, schema_store
from .. import skill_contract
from ..deps import _sub, _uid_of, can_see_family, require_level, user, уровень_не_ниже
from ..report_context import _labels_ctx, _report_context
from ..run_exec import _template_for_run, execute_agent_run
from .agents import agent_slots

router = APIRouter()


def _norm(x: str) -> str:
    return " ".join(str(x or "").lower().replace("ё", "е").split())


def _score_candidate(rec: dict, q: str, fields: list) -> float:
    """Совпадение записи с запросом: точное вхождение кода, вхождение слова, нечёткое сходство."""
    import difflib
    qn = _norm(q)
    if not qn:
        return 0.0
    words = [w for w in qn.split() if len(w) > 2]
    best = 0.0
    for f in fields:
        v = _norm(rec.get(f))
        if not v:
            continue
        if qn == v:
            return 1.0
        if v in qn or qn in v:
            best = max(best, 0.9)
        hit = sum(1 for w in words if w in v)
        if hit:
            best = max(best, 0.5 + 0.12 * min(3, hit))
        best = max(best, difflib.SequenceMatcher(None, qn, v).ratio() * 0.8)
    return round(min(1.0, best), 3)


_user_hits: dict = {}          # user -> [timestamps] (скользящее окно, per-replica)


_run_locks: dict = {}          # ключ дедупа -> asyncio.Lock (сериализация одинаковых сабмитов)


_FANOUT_MAX = max(1, int(os.getenv("ABOP_FANOUT_MAX", "20")))   # защита от веера на двести заданий


# ═══════════════ ПИЛОТ 1С (Блок 4): карточки находок, слепая разметка, метрики, нормы ═══════════════
_AUDIT_ROLES = ("auditor-1c", "investigator-1c")


@router.get("/api/resolve/{entity}")
async def resolve_entity(entity: str, q: str = "", limit: int = 8, u: dict = Depends(user)) -> dict:
    """Кандидаты предмета по данным: точное совпадение, вхождение, нечёткое сходство.

    Возвращает {mode, candidates}. mode: `exact` — один уверенный кандидат, подставляется молча;
    `choose` — показать выбор; `narrow` — кандидатов слишком много, нужен уточняющий признак;
    `empty` — не нашли. Так интерфейс не решает сам, когда спрашивать, а следует данным."""
    ent = _re.sub(r"[^a-zA-Z0-9_\-]", "", str(entity or ""))
    if not ent:
        raise HTTPException(422, "нужна сущность")
    try:
        rows = await _asyncio.to_thread(ape.data_query, ent, None, None, 500)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"данные недоступны: {type(ex).__name__}: {ex}")
    if not rows:
        return {"entity": ent, "mode": "empty", "candidates": [], "total": 0}

    fields = [f for f in ("id", "name", "название", "customer", "manager", "code", "title")
              if any(f in r for r in rows[:5])]
    scored = []
    for r in rows:
        sc = _score_candidate(r, q, fields) if q else 0.0
        scored.append((sc, r))
    scored.sort(key=lambda x: -x[0])
    top = [{"score": sc, "record": r} for sc, r in scored[: max(1, min(50, int(limit or 8)))]]

    best = top[0]["score"] if top else 0.0
    second = top[1]["score"] if len(top) > 1 else 0.0
    if not q:
        mode = "choose" if len(rows) <= 8 else "narrow"
    elif best >= 0.85 and best - second >= 0.2:
        mode = "exact"
    elif best >= 0.35:
        mode = "choose"
    elif len(rows) > 8:
        mode = "narrow"
    else:
        mode = "choose"
    # matched=false: запрос вообще не про эти данные. Показать список всё равно полезно, но подписать
    # честно «по запросу не нашли», а не делать вид, что 0.38 это совпадение.
    return {"entity": ent, "mode": mode, "query": q, "total": len(rows), "matched": bool(q) and best >= 0.45,
            "candidates": [t for t in top if (t["score"] > 0 or not q)][: int(limit or 8)]}


def _diff_key(x: dict) -> str:
    """Стабильный ключ находки/расследования для сравнения прогонов: id движка, иначе проверка+документ."""
    if not isinstance(x, dict):
        return ""
    rid = str(x.get("id") or "").strip()
    if rid:
        return rid
    doc = x.get("документ") if isinstance(x.get("документ"), dict) else {}
    return (str(x.get("проверка") or x.get("симптом") or "") + "|" + str(doc.get("Номер") or doc.get("номер") or ""))[:160]


def _diff_view(x: dict) -> dict:
    """Что показываем в диффе по одной находке (и по чему считаем «изменилась»)."""
    doc = x.get("документ") if isinstance(x.get("документ"), dict) else {}
    return {"id": _diff_key(x), "класс": x.get("класс") or "", "серьёзность": x.get("серьёзность") or "",
            "проверка": str(x.get("проверка") or x.get("симптом") or "")[:160],
            "сумма": str(x.get("сумма") or x.get("Сумма") or (x.get("сверка") or {}).get("разница_₽") or ""),
            "документ": str(doc.get("Номер") or doc.get("номер") or "")[:60]}


def _board_scope(kind: str, u: dict, family: str = "") -> str:
    k = str(kind or "user").strip().lower()
    if k in ("family", "fam", "отдел"):
        fam = str(family or u.get("department") or "").strip()
        if not fam or fam == "*":
            raise HTTPException(422, "для доски отдела нужна семья")
        if not can_see_family(u, fam):
            raise HTTPException(403, f"доска отдела «{fam}» вне вашей области ({u.get('department')})")
        return blackboard.family_scope(fam)
    return blackboard.user_scope(_sub(u))


@router.post("/api/runs/{run_id}/to-board")
async def board_from_run(run_id: str, body: dict, u: dict = Depends(user)) -> dict:
    """Вынести итоги прогона на доску: по одному факту на навык, со ссылкой на прогон.

    Берём то, что навык объявил итогом (ту же сводку, что показывает карточка), а не весь результат:
    доска — общая память, а не копия прогонов. Значение остаётся дословным.
    """
    run = await _run_visible(run_id, u)
    scope = _board_scope(str((body or {}).get("kind") or "user"), u, str((body or {}).get("family") or ""))
    ttl = int((body or {}).get("ttl_sec") or blackboard.DEFAULT_TTL_SEC)
    put: list[dict] = []
    for so in (run.get("skill_outputs") or []):
        sid = str(so.get("skill") or "")
        st = so.get("structured")
        if not sid or not isinstance(st, dict) or not st:
            continue
        row = await blackboard.put_fact(
            scope, sid, st, author=f"{run.get('agent_name') or run.get('agent_id') or 'агент'} · {sid}",
            note=str((body or {}).get("note") or ""), ttl_sec=ttl,
            run_id=str(run.get("run_id") or run.get("id") or run_id))
        if row:
            put.append({"key": row["key"], "author": row["author"]})
    if not put:
        raise HTTPException(422, "в прогоне нет структурированных итогов — выносить нечего")
    await audit_store.record(u.get("name") or _sub(u), "board.from_run", scope,
                             {"run_id": run_id, "keys": [p["key"] for p in put]})
    return {"ok": True, "scope": scope, "facts": put,
            "note": f"на доску вынесено фактов: {len(put)} · срок {ttl // 86400} дн."}


_USER_RATE_MAX = max(1, int(os.getenv("ABOP_USER_RATE_MAX", "30")))     # прогонов на юзера за окно


_USER_RATE_WINDOW = max(10, int(os.getenv("ABOP_USER_RATE_WINDOW", "300")))  # окно, сек


def _rate_check(user: str) -> bool:
    """Скользящее окно: не более _USER_RATE_MAX прогонов на пользователя за _USER_RATE_WINDOW сек."""
    now = _time.time()
    hits = [t for t in _user_hits.get(user, []) if now - t < _USER_RATE_WINDOW]
    if len(hits) >= _USER_RATE_MAX:
        _user_hits[user] = hits
        return False
    hits.append(now)
    _user_hits[user] = hits
    return True


def _run_lock(key: str) -> "_aio.Lock":
    """Per-key lock: одинаковые сабмиты (юзер+агент[+idempotency_key]) сериализуются — второй дождётся
    первого и получит его результат из кэша (дедуп двойного клика без двойного тяжёлого прогона)."""
    lk = _run_locks.get(key)
    if lk is None:
        lk = _aio.Lock()
        _run_locks[key] = lk
    return lk


@router.post("/api/runs")
async def run_start(body: dict, u: dict = Depends(user),
                    request_async: str = Query(default="", alias="async")) -> JSONResponse:
    """Запуск прогона агента. Тело: {agent_id} ИЛИ {contract_audit_id}. Синхронно → 201 с результатом;
    {async:true} или ?async=1 → 202 с job_id (очередь, см. GET /api/runs/jobs/{id})."""
    agent_id = str((body or {}).get("agent_id", "")).strip()
    audit_id = str((body or {}).get("contract_audit_id", "")).strip()
    if not agent_id and audit_id:
        lst = await agent_store.list_for(audit_id)
        if not lst:
            raise HTTPException(404, "нет сохранённого агента для контракта")
        agent_id = lst[0]["id"]
    agent = await agent_store.get(agent_id) if agent_id else None
    if not agent:
        raise HTTPException(404, "нет такого AgentVersion")
    # Лестница ответственности на запуске (PRD, раздел 2). Проверяем КЛАСС действий агента, а не
    # имена каналов: прежде здесь стояли два перечня — «каналы доставки, разрешённые боту» и
    # «режимы навыков» — и оба отвечали на один вопрос «какого уровня то, что агент делает».
    # Проверка стоит НА ЗАПУСКЕ, а не фильтром списка: спрятать агента в списке — значит
    # защититься от интерфейса, а не от запроса.
    действия = levels.действия_агента(agent, ape.skill_safety)
    if u.get("channel"):
        # Политика канала УЖЕ лестницы и живёт в `channel_tokens.вне_канала`: уровень действия и
        # «что можно начинать из переписки» — разные вопросы. Класс `по_адресату` проходит
        # намеренно: адресата на запуске часто ещё нет, и проверит его доставка
        # (`run_exec._deliver_out_nodes` + подтверждение заявки).
        вне = channel_tokens.вне_канала(действия)
        if вне:
            raise HTTPException(403, "в канале «" + str(u.get("channel")) + "» нельзя запускать "
                                "этого агента: " + "; ".join(sorted(вне))
                                + ". Запустите его из приложения, где видно последствия: из "
                                "переписки доступны чтение, расчёт и письмо коллеге")
    contract = await contract_store.get(agent.get("contract_audit_id") or audit_id)
    if not contract:
        contract = {"intake": {"autonomy_ceiling": agent.get("autonomy_max") or "A2"}, "bundle": {}}
    actor = u.get("name") or u.get("sub") or "dev"
    # per-user rate-limit (защита от «стучания» одним пользователем)
    if not _rate_check(actor):
        obs.inc("abop_rate_limited_total")
        raise HTTPException(429, f"слишком много прогонов: лимит {_USER_RATE_MAX} за {_USER_RATE_WINDOW}с — подождите")
    # У3 подтверждается В НАЧАЛЕ сценария (PRD, 2.3). Подтверждать последний шаг многосоставной
    # цепочки бессмысленно: к этому моменту всё остальное уже произошло, и «нет» ничего не отменит.
    # Поэтому агент с объявленным необратимым действием не запускается напрямую — рождается заявка
    # на ЗАПУСК, и человек решает один раз, видя план целиком.
    #
    # Решает `manager+` (лестница: У3 — менеджер зоны ответственности). Запускающий менеджер
    # проходит сам: он и есть точка решения, и запуском он её уже применил. Адресный владелец
    # операции карты процессов — следующий пункт порядка работ (PRD, 10.4); пока заявка видна
    # отделу, как все остальные.
    необратимые = [д for д in действия if д["класс"] == levels.НЕОБРАТИМОЕ]
    if необратимые and (u.get("channel") or not уровень_не_ниже(u, "manager")):
        перечень = ", ".join(sorted({str(д["что"]) for д in необратимые}))
        item = await hitl_store.create(
            run_id="", agent_id=agent["id"], family=agent.get("family") or "",
            node=str((необратимые[0] or {}).get("узел") or ""),
            title=f"Запуск сценария с необратимым действием: {agent.get('name') or agent['id']}",
            channel="spawn", to_addr="",
            payload={"kind": "spawn", "agent_name": agent.get("name"),
                     "уровень": 3, "класс": levels.НЕОБРАТИМОЕ,
                     "основание": levels.основание(3),
                     # Запрос едет с заявкой целиком: после подтверждения прогон должен пойти с тем
                     # же предметом и контекстом, а не с пустым. Прежний путь spawn их терял.
                     "запуск": {"contract_audit_id": agent.get("contract_audit_id") or audit_id,
                                # Прогон остаётся ПРОГОНОМ ЗАКАЗЧИКА, а не подтверждающего: иначе
                                # человек перестаёт видеть в журнале то, что сам же и просил.
                                "actor": actor, "actor_sub": _sub(u),
                                "user_context": str((body or {}).get("context") or "")[:20000],
                                "deliver_filter": "" if isinstance((body or {}).get("deliver"), bool)
                                                  else str((body or {}).get("deliver") or "").strip()},
                     "html": ("<p>Агент «" + str(agent.get("name") or agent["id"]) + "» содержит "
                              "необратимые действия: " + перечень + ".</p><p>" + levels.основание(3)
                              + ". После подтверждения прогон встанет в очередь и выполнится "
                              "целиком.</p>")},
            requested_by=actor)
        await audit_store.record(actor, "run.spawn_hitl", agent["id"],
                                 {"уровень": 3, "действия": перечень, "hitl_id": item["id"]},
                                 severity="warn")
        return JSONResponse({"state": "awaiting_hitl", "hitl_id": item["id"], "уровень": 3,
                             "основание": levels.основание(3), "действия": перечень,
                             "note": "сценарий с необратимым действием подтверждают до запуска"},
                            status_code=202)
    user_context = str((body or {}).get("context") or "").strip()[:20000]  # задача/файл/ссылка из чата
    # Сквозной ID: агент адресен под пользователя — подмешиваем его профиль (кто он, отдел, аккаунты
    # в системах: какой ящик/Redmine-исполнитель/CRM-владелец). Наследует RBAC/ABAC (department из JWT).
    try:
        _idb = await identity_store.bundle(_uid_of(u), department=u.get("department"), roles=u.get("roles"))
        _sys = _idb.get("systems") or {}
        if _sys or _idb.get("department"):
            _who = [f"Пользователь: {_idb.get('uid')}", f"отдел (ABAC): {_idb.get('department')}",
                    f"роли: {', '.join(_idb.get('roles') or []) or '—'}"]
            for s, info in _sys.items():
                _who.append(f"  · {s}: {info.get('external_id') or ''} {info.get('display') or ''}".rstrip())
            user_context = ("=== АДРЕСНОСТЬ (от имени кого работаем) ===\n" + "\n".join(_who)
                            + "\n\n" + user_context).strip()
    except Exception:  # noqa: BLE001 — identity опционален, не валим прогон
        pass
    # дерево решений: '', chat, redmine, email… Булево true означает «доставлять во все каналы»:
    # раньше str(True) превращался в фильтр «True», который не совпадал ни с чем, и доставка молча
    # отключалась — прогон выглядел успешным, но наружу не уходило ничего.
    # Предмет работы из слота навыка: {поле: значение}. Сужает выборку данных, чтобы агент не
    # смешивал проекты. Приходит из карточки уточнения в интерфейсе.
    # Бюджет прогона: {max_tokens, max_rub, max_sec}. Перерасход останавливает работу пометкой, а не
    # общим таймаутом задания, который читается как «прогон не выполнен».
    _bd = (body or {}).get("budget") if isinstance((body or {}).get("budget"), dict) else {}
    _budget = {k: v for k, v in _bd.items() if k in ("max_tokens", "max_rub", "max_sec") and v}
    # Разовые лимиты этого запуска: перекрывают настройку среды, но только на этот прогон.
    _lm = (body or {}).get("limits") if isinstance((body or {}).get("limits"), dict) else {}
    _run_limits = {k: v for k, v in _lm.items() if k in runner.LIMIT_FIELDS and v not in (None, "")}
    _ds = (body or {}).get("scope") if isinstance((body or {}).get("scope"), dict) else {}
    _data_scope = {str(k): str(v) for k, v in _ds.items() if str(v or "").strip()} or None
    _dv = (body or {}).get("deliver")
    deliver_filter = "" if isinstance(_dv, bool) else str(_dv or "").strip()
    use_cache = not bool((body or {}).get("no_cache"))  # {no_cache:true} → форс свежий прогон
    if user_context or deliver_filter or _data_scope:
        use_cache = False   # контекст, выбор доставки и предмет работы меняют прогон → кэш обходим
    # идемпотентность: одинаковые сабмиты (юзер+агент[+idempotency_key]) сериализуются per-key lock →
    # второй дождётся первого и заберёт результат из кэша (двойной клик не запускает двойной прогон)
    idem = str((body or {}).get("idempotency_key", "")).strip()
    lock_key = f"{actor}:{agent_id}:{idem}"
    # ── async-режим (гейт масштабирования): 202 + job_id, исполняет пул воркеров, клиент поллит
    #    GET /api/runs/jobs/{id}. Не больше ABOP_USER_CONCURRENT прогонов на пользователя одновременно,
    #    одинаковые задания дедуплицируются, память защищена бэкпрешером, есть таймаут и отмена. ──
    if bool((body or {}).get("async")) or str(request_async or "").lower() in ("1", "true", "yes"):
        job = await run_queue.enqueue(agent_id=agent["id"], actor=actor, dedupe_key=lock_key if idem else None,
                                      payload={"contract_audit_id": agent.get("contract_audit_id") or audit_id,
                                               "use_cache": use_cache, "user_context": user_context,
                                               "deliver_filter": deliver_filter, "data_scope": _data_scope,
                                               "budget": _budget or None,
                                               "limits": _run_limits or None,
                                               # Владелец личной доски едет с заданием: имя может
                                               # смениться, а доска переживает переименования.
                                               "actor_sub": _sub(u),
                                               # Из какого канала пришёл запуск. Нужен исполнителю:
                                               # отправка наружу из канала идёт только через
                                               # подтверждение человеком, и решает это доставка,
                                               # а не маршрут.
                                               "channel": str(u.get("channel") or ""),
                                               "trace_id": obs.current_trace_id()},
                                      priority=int((body or {}).get("priority") or 5))
        await run_bus.bus().publish_request(job, obs.current_trace_id())
        obs.inc("abop_run_jobs_total", status="queued")
        pos = await run_queue.position(job["id"])
        return JSONResponse({**run_queue.public(job, pos), "poll": f"/api/runs/jobs/{job['id']}",
                             "deduped": bool(job.get("deduped"))}, status_code=202)
    async with _run_lock(lock_key):
        out = await execute_agent_run(agent, contract, actor, started_by_sub=_sub(u),
                                      use_cache=use_cache, data_scope=_data_scope,
                                      budget=_budget or None, limits=_run_limits or None,
                                      user_context=user_context, deliver_filter=deliver_filter,
                                      из_канала=str(u.get("channel") or ""))
    return JSONResponse({"run_id": out["saved"]["id"], **out["result"]}, status_code=201)


@router.get("/api/runs/jobs/{job_id}")
async def run_job_status(job_id: str, u: dict = Depends(user)) -> dict:
    """Статус задания очереди: queued (с позицией) / running / done (+ полный результат) / failed / cancelled.
    Видит автор задания, admin/support — любое."""
    job = await run_queue.get(job_id)
    if not job:
        raise HTTPException(404, "нет такого задания")
    actor = u.get("name") or u.get("sub") or "dev"
    if job["actor"] != actor and u.get("level") not in ("admin", "support"):
        raise HTTPException(403, "чужое задание")
    pos = await run_queue.position(job_id) if job["status"] == "queued" else 0
    out = run_queue.public(job, pos)
    if job["status"] == "done" and job.get("run_id"):
        run = await run_store.get(job["run_id"])
        if run:
            run["run_id"] = job["run_id"]
            out["run"] = run
    if (job.get("kind") or "run") == "pipeline":
        cp = job.get("checkpoint") or {}
        out["pipeline"] = (job.get("payload") or {}).get("pid")
        out["name"] = (job.get("payload") or {}).get("name")
        out["steps"] = cp.get("steps") or []
    return out


@router.post("/api/runs/jobs/{job_id}/cancel")
async def run_job_cancel(job_id: str, u: dict = Depends(user)) -> dict:
    """Отменить задание: из очереди — сразу; выполняющееся — кооперативно (навыки, что ещё не начались,
    не стартуют). Автор или admin."""
    actor = u.get("name") or u.get("sub") or "dev"
    res = await run_queue.cancel(job_id, actor=actor, admin=u.get("level") in ("admin", "support"))
    if not res:
        raise HTTPException(404, "нет такого задания")
    if res.get("denied"):
        raise HTTPException(403, "чужое задание")
    obs.inc("abop_run_jobs_total", status="cancel_requested")
    return run_queue.public(res)


@router.get("/api/runs/jobs")
async def run_jobs_mine(limit: int = 30, u: dict = Depends(user)) -> dict:
    """Мои задания (admin/support — все) — для «моих прогонов» и уведомлений."""
    actor = u.get("name") or u.get("sub") or "dev"
    all_ = u.get("level") in ("admin", "support")
    jobs = await run_queue.list_jobs(actor=None if all_ else actor, limit=max(1, min(200, limit)))
    return {"jobs": [run_queue.public(j) for j in jobs]}


@router.get("/api/runs/groups/{group_id}/board")
async def run_group_board(group_id: str, u: dict = Depends(user)) -> dict:
    """Что ветви группы выложили в общую память: ключ, автор, значение, расхождения."""
    require_level(u, "manager")
    board = await blackboard.board_of(group_id)
    contr = board.contradictions()
    return {"group_id": group_id, "entries": board.export(), "summary": board.summary(),
            "contradictions": contr, "arbitration": arbiter.report(arbiter.resolve_all(contr))}


@router.get("/api/runs/{run_id}/board")
async def run_board(run_id: str, u: dict = Depends(user)) -> dict:
    """Доска одного прогона: выводы навыков с авторством и решения арбитра по расхождениям."""
    run = await run_store.get(run_id)
    if not run:
        raise HTTPException(404, "нет такого прогона")
    payload = run.get("payload") or run
    entries = await blackboard.load("run-" + str(run_id)) or payload.get("board_entries") or []
    board = blackboard.Board(run_id, entries)
    return {"run_id": run_id, "entries": entries, "summary": board.summary(),
            "contradictions": board.contradictions(),
            "arbitration": (payload.get("run_metrics") or {}).get("arbitration"),
            "data_snapshot": (payload.get("run_metrics") or {}).get("data_snapshot")}


@router.post("/api/runs/fan-out")
async def run_fan_out(body: dict, u: dict = Depends(user)) -> dict:
    """Запустить агента по МНОГИМ предметам сразу: одна группа заданий, по заданию на предмет.

    Тело: {agent_id, slot?, values?|query?, budget?, deliver?, limit?}. Предметы берутся из values,
    либо подбираются по query через разрешение предмета, либо берутся все доступные записи сущности
    слота. Каждое задание получает свой scope, поэтому агент видит только свой предмет и не смешивает
    их — это та ошибка, которую мы уже лечили в одиночном прогоне.
    """
    require_level(u, "manager")
    agent_id = str((body or {}).get("agent_id") or "").strip()
    agent = await agent_store.get(agent_id)
    if not agent:
        raise HTTPException(404, "нет такого агента")
    if not can_see_family(u, agent.get("family")):
        raise HTTPException(403, "нет доступа к семье агента")

    # какой слот разворачиваем: указанный или первый обязательный слот агента
    slots = (await agent_slots(agent_id, u)).get("slots") or []
    slot_name = str((body or {}).get("slot") or "").strip()
    slot = next((s for s in slots if s.get("name") == slot_name), None) if slot_name else \
        next((s for s in slots if s.get("required")), None)
    if not slot:
        raise HTTPException(422, "у агента нет предмета работы, по которому можно развернуть веер")
    entity = str(slot.get("entity") or "")

    vals = (body or {}).get("values")
    items: list[dict] = []
    if isinstance(vals, list) and vals:
        items = [{"value": str(v), "record": {"id": str(v)}} for v in vals]
    else:
        res = await resolve_entity(entity, str((body or {}).get("query") or ""), 200, u)
        for c in res.get("candidates") or []:
            rec = c.get("record") or {}
            key = rec.get("id") or rec.get("название") or rec.get("name")
            if key:
                items.append({"value": str(key), "record": rec})
    if not items:
        raise HTTPException(404, f"не нашлось предметов сущности «{entity}» для веера")
    lim = max(1, min(_FANOUT_MAX, int((body or {}).get("limit") or _FANOUT_MAX)))
    cut = len(items) - lim if len(items) > lim else 0
    items = items[:lim]

    _bd = (body or {}).get("budget") if isinstance((body or {}).get("budget"), dict) else {}
    budget = {k: v for k, v in _bd.items() if k in ("max_tokens", "max_rub", "max_sec") and v} or None
    deliver = str((body or {}).get("deliver") or "chat")
    ctx = str((body or {}).get("context") or "")[:8000]
    actor = u.get("name") or u.get("sub") or "dev"
    gid = "grp-" + _hashlib.sha1(f"{agent_id}:{slot['name']}:{_t.time()}".encode()).hexdigest()[:10]

    # Снимок данных на группу: все ветви считают по одной картине. Берём объём и отпечаток выборки
    # сущности слота ДО запуска — иначе долгий веер начинает на одних данных, а заканчивает на других,
    # и разные цифры ветвей объяснить нечем.
    _snap = None
    try:
        _rows = await _asyncio.to_thread(ape.data_query, entity, limit=5000)
        _snap = blackboard.snapshot({entity: _rows or []}, scope=gid)
    except Exception as _sex:  # noqa: BLE001 — снимок усиление, а не условие запуска
        obs.log_event("warning", "fanout.snapshot_failed", entity=entity, error=str(_sex)[:200])
    _parent_trace = obs.current_trace_id()

    jobs = []
    for it in items:
        label = it["record"].get("name") or it["record"].get("название") or it["value"]
        job = await run_queue.enqueue(
            agent_id=agent_id, actor=actor, kind="run", group_id=gid, priority=int((body or {}).get("priority") or 6),
            payload={"contract_audit_id": agent.get("contract_audit_id") or "",
                     "use_cache": False, "deliver_filter": deliver, "budget": budget,
                     # доска группы: ветвь видит выводы соседей; снимок: одна картина данных на всех
                     "board_scope": gid, "data_snapshot": _snap, "parent_trace_id": _parent_trace,
                     "data_scope": {slot["name"]: it["value"]},
                     "user_context": (ctx + f"\n\n=== ПРЕДМЕТ РАБОТЫ ({slot['name']}) ===\n" +
                                      "\n".join(f"{k}: {v}" for k, v in it["record"].items()
                                                 if not isinstance(v, (dict, list)))).strip(),
                     "trace_id": obs.current_trace_id(), "fan_item": it["value"], "fan_label": label,
                     # предмет и сущность нужны сводке: по ним она берёт сквозной ключ из контракта,
                     # а не угадывает его по содержимому результата
                     "fan_slot": slot["name"], "fan_entity": entity})
        await run_bus.bus().publish_request(job, obs.current_trace_id())
        jobs.append({"job_id": job["id"], "item": it["value"], "label": label})
    obs.log_event("info", "fanout.started", group=gid, agent=agent_id, slot=slot["name"], jobs=len(jobs),
                  parent_trace=_parent_trace, snapshot=(_snap or {}).get("id") or "")
    obs.gauge("abop_fanout_jobs", len(jobs), group=gid)
    await audit_store.record(actor, "run.fan_out", gid, {"agent": agent_id, "slot": slot["name"], "jobs": len(jobs)})
    return {"group_id": gid, "agent_id": agent_id, "slot": slot["name"], "entity": entity,
            "jobs": jobs, "count": len(jobs), "skipped_over_limit": cut,
            "board_scope": gid, "data_snapshot": _snap, "parent_trace_id": _parent_trace,
            "note": f"запущено заданий: {len(jobs)}" + (f"; за пределом лимита осталось {cut}" if cut else "")}


async def _group_join_keys(st: dict, results: dict) -> list[str]:
    """Чем сводить результаты ветвей — берём из ОБЪЯВЛЕННОГО, а не угадываем по содержимому.

    Два источника, оба уже описаны в контрактах:
      * предмет веера (имя слота, по которому разворачивали группу) — он отличает ветви друг от
        друга: номера пунктов у разных проектов совпадают, и без него чужие записи схлопнутся;
      * `produces.key` тех навыков, что в этих прогонах отдали списки, — он опознаёт запись внутри
        ветви.
    Если не объявлено ничего — честный запасной вариант `id`, как и раньше.
    """
    keys: list[str] = []
    # предмет веера
    for it in (st.get("items") or [])[:1]:
        job = await run_queue.get(it.get("id") or "")
        slot = ((job or {}).get("payload") or {}).get("fan_slot")
        if slot:
            keys.append(str(slot))
    # ключи записей от навыков, которые действительно отработали
    tpls = {t["id"]: t for t in (await schema_store.all() or [])}
    seen: set[str] = set()
    for r in results.values():
        for sid in (r.get("skills") or []):
            if sid in seen:
                continue
            seen.add(sid)
            for p in skill_contract.produces_list((tpls.get(sid) or {}).get("produces")):
                k = str(p.get("key") or "").strip()
                if k and k not in keys:
                    keys.append(k)
    return keys or ["id"]


@router.get("/api/runs/groups/{group_id}/summary")
async def run_group_summary(group_id: str, key: str = "", u: dict = Depends(user)) -> dict:
    """Сводка по группе: результаты завершённых заданий, сведённые в одну таблицу.

    Ветвь без результата не ломает сводку: она попадает в список незавершённых. Ключ сведения можно
    задать (через запятую для составного), иначе берётся предмет веера плюс идентификатор записи."""
    st = await run_queue.group_status(group_id)
    if not st.get("jobs"):
        raise HTTPException(404, "нет заданий с такой группой")
    results: dict = {}
    pending: list[str] = []
    for it in st.get("items") or []:
        rid = it.get("run_id")
        if not rid:
            pending.append(it.get("id"))
            continue
        run = await run_store.get(rid)
        if not run:
            pending.append(it.get("id"))
            continue
        flat: dict = {}
        for o in (run.get("skill_outputs") or []):
            for k, v in (o.get("structured") or {}).items():
                flat.setdefault(k, v)
        label = ((await run_queue.get(it["id"])) or {}).get("payload", {}).get("fan_label") or it.get("agent_id")
        results[it["id"]] = {"data": flat, "agent_name": label, "run_id": rid,
                             "skills": [o.get("skill") for o in (run.get("skill_outputs") or []) if o.get("skill")]}
    keys = [k.strip() for k in str(key or "").split(",") if k.strip()]
    if not keys:
        keys = await _group_join_keys(st, results)
    merged = pipeline_graph.merge({"policy": "by_key", "from": list(results), "key": keys}, results)
    # Таблица на 24 строки — ещё не ответ руководителю. Сводим шапки ветвей: по каждому предмету
    # короткая выжимка (без списков) и суммы по числовым полям, чтобы было видно общую картину.
    per_item, numeric = [], {}
    for jid, r in results.items():
        head = {k: v for k, v in (r.get("data") or {}).items() if not isinstance(v, list)}
        flat = {}
        for k, v in head.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if not isinstance(v2, (dict, list)):
                        flat[f"{k}.{k2}"] = v2
            else:
                flat[k] = v
        for k, v in flat.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric[k] = round(numeric.get(k, 0) + v, 2)
        per_item.append({"job_id": jid, "предмет": r.get("agent_name"), "run_id": r.get("run_id"),
                         "показатели": flat})
    # Арбитраж: в слиянии при расхождении выживало значение последней ветви, и человек получал цифру
    # без следа спора. Теперь спор разрешается правилом, а где правило не различает варианты или цена
    # ошибки высока (срок, деньги, предлагаемое мероприятие) — расхождение остаётся открытым и явно
    # адресовано человеку, а не подчищено.
    rows = (merged.get("data") or {}).get("сведено") or []
    board = await blackboard.board_of(group_id)
    contr = board.contradictions()
    decisions = arbiter.resolve_all(contr)
    for d in decisions:
        board.resolve(d["key"], d.get("chosen"), by=d.get("by") or "human",
                      reason=d.get("reason") or "", variants=d.get("variants"))
    arbiter.apply_to_rows(rows, decisions)
    arb = arbiter.report(decisions)
    # Картина данных группы: сначала снимок, заданный при запуске веера, иначе снимок первой ветви,
    # которая его записала. Пустое поле означало бы «ветви сверяли неизвестно что».
    snap = None
    for _it in (st.get("items") or []):
        _p = ((await run_queue.get(_it["id"])) or {}).get("payload") or {}
        if _p.get("data_snapshot"):
            snap = _p["data_snapshot"]
            break
    if not snap:
        for _r in results.values():
            _run = await run_store.get(_r.get("run_id") or "")
            _sn = (((_run or {}).get("payload") or _run or {}).get("run_metrics") or {}).get("data_snapshot")
            if _sn:
                snap = dict(_sn, note="снимок первой завершившейся ветви: группа запускалась без общего")
                break
    return {"group_id": group_id, "jobs": st.get("jobs"), "by_status": st.get("by_status"),
            "ready": len(results), "pending": pending, "key": keys,
            "по_предметам": per_item, "суммы": numeric,
            "merged": merged.get("data") or {}, "conflicts": merged.get("conflicts") or [],
            "arbitration": arb, "data_snapshot": snap, "board": board.summary(),
            "note": (merged.get("note") or "") + " · " + arb["note"]}


@router.get("/api/runs/groups/{group_id}")
async def run_group_status(group_id: str, u: dict = Depends(user)) -> dict:
    """Состояние группы заданий: сколько в каком статусе, что стоило, какие ветви упали.

    Веер по предмету — это десятки заданий; без группы человек видел бы их как несвязанный список."""
    st = await run_queue.group_status(group_id)
    if not st.get("jobs"):
        raise HTTPException(404, "нет заданий с такой группой")
    return st


@router.post("/api/runs/groups/{group_id}/cancel")
async def run_group_cancel(group_id: str, u: dict = Depends(user)) -> dict:
    """Отменить всю группу заданий (manager+): человек останавливает работу, а не каждую ветвь."""
    require_level(u, "manager")
    n = await run_queue.cancel_group(group_id, actor=u.get("name") or u.get("sub") or "dev")
    await audit_store.record(u.get("name") or "dev", "run.group_cancel", group_id, {"cancelled": n})
    return {"group_id": group_id, "cancelled": n}


@router.post("/api/runs/jobs/{job_id}/retry-step")
async def run_retry_step(job_id: str, body: dict = None, u: dict = Depends(user)) -> dict:
    """Повторить ОДИН шаг цепочки, не перезапуская остальные (manager+).

    Тело: {step: "id шага"}. Раньше повтор был только целым заданием с самого начала, поэтому одна
    упавшая ветвь заставляла прогонять заново всё, что уже посчиталось и стоило денег."""
    require_level(u, "manager")
    step = str((body or {}).get("step") or "").strip()
    if not step:
        raise HTTPException(422, "нужен идентификатор шага")
    job = await run_queue.get(job_id)
    if not job:
        raise HTTPException(404, "нет такого задания")
    cp = dict(job.get("checkpoint") or {})
    steps = list(cp.get("steps") or [])
    if not any(d.get("id") == step for d in steps):
        raise HTTPException(404, f"в задании нет шага «{step}»")
    cp["steps"] = [d for d in steps if d.get("id") != step]
    (cp.get("results") or {}).pop(step, None)
    await run_queue.set_checkpoint(job_id, cp)
    # задание возвращается в очередь: чекпоинт уже без этого шага, поэтому повторится только он
    await run_queue.fail(job_id, "повтор шага «%s» по запросу оператора" % step, requeue=True)
    await audit_store.record(u.get("name") or "dev", "run.retry_step", job_id, {"step": step})
    return {"job_id": job_id, "step": step, "requeued": True}


@router.get("/api/runs/queue")
async def run_queue_stats(u: dict = Depends(user)) -> dict:
    """Состояние очереди: глубина по статусам, воркеры, лимиты, память процесса, шина. admin/support."""
    require_level(u, "support")
    st = await run_queue.stats()
    st["bus"] = run_bus.describe()
    return st


async def _run_visible(run_id: str, u: dict) -> dict:
    run = await run_store.get(run_id)
    if not run:
        raise HTTPException(404, "прогон не найден")
    ag = await agent_store.get(run.get("agent_id") or "")
    if ag and not can_see_family(u, ag.get("family")):
        raise HTTPException(403, "прогон другого отдела")
    run = dict(run)
    run["run_id"] = run.get("run_id") or run.get("id") or run_id
    run["agent_name"] = (ag or {}).get("name")
    return run


@router.get("/api/runs/{run_id}/findings")
async def run_findings(run_id: str, u: dict = Depends(user)) -> dict:
    """Карточки находок прогона: участки, тип расхождения, сумма, цепочка с разрывом, объяснение, норма, разметка."""
    run = await _run_visible(run_id, u)
    labels = (await finding_store.labels_for_runs([run["run_id"]])).get(run["run_id"], {})
    cards = findings.cards_for_run(run, labels)
    return {"run_id": run["run_id"], "agent_id": run.get("agent_id"), "agent_name": run.get("agent_name"),
            "items": cards, "metrics": findings.pilot_metrics(cards)}


@router.delete("/api/runs/{run_id}/findings/{finding_id}/label")
async def run_finding_label_delete(run_id: str, finding_id: str, u: dict = Depends(user)) -> dict:
    """Снять экспертную разметку с находки (ошибочная метка не должна навсегда искажать метрики пилота). manager+."""
    require_level(u, "manager")
    run = await _run_visible(run_id, u)
    ok = await finding_store.delete_label(run["run_id"], finding_id)
    actor = u.get("name") or u.get("sub") or "dev"
    await audit_store.record(actor, "finding.label.delete", f"{run['run_id']}/{finding_id}", {"existed": ok})
    return {"ok": True, "removed": ok}


@router.post("/api/runs/{run_id}/findings/{finding_id}/label")
async def run_finding_label(run_id: str, finding_id: str, body: dict, u: dict = Depends(user)) -> dict:
    """Слепая разметка эксперта в интерфейсе: decision confirmed|rejected|unsure, manual_miss (вручную бы не нашли), comment.
    Метрики пилота (точность/межучастковые/«не нашли бы вручную») считаются по этим меткам автоматически."""
    run = await _run_visible(run_id, u)
    decision = str((body or {}).get("decision") or "").strip()
    if decision not in finding_store.DECISIONS:
        raise HTTPException(422, "decision: confirmed | rejected | unsure")
    expert = u.get("name") or u.get("sub") or "dev"
    row = await finding_store.set_label(run["run_id"], finding_id, expert, decision,
                                        bool((body or {}).get("manual_miss")), str((body or {}).get("comment") or "")[:2000])
    await audit_store.record(expert, "finding.label", run["run_id"] + "/" + finding_id,
                             {"decision": decision, "manual_miss": bool((body or {}).get("manual_miss"))})
    labels = (await finding_store.labels_for_runs([run["run_id"]])).get(run["run_id"], {})
    cards = findings.cards_for_run(run, labels)
    return {"ok": True, "label": row, "metrics": findings.pilot_metrics(cards)}


@router.delete("/api/runs/{run_id}/findings/{finding_id}/label")
async def run_finding_unlabel(run_id: str, finding_id: str, u: dict = Depends(user)) -> dict:
    """Снять разметку эксперта (ошибочный клик)."""
    run = await _run_visible(run_id, u)
    ok = await finding_store.delete_label(run["run_id"], finding_id)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "finding.unlabel", run["run_id"] + "/" + finding_id, {})
    return {"ok": ok}


@router.get("/api/findings")
async def findings_journal(limit: int = 60, runs: int = 12, agent_id: str = "", u: dict = Depends(user)) -> dict:
    """Журнал находок пилота 1С по последним прогонам аудитора/следователя (ABAC по семье) + метрики пилота."""
    briefs = {a["id"]: a for a in await agent_store.list_for(None, limit=500)}
    briefs.update({a["id"]: a for a in await agent_store.list_for(None, limit=500, archived=True)})
    items = await run_store.list_runs(agent_id=agent_id or None, limit=300)
    picked = []
    for it in items:
        ag = briefs.get(it.get("agent_id"), {})
        if not can_see_family(u, ag.get("family")):
            continue
        if not agent_id and (ag.get("role") or "") not in _AUDIT_ROLES:
            continue
        picked.append((it, ag))
        if len(picked) >= max(1, min(50, runs)):
            break
    run_ids = [it["id"] for it, _ in picked]
    labels = await finding_store.labels_for_runs(run_ids)
    cards: list[dict] = []
    seen: dict[str, dict] = {}   # одна и та же находка в повторных прогонах — показываем свежую, разметку берём откуда есть
    for it, ag in picked:        # picked — от свежих к старым
        full = await run_store.get(it["id"])
        if not full:
            continue
        full = dict(full); full["run_id"] = it["id"]; full["agent_name"] = ag.get("name")
        for c in findings.cards_for_run(full, labels.get(it["id"], {})):
            c["agent_name"] = ag.get("name")
            key = (ag.get("contract_audit_id") or ag.get("id") or "") + "|" + c["id"]
            prev = seen.get(key)
            if prev is None:
                seen[key] = c
                cards.append(c)
            elif not prev.get("label") and c.get("label"):
                prev["label"] = dict(c["label"], from_run=c["run_id"])
    metrics = findings.pilot_metrics(cards)
    return {"items": cards[:max(1, min(500, limit))], "total": len(cards), "runs": run_ids, "metrics": metrics}


@router.get("/api/runs")
async def runs_list(agent_id: str = "", limit: int = 100, u: dict = Depends(user)) -> dict:
    """Журнал прогонов (по умолчанию последние 100, ?limit=1..500). ABAC: не-admin видит только
    прогоны агентов своего отдела. Обогащается именем/семьёй агента для отображения во Флоте/Обзоре."""
    items = await run_store.list_runs(agent_id=agent_id or None, limit=max(1, min(500, int(limit or 100))))
    # обогащение агентом: берём ЛЁГКИЕ сводки одним запросом (id/name/family/version/role без graph),
    # вместо полного agent_store.get() на каждого агента (тот тянул тяжёлый graph-JSONB ради 4 полей).
    briefs = {a["id"]: a for a in await agent_store.list_for(None)}
    briefs.update({a["id"]: a for a in await agent_store.list_for(None, archived=True)})  # + Лимб
    out = []
    for it in items:
        ag = briefs.get(it.get("agent_id"), {})
        fam = ag.get("family")
        if not can_see_family(u, fam):
            continue
        it["agent_name"] = ag.get("name") or it.get("agent_id")
        it["family"] = fam
        it["version"] = ag.get("version")
        it["role"] = ag.get("role")
        out.append(it)
    return {"runs": out}


@router.get("/api/runs/search")
async def runs_search(q: str = "", agent_id: str = "", verdict: str = "", days: int = 0,
                      limit: int = 50, u: dict = Depends(user)) -> dict:
    """Поиск по СОДЕРЖИМОМУ прогонов: находки, ответы навыков, доставка (jsonb → текст в Postgres).
    Возвращает записи журнала плюс совпавшие фрагменты — видно, за что нашлось. ABAC как в журнале."""
    words = [w for w in str(q or "").strip().split() if len(w) > 1][:6]
    where, args = ["TRUE"], []
    if agent_id:
        where.append("agent_id = %s")
        args.append(agent_id)
    if verdict in ("ok", "issues"):
        where.append("verdict_ok = %s")
        args.append(verdict == "ok")
    if days:
        where.append("created_at > now() - make_interval(days => %s)")
        args.append(int(days))
    for w in words:
        where.append("payload::text ILIKE %s")
        args.append("%" + w + "%")
    args.append(max(1, min(200, int(limit or 50))))
    sql = ("SELECT id, agent_id, verdict_ok, created_at, payload::text FROM runs WHERE "
           + " AND ".join(where) + " ORDER BY created_at DESC LIMIT %s")
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(sql, tuple(args))
        rows = await cur.fetchall()

    briefs = {a["id"]: a for a in await agent_store.list_for(None)}
    briefs.update({a["id"]: a for a in await agent_store.list_for(None, archived=True)})
    out = []
    for rid, aid, ok, created, blob in rows:
        ag = briefs.get(aid, {})
        if not can_see_family(u, ag.get("family")):
            continue
        hits = []
        if words:
            low = blob.lower()
            for w in words:
                i = low.find(w.lower())
                if i < 0:
                    continue
                frag = blob[max(0, i - 90):i + 130].replace("\\n", " ")
                hits.append(_re.sub(r"\s+", " ", frag).strip())
                if len(hits) >= 3:
                    break
        out.append({"id": rid, "agent_id": aid, "agent_name": ag.get("name") or aid,
                    "family": ag.get("family"), "verdict_ok": ok,
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
                    "hits": hits})
    return {"runs": out, "count": len(out), "query": q}


def шаги_с_источниками(run: dict) -> list[dict]:
    """Что именно дошло до каждого навыка (пункт 6 разбора 05.10).

    В карточке прогона было видно, ЧТО навык ответил, но не видно, из чего он это взял: сущности
    Data Plane, документы по ссылке, вложения. Вопрос «а он вообще мой файл прочитал?» оставался
    без ответа. Данные были в записи прогона всё это время — не хватало их собрать в одно место.
    """
    документы = [d.get("name") or d.get("path")
                 for d in ((run.get("run_metrics") or {}).get("documents") or []) if d.get("ok")]
    снимок = ((run.get("run_metrics") or {}).get("data_snapshot") or {})
    out = []
    for f in (run.get("findings") or []):
        if not isinstance(f, dict) or not f.get("skill"):
            continue
        сущности = [str(e) for e in (f.get("entities") or [])]
        out.append({"skill": f["skill"], "model": f.get("model") or "",
                    "entities": сущности,
                    "records": {e: (снимок.get(e) or {}).get("records") for e in сущности
                                if isinstance(снимок.get(e), dict)},
                    "documents": документы if f.get("model") else [],
                    "input_tokens": f.get("input_tokens") or 0,
                    "output_tokens": f.get("output_tokens") or 0,
                    "skipped": bool(f.get("skipped"))})
    return out


@router.get("/api/runs/{run_id}")
async def run_get(run_id: str, u: dict = Depends(user)) -> dict:
    """Полная запись прогона (волны, доска, вердикт, run_metrics)."""
    r = await run_store.get(run_id)
    if not r:
        raise HTTPException(404, "нет такого прогона")
    # «Источники шага» собираем здесь, а не в браузере: данные были в записи всё это время, но
    # разбросаны по findings и run_metrics, и каждый клиент собирал бы их по-своему.
    return {**r, "step_sources": шаги_с_источниками(r)}


def _diff_side(run: dict) -> dict:
    items = {}
    for f in (run.get("findings") or []):
        if isinstance(f, dict) and (f.get("проверка") or f.get("наблюдение")):
            items[_diff_key(f)] = _diff_view(f)
    invs = {}
    for iv in (run.get("investigations") or []):
        if isinstance(iv, dict) and iv.get("id"):
            invs[_diff_key(iv)] = _diff_view(iv)
    cost = ((run.get("run_metrics") or {}).get("cost") or {})
    tim = ((run.get("run_metrics") or {}).get("timings") or {})
    dl = run.get("delivery") or []
    return {"findings": items, "investigations": invs,
            "skills": sorted({s.get("skill") for s in (run.get("skill_outputs") or []) if s.get("skill")}),
            "metrics": {"rub": cost.get("rub"), "models": sorted((cost.get("by_model") or {}).keys()),
                        "total_ms": tim.get("total_ms"), "llm_ms_total": tim.get("llm_ms_total"),
                        "commands": len([d for d in dl if d.get("mode") == "awaiting_hitl"]),
                        "skipped": len([d for d in dl if d.get("mode") == "skipped"])}}


def _diff_pair(a: dict, b: dict, key: str) -> dict:
    """a — база (старый прогон), b — текущий. Возвращает появилось/ушло/изменилось."""
    A, B = a[key], b[key]
    added = [B[k] for k in B if k not in A]
    gone = [A[k] for k in A if k not in B]
    changed = []
    for k in B:
        if k in A and A[k] != B[k]:
            changed.append({"id": k, "было": A[k], "стало": B[k],
                            "поля": sorted(f for f in B[k] if A[k].get(f) != B[k].get(f) and f != "id")})
    return {"добавились": added, "ушли": gone, "изменились": changed,
            "всего_было": len(A), "всего_стало": len(B)}


@router.get("/api/runs/{run_id}/diff")
async def run_diff(run_id: str, vs: str = "", u: dict = Depends(user)) -> dict:
    """Сравнение двух прогонов: что в находках появилось, что ушло, что изменилось (класс/сумма/серьёзность),
    плюс навыки и метрики. `vs` пусто → предыдущий прогон того же агента (регресс после правок шаблонов)."""
    cur = await _run_visible(run_id, u)
    base_id = vs.strip()
    if not base_id:
        prev = [r for r in await run_store.list_runs(agent_id=cur.get("agent_id"), limit=50)
                if (r.get("id") or r.get("run_id")) != (cur.get("run_id") or run_id)]
        if not prev:
            raise HTTPException(404, "нет более раннего прогона этого агента — сравнивать не с чем")
        base_id = prev[0].get("id") or prev[0].get("run_id")
    base = await _run_visible(base_id, u)
    A, B = _diff_side(base), _diff_side(cur)
    same_skills = sorted(set(A["skills"]) & set(B["skills"]))
    return {"run_id": cur.get("run_id") or run_id, "base_run_id": base.get("run_id") or base_id,
            "agent_id": cur.get("agent_id"), "base_agent_id": base.get("agent_id"),
            "created_at": {"base": base.get("created_at"), "current": cur.get("created_at")},
            "находки": _diff_pair(A, B, "findings"),
            "расследования": _diff_pair(A, B, "investigations"),
            "навыки": {"общие": same_skills, "только_сейчас": sorted(set(B["skills"]) - set(A["skills"])),
                       "только_раньше": sorted(set(A["skills"]) - set(B["skills"]))},
            "метрики": {"было": A["metrics"], "стало": B["metrics"]}}


@router.get("/api/runs/{run_id}/report")
async def run_report(run_id: str, template: str = "", format: str = "html", u: dict = Depends(user)):
    """Отчёт прогона по шаблону (reports/<id>.html → БД report_templates): HTML или PDF по требованию.
    template пусто → авто-выбор по форме результата (audit1c/invest/digest/default). PDF — рендерер ABOP (Gotenberg)."""
    run = await _run_visible(run_id, u)
    ag = await agent_store.get(run.get("agent_id") or "") or {"id": run.get("agent_id"), "name": run.get("agent_name") or run.get("agent_id")}
    import re as _re
    tid = _re.sub(r"[^a-z0-9_-]", "", str(template or "").lower())
    tpl = await _template_for_run(run, forced=tid)
    if not tpl:
        raise HTTPException(404, "нет шаблона отчёта")
    tid = tpl.get("id") or tid or "composed"
    _ctx = await _asyncio.to_thread(_report_context, ag, run)
    html_doc = report_store.render(tpl, {**_ctx, **(await _labels_ctx(run))})
    if str(format).lower() != "pdf":
        from fastapi.responses import HTMLResponse
        return HTMLResponse(html_doc)
    from fastapi.responses import FileResponse
    out = await _asyncio.to_thread(ape._t_pdf_render, {"html": html_doc, "name": f"abop_{run['run_id']}_{tid}"})
    m = _re.search(r"PDF готов:\s*(\S+)", str(out or ""))
    if not m:
        raise HTTPException(502, f"PDF не собран: {str(out)[:200]}")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "run.report_pdf", run["run_id"], {"template": tid})
    _fn = _re.sub(r"[^\w-]+", "_", str(ag.get("name") or "abop")) + "_" + str(run["run_id"]) + ".pdf"
    return FileResponse(m.group(1), media_type="application/pdf", filename=_fn)


@router.get("/api/runs/{run_id}/metrics")
async def run_metrics(run_id: str, u: dict = Depends(user)) -> dict:
    """RunMetrics прогона (abop.run_metrics/1.0) — обратная петля к LUDA (SDD §4-bis)."""
    r = await run_store.get(run_id)
    if not r:
        raise HTTPException(404, "нет такого прогона")
    return r.get("run_metrics") or {}


async def _job_by_ref(ref: str) -> dict | None:
    """Задание по его id ИЛИ по id прогона: клиент знает то одно, то другое."""
    j = await run_queue.get(ref)
    if j:
        return j
    for x in await run_queue.list_jobs(actor=None, limit=200):
        if x.get("run_id") == ref:
            return x
    return None


@router.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str, u: dict = Depends(user)):
    """Живой ход прогона (SSE). Единственная заглушка API, закрытая 08.10 (п.14 исследования).

    Событий ровно столько, сколько рантайм действительно наблюдает, — выдумывать недостающие
    тут нельзя, это тот же класс ошибки, что демо-данные в интерфейсе:

    * `hello` — задание найдено: статус, позиция в очереди, число навыков;
    * `queue` — позиция меняется, пока задание ждёт воркера;
    * `wave_start` — сменилась фаза прогона (данные → навыки → сборка отчёта → доставка);
    * `agent_step` — навык сменил состояние (running / done / error) с таймингом: это и есть
      ответ на «что сейчас делает агент», которого не хватало по разбору 05.10;
    * `board_event` — на доске прогона появился новый вывод с авторством;
    * `hitl_request` — задание встало на подтверждение человека;
    * `run_done` — финал с вердиктом и стоимостью; дальше поток закрывается;
    * `ping` — каждые 20 с, чтобы прокси не рвал соединение.

    `handoff` и `audit_verdict` отдельными событиями не шлём: передача между навыками видна
    парой `agent_step`, а вердикт аудитора приходит внутри `run_done`. Обещать событие, которого
    рантайм не порождает, — значит снова описать контракт, которому ничего не соответствует.
    """
    import asyncio as _asyncio
    import json as _json

    from fastapi.responses import StreamingResponse

    задание = await _job_by_ref(run_id)
    if задание is None:
        raise HTTPException(404, "прогон или задание не найдено: " + run_id)
    if задание.get("agent_id"):
        агент = await agent_store.get(задание["agent_id"])
        if агент and not can_see_family(u, агент.get("family")):
            raise HTTPException(403, "прогон чужого отдела")
    jid = задание["id"]
    scope = "run:" + str(задание.get("run_id") or jid)

    def _пакет(тип: str, **поля) -> str:
        return "data: " + _json.dumps({"type": тип, "job_id": jid, **поля}, ensure_ascii=False) + "\n\n"

    async def gen():
        прошлый_статус = ""
        прошлая_фаза = ""
        прошлые_навыки: dict = {}
        прошлая_позиция = -1
        видели_доску: set = set()
        молчим = 0.0
        j = задание
        поз = await run_queue.position(jid)
        прог = ((j.get("checkpoint") or {}).get("run") or {})
        yield _пакет("hello", run_id=j.get("run_id"), status=j.get("status"), position=поз,
                     total=прог.get("total") or 0, agent_id=j.get("agent_id"))
        while True:
            await _asyncio.sleep(1.0)
            молчим += 1.0
            j = await run_queue.get(jid)
            if j is None:
                yield _пакет("run_done", status="unknown",
                             note="задание исчезло из очереди (очистка журнала)")
                return
            статус = str(j.get("status") or "")
            прог = ((j.get("checkpoint") or {}).get("run") or {})

            if статус == "queued":
                поз = await run_queue.position(jid)
                if поз != прошлая_позиция:
                    прошлая_позиция = поз
                    молчим = 0.0
                    yield _пакет("queue", position=поз)

            фаза = str(прог.get("phase") or "")
            if фаза and фаза != прошлая_фаза:
                прошлая_фаза = фаза
                молчим = 0.0
                yield _пакет("wave_start", phase=фаза, total=прог.get("total") or 0)

            for sid, сост in (прог.get("skills") or {}).items():
                if прошлые_навыки.get(sid) != сост:
                    прошлые_навыки[sid] = dict(сост) if isinstance(сост, dict) else сост
                    молчим = 0.0
                    yield _пакет("agent_step", skill=sid, **(сост if isinstance(сост, dict) else {"state": сост}))

            try:
                for e in await blackboard.load(scope):
                    ключ = str(e.get("key")) + ":" + str(e.get("author"))
                    if ключ not in видели_доску:
                        видели_доску.add(ключ)
                        молчим = 0.0
                        yield _пакет("board_event", key=e.get("key"), author=e.get("author"),
                                     note=e.get("note"))
            except Exception:  # noqa: BLE001 — доска не обязана существовать у каждого прогона
                pass

            if статус != прошлый_статус:
                прошлый_статус = статус
                молчим = 0.0
                if статус == "awaiting_hitl":
                    заявки = [h for h in await hitl_store.list_pending()
                              if str(h.get("run_id") or "") in (j.get("run_id"), jid)]
                    yield _пакет("hitl_request", pending=len(заявки),
                                 title=(заявки[0].get("title") if заявки else None))
                elif статус in ("done", "failed", "cancelled"):
                    итог = None
                    if j.get("run_id"):
                        итог = await run_store.get(j["run_id"])
                    в = (итог or {}).get("verdict") or {}
                    yield _пакет("run_done", status=статус, run_id=j.get("run_id"),
                                 verdict_ok=bool(в.get("ok")),
                                 autonomy_used=в.get("autonomy_used"),
                                 error=j.get("error"),
                                 cost=((итог or {}).get("run_metrics") or {}).get("cost") or {})
                    return

            if молчим >= 20.0:
                молчим = 0.0
                yield _пакет("ping", status=статус)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
