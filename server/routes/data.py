# -*- coding: utf-8 -*-
"""Data Plane: коннекторы, рецепты, канонические сущности, происхождение и воздействие.

Домен вынесен из монолита (п.23 исследования 08.10). Связность здесь высокая, а наружу торчит
ровно одно: определения живут в `dataplane_store`, записи — в `dataplane_records`, а исполняет
рецепты ядро `ape`, которому сервер инжектит оба стора на старте.
"""
from __future__ import annotations

import asyncio as _asyncio
import os

from fastapi import APIRouter, Depends, HTTPException

from cli import ape

from .. import access, agent_store, audit_store, cachebus, compute, dataplane_records
from .. import dataplane_store, families_store, run_store, schema_store, slava
from .. import observability as obs
from .. import skill_store, systems_store
from ..deps import _sub, can_see_family, require_level, user

router = APIRouter()

@router.get("/api/data/lineage")
async def data_lineage(u: dict = Depends(user)) -> dict:
    """Карта Data Plane: сущность → рецепты(наполняют) → навыки(потребляют) → роли/агенты. §5 «Карта»."""
    return await _asyncio.to_thread(ape.data_lineage)


@router.get("/api/impact")
async def impact_analysis(kind: str, id: str, u: dict = Depends(user)) -> dict:
    """Анализ воздействия (#12): что затронет изменение узла (сущность/рецепт/навык/система) — вниз по
    цепочке Data Plane до РАЗВЁРНУТЫХ агентов, с их семьёй/версией/последним прогоном (цена/находки).
    «Что изменит, где агенты развёрнуты → цена/результат». kind ∈ entity|recipe|skill|system|family."""
    kind = (kind or "").strip().lower()
    tid = (id or "").strip()
    if not tid:
        raise HTTPException(422, "нужен id узла")
    lin = await _asyncio.to_thread(ape.data_lineage)
    ents = lin.get("entities") or []
    ent_by = {e["entity"]: e for e in ents}

    aff_entities: set[str] = set()
    aff_skills: set[str] = set()
    aff_recipes: set[str] = set()

    def _from_entity(e: str) -> None:
        ent = ent_by.get(e)
        if not ent:
            return
        aff_entities.add(e)
        for r in ent.get("recipes") or []:
            aff_recipes.add(r.get("id"))
        for c in ent.get("consumers") or []:
            aff_skills.add(c.get("skill"))

    if kind == "entity":
        _from_entity(tid)
    elif kind == "recipe":
        for e in ents:
            if any((r.get("id") == tid) for r in e.get("recipes") or []):
                aff_recipes.add(tid)
                _from_entity(e["entity"])
    elif kind == "skill":
        aff_skills.add(tid)
        for e in ents:
            if any((c.get("skill") == tid) for c in e.get("consumers") or []):
                aff_entities.add(e["entity"])
    elif kind == "system":
        for c in lin.get("connectors") or []:
            if str(c.get("system_id") or c.get("system") or "") == tid:
                aff_recipes.add(c.get("id"))
        for e in ents:
            for r in e.get("recipes") or []:
                if str(r.get("system_id") or "") == tid:
                    aff_recipes.add(r.get("id"))
                    _from_entity(e["entity"])
    elif kind != "family":
        raise HTTPException(422, "kind ∈ entity|recipe|skill|system|family")

    # затронутые РАЗВЁРНУТЫЕ агенты: те, чей граф несёт затронутый навык (или семья=tid).
    # list_for отдаёт brief без графа → дедуплицируем до ПОСЛЕДНЕЙ версии на контракт и тянем полный агент.
    briefs = await agent_store.list_for(None)
    latest: dict[str, dict] = {}
    for b in briefs:
        cid = b.get("contract_audit_id") or b.get("id")
        if cid not in latest or (b.get("version") or 0) > (latest[cid].get("version") or 0):
            latest[cid] = b
    runs_index: dict[str, dict] = {}
    for r in await run_store.list_runs(limit=400):
        aid = r.get("agent_id")
        if aid and aid not in runs_index:      # первый = самый свежий (list_runs по убыванию)
            runs_index[aid] = r
    aff_agents = []
    tot_rub = 0.0
    tot_tokens = 0
    for b in latest.values():
        if not can_see_family(u, b.get("family")):
            continue
        a = await agent_store.get(b["id"]) or b
        askills = {n.get("skill") for n in (a.get("graph") or {}).get("nodes") or [] if n.get("skill")}
        hit = (kind == "family" and a.get("family") == tid) or bool(askills & aff_skills)
        if not hit:
            continue
        run = runs_index.get(a["id"]) or {}
        cost = run.get("cost") or {}
        rub = float(cost.get("rub") or 0)
        tokens = int(cost.get("input_tokens") or 0) + int(cost.get("output_tokens") or 0)
        tot_rub += rub
        tot_tokens += tokens
        aff_agents.append({
            "id": a["id"], "name": a.get("name"), "family": a.get("family"),
            "version": a.get("version"), "status": a.get("status"),
            "skills_hit": sorted(askills & aff_skills),
            "last_run": {"id": run.get("id"), "when": (run.get("created_at") or "").replace("T", " ")[:16],
                         "rub": round(rub, 4), "tokens": tokens,
                         "findings": (run.get("findings_summary") or {}).get("total")} if run else None})

    return {"target": {"kind": kind, "id": tid},
            "affected": {"entities": sorted(x for x in aff_entities if x),
                         "recipes": sorted(x for x in aff_recipes if x),
                         "skills": sorted(x for x in aff_skills if x),
                         "agents": aff_agents},
            "summary": {"agents": len(aff_agents), "entities": len(aff_entities),
                        "skills": len(aff_skills), "recipes": len(aff_recipes),
                        "est_rub_per_cycle": round(tot_rub, 4), "est_tokens_per_cycle": tot_tokens}}


