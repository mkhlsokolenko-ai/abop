# -*- coding: utf-8 -*-
"""План и подбор: одно решение по задаче и то, из чего оно собрано.

Здесь живут разбор фразы, исполнимый план по контрактам, оценка стоимости до запуска, обучение
подбора на исходе предложения и единая точка решения — оркестратор (ADR-041): «запустить агента»,
«собрать цепочку», «доспросить» или «ответить текстом», с трассой «почему».

Правило домена: короткий запрос не угадывается. Если данных на план не хватает, среда называет
причину и задаёт вопросы — пустой план это ответ, а не ошибка (ADR-037).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cli import ape

from .. import access, admin_store, agent_store, assembly, audit_store, choice_store, clients
from .. import cost_estimate, nlu, orchestrator, planner, report_compose, report_store, run_store
from .. import schema_store, semantic_pick

from .data import data_entities
from ..deps import _sub, require_level, user

from .. import observability as obs
from ..config import settings

# Подбор моделью. Включён по РЕЗУЛЬТАТУ замера, а не заранее: `ops/planner_model.py` на 126
# запросах показал 18 из 63 первым у правил и 41 из 63 с ведущим голосом модели на формулировке,
# которую журнал не видел (в плане — 21 и 48). Цена: один запрос к своей модели на подбор, около
# секунды и ~2.6 тыс. входных токенов на своём боксе. Выключатель оставлен: эндпоинт может быть
# платным или недоступным, и тогда подбор работает словарём, а в ответе написано, что модель не
# участвовала.
import os as _os
_МОДЕЛЬЮ = _os.getenv("ABOP_PLAN_MODEL", "1") != "0"
from ..nlu import nlu_config
from ..vectors import cosine as _cosine
from .agents import agent_author
from .agents import agents_match
from .pipelines import pipeline_save

router = APIRouter()


_SKILL_EMB_CACHE: dict[str, tuple[str, list]] = {}


@router.post("/api/orchestrate")
async def orchestrate(body: dict, u: dict = Depends(user)) -> dict:
    """Одно решение по задаче: кого звать, что собрать, какие границы — и запись, почему именно так.

    До этого каждый канал (чат десктопа, веб, крон, шина) сам сшивал последовательность: достаточно
    ли описания → есть ли готовый агент → собрать ли из навыков → какой документ. Они расходились:
    карточка в чате держала план, собранный до правки подбора, и человек запускал устаревший. Теперь
    решение принимается здесь, целиком, и ложится в журнал.

    Тело: {task, slots?: {имя: значение}, max_steps?}. Ответ — решение и его трасса; выполнять его
    (запустить агента, собрать из навыков) вызывающая сторона идёт отдельными ручками: оркестратор
    решает, а не действует. Это не формальность — у действия своя проверка прав и свой журнал.
    """
    task = str((body or {}).get("task") or (body or {}).get("goal") or "").strip()
    if not task:
        raise HTTPException(422, "нужна задача")
    max_steps = max(1, min(6, int((body or {}).get("max_steps") or 4)))
    slots_given = {str(k) for k, v in ((body or {}).get("slots") or {}).items() if str(v or "").strip()}

    # Факты собираем здесь, решение принимает чистая функция: тогда его можно проверить тестом.
    matches = []
    try:
        m = await agents_match({"q": task}, u)
        for it in (m.get("matches") or [])[:5]:
            # Навыки агента нужны решению: «звать готового или собирать» решается сравнением того,
            # что агент УМЕЕТ, с тем, что нужно задаче — а не двух счётов из разных шкал.
            ag = await agent_store.get(str(it.get("id") or "")) or {}
            sk = [str(n.get("skill") or "") for n in ((ag.get("graph") or {}).get("nodes") or [])
                  if n.get("kind") == "skill" and n.get("skill")]
            matches.append({"id": it.get("id"), "name": it.get("name"), "score": it.get("score"),
                            "skills": sk})
    except Exception:  # noqa: BLE001 — без подбора агентов решение всё равно принимается
        matches = []
    # План для РЕШЕНИЯ считаем без подсказок от похожих агентов. Иначе получается круг: агент
    # «ADR» похож на задачу → его навык adr-writer получает прибавку → он возглавляет план →
    # агент «покрывает план» → запускаем ADR на разбор идеи сервиса. Подсказка подтверждала сама
    # себя, и никакая правка подбора этого не перебивала. Проверено на проде 05.10.
    plan = await plan_auto({"task": task, "slots": (body or {}).get("slots") or {},
                            "max_steps": max_steps, "hints": False}, u)
    _sids = [str(s.get("skill") or "") for s in (plan.get("steps") or []) if s.get("skill")]
    cached = False
    if len(_sids) >= orchestrator.EDITOR_FROM:
        _cid = report_compose.cache_id(_sids)
        cached = bool(_cid and (await report_store.get(_cid) or {}).get("layout"))

    d = orchestrator.decide(task, catalog={}, entities=set(plan.get("entities_ready") or []),
                            slots=slots_given, matches=matches, plan=plan, cached_layout=cached,
                            max_steps=max_steps)
    out = d.as_dict()
    out["итог"] = orchestrator.explain(d)
    # Журнал решений: по нему видно, почему выбран этот исполнитель. Именно это дважды позволило
    # поймать неверный подбор — незаметный оркестратор выдал бы гладкий документ не по задаче.
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "orchestrator.decide",
                             d.agent_id or ",".join(d.skills) or d.kind,
                             {"kind": d.kind, "task": task[:200], "skills": d.skills,
                              "editor": d.editor, "form": d.form, "facts": d.facts,
                              "why": d.why[:8]})
    obs.log_event("info", "orchestrator.decide", kind=d.kind, skills=len(d.skills),
                  editor=d.editor or "-", score=d.facts.get("подбор_агента"))
    return out


async def _skill_semantics(q: str, catalog: dict) -> dict:
    """Смысловая близость запроса к каждому навыку (эмбеддинги BGE-M3, best-effort).

    Семантика применялась только к агентам, а навыки искались словами — и запрос, написанный другими
    словами, не находил навык вовсе. Кэш по содержимому описания: методики меняются редко, пересчёт
    на каждый запрос стоил бы дороже самого подбора. Сбой эмбеддингов не ломает подбор: словарная
    часть работает всегда.
    """
    import hashlib as _h
    out: dict = {}
    if not q or not catalog:
        return out
    try:
        need, ids = [], []
        for sid, meta in catalog.items():
            doc = " ".join(str(meta.get(k) or "") for k in ("title", "short"))[:400] \
                + " " + str(meta.get("body") or "")[:1200]
            dh = _h.md5(doc.encode("utf-8")).hexdigest()
            c = _SKILL_EMB_CACHE.get(sid)
            if not c or c[0] != dh:
                need.append(doc)
                ids.append((sid, dh))
        if need:
            embs = await clients.embed(need)
            for (sid, dh), e in zip(ids, embs):
                _SKILL_EMB_CACHE[sid] = (dh, e)
        qe = (await clients.embed([q]))[0]
        for sid in catalog:
            c = _SKILL_EMB_CACHE.get(sid)
            if c:
                out[sid] = round(_cosine(qe, c[1]), 4)
    except Exception as _ex:  # noqa: BLE001 — смысл усиление, а не условие подбора
        obs.log_event("warning", "plan.semantics_failed", error=str(_ex)[:160])
        return {}
    return out


@router.post("/api/plan/auto")
async def plan_auto(body: dict, u: dict = Depends(user)) -> dict:
    """Собрать исполнимую цепочку под задачу ПО КОНТРАКТАМ навыков (этап 8).

    Отличие от подбора по фразе: тот отвечает на вопрос «кто похож», а этот — «что из похожего
    вообще сможет выполниться здесь и сейчас». Планировщик смотрит на фактическое состояние среды:
    какие сущности наполнены, какой предмет работы известен, что отдают предыдущие шаги, — и
    достраивает недостающие звенья теми навыками, которые их производят.

    Тело: {task, slots?: {имя: значение}, max_steps?}. Пустой план — это ответ: он говорит, чего не
    хватает, вместо того чтобы собрать красивую цепочку и упасть на середине.
    """
    task = str((body or {}).get("task") or (body or {}).get("goal") or "").strip()
    if not task:
        raise HTTPException(422, "нужна задача")
    max_steps = max(1, min(6, int((body or {}).get("max_steps") or 4)))

    # каталог: методика, контракт и то, что навык отдаёт
    catalog: dict = {}
    tpls = {t["id"]: t for t in (await schema_store.all() or [])}
    fam = access.scope_key(family=str((body or {}).get("family") or "")) if (body or {}).get("family") else ""
    for sid in list(ape.SKILLS):
        meta = ape.SKILLS.get(sid) or ()
        tpl = tpls.get(sid) or {}
        catalog[sid] = {
            "title": meta[0] if len(meta) > 0 else sid,
            "short": meta[1] if len(meta) > 1 else "",
            # Полная методика, а не однострочное описание: по короткой фразе навык не отличить от
            # соседнего, и планировщик выбирал похожий, а не подходящий.
            "body": (ape.load_skill_body(sid) or (meta[2] if len(meta) > 2 else ""))[:6000],
            "inputs": tpl.get("inputs") or {},
            "produces": tpl.get("produces") or {},
            # Куда навык объявил доставку: если человек назвал адресата («в трекер»), планировщик
            # сравнивает объявленное с названным, а не угадывает по словам методики.
            "delivery": tpl.get("delivery") or {},
            "mode": (ape.skill_safety(sid) or {}).get("mode") or "read",
        }

    # сущности, в которых РЕАЛЬНО есть данные: навык на пустой сущности не отработает
    ents = {e["entity"] for e in ((await data_entities(u)).get("entities") or []) if (e.get("rows") or 0) > 0}
    slots_given = {str(k) for k, v in ((body or {}).get("slots") or {}).items() if str(v or "").strip()}

    # подсказки по смыслу: у подбора агентов уже есть словарь и сравнение по смыслу — используем их.
    # Вызывающая сторона может их отключить (`"hints": false`): решение «звать готового или собирать»
    # нельзя принимать на плане, который сам же построен под похожего агента — это круг.
    hints: dict = {}
    try:
        if (body or {}).get("hints") is False:
            raise RuntimeError("подсказки отключены вызывающей стороной")
        m = await agents_match({"q": task}, u)
        for it in (m.get("matches") or [])[:5]:
            ag = await agent_store.get(it.get("id") or "")
            for n in (((ag or {}).get("graph") or {}).get("nodes") or []):
                if n.get("skill"):
                    hints[n["skill"]] = max(float(hints.get(n["skill"]) or 0), float(it.get("score") or 0))
    except Exception:  # noqa: BLE001 — подсказки усиление, а не условие работы планировщика
        hints = {}

    sem = await _skill_semantics(planner.without_links(task), catalog)
    try:
        # Две прибавки разной природы, и они складываются: СКЛОННОСТЬ человека к навыку плюс
        # связь СЛОВ этого запроса с тем, что люди арендатора брали на такие слова. Вторая и есть
        # обучение на правках: поменял предложенное руками — слова запроса начинают тянуть к
        # выбранному. Обе держатся ниже порога уверенного подбора, поэтому решают ничью, а не
        # назначают исполнителя.
        pref = await choice_store.prefer(_sub(u))
        for sid, add in (await choice_store.prefer_terms(task)).items():
            pref[sid] = round(min(choice_store.PREFER_CAP + choice_store.TERMS_CAP,
                                  float(pref.get(sid) or 0) + add), 4)
    except Exception as _ex:  # noqa: BLE001 — предпочтения усиление, а не условие подбора
        obs.log_event("warning", "plan.prefer_failed", error=str(_ex)[:160])
        pref = {}
    # Голос модели из закрытого списка навыков. Нужен ровно там, где словарь бессилен: перефраз
    # без общих основ («кто мне не заплатил» ↔ «дебиторская задолженность»). Выключатель есть и
    # по умолчанию ВЫКЛЮЧЕН: каждый подбор — это запрос к модели, то есть деньги и задержка, и
    # включать такое до измерения значит платить за необоснованную надежду.
    выбор_модели: dict = {}
    голос = {"ok": False, "почему": "подбор моделью выключен (ABOP_PLAN_MODEL)"}
    if _МОДЕЛЬЮ:
        голос = await semantic_pick.голос(planner.without_links(task), catalog)
        выбор_модели = голос.get("выбор") or {}
        if not голос.get("ok"):
            obs.log_event("warning", "plan.model_silent", why=str(голос.get("почему"))[:160])
    p = planner.plan(task, catalog, entities=ents, slots=slots_given, max_steps=max_steps,
                     hints=hints, semantic=sem, prefer=pref, model_pick=выбор_модели)
    # Участвовала модель или нет — в ответе словами. Иначе «почему выбран этот навык» читается
    # одинаково и когда модель подтвердила выбор, и когда она молчала: решение принимают по этому.
    p["подбор_моделью"] = {"участвовала": bool(голос.get("ok")),
                           "модель": голос.get("модель") or "",
                           "назвала": голос.get("порядок") or [],
                           "почему": голос.get("почему") or ""}
    # Форму спрашиваем у реестра форм: бланк вертикали объявляет, чьи результаты оформляет, и знает
    # предмет лучше правила по виду результата. Правило по виду остаётся фолбэком — на случай, когда
    # под навыки плана бланка нет.
    _sids = [str(st.get("skill") or "") for st in (p.get("steps") or []) if st.get("skill")]
    try:
        _by_skill = await report_store.template_for_skills(_sids)
    except Exception:  # noqa: BLE001 — реестр недоступен: решает вид результата
        _by_skill = ""
    p["report_template"] = _by_skill or planner.report_template(p.get("steps") or [], catalog)
    p["entities_ready"] = sorted(ents)
    p["family"] = fam
    obs.log_event("info", "plan.auto", task=task[:80], steps=len(p.get("steps") or []),
                  ok=bool(p.get("ok")))
    return p


async def _agent_for_skills(sids: list[str], u: dict) -> dict | None:
    """Готовый агент ровно на этот набор навыков — чтобы сборка из плана не плодила однофамильцев.

    Прежде каждый шаг плана заводил нового агента (а при повторной сборке — новую версию того же),
    и после нескольких проб «Мои агенты» зарастали карточками, отличающимися только номером версии.
    Если подходящий агент уже есть, цепочка должна ссылаться на него.

    Подходящим считаем активного агента, у которого набор узлов-навыков совпадает с планом и нет
    вывода во внешний канал: собранный по плану агент не должен неожиданно начать отправлять письма
    или заводить задачи только потому, что когда-то такого агента собрали с доставкой.

    Свои агенты предпочтительнее общих: у общего может не хватить прав на запуск у этого человека.
    """
    try:
        rows = await agent_store.list_for(limit=200) or []
    except Exception:  # noqa: BLE001 — не нашли кандидатов, соберём нового
        return None
    import hashlib as _hl
    mine_prefix = "authored-" + _hl.md5(str(u.get("sub") or u.get("name") or "dev").encode("utf-8")).hexdigest()[:6]
    best: tuple[int, dict] | None = None
    for row in rows:
        if (row.get("status") or "") == "retired":
            continue
        ag = await agent_store.get(row.get("id") or "")
        graph = (ag or {}).get("graph") or {}
        nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
        skills = [str(n.get("skill") or "") for n in nodes if n.get("kind") == "skill"]
        if sorted(skills) != sorted(sids):
            continue
        outward = any(n.get("kind") == "out"
                      and str((n.get("out") or {}).get("channel") or "") not in assembly._LOCAL_CHANNELS
                      for n in nodes)
        if outward:
            continue
        rank = 0 if str(row.get("contract_audit_id") or "").startswith(mine_prefix) else 1
        if best is None or rank < best[0]:
            best = (rank, ag)
        if rank == 0:
            break                      # свой нашёлся — дальше искать нечего
    return best[1] if best else None


@router.post("/api/plan/estimate")
async def plan_estimate(payload: dict, u: dict = Depends(user)) -> dict:
    """Во что обойдётся цепочка ДО запуска (пункт 5 разбора 05.10).

    Человек собирал шесть навыков и жал «запустить», не зная цены. Бюджет прогона (ADR-035) умеет
    остановить перерасход, но останавливает он уже потраченное.

    Числа — из собственного журнала: медиана токенов по прошлым прогонам навыка. Оценка сама
    улучшается по мере работы, а навык без истории помечается отдельно, вместо того чтобы
    получить правдоподобную выдумку.
    """
    шаги = [str(x) for x in (payload.get("steps") or []) if str(x).strip()]
    if not шаги:
        raise HTTPException(422, "steps: пусто")
    # Модель берём ту же, что пойдёт в прогон: первая в каскаде профиля. Иначе оценка считалась
    # бы по одной цене, а работа — по другой.
    каскад = settings.cascade_for(str(payload.get("profile") or "standard"))
    модель = str(payload.get("model") or "").strip() or (каскад[0] if каскад else "")
    прогоны = []
    for сводка in await run_store.list_runs(limit=120):
        полный = await run_store.get(сводка["id"])
        if полный:
            прогоны.append(полный)
    история = cost_estimate.наблюдения(прогоны)
    from .pricing import cost_rub
    итог = cost_estimate.оценить(steps=шаги, history=история, model=модель, цена=cost_rub)
    return {**итог, "runs_seen": len(прогоны)}


@router.post("/api/plan/feedback")
async def plan_feedback(body: dict, u: dict = Depends(user)) -> dict:
    """Исход предложения: человек отказался или взял своё. Нужен, чтобы подбор учился.

    Отмена — самый ценный сигнал: её делают осознанно, в отличие от согласия, которое часто
    случается по инерции. Без этой ручки отказ оставался только в карточке чата.
    """
    task = str((body or {}).get("task") or "")
    offered = [str(x) for x in ((body or {}).get("offered") or [])]
    taken = [str(x) for x in ((body or {}).get("taken") or [])]
    outcome = str((body or {}).get("outcome") or "cancelled")
    if outcome not in ("accepted", "edited", "cancelled"):
        raise HTTPException(422, "исход бывает accepted | edited | cancelled")
    await choice_store.record(_sub(u), task, offered, taken, outcome)
    return {"ok": True}


@router.get("/api/plan/prefer")
async def plan_prefer(u: dict = Depends(user)) -> dict:
    """Что подбор выучил на решениях этого человека — чтобы это можно было посмотреть, а не гадать."""
    return {"prefer": await choice_store.prefer(_sub(u)),
            "history": (await choice_store.history(_sub(u), 20))[:20]}


@router.get("/api/plan/terms")
async def plan_terms(task: str = "", u: dict = Depends(user)) -> dict:
    """Что подбор выучил на СЛОВАХ: какие навыки тянет эта формулировка и почему.

    Нужна, чтобы обучение было видно, а не угадывалось: прибавка невидимая глазом — это та же
    магия, против которой написан весь остальной подбор.
    """
    прибавка = await choice_store.prefer_terms(task) if task.strip() else {}
    return {"task": task, "terms": sorted(choice_store._основы(task)),
            "prefer_terms": прибавка, "cap": choice_store.TERMS_CAP,
            "поддержка_мин": choice_store.ПОДДЕРЖКА_МИН,
            "решений_для_полного_веса": choice_store.ПОДДЕРЖКА_ПОЛНАЯ}


@router.post("/api/plan/auto/build")
async def plan_auto_build(body: dict, u: dict = Depends(user)) -> dict:
    """Собрать из плана исполнителя — ОДНОГО агента на все навыки плана.

    Прежде каждый шаг плана становился отдельным агентом, а шаги — цепочкой. Владелец, прогнав такую
    цепочку, сказал главное: «навыки работают в вакууме, и отчёт получается на каждый шаг». Так и
    было. У цепочки агентов вход следующего шага — это ТЕКСТОВЫЙ блок с результатом предыдущего,
    склеенный в промпт; каждый шаг — отдельный прогон, а значит и отдельный отчёт.

    Внутри ОДНОГО агента всё иначе: навыки идут волнами, выход кладётся на доску прогона под
    объявленным ключом, а приёмник получает ровно тот ключ, который объявил входом (`from: skill` /
    `from: board`). Это и есть точная передача — по контракту, а не пересказом. И прогон один, значит
    отчёт один: бланк вертикали собирает разделы по всем навыкам прогона.
    Поэтому план теперь собирается в одного агента, а цепочка остаётся по явной просьбе
    (`as_chain: true`) — она нужна там, где у шагов разная доставка или своё подтверждение.
    """
    require_level(u, "manager")
    steps = (body or {}).get("steps") or []
    if not steps:
        raise HTTPException(422, "нечего собирать: план пуст")
    name = str((body or {}).get("name") or "Агент по плану").strip()
    reuse = bool((body or {}).get("reuse", True))      # по умолчанию переиспользуем готовых
    as_chain = bool((body or {}).get("as_chain", False))
    sids: list[str] = []
    for st in steps:
        sid = str(st.get("skill") or "")
        if sid not in ape.SKILLS:
            raise HTTPException(422, f"нет навыка «{sid}»")
        if sid not in sids:
            sids.append(sid)

    def _family_of(sid: str) -> str:
        fams = [f for f, spec in ape.AGENT_FAMILIES.items()
                if any(sid in (m[1] or []) for m in (spec.get("members") or {}).values())]
        return fams[0] if fams else "management"

    async def _author(one_name: str, skills: list[str]) -> dict:
        """Создать агента и вернуть тело ответа: agent_author отдаёт JSONResponse."""
        resp = await agent_author({"name": one_name, "family": _family_of(skills[0]), "skills": skills}, u)
        import json as _json  # noqa: PLC0415 — как и в соседних роутах, модуль по месту
        ag = _json.loads(bytes(resp.body).decode()) if hasattr(resp, "body") else (resp or {})
        if not ag.get("id"):
            raise HTTPException(500, "агент по плану не собрался")
        return ag

    if not as_chain:
        # Редактор отчёта добавляется сам, когда навыков больше одного: иначе человек получит
        # документ, сшитый механически по волнам, хотя порядок разделов под задачу бывает другим.
        # Но только если раскладки для этого набора ещё нет: вызов модели нужен на новое сочетание,
        # а не на каждый запуск — дальше работает кэш.
        editor = ""
        if len(sids) >= 2 and report_compose.EDITOR_SKILL not in sids:
            _cid = report_compose.cache_id(sids)
            _cached = await report_store.get(_cid) if _cid else None
            if _cached and _cached.get("layout"):
                editor = "cached"
            else:
                sids = sids + [report_compose.EDITOR_SKILL]
                editor = "added"
        # Решение человека — факт, который стоит помнить: он взял ИМЕННО эти навыки, и если правил
        # цепочку руками, это сильнее простого согласия.
        try:
            await choice_store.record(_sub(u), str((body or {}).get("task") or ""),
                                      list((body or {}).get("offered") or sids), sids,
                                      "edited" if (body or {}).get("manual") else "accepted")
        except Exception as _ex:  # noqa: BLE001
            obs.log_event("warning", "plan.choice_failed", error=str(_ex)[:160])
        exist = await _agent_for_skills(sids, u) if reuse else None
        _ed = {"added": " Добавлен редактор отчёта — он сложит разделы в один документ.",
               "cached": " Раскладка отчёта для этого набора уже известна: редактор не нужен."}.get(editor, "")
        if exist:
            return {"ok": True, "agent_id": exist["id"], "name": exist.get("name"), "skills": sids,
                    "pipeline": None, "created": 0, "reused": 1, "editor": editor,
                    "agents": [{"agent_id": exist["id"], "name": exist.get("name"),
                                "skills": sids, "reused": True}],
                    "note": f"готовый агент «{exist.get('name')}» уже умеет эти навыки ({len(sids)}) — "
                            f"собирать нечего, запускаю его." + _ed}
        ag = await _author(name, sids)
        return {"ok": True, "agent_id": ag["id"], "name": name, "skills": sids,
                "pipeline": None, "created": 1, "reused": 0, "editor": editor,
                "agents": [{"agent_id": ag["id"], "name": name, "skills": sids, "reused": False}],
                "note": f"собран агент из {len(sids)} навыков: передача между ними идёт по контрактам, "
                        f"отчёт будет один." + _ed}

    # ── цепочка по явной просьбе: у шагов разная доставка или своё подтверждение ──
    made, ids = [], {}
    for st in steps:
        sid = str(st.get("skill") or "")
        if sid in ids:
            continue
        exist = await _agent_for_skills([sid], u) if reuse else None
        if exist:
            ids[sid] = exist["id"]
            made.append({"skill": sid, "agent_id": exist["id"], "family": exist.get("family") or _family_of(sid),
                         "name": exist.get("name"), "reused": True})
            continue
        ag = await _author(str(st.get("title") or sid), [sid])
        ids[sid] = ag["id"]
        made.append({"skill": sid, "agent_id": ag["id"], "family": _family_of(sid),
                     "name": ag.get("name"), "reused": False})
    pl_steps = []
    for i, st in enumerate(steps, 1):
        sid = str(st.get("skill") or "")
        step = {"id": "p" + str(i), "agent_id": ids[sid], "deliver": "chat"}
        after = [f"p{j}" for j, prev in enumerate(steps, 1) if str(prev.get("skill")) in (st.get("after") or [])]
        if after:
            step["after"] = after
        pl_steps.append(step)
    _reused = sum(1 for m in made if m.get("reused"))
    _new = len(made) - _reused
    _who = f"агентов {len(made)}: новых {_new}, переиспользовано {_reused}"
    if len(pl_steps) < 2:
        return {"ok": True, "agents": made, "pipeline": None, "created": _new, "reused": _reused,
                "agent_id": made[0]["agent_id"],
                "note": f"в плане один шаг — цепочка не нужна, запускаю агента напрямую ({_who})"}
    pl = await pipeline_save({"name": name, "steps": pl_steps}, u)
    return {"ok": True, "agents": made, "pipeline": pl.get("id"), "name": pl.get("name"),
            "created": _new, "reused": _reused,
            "note": f"{_who}, цепочка из {len(pl_steps)} шагов"}


@router.post("/api/plan")
def plan(body: dict, u: dict = Depends(user)) -> dict:
    """Превью декомпозиции цели по семьям (детерминированно, без LLM). §9.1 запуск."""
    goal = str((body or {}).get("goal", "")).strip()
    if not goal:
        raise HTTPException(422, "нужна goal")
    fams = ape.detect_families(goal)
    waves = [[{"task": f"{goal} — аспект: {ape.AGENT_FAMILIES[f]['title']}", "family": f}
              for f in fams]] if len(fams) >= 2 else [[{"task": goal, "family": ape.route_family(goal)}]]
    return {"goal": goal, "families": fams, "preview_waves": waves,
            "note": "предварительный план (эвристика); финальный план строит LLM-планировщик при запуске"}


@router.get("/api/nlu/parse")
async def nlu_parse(q: str = "", u: dict = Depends(user)) -> dict:
    """Разбор фразы для песочницы в UI: лексемы, этапы, триггер и его окружение. Ничего не запускает."""
    return {"q": q, **nlu.describe(q), "triggers": sorted(set(nlu.TRIGGERS.values())), "config": await nlu_config()}


@router.get("/api/nlu/config")
async def nlu_config_get(u: dict = Depends(user)) -> dict:
    return await nlu_config()


@router.post("/api/nlu/config")
async def nlu_config_set(body: dict, u: dict = Depends(user)) -> dict:
    """Пороги подбора: ниже min_confidence — предупреждение, weak_stage — «слабый шаг», rerank — уточнять ли моделью."""
    require_level(u, "admin")
    cur = await nlu_config()
    for k in ("min_confidence", "weak_stage"):
        if k in (body or {}):
            try:
                v = float(body[k])
            except Exception:  # noqa: BLE001
                raise HTTPException(422, f"{k}: число 0..1")
            if not 0.0 <= v <= 1.0:
                raise HTTPException(422, f"{k}: число 0..1")
            cur[k] = v
    if "rerank" in (body or {}):
        cur["rerank"] = bool(body["rerank"])
    if "max_stages" in (body or {}):
        cur["max_stages"] = max(2, min(12, int(body["max_stages"])))
    actor = u.get("name") or u.get("sub") or "dev"
    await admin_store.save("nluConfig", cur, editor=actor)
    await audit_store.record(actor, "nlu.config", "nluConfig", cur)
    return cur
