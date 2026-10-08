# -*- coding: utf-8 -*-
"""Шаблоны: вид отчёта и структура извлечения — в базе, а не в сборке.

Домен вынесен из монолита (ADR-052, docs/RASKLADKA_SERVERA.md). Два разных шаблона, которые легко
спутать:

* **шаблон отчёта** (`report-templates`) — как документ ВЫГЛЯДИТ: HTML, CSS, pdf-опции и раскладка
  бланка (порядок разделов и графы вертикали);
* **шаблон извлечения** (`schema-templates`) — какую СТРУКТУРУ навык обязан вернуть: JSON Schema,
  по которой потом строятся таблицы отчёта.

Переезд ждал выноса слоя сборки контекста отчёта в `server/report_context.py`: предпросмотр шаблона
зовёт тот же контекст, что карточка прогона, и пока он лежал в приложении, домен притащил бы
приложение за собой.

Правило домена: встроенный шаблон не удаляется, а **сбрасывается** к поставочному виду. Удалить
можно только свой — иначе откатить неудачную правку бланка было бы нечем.

Зависимости наружу: `server/deps`, сторы и `report_context`; `web_api` не импортируется.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from .. import agent_store, audit_store, charts, delivery as delivery_mod
from .. import report_store, run_store, schema_store, skill_contract, skill_templates, systems_store
from ..deps import require_level, user
from ..report_context import _labels_ctx, _report_context

router = APIRouter()


# ── Шаблоны отчётов (Schema-driven: вид отчёта в БД, меняется без передеплоя) ──
@router.get("/api/report-templates")
async def report_templates_list(u: dict = Depends(user)) -> dict:
    return {"templates": await report_store.all()}


@router.get("/api/report-templates/{tid}")
async def report_template_get(tid: str, u: dict = Depends(user)) -> dict:
    t = await report_store.get(tid)
    if not t:
        raise HTTPException(404, "нет такого шаблона отчёта")
    return t


@router.post("/api/report-templates/{tid}")
async def report_template_save(tid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Создать/править шаблон отчёта: HTML+CSS+pdf_options и РАСКЛАДКУ бланка. Admin-уровень.

    Раскладка (порядок разделов и графы документа вертикали) и область применения переносятся из
    существующей записи, если их не передали: иначе сохранение из редактора, где видно только HTML,
    стирало бы устройство документа — у бланка в HTML всего одна строка `{{blank}}`.
    """
    require_level(u, "admin")
    import re as _re
    tid = _re.sub(r"[^a-z0-9_-]", "", str(tid).strip().lower())
    if not tid:
        raise HTTPException(422, "нужен id шаблона (slug a-z0-9_-)")
    existing = await report_store.get(tid)
    editor = u.get("name") or u.get("sub") or "dev"
    layout = (body or {}).get("layout")
    if layout is None:
        layout = (existing or {}).get("layout") or []
    else:
        from .. import report_form
        if not isinstance(layout, list) or not all(isinstance(x, dict) for x in layout):
            raise HTTPException(422, "раскладка — список блоков (объектов)")
        unknown = sorted({str(x.get("t") or "") for x in layout} - set(report_form.BLOCK_NAMES))
        if unknown:
            raise HTTPException(422, "неизвестные блоки раскладки: " + ", ".join(unknown)
                                + " · доступны: " + ", ".join(sorted(report_form.BLOCK_NAMES)))
    card = await report_store.save(tid, {"name": (body or {}).get("name") or tid,
                                         "html": (body or {}).get("html") or (existing or {}).get("html") or "",
                                         "css": (body or {}).get("css") if (body or {}).get("css") is not None else (existing or {}).get("css") or "",
                                         "pdf_options": (body or {}).get("pdf_options") or (existing or {}).get("pdf_options") or {},
                                         "layout": layout,
                                         "for_skills": (body or {}).get("for_skills")
                                         if (body or {}).get("for_skills") is not None
                                         else (existing or {}).get("for_skills") or []},
                                   editor=editor, builtin=bool(existing and existing.get("builtin")))
    await audit_store.record(editor, "report_template.save", tid, {"name": card.get("name")})
    return card