@router.get("/api/data/entities")
async def data_entities(u: dict = Depends(user)) -> dict:
    """Сущности, которые реально существуют в среде: реестр схем + данные в хранилище + то, что
    объявили навыки.

    Реестр canonical-схем намеренно открыт: вертикаль заводит свои сущности без правки кода. Поэтому
    список для интерфейса нельзя брать из одного реестра — иначе редактор контракта не покажет
    сущность, с которой навык уже работает.
    """
    known = dict(ape.CANONICAL_SCHEMAS)
    out: dict[str, dict] = {e: {"entity": e, "known": True, "hint": v.get("hint") or "",
                                "required": v.get("required") or [], "rows": 0, "skills": 0}
                            for e, v in known.items()}
    # что лежит в хранилище: имя сущности = имя файла canonical store
    import glob as _glob
    import os as _os
    _dir = _os.path.dirname(ape._data_path("x"))
    for path in _glob.glob(_os.path.join(_dir, "*.jsonl")):
        e = _os.path.basename(path)[:-6]
        row = out.setdefault(e, {"entity": e, "known": False, "hint": "", "required": ["id"],
                                 "rows": 0, "skills": 0})
        try:
            row["rows"] = len(await _asyncio.to_thread(ape.data_query, e, limit=5000))
        except Exception:  # noqa: BLE001 — пустая или битая сущность не должна ломать список
            row["rows"] = 0
    # что объявили навыки — источниками данных и входами контракта
    for sid in list(ape.SKILLS):
        for ds in (ape.skill_datasources_resolved(sid) or []):
            e = ds.get("entity")
            if e:
                out.setdefault(e, {"entity": e, "known": False, "hint": ds.get("note") or "",
                                   "required": ["id"], "rows": 0, "skills": 0})["skills"] += 1
    for tpl in (await schema_store.all() or []):
        for bucket in ("required", "optional"):
            for it in ((tpl.get("inputs") or {}).get(bucket) or []):
                e = (it or {}).get("entity")
                if e:
                    out.setdefault(e, {"entity": e, "known": False, "hint": "", "required": ["id"],
                                       "rows": 0, "skills": 0})["skills"] += 1
    rows = sorted(out.values(), key=lambda r: (-r["rows"], -r["skills"], r["entity"]))
    return {"entities": rows, "count": len(rows)}


