# -*- coding: utf-8 -*-
"""Семьи ролей: единый реестр отделов и корпус знаний семьи.

Домен вынесен из монолита (ADR-052, docs/RASKLADKA_SERVERA.md). Семья в ABOP — одновременно
онтология (Семья→Роль→Навык, ADR-005) и единица ABAC: отдел человека сверяется с семьёй агента.
Поэтому реестр один и живёт в БД (families-registry), а не дублируется константой на фронте.

Корпус знаний семьи набирается из sLAVA под нормативные запросы `_FAMILY_NORM_QUERY`. Число таких
запросов невелико и это видно в ответе: «законодательная база семьи» из четырёх поисков — не
законодательная база, и выдавать её за таковую нельзя (ADR-016).

Зависимости наружу: `server/deps`, сторы и ядро `ape`; `web_api` не импортируется.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cli import ape

from .. import access, audit_store, families_store, slava
from ..deps import require_level, user

router = APIRouter()

# Деление на бизнесовые и инженерные семьи: первым нужен корпус норм и ABAC по отделу, вторым —
# доступ к системам разработки. Константа живёт здесь, у реестра семей, а не в приложении.
BIZ_FAMILIES = {"analytics", "finance", "credit", "architecture", "management"}


@router.get("/api/family/collections")
async def family_collections(u: dict = Depends(user)) -> dict:
    """Коллекции sLAVA (в т.ч. slava_fam_<family>) + доступность текущему отделу (ABAC)."""
    try:
        cols = await slava.collections()
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"sLAVA недоступна: {ex}")
    return {"collections": cols, "department": u.get("department")}


@router.post("/api/family/seed")
async def family_seed(body: dict, u: dict = Depends(user)) -> dict:
    """Наполнить корпуса семей РЕАЛЬНЫМ знанием из каталога навыков: тело каждого навыка (методика .md)
    → slava_fam_<family> той семьи, что несёт навык. Тело: {family?} (пусто ⇒ все семьи). Admin-уровень."""
    require_level(u, "admin")
    only = str((body or {}).get("family") or "").strip()
    out = {}
    for fid, fam in ape.AGENT_FAMILIES.items():
        if only and fid != only:
            continue
        col = slava.fam_collection(fid)
        sids = []
        for _mk, (_mt, sk) in (fam.get("members") or {}).items():
            for s in sk:
                if s not in sids:
                    sids.append(s)
        n = 0
        for sid in sids:
            if sid not in ape.SKILLS:
                continue
            title, short, _instr = ape.SKILLS[sid]
            try:
                body_txt = (ape.load_skill_body(sid) or "")[:4000]
            except Exception:  # noqa: BLE001
                body_txt = ""
            text = f"[{fam.get('title', fid)}] Навык «{title}». {short}\n\n{body_txt}".strip()
            if len(text) < 20:
                continue
            try:
                await slava.ingest(col, f"{sid}.md", text, tenant="abop", replace=False, doc_id=sid)
                n += 1
            except Exception:  # noqa: BLE001 — пропускаем сбойный навык, продолжаем
                pass
        out[fid] = {"collection": col, "skills_ingested": n}
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.seed", only or "all",
                             {"families": len(out)})
    return {"seeded": out, "families": len(out)}


# СПЕЦИФИЧНЫЕ под-запросы (sLAVA отсекает широкие/generic по релевантности — нужны точные темы норм)
_FAMILY_NORM_QUERY = {
    "finance": ["вычет НДС при наличии счёта-фактуры", "признание выручки и себестоимости ФСБУ",
                "налог на прибыль расходы уменьшают доход", "уточнённая декларация по НДС"],
    "audit": ["реализация без счёта-фактуры выданного НДС", "поступление без счёта-фактуры вычет НДС",
              "переходящая операция разные периоды", "сделки взаимозависимых лиц деловая цель"],
    "credit": ["резервы на возможные потери по ссудам", "оценка кредитоспособности заёмщика",
               "обеспечение по кредиту залог"],
    "analytics": ["финансовые показатели отчётность", "оценка эффективности рентабельность"],
    "management": ["договорные условия сроки ответственность", "регламент бизнес-процесса"],
    "architecture": ["требования к интеграции API", "стандарты проектирования систем"],
    "engineering": ["требования к разработке тестирование", "качество кода стандарты"],
    "research": ["методология исследования источники", "анализ данных достоверность"],
    "critic": ["контроль соответствия требованиям", "проверка качества результата"],
    "decisions": ["полномочия и согласование решений", "governance принятие решений"],
}


@router.post("/api/family/seed-norms")
async def family_seed_norms(body: dict, u: dict = Depends(user)) -> dict:
    """Раздать ЗАКОНОДАТЕЛЬНЫЕ НОРМЫ по корпусам семей из большого корпуса регламента (slava_reglament_ru):
    семантический запрос по домену семьи → top-N чанков → в slava_fam_<family>. Тело: {family?, query?,
    count?, source?}. Пусто family ⇒ норм-релевантные семьи (finance/audit/credit). Admin-уровень.
    NB: запускать ПО ОДНОЙ семье с паузой (sLAVA cooldown)."""
    require_level(u, "admin")
    src = str((body or {}).get("source") or "slava_reglament_ru").strip()
    count = int((body or {}).get("count") or 6)
    fam = str((body or {}).get("family") or "").strip()
    fams = [fam] if fam else ["finance", "audit", "credit"]
    out = {}
    for fid in fams:
        qs = ([str((body or {}).get("query"))] if (body or {}).get("query")
              else _FAMILY_NORM_QUERY.get(fid, [fid]))
        seen_txt, chunks = set(), []
        for q in qs:  # специфичные под-запросы: собираем уникальные чанки
            try:
                for s in (await slava.query(src, q, top_k=max(2, count // len(qs)), tenant="abop")).get("sources") or []:
                    txt = (s.get("text") or "").strip()
                    if len(txt) >= 30 and txt[:120] not in seen_txt:
                        seen_txt.add(txt[:120])
                        chunks.append(txt)
            except Exception:  # noqa: BLE001
                pass
        col = slava.fam_collection(fid)
        n = 0
        for i, txt in enumerate(chunks[:count * 2]):
            try:
                await slava.ingest(col, f"norm_reg_{i}.txt", "[закон/норма] " + txt, tenant="abop",
                                   replace=False, doc_id=f"reg_{fid}_{i}")
                n += 1
            except Exception:  # noqa: BLE001
                pass
        out[fid] = {"collection": col, "norms_ingested": n, "from": src, "subqueries": len(qs)}
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.seed_norms", fam or "norm-fams",
                             {"families": len(out)})
    return {"seeded_norms": out}


@router.post("/api/family/ingest")
async def family_ingest(body: dict, u: dict = Depends(user)) -> dict:
    """Загрузить знание/регламент в корпус СЕМЬИ (slava_fam_<family>) через sLAVA. ABAC: отдел = семья
    (или admin/support). Тело: {family, text, filename?, replace?}. §graph-RAG по семьям."""
    require_level(u, "manager")
    family = str((body or {}).get("family") or "").strip()
    text = str((body or {}).get("text") or "").strip()
    if not family or not text:
        raise HTTPException(422, "нужны family и text")
    if not access.can_reach_family(u.get("department"), family):
        await access.audit_denial(u.get("name") or u.get("sub") or "dev", u.get("department"),
                                  slava.fam_collection(family), "family.ingest", "отдел ≠ семья")
        raise HTTPException(403, f"нет доступа к корпусу семьи «{family}» (изоляция знания)")
    col = slava.fam_collection(family)
    try:
        res = await slava.ingest(col, str((body or {}).get("filename") or f"{family}.txt"), text,
                                 tenant="abop", replace=bool((body or {}).get("replace")))
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"sLAVA ingest не удался: {ex}")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.ingest", col, {"family": family})
    return {"family": family, "collection": col, "result": res}


@router.post("/api/family/query")
async def family_query(body: dict, u: dict = Depends(user)) -> dict:
    """RAG-поиск по корпусу СЕМЬИ (slava_fam_<family>) через sLAVA. ABAC-изоляция: отдел = семья
    (аналитик не читает корпус архитектуры). Тело: {family, query, top_k?}."""
    family = str((body or {}).get("family") or "").strip()
    q = str((body or {}).get("query") or "").strip()
    if not family or not q:
        raise HTTPException(422, "нужны family и query")
    if not access.can_reach_family(u.get("department"), family):
        await access.audit_denial(u.get("name") or u.get("sub") or "dev", u.get("department"),
                                  slava.fam_collection(family), "family.query", "отдел ≠ семья")
        raise HTTPException(403, f"нет доступа к корпусу семьи «{family}» (изоляция знания)")
    col = slava.fam_collection(family)
    try:
        res = await slava.query(col, q, top_k=int((body or {}).get("top_k") or 5), tenant="abop")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"sLAVA query не удался: {ex}")
    return {"family": family, "collection": col, "result": res}

@router.get("/api/families")
async def families(u: dict = Depends(user)) -> dict:
    """Ростер Семья→Роль→Навык из ЕДИНОГО реестра families_store (сид из кода, правки/кастомные в БД).
    §4/§6 ABOP_SCREENS. Единый источник семей/отделов (ABAC department==family)."""
    reg = await families_store.all()
    if not reg:                          # реестр ещё не засеян (первый старт до seed) → отдаём код
        reg = [{"id": fid, "title": fam["title"], "profile": fam["profile"], "mission": fam["mission"],
                "kind": "business" if fid in BIZ_FAMILIES else "engineering",
                "members": {mk: [mt, sk] for mk, (mt, sk) in fam["members"].items()}}
               for fid, fam in ape.AGENT_FAMILIES.items()]
    out = []
    for f in reg:
        mem = f.get("members") or {}
        members = [{"key": mk, "title": (v[0] if isinstance(v, (list, tuple)) else mk),
                    "skills": (v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else [])}
                   for mk, v in mem.items()]
        out.append({"id": f["id"], "title": f.get("title") or f["id"], "profile": f.get("profile") or "research",
                    "mission": f.get("mission") or "", "kind": f.get("kind") or "custom",
                    "builtin": bool(f.get("builtin")), "members": members})
    return {"families": out}


@router.post("/api/families")
async def family_save(body: dict, u: dict = Depends(user)) -> dict:
    """Создать/править семью (в т.ч. СВОЮ кастомную) в едином реестре. Кастомная семья = валидный
    отдел для ABAC (department==family) и тег навыка. Тело: {id, title?, mission?, profile?, members?}.
    id — slug [a-z0-9_-]. Admin-уровень (реестр семей = чувствительно к доступу)."""
    require_level(u, "admin")
    import re as _re
    fid = _re.sub(r"[^a-z0-9_-]", "", str((body or {}).get("id") or "").strip().lower())
    if not fid:
        raise HTTPException(422, "нужен id семьи (slug a-z0-9_-)")
    existing = await families_store.get(fid)
    kind = "custom" if not existing else (existing.get("kind") or "custom")
    editor = u.get("name") or u.get("sub") or "dev"
    card = await families_store.save(fid, {"title": (body or {}).get("title") or fid,
                                           "mission": (body or {}).get("mission") or "",
                                           "profile": (body or {}).get("profile") or "research",
                                           "kind": kind, "members": (body or {}).get("members") or (existing or {}).get("members") or {}},
                                     editor=editor, builtin=bool(existing and existing.get("builtin")))
    await audit_store.record(editor, "family.save", fid, {"title": card.get("title")})
    return card


@router.delete("/api/families/{fid}")
async def family_delete(fid: str, u: dict = Depends(user)) -> dict:
    """Удалить кастомную семью (встроенные защищены). Admin-уровень."""
    require_level(u, "admin")
    ok = await families_store.delete(fid)
    if not ok:
        raise HTTPException(400, "нельзя удалить (нет такой или встроенная семья)")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "family.delete", fid, {})
    return {"ok": True}
