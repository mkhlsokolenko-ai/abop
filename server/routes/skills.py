# -*- coding: utf-8 -*-
"""Навыки: карточка, контракт, источники данных и покрытие каталога.

Навык — методика одного шага: что сделать и что считать результатом. Домен отдаёт карточки,
правит их поверх поставочных (оверлей в БД, репо не трогаем) и показывает, какие сущности данных
навык объявил и чем они закрыты.

Правило домена: правка навыка — это ОВЕРЛЕЙ поверх версии из репозитория, а не замена файла.
Поэтому «вернуть версию репо» всегда возможно, а обновление поставки не затирает правки заказчика.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cli import ape

from .. import audit_store, cachebus, families_store, schema_store, skill_contract, skill_store
from .. import skill_tools

from ..deps import can_see_family, require_level, user

router = APIRouter()


# поля навыка, которые оператор правит в UI и которые оверрайдятся из Postgres (skill_store.patch)
_SKILL_TEXT_FIELDS = ("title", "short", "flow", "when", "method", "dod", "anti")


# output — формат вывода навыка (structured|freeform), редактируется в UI. mode/egress/cite —
# отображаются, но governance-безопасность узла берётся авторитетно из каталога (assembly), не из правок.
_SKILL_SAFETY_FIELDS = ("mode", "egress", "cite", "output", "schema_template_id")


async def _refresh_skill_ds_cache() -> None:
    """Подтянуть data-need + флаг формата вывода (output) оверрайды из Postgres → инжект в ape."""
    try:
        ape.set_skill_ds_overrides(await skill_store.datasources_map())
        ape.set_skill_output_overrides(await skill_store.output_map())
    except Exception:  # noqa: BLE001
        pass


def _skill_families() -> dict:
    in_fam: dict = {}
    for fid, fam in ape.AGENT_FAMILIES.items():
        for _mk, (_mt, sk) in fam["members"].items():
            for s in sk:
                in_fam.setdefault(s, []).append(fid)
    return {k: sorted(set(v)) for k, v in in_fam.items()}


def _skill_base_card(sid: str, fam: dict) -> dict:
    title, short, _instr = ape.SKILLS[sid]
    parsed = ape.parse_skill_md(sid)   # секции/шаги — чтобы drawer не был пустым
    return {"id": sid, "title": title, "short": short,
            "safety": ape.skill_safety(sid), "scope": ape.skill_scope(sid),
            "families": fam.get(sid, []),
            "sections": parsed["sections"], "flow": parsed["flow"],
            "when": parsed["when"], "method": parsed["method"],
            "dod": parsed["dod"], "anti": parsed["anti"],
            "datasources": ape.skill_datasources_resolved(sid)}


def _overlay_skill(card: dict, ov: dict | None) -> dict:
    """Наложить сохранённые в Postgres правки навыка (общие для всех) поверх базового каталога (.md).
    Governance-безопасность узла берётся АВТОРИТЕТНО из базового каталога (assembly), а не из правок."""
    if ov:
        patch = ov.get("patch") or {}
        for k in _SKILL_TEXT_FIELDS:
            if k in patch and patch[k]:
                card[k] = patch[k]
        saf = dict(card.get("safety") or {})
        for k in _SKILL_SAFETY_FIELDS:
            if k in patch:
                saf[k] = patch[k]
        card["safety"] = saf
        # привязка навыка к семьям из UI (тег семьи): PG-список перекрывает вычисленный из ростера.
        # Влияет на ABAC-видимость (_skill_visible) и фильтр по семьям на экране навыков.
        if isinstance(patch.get("families"), list):
            card["families"] = sorted(set(patch["families"]))
            card["families_edited"] = True
        card["version"] = ov.get("version")
        card["editor"] = ov.get("editor")
        card["edited_at"] = ov.get("updated_at")
        # какой шаблон извлечения реально применяется к навыку: без этого поля привязка видна только
        # в UI, и сверка «ответ в схеме» шла не с той схемой (мы так потеряли 4 поля у разбора почты)
        if patch.get("schema_template_id"):
            card["schema_template_id"] = patch["schema_template_id"]
    else:
        card["version"] = "v1.0"
        card["editor"] = None
        card["edited_at"] = None
    return card


def _skill_visible(u: dict, card: dict) -> bool:
    """ABAC для навыков: admin/support (область *) видят все; общий навык (вне семей) виден всем;
    иначе — только если ХОТЯ БЫ одна семья навыка совпадает с отделом пользователя (department == family).
    Так менеджер видит менеджерские навыки, финансист — финансовые (изоляция периметра по данным)."""
    fams = card.get("families") or []
    if not fams:                      # навык вне семей — общий инструмент, доступен всем ролям
        return True
    return any(can_see_family(u, f) for f in fams)


@router.get("/api/skills")
async def skills(u: dict = Depends(user), all: bool = False) -> dict:
    """Каталог навыков (.md) + PG-правки. По умолчанию ФИЛЬТРУЕТСЯ по ABAC (навыки своей семьи +
    общие); `?all=1` — полный каталог (инженерная платформа/сборка агентов). §4."""
    fam = _skill_families()
    overrides = await skill_store.all()
    cards = [_overlay_skill(_skill_base_card(sid, fam), overrides.get(sid)) for sid in ape.SKILLS]
    total = len(cards)
    if not all:
        cards = [c for c in cards if _skill_visible(u, c)]
    return {"skills": cards, "count": len(cards), "total": total}


@router.get("/api/skills/{sid}")
async def skill(sid: str, u: dict = Depends(user)) -> dict:
    """Полное тело навыка (progressive disclosure = use_skill) + PG-правки. §4 drawer."""
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    card = _overlay_skill(_skill_base_card(sid, _skill_families()), await skill_store.get(sid))
    parsed = ape.parse_skill_md(sid)
    card["body"] = ape.load_skill_body(sid)
    card["intro"] = parsed["intro"]
    _out = card
    try:
        _out["tools"] = skill_tools.tools_for(sid)
    except Exception:  # noqa: BLE001
        _out["tools"] = []
    return _out


@router.post("/api/skills/{sid}")
async def skill_save(sid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить правки навыка НОВОЙ версией в Postgres (общие для всех пользователей, переживают
    перенакат/рестарт — не localStorage). Редактор = текущий пользователь. Тело: {title?, short?,
    mode?, egress?, cite?, flow?, when?, method?, dod?, anti?}. §БД-фаза (persistence-localstorage-hole)."""
    require_level(u, "manager")  # analyst — только чтение
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    allowed = set(_SKILL_TEXT_FIELDS) | set(_SKILL_SAFETY_FIELDS)
    patch = {k: v for k, v in (body or {}).items() if k in allowed}
    # тег семьи навыка: привязка к семьям из ЕДИНОГО реестра (известным ИЛИ своей кастомной через «＋»).
    # Кастомные — slug [a-z0-9_-] (для корректного ABAC: department==family).
    if isinstance((body or {}).get("families"), list):
        import re as _re
        known = await families_store.ids() or set(ape.AGENT_FAMILIES.keys())
        fams = []
        for f in body["families"]:
            f = str(f).strip()
            if not f:
                continue
            if f in known:
                fams.append(f)                       # семья из реестра
            else:
                slug = _re.sub(r"[^a-z0-9_-]", "", f.lower())  # кастомная — санитизируем в slug
                if slug:
                    fams.append(slug)
        patch["families"] = sorted(set(fams))
    if not patch:
        raise HTTPException(422, "нет полей для сохранения")
    editor = u.get("name") or u.get("sub") or "dev"
    ov = await skill_store.save_patch(sid, patch, editor=editor)
    if "output" in patch or "families" in patch:  # формат вывода / привязка семьи → на другие реплики
        await _refresh_skill_ds_cache()
        await cachebus.notify("skills")
    await audit_store.record(editor, "skill.save", sid,
                             {"version": ov.get("version"), "fields": sorted(patch.keys())})
    return _overlay_skill(_skill_base_card(sid, _skill_families()), ov)