@router.get("/api/data/adapters")
def adapters(u: dict = Depends(user)) -> dict:
    """Каталог адаптеров с дескрипторами (label/category/src_fields/egress/badge/available)
    + сущности canonical. §5 селекты редактора строятся из этого (расширяем, не переделываем)."""
    return {"adapters": sorted(ape.SOURCE_ADAPTERS), "catalog": ape.adapter_catalog(),
            "entities": sorted(ape.CANONICAL_SCHEMAS)}


@router.get("/api/data/schema/{entity}")
def data_schema(entity: str, u: dict = Depends(user)) -> dict:
    """Data Contract сущности (обязательные поля). §5 редактор рецепта."""
    return ape.data_schema(entity)


async def _refresh_dataplane_cache() -> None:
    """Подтянуть рецепты/коннекторы из Postgres и инжектнуть в ape (общие для всех, переживают перенакат)."""
    try:
        # ФУНКЦИЯ, а не снимок: рецепты и коннекторы принадлежат арендатору, и готовый список
        # про него не знает. Поймано живой проверкой на проде: вендор, открывший данные клиента,
        # видел рецепты своего арендатора.
        ape.set_recipe_store(dataplane_records.рецепты)
        ape.set_connector_store(dataplane_records.коннекторы)
    except Exception:  # noqa: BLE001
        pass


def _attach_record_store() -> None:
    """Канонические записи — из Postgres, а не из файлов тома.

    Определения (рецепты, коннекторы) уехали в БД давно, записи оставались последним файловым
    слоем: вторая реплика на другом хосте видела пустую сущность, а том `abop_ape` был
    единственной точкой потери данных. Инъекция — тем же приёмом, что у рецептов: `ape` остаётся
    пригодным как CLI без сервера.
    """
    try:
        dataplane_records.init()
        ape.set_record_store(dataplane_records)
        obs.log_event("info", "dataplane.records.store", backend="pg" if dataplane_records._has_pg() else "mem")
    except Exception as ex:  # noqa: BLE001 — без стора работаем файлами, но это надо видеть
        obs.log_event("error", "dataplane.records.store_fail", error=f"{type(ex).__name__}: {ex}")


async def _backfill_records_from_files() -> int:
    """Одноразовый перенос ~/.ape/data/*.jsonl в Postgres: сущность пуста в БД — заливаем файл.

    Непустую не трогаем: повторный запуск сервера не должен дублировать записи, а ручная правка
    в БД не должна затираться старым файлом.
    """
    if not dataplane_records._has_pg():
        return 0
    import glob
    import json as _json
    каталог = os.path.join(os.path.expanduser("~"), ".ape", "data")
    перенесено = 0
    for путь in sorted(glob.glob(os.path.join(каталог, "*.jsonl"))):
        сущность = os.path.splitext(os.path.basename(путь))[0]
        try:
            if dataplane_records.count(сущность):
                continue
            with open(путь, encoding="utf-8") as f:
                записи = [_json.loads(x) for x in f if x.strip()]
            if not записи:
                continue
            dataplane_records.append(сущность, записи)
            перенесено += len(записи)
            obs.log_event("info", "dataplane.records.backfill", entity=сущность, records=len(записи))
        except Exception as ex:  # noqa: BLE001 — одна битая сущность не должна ронять старт
            obs.log_event("warn", "dataplane.records.backfill_fail", entity=сущность,
                          error=f"{type(ex).__name__}: {ex}")
    return перенесено