@router.post("/api/report-templates/{tid}/preview")
async def report_template_preview(tid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Превью рендера шаблона на демо-данных (или переданных). Возвращает готовый HTML."""
    tpl = await report_store.get(tid)
    if not tpl:
        raise HTTPException(404, "нет такого шаблона")
    demo = {"title": "Демо-отчёт", "agent": "Пример агента", "date": "01.01.2026",
            "verdict": "✓ пройден · автономия A1 · волн 3",
            "findings_total": 2, "investigations_total": 1,
            "by_class": "<div class='badges'><span class='b A'>A: 1</span><span class='b B'>B: 0</span>"
                        "<span class='b C'>C: 1</span><span class='b D'>D: 0</span></div>",
            "charts": charts.render_spec({"type": "bar", "title": "Находки по классам критичности",
                                          "x": ["A", "B", "C", "D"], "series": [{"name": "шт", "data": [1, 6, 1, 2]}]}),
            "findings": "<div class='fnd'><span class='cls'>A</span><b>НДС не сходится с декларацией</b> — "
                        "расхождение 110 000 ₽<span class='norm'>§ НК РФ ст.171</span></div>"
                        "<div class='fnd'><span class='cls'>C</span><b>Нет счёта-фактуры</b></div>",
            "investigations": "<div class='inv'><span class='sev'>высокая</span> <span class='sym'>INV-1 — "
                              "выручка без реализации</span><div class='chain'>реализация: ✓ → взаиморасчёты: ✗</div>"
                              "<span class='delta'>расхождение Δ 110000 ₽</span></div>",
            "skills": "<div class='sk'><h3>mail-triage</h3><div class='task'>тема: Согласовать счёт СК-902 · "
                      "срок: 25.09 · приоритет: высокий</div><div class='task'>тема: Проверить БФТ</div></div>",
            "deliveries": "<div class='dl'>redmine → #— · awaiting_hitl</div>"}
    ctx = {**demo, **((body or {}).get("context") or {})}
    # Бланк вертикали нечем показать на демо-строках: его графы берутся из структурированных ответов
    # навыков. Если дали прогон — рендерим на нём, это и есть честное превью формы.
    rid = str((body or {}).get("run_id") or "")
    # Порядок такой: заданный прогон → последний подходящий прогон этой формы → образец по
    # json_schema её навыков. Иначе превью бланка показывает общий вид, и в интерфейсе форма
    # выглядит неизменившейся — именно так это и выглядело.
    run, note = None, ""
    if rid:
        run = await run_store.get(rid)
        if not run:
            raise HTTPException(404, "нет такого прогона")
    elif tpl.get("layout"):
        run = await _last_run_for_skills(tpl.get("for_skills") or [])
        if run:
            note = "Превью на прогоне " + str(run.get("run_id") or run.get("id") or "")
        else:
            run = _example_run(tpl.get("for_skills") or [])
            note = ("Превью на ОБРАЗЦЕ значений по схемам навыков формы: это устройство документа, "
                    "а не данные прогона")
    if run is not None:
        ag = await agent_store.get(run.get("agent_id") or "") or {"name": run.get("agent_name") or tpl.get("name") or ""}
        ctx = {**(await asyncio.to_thread(_report_context, ag, run)),
               **(await _labels_ctx(run)), **((body or {}).get("context") or {})}
    html = report_store.render(tpl, ctx)
    if note:
        import html as _h
        html = html.replace("<body>", "<body><div class='meth' style='margin:0 0 14px'>"
                            + _h.escape(note) + "</div>", 1)
    return {"html": html, "note": note}


async def _last_run_for_skills(skills) -> dict | None:
    """Последний прогон, в котором работал хоть один навык формы: честное превью на реальных данных."""
    want = {str(s) for s in (skills or []) if s}
    if not want:
        return None
    try:
        rows = await run_store.list_runs(limit=60)
    except Exception:  # noqa: BLE001 — журнал недоступен: обойдёмся образцом по схеме
        return None
    for r in rows:
        full = await run_store.get(str(r.get("run_id") or r.get("id") or ""))
        outs = (full or {}).get("skill_outputs") or []
        if any(str(o.get("skill")) in want and o.get("structured") for o in outs):
            return full
    return None


def _example_run(skills) -> dict:
    """Прогон-образец по json_schema навыков формы: показывает устройство бланка без данных стенда."""
    from .. import report_form
    outs = []
    try:
        tpls = skill_templates.load_all()
    except Exception:  # noqa: BLE001
        tpls = {}
    for sid in (skills or []):
        sch = (tpls.get(str(sid)) or {}).get("json_schema") or {}
        ex = report_form.example_from_schema(sch)
        if isinstance(ex, dict) and ex:
            outs.append({"skill": str(sid), "structured": ex})
    return {"run_id": "образец", "started_by": "превью", "skill_outputs": outs}


@router.post("/api/report-templates/{tid}/reset")
async def report_template_reset(tid: str, u: dict = Depends(user)) -> dict:
    """Вернуть шаблон отчёта к версии из поставки (reports/<id>.html).

    Посев намеренно не перезаписывает шаблоны, которые правили в интерфейсе: иначе чужая правка
    пропадала бы при каждом обновлении. Но когда форма отчёта согласована заново, нужен штатный
    способ вернуться к ней — без похода в базу руками.
    """
    require_level(u, "admin")
    files = report_store.load_files()
    spec = files.get(tid)
    if not spec:
        raise HTTPException(404, f"в поставке нет шаблона «{tid}»")
    card = await report_store.save(tid, spec, editor="seed", builtin=True)
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "report_template.reset", tid, {})
    return {"ok": True, "id": tid, "name": card.get("name"),
            "note": "шаблон возвращён к версии из поставки"}


@router.delete("/api/report-templates/{tid}")
async def report_template_delete(tid: str, u: dict = Depends(user)) -> dict:
    require_level(u, "admin")
    ok = await report_store.delete(tid)
    if not ok:
        raise HTTPException(400, "нельзя удалить (нет такого или встроенный шаблон)")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "report_template.delete", tid, {})
    return {"ok": True}


# ── Шаблоны извлечения (JSON Schema): структура данных навыка из БД (Schema-driven extraction) ──
@router.get("/api/schema-templates")
async def schema_templates_list(u: dict = Depends(user)) -> dict:
    return {"templates": await schema_store.all()}


@router.get("/api/schema-templates/{tid}")
async def schema_template_get(tid: str, u: dict = Depends(user)) -> dict:
    t = await schema_store.get(tid)
    if not t:
        raise HTTPException(404, "нет такого шаблона извлечения")
    return t


@router.post("/api/schema-templates/import")
async def schema_templates_import(body: dict, u: dict = Depends(user)) -> dict:
    """Импорт шаблонов извлечения в БД (источник правды в рантайме) БЕЗ пересборки образа:
    body {templates:[{id,name,instruction,json_schema,max_tokens,tool_steps,delivery,inputs,produces,slots}],
    source:"repo"|"manual", force:bool}. Контракт (inputs/produces/slots) переносится вместе со схемой.
    source=repo → builtin (маркер отпечатка, посев идемпотентен); ручные (builtin=false) без force не перезаписываются.
    Каждая схема проверяется на strict-совместимость. Admin-уровень."""
    require_level(u, "admin")
    import re as _re
    items = (body or {}).get("templates") or []
    if not isinstance(items, list) or not items or len(items) > 200:
        raise HTTPException(422, "templates: список из 1..200 шаблонов")
    source = str((body or {}).get("source") or "manual")
    force = bool((body or {}).get("force"))
    editor = u.get("name") or u.get("sub") or "dev"
    imported, skipped, errors = [], [], {}
    _known_systems = {str(x.get("id")) for x in (await systems_store.all() or []) if x.get("id")}
    for raw in items:
        tid = _re.sub(r"[^a-z0-9_-]", "", str((raw or {}).get("id") or "").strip().lower())
        if not tid:
            errors["?"] = ["нужен id (slug a-z0-9_-)"]; continue
        errs = skill_templates.validate_schema((raw or {}).get("json_schema"))
        if not (raw or {}).get("instruction"):
            errs.append("нужна instruction")
        errs += delivery_mod.validate_delivery((raw or {}).get("delivery"), systems=_known_systems)
        if errs:
            errors[tid] = errs; continue
        cur = await schema_store.get(tid)
        if cur and not cur.get("builtin") and not force:
            skipped.append(tid); continue
        # Контракт навыка (inputs/produces/slots) и лимит шагов инструментов переносятся ВМЕСТЕ со
        # схемой. Без них заливка молча обнуляла контракты всего каталога — а на них держатся план по
        # контрактам, проверка покрытия входов, доска прогона и арбитраж.
        _CARRY = ("name", "instruction", "json_schema", "max_tokens", "tool_steps",
                  "delivery", "inputs", "produces", "slots")
        card = {"name": raw.get("name") or tid, "instruction": raw.get("instruction"), "json_schema": raw["json_schema"],
                "max_tokens": int(raw.get("max_tokens") or 0) or None,
                "tool_steps": raw.get("tool_steps"),
                "delivery": raw.get("delivery") if isinstance(raw.get("delivery"), dict) and raw.get("delivery") else None,
                "inputs": raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {},
                "produces": raw.get("produces") if isinstance(raw.get("produces"), (dict, list)) else {},
                "slots": raw.get("slots") if isinstance(raw.get("slots"), list) else [],
                "fingerprint": skill_templates.fingerprint({k: raw[k] for k in _CARRY if k in raw})}
        await skill_templates.upsert(schema_store, tid, card, editor=editor, builtin=(source == "repo"))
        imported.append(tid)
    await audit_store.record(editor, "schema_template.import", source, {"imported": len(imported), "skipped": len(skipped), "errors": len(errors)})
    return {"imported": imported, "skipped": skipped, "errors": errors}


@router.post("/api/schema-templates/{tid}/reset")
async def schema_template_reset(tid: str, u: dict = Depends(user)) -> dict:
    """Сбросить шаблон к версии из репозитория (skills/<id>/template.json внутри образа)."""
    require_level(u, "admin")
    card = skill_templates.load_one(tid)
    if not card:
        raise HTTPException(404, "в репозитории нет шаблона для этого навыка")
    editor = u.get("name") or u.get("sub") or "dev"
    out = await skill_templates.upsert(schema_store, tid, card, editor=editor, builtin=True)
    await audit_store.record(editor, "schema_template.reset", tid, {})
    return out


@router.post("/api/schema-templates/{tid}")
async def schema_template_save(tid: str, body: dict, u: dict = Depends(user)) -> dict:
    """Создать/править шаблон извлечения (JSON Schema + инструкция-парсер + max_tokens). Навык ссылается на него
    полем schema_template_id → ЛЛМ раскладывает данные строго по схеме. Ручная правка снимает builtin:
    посев из репо её больше не перекрывает (вернуть — /reset). Admin-уровень."""
    require_level(u, "admin")
    import re as _re
    tid = _re.sub(r"[^a-z0-9_-]", "", str(tid).strip().lower())
    if not tid:
        raise HTTPException(422, "нужен id шаблона (slug a-z0-9_-)")
    existing = await schema_store.get(tid)
    sch = (body or {}).get("json_schema")
    if sch is None:
        sch = (existing or {}).get("json_schema") or {}
    if not isinstance(sch, dict):
        raise HTTPException(422, "json_schema должен быть объектом JSON Schema")
    errs = skill_templates.validate_schema(sch)
    if errs:
        raise HTTPException(422, "схема не strict-совместима: " + "; ".join(errs[:5]))
    instr = (body or {}).get("instruction")
    instr = skill_templates.strip_markers(instr if instr is not None else (existing or {}).get("instruction") or "")
    mt = (body or {}).get("max_tokens")
    mt = int(mt) if mt not in (None, "") else (existing or {}).get("max_tokens")
    # Шаги инструментов навыка. Ноль осмыслен — «в инструменты не ходить», поэтому пустую строку
    # (вернуть к значению среды) отличаем от нуля явной проверкой, а не через «или».
    _ts = (body or {}).get("tool_steps") if "tool_steps" in (body or {}) else (existing or {}).get("tool_steps")
    if str(_ts if _ts is not None else "").strip() == "":
        _ts = None
    else:
        try:
            _ts = int(_ts)
        except (TypeError, ValueError):
            raise HTTPException(422, "шаги инструментов — целое число от 0 до 8")
        if not (0 <= _ts <= 8):
            raise HTTPException(422, "шаги инструментов — от 0 до 8: больше восьми навык ходит по кругу")
    if mt is not None and not (256 <= mt <= 16000):
        raise HTTPException(422, "max_tokens: 256..16000")
    dl = (body or {}).get("delivery") if "delivery" in (body or {}) else (existing or {}).get("delivery")
    dl = dl if isinstance(dl, dict) and dl else None
    if dl:
        _known = {str(x.get("id")) for x in (await systems_store.all() or []) if x.get("id")}
        _derr = delivery_mod.validate_delivery(dl, systems=_known)
        if _derr:
            raise HTTPException(422, "delivery: " + "; ".join(_derr))
    editor = u.get("name") or u.get("sub") or "dev"
    # слоты: какой предмет работы навык обязан получить до запуска (проект, контрагент, период)
    sl = (body or {}).get("slots") if "slots" in (body or {}) else (existing or {}).get("slots")
    sl = sl if isinstance(sl, list) else []
    for _s in sl:
        if not isinstance(_s, dict) or not _s.get("name") or not _s.get("entity"):
            raise HTTPException(422, "слот описывается объектом с полями name и entity")
    # контракт навыка: чем кормить и что отдаёт. Проверяем до сохранения, иначе сборка агента
    # получит описание, по которому нельзя посчитать покрытие входов.
    inp = (body or {}).get("inputs") if "inputs" in (body or {}) else (existing or {}).get("inputs")
    prod = (body or {}).get("produces") if "produces" in (body or {}) else (existing or {}).get("produces")
    inp = inp if isinstance(inp, dict) else {}
    prod = prod if isinstance(prod, (dict, list)) else {}   # навык может отдавать несколько списков
    _cerr = skill_contract.validate_contract({"inputs": inp, "produces": prod, "json_schema": sch, "slots": sl})
    if _cerr:
        raise HTTPException(422, "контракт навыка: " + "; ".join(_cerr[:5]))
    card = await schema_store.save(tid, {"name": (body or {}).get("name") or (existing or {}).get("name") or tid,
                                         "json_schema": sch, "instruction": instr, "max_tokens": mt,
                                         "tool_steps": _ts,
                                         "delivery": dl, "slots": sl, "inputs": inp, "produces": prod},
                                   editor=editor, builtin=False)
    await audit_store.record(editor, "schema_template.save", tid, {"name": card.get("name"), "was_builtin": bool(existing and existing.get("builtin"))})
    return card


@router.delete("/api/schema-templates/{tid}")
async def schema_template_delete(tid: str, u: dict = Depends(user)) -> dict:
    require_level(u, "admin")
    ok = await schema_store.delete(tid)
    if not ok:
        raise HTTPException(400, "нельзя удалить (нет такого или встроенный шаблон)")
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "schema_template.delete", tid, {})
    return {"ok": True}