@router.post("/api/skills/{sid}/datasources")
async def skill_datasources_set(sid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Оператор правит data-need навыка (ADR-032): какие сущности/поля берёт навык → Postgres. §4 drawer.
    Тело: {datasources:[{entity, fields[], kind?, note?}]}. Возвращает резолвнутые (с рецептом по entity)."""
    require_level(u, "manager")
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    norm = ape.normalize_skill_datasources((body or {}).get("datasources") or [])
    editor = u.get("name") or u.get("sub") or "dev"
    await skill_store.save_datasources(sid, norm, editor=editor)
    await _refresh_skill_ds_cache()   # чтобы build_agent_spec/lineage сразу видели новые источники
    await cachebus.notify("skills")   # инвалидировать кэш навык-источников на других репликах
    await audit_store.record(editor, "skill.datasources", sid, {"count": len(norm)})
    return {"id": sid, "datasources": ape.skill_datasources_resolved(sid)}


@router.get("/api/catalog/fields")
async def catalog_fields(u: dict = Depends(user)) -> dict:
    """Словарь понятий: канонические имена полей, их синонимы и употребление в каталоге.

    По нему интерфейс подсказывает автору навыка каноническое имя и показывает, где в каталоге
    одно понятие названо по-разному (из-за этого шаги цепочки не соединялись)."""
    used: dict[str, set] = {}

    def walk(node, sid):
        if not isinstance(node, dict):
            return
        for k, v in (node.get("properties") or {}).items():
            used.setdefault(k, set()).add(sid)
            if isinstance(v, dict):
                walk(v, sid)
                walk(v.get("items") or {}, sid)

    for t in await schema_store.all():
        walk(t.get("json_schema") or {}, t.get("id"))

    rows = []
    for canon, syns in sorted(skill_contract.CANON.items()):
        alts = [{"name": a, "skills": sorted(used.get(a, []))} for a in syns if a in used]
        rows.append({"canon": canon, "skills": sorted(used.get(canon, [])),
                     "synonyms": sorted(syns), "used_synonyms": alts,
                     "split": bool(alts and used.get(canon))})
    return {"fields": rows, "unique_names": len(used),
            "split_count": sum(1 for r in rows if r["split"])}


@router.get("/api/skills/{sid}/slots")
async def skill_slots(sid: str, u: dict = Depends(user)) -> dict:
    """Какие предметы навык обязан получить до запуска (из шаблона извлечения)."""
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    ov = ((await skill_store.get(sid)) or {}).get("patch") or {}
    tid = ov.get("schema_template_id") or sid
    tpl = await schema_store.get(tid) or await schema_store.get(sid) or {}
    slots = tpl.get("slots") or (tpl.get("spec") or {}).get("slots") or []
    return {"skill": sid, "template_id": tpl.get("id") or tid, "slots": slots}


@router.get("/api/skills/{sid}/contract")
async def skill_contract_get(sid: str, u: dict = Depends(user)) -> dict:
    """Контракт навыка: чем кормить (inputs), что отдаёт (produces), какой предмет уточнять (slots).

    По нему сборка агента считает покрытие входов, а планировщик цепочки понимает, можно ли поставить
    навык после другого. До контракта вход навыка был описан только прозой в инструкции."""
    if sid not in ape.SKILLS:
        raise HTTPException(404, "нет навыка")
    ov = ((await skill_store.get(sid)) or {}).get("patch") or {}
    tid = ov.get("schema_template_id") or sid
    tpl = await schema_store.get(tid) or await schema_store.get(sid) or {}
    inputs = tpl.get("inputs") or {}
    ds = []
    try:
        ds = [d.get("entity") for d in (ape.skill_datasources_resolved(sid) or []) if d.get("entity")]
    except Exception:  # noqa: BLE001 — область данных опциональна
        ds = []
    return {"skill": sid, "template_id": tpl.get("id") or tid,
            "inputs": inputs, "produces": tpl.get("produces") or {}, "slots": tpl.get("slots") or [],
            "delivery": tpl.get("delivery") or None, "entities": ds,
            "has_schema": bool((tpl.get("json_schema") or {}).get("properties")),
            "errors": skill_contract.validate_contract(tpl),
            "field_hints": skill_contract.field_warnings(tpl)}


@router.get("/api/catalog/coverage")
async def catalog_coverage(u: dict = Depends(user)) -> dict:
    """Готовность каталога к сборке: у скольких навыков есть схема, контракт входа, область данных,
    слоты и доставка. Отчёт, по которому видно, что ещё предстоит привести к единому виду."""
    tpls = {t["id"]: t for t in await schema_store.all()}
    ov = await skill_store.all()
    rows, totals = [], {"всего": 0, "схема": 0, "вход": 0, "выход": 0, "данные": 0, "слоты": 0, "доставка": 0}
    for sid in sorted(ape.SKILLS):
        patch = (ov.get(sid) or {}).get("patch") or {}
        tid = patch.get("schema_template_id") or sid
        t = tpls.get(tid) or tpls.get(sid) or {}
        try:
            ents = [d.get("entity") for d in (ape.skill_datasources_resolved(sid) or []) if d.get("entity")]
        except Exception:  # noqa: BLE001
            ents = []
        ref = sid in getattr(ape, "SKILL_REFERENCE", frozenset())
        r = {"skill": sid, "template_id": t.get("id") or "", "reference": ref,
             "схема": bool((t.get("json_schema") or {}).get("properties")),
             "вход": bool((t.get("inputs") or {}).get("required") or (t.get("inputs") or {}).get("optional")),
             "выход": bool(t.get("produces")),
             "данные": bool(ents), "слоты": bool(t.get("slots")), "доставка": bool(t.get("delivery")),
             "ошибки": skill_contract.validate_contract(t)}
        totals["всего"] += 1
        if ref:
            # справочные навыки (стилевые гайды) в цепочку не встраиваются: считаем отдельно, иначе
            # покрытие каталога вечно будет выглядеть незакрытым
            totals["справочные"] = totals.get("справочные", 0) + 1
            rows.append(r)
            continue
        for k in ("схема", "вход", "выход", "данные", "слоты", "доставка"):
            totals[k] += 1 if r[k] else 0
        rows.append(r)
    return {"totals": totals, "skills": rows}