async def _backfill_dataplane_from_files() -> None:
    """Одноразовый перенос существующих файловых рецептов/коннекторов (~/.ape) в Postgres, чтобы
    демо-данные не исчезли при переходе на PG. Читает файлы, пока ape ещё в файловом режиме (стор
    не инжектнут); выполняется только если в PG соответствующая таблица пуста."""
    try:
        if not await dataplane_store.recipes_all():
            for name in await _asyncio.to_thread(ape.data_recipes):          # файловый режим
                try:
                    await dataplane_store.save_recipe(name, await _asyncio.to_thread(ape.data_load_recipe, name), editor="migrate")
                except Exception:  # noqa: BLE001
                    pass
        if not await dataplane_store.connectors_all():
            for c in await _asyncio.to_thread(ape.data_connectors):           # файловый режим
                if c.get("id"):
                    await dataplane_store.save_connector(c["id"], c, editor="migrate")
    except Exception:  # noqa: BLE001
        pass


# ── Коннекторы-инстансы (подключённые источники) — §5 таб «Коннекторы» ──
@router.get("/api/data/connectors")
async def connectors_list(u: dict = Depends(user)) -> dict:
    """Коннекторы + (если привязан system_id) резолв эндпоинта из реестра систем и флаг `allowed`
    для текущего отдела (ABAC). §5 таб «Коннекторы»."""
    key = access.scope_key(department=u.get("department"))
    out = []
    for c in await _asyncio.to_thread(ape.data_connectors):
        c = dict(c)
        sid = c.get("system_id")
        if sid:
            sysrec = await systems_store.get(sid)
            if sysrec:
                c["system"] = {"id": sid, "kind": sysrec["kind"], "egress": sysrec["egress"], "scope": sysrec.get("scope") or []}
                ok, reason = access.can_reach_system(key, sysrec)
                c["allowed"] = ok
                c["access_reason"] = reason
        out.append(c)
    return {"connectors": out}


@router.post("/api/data/connectors")
async def connector_save(body: dict, u: dict = Depends(user)) -> dict:
    """Подключить коннектор (сохранить инстанс источника в Postgres). §5 [＋ подключить]. Если задан
    system_id — эндпоинт/egress берутся из реестра систем (не хардкод URL); ABAC-гейт по scope."""
    require_level(u, "manager")
    if not str((body or {}).get("title", "")).strip():
        raise HTTPException(422, "нужен title коннектора")
    card = await _asyncio.to_thread(ape.build_connector, body)
    # slava/vector-коннектор = KNOWLEDGE-источник (не canonical): храним корпус (collection/family/top_k)
    if card.get("adapter") in ("slava", "vector"):
        fam = str((body or {}).get("family", "")).strip()
        col = str((body or {}).get("collection", "")).strip() or (slava.fam_collection(fam) if fam else "")
        card["corpus"] = {"collection": col, "family": fam or None, "top_k": int((body or {}).get("top_k") or 4)}
        card["kind_role"] = "knowledge"
    sid = str((body or {}).get("system_id", "")).strip()
    if sid:
        sysrec = await systems_store.get(sid)
        if not sysrec:
            raise HTTPException(422, f"нет системы «{sid}» в реестре")
        card["system_id"] = sid
        # эндпоинт из реестра: base_url + относительный путь (если target не задан явно)
        path = str((body or {}).get("path", "")).strip()
        if not card.get("target") or path:
            card["target"] = (sysrec.get("base_url") or "").rstrip("/") + ("/" + path.lstrip("/") if path else "")
        card["egress"] = sysrec.get("egress")
    editor = u.get("name") or u.get("sub") or "dev"
    await dataplane_store.save_connector(card["id"], card, editor=editor)
    await _refresh_dataplane_cache()
    await cachebus.notify("dataplane")
    await audit_store.record(editor, "data.connector", card["id"],
                             {"adapter": card.get("adapter"), "system_id": card.get("system_id")})
    return card


@router.post("/api/data/connectors/test")
def connector_test(body: dict, u: dict = Depends(user)) -> dict:
    """Тест-прогон коннектора: читает несколько строк источника (без записи). §5 [Тест-прогон]."""
    spec = body or {}
    if not str(spec.get("target") or spec.get("path") or "").strip():
        raise HTTPException(422, "не указан источник (путь к файлу / URL / строка подключения)")
    if not spec.get("adapter") and spec.get("kind"):
        spec = dict(spec, adapter=spec.get("kind"))
    try:
        return ape.data_test_connector(spec)
    except KeyError as ex:  # noqa: BLE001 — UX-аудит: «KeyError: 'path'» уходил в UI как есть
        raise HTTPException(422, f"в описании источника не хватает поля {ex}")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, f"источник не читается: {type(ex).__name__}: {str(ex)[:200]}")


# ── Рецепты (источник → canonical) — §5 таб «Рецепты» ──
@router.get("/api/data/recipes")
def recipes_list(u: dict = Depends(user)) -> dict:
    return {"recipes": ape.data_recipes_cards()}


@router.get("/api/data/recipes/{name}")
async def recipe_get(name: str, u: dict = Depends(user)) -> dict:
    try:
        return await _asyncio.to_thread(ape.data_load_recipe, name)
    except (OSError, ValueError):
        raise HTTPException(404, "нет рецепта")


@router.post("/api/data/recipes")
async def recipe_save(body: dict, u: dict = Depends(user)) -> dict:
    """Сохранить рецепт (нормализует UI-форму в canonical) в Postgres. §5 [Сохранить]."""
    require_level(u, "manager")
    name = str((body or {}).get("recipe") or (body or {}).get("title", "")).strip()
    if not name:
        raise HTTPException(422, "нужно имя рецепта")
    try:
        r = ape.normalize_recipe(name, body)
    except Exception as ex:  # noqa: BLE001 — любой сбой нормализации → 400, не 500
        raise HTTPException(400, f"не удалось сохранить рецепт: {ex}")
    editor = u.get("name") or u.get("sub") or "dev"
    await dataplane_store.save_recipe(r["recipe"], r, editor=editor)
    await _refresh_dataplane_cache()
    await cachebus.notify("dataplane")
    await audit_store.record(editor, "data.recipe", r["recipe"], {"entity": r.get("entity")})
    return r


@router.delete("/api/data/recipes/{name}")
async def recipe_delete(name: str, u: dict = Depends(user)) -> dict:
    """Удалить рецепт (manager+). UX-аудит W-H5: раньше кнопка удаляла только в UI."""
    require_level(u, "manager")
    ok = await dataplane_store.delete_recipe(name)
    if not ok:
        raise HTTPException(404, "нет такого рецепта")
    await _refresh_dataplane_cache()
    await cachebus.notify("dataplane")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "data.recipe.delete", name, {}, severity="warn")
    return {"deleted": True, "recipe": name}


@router.delete("/api/data/connectors/{cid}")
async def connector_delete(cid: str, u: dict = Depends(user)) -> dict:
    """Удалить коннектор-инстанс (manager+)."""
    require_level(u, "manager")
    ok = await dataplane_store.delete_connector(cid)
    if not ok:
        raise HTTPException(404, "нет такого коннектора")
    await _refresh_dataplane_cache()
    await cachebus.notify("dataplane")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "data.connector.delete", cid, {}, severity="warn")
    return {"deleted": True, "connector": cid}


@router.post("/api/compute")
async def compute_run(body: dict, u: dict = Depends(user)) -> dict:
    """Whitelisted-расчёт над Data Plane (compute-tool): stats/group_by/top/reconcile. Числа считает КОД
    (детерминированно), никакого произвольного кода. Возвращает {op, result, chart?, chart_svg?}."""
    try:
        return await _asyncio.to_thread(compute.run_html, body or {})
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(500, f"compute: {type(ex).__name__}: {ex}")


@router.post("/api/data/recipe/preview")
def recipe_preview(body: dict, u: dict = Depends(user)) -> dict:
    """Dry-run рецепта на выборке БЕЗ записи. §5 [Проверить] → предпросмотр canonical + счётчики."""
    try:
        limit = int((body or {}).get("limit", 20) or 20)
        return ape.data_preview(body, limit=limit)
    except Exception as ex:  # noqa: BLE001 — сбой адаптера/нормализации/ввода → 400 с текстом, НИКОГДА не 500
        raise HTTPException(400, str(ex))


@router.post("/api/data/recipes/{name}/run")
async def recipe_run(name: str, body: dict | None = None, u: dict = Depends(user)) -> dict:
    """Применить сохранённый рецепт и записать в canonical store. §5 публикация.

    Тело (необязательно): `{"reset": true}` — перечитать сущность НАЧИСТО. Нужно, когда записи в
    источнике УДАЛЯЛИ: у удаления нет новой версии, и без этого режима снесённая в трекере задача
    остаётся в Data Plane, а агент считает её существующей.
    """
    reset = bool((body or {}).get("reset"))
    try:
        entity, written, dropped, invalid = await _asyncio.to_thread(ape.data_run, name, reset)
    except Exception as ex:  # noqa: BLE001 — сбой источника/рецепта → 400
        raise HTTPException(400, str(ex))
    if reset:
        await audit_store.record(u.get("name") or u.get("sub") or "dev", "data.recipe.reset", name,
                                 {"entity": entity, "written": written})
    return {"entity": entity, "written": written, "dropped": dropped, "invalid": invalid,
            "mode": "начисто" if reset else "дозапись"}


@router.post("/api/data/recipes/{name}/rebind")
async def recipe_rebind(name: str, body: dict, u: dict = Depends(user)) -> dict:
    """Перепривязка рецепта на графе «Данные»: сменить целевую сущность (entity) и опц. запустить.
    Тело: {entity: <новая сущность>, run?: bool}. Меняет recipe.entity+emit.schema, сохраняет,
    при run=true сразу пишет в canonical store. Питает drag-перепривязку в графе Data Plane."""
    require_level(u, "manager")  # перепривязка = мутация Data Plane (analyst read-only)
    entity = str((body or {}).get("entity", "")).strip()
    if not entity:
        raise HTTPException(422, "нужна целевая entity")
    if entity not in ape.CANONICAL_SCHEMAS:
        raise HTTPException(422, f"неизвестная сущность {entity!r} (нет в canonical schemas)")
    try:
        r = await _asyncio.to_thread(ape.data_load_recipe, name)
    except (OSError, ValueError):
        raise HTTPException(404, "нет рецепта")
    r["entity"] = entity
    r.setdefault("emit", {})["schema"] = entity
    saved = ape.normalize_recipe(name, r)
    editor = u.get("name") or u.get("sub") or "dev"
    await dataplane_store.save_recipe(saved["recipe"], saved, editor=editor)
    await _refresh_dataplane_cache()   # чтобы data_run ниже прочитал перепривязанный рецепт
    await cachebus.notify("dataplane")
    out = {"recipe": saved.get("recipe"), "entity": entity, "rebound": True}
    await audit_store.record(editor, "data.rebind", saved.get("recipe"), {"entity": entity})
    if (body or {}).get("run"):
        try:
            ent, written, dropped, invalid = await _asyncio.to_thread(ape.data_run, saved.get("recipe"))
            out.update({"ran": True, "written": written, "dropped": dropped, "invalid": invalid})
        except Exception as ex:  # noqa: BLE001 — перепривязка удалась, прогон нет → сообщаем
            out.update({"ran": False, "run_error": str(ex)})
    return out


@router.get("/api/data/query/{entity}")
def data_query(entity: str, limit: int = 30, u: dict = Depends(user)) -> dict:
    """Чтение canonical store (только свежие). §5 предпросмотр."""
    return {"entity": entity, "records": ape.data_query(entity, limit=limit)}


# ── Раскладка канвы (координаты узлов) в Postgres, НЕ localStorage (директива владельца) ──
