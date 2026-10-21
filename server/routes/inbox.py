# -*- coding: utf-8 -*-
"""Входящие и здоровье связей: одно место, где решения ждут человека, и одно — где видно связи.

Две мысли, подсмотренные у финансовых агентных платформ и верные для любой вертикали.

**Входящие.** Решение, которое ждёт человека, не должно жить в журнале прогонов: чтобы его найти,
надо помнить, какой прогон его родил. Здесь оно лежит рядом с доказательством — цитатой,
основанием, посчитанной величиной, — и ответ «да/нет» требует одного движения, а не раскопок.

**Здоровье связей.** Вопрос «откуда это число» имеет право на короткий ответ: какие системы
подключены, когда последний раз читались, где вместо живого источника стоит заглушка. Данные для
этого у нас уже есть — реестр систем, происхождение записей Data Plane и пометка «заглушка»;
не хватало места, где это сведено.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from .. import dataplane_records as dr
from .. import findings_registry as реестр
from .. import hitl_store, systems_store
from ..deps import can_see_family, require_level, user

router = APIRouter()


@router.get("/api/inbox")
async def inbox(project: str = "", process: str = "", u: dict = Depends(user)) -> dict:
    """Что ждёт человека: подтверждения внешних действий и находки без решения.

    Два потока в одном месте, но не в одной куче: заявка на внешнее действие требует «отправить
    или нет», находка — «это проблема или норма». Путать их нельзя, поэтому они разделены полем
    `вид`, а не сведены в один список.

    Имена параметров запроса — латиницей, и это не вкусовщина: кириллическое имя уезжает
    процентной кодировкой у каждого клиента, а стандартный клиент Python на нём просто падает.
    На этом уже обожглись в очереди подтверждений.
    """
    заявки = []
    for з in await hitl_store.list_pending():
        if not can_see_family(u, з.get("family")):
            continue
        payload = з.get("payload") or {}
        заявки.append({
            "вид": "подтверждение",
            "id": з.get("id"),
            "что": з.get("title") or з.get("subject") or "внешнее действие",
            "куда": str(з.get("to") or payload.get("system") or ""),
            "прогон": з.get("run_id") or "",
            "семья": з.get("family") or "",
            "кому": з.get("addressed_to") or "",
            "когда": з.get("created_at") or "",
            "доказательство": str(payload.get("preview") or payload.get("html") or "")[:400],
        })
    находки = [{
        "вид": "находка",
        "id": н.get("id"),
        "что": н.get("код"),
        "предмет": н.get("предмет"),
        "название": н.get("название"),
        "серьёзность": н.get("серьёзность"),
        "величина": н.get("величина"),
        "основание": н.get("основание"),
        "доказательство": н.get("доказательство"),
        "проект": н.get("проект"),
        "процесс": н.get("процесс"),
        "когда": н.get("впервые"),
        "прогон": н.get("прогон"),
    } for н in реестр.список(project, process, состояние="найдено")]
    return {"заявки": заявки, "находки": находки,
            "всего": len(заявки) + len(находки),
            "сводка": реестр.сводка(project, process)}


@router.get("/api/registry/findings")
async def findings_list(project: str = "", process: str = "", state: str = "",
                        open_only: int = 0, u: dict = Depends(user)) -> dict:
    """Реестр находок между прогонами: что нашли, что признали, что перестало воспроизводиться.

    Путь свой, а не `/api/findings`: тот занят журналом находок пилота 1С, и две ручки на одном пути означали бы, что одна из них мёртвая — молча.
    """
    строки = реестр.список(project, process, state, bool(open_only))
    return {"findings": строки, "всего": len(строки),
            "сводка": реестр.сводка(project, process)}


@router.post("/api/registry/findings/{finding_id}/decide")
async def findings_decide(finding_id: str, body: dict | None = None,
                          u: dict = Depends(user)) -> dict:
    """Решение человека по находке: подтвердить или отклонить.

    Отказ — такой же законный исход, как согласие, и он записывается так же: по отклонённым
    находкам правят правила, а не делают вид, что их не было. Уровень «manager» — потому что
    решение по находке меняет метрику пользы, а её нельзя править кому угодно.
    """
    require_level(u, "manager")
    тело = body or {}
    состояние = str(тело.get("состояние") or "").strip()
    try:
        итог = реестр.решение(finding_id, состояние=состояние,
                              кем=str(u.get("name") or u.get("sub") or "—"),
                              почему=str(тело.get("почему") or ""))
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    if not итог:
        raise HTTPException(404, "находки с таким id в реестре нет")
    return {"ok": True, "finding": итог, "сводка": реестр.сводка(
        str(итог.get("проект") or ""), str(итог.get("процесс") or ""))}


@router.get("/api/health/links")
async def health_links(u: dict = Depends(user)) -> dict:
    """Здоровье связей: системы реестра, свежесть данных и где стоит заглушка.

    «Когда последний раз читали» берётся из происхождения записей Data Plane, а не из журнала
    прогонов: журнал говорит, что рецепт запускали, а происхождение — что данные доехали. Это
    разные факты, и в пилоте путаница между ними стоит доверия.
    """
    системы = await systems_store.all() or []
    свежесть, заглушки = {}, {}
    for сущность in ("issue", "commit", "comment", "code_quality", "roadmap_item", "transcript",
                     "plan_line", "portfolio_line", "flow_line", "stage_rule", "plan_rule",
                     "metric_rule", "finding"):
        try:
            строки = dr.query(сущность, None, None, 2000)
        except Exception:  # noqa: BLE001 — сущности может не быть вовсе
            continue
        if not строки:
            continue
        метки = [float((с.get("provenance") or {}).get("fetched_at") or 0) for с in строки]
        рецепты = sorted({str((с.get("provenance") or {}).get("recipe") or "") for с in строки})
        свежесть[сущность] = {
            "записей": len(строки),
            "последняя_загрузка": (time.strftime("%Y-%m-%d %H:%M",
                                                 time.localtime(max(метки))) if any(метки) else ""),
            "часов_назад": (round((time.time() - max(метки)) / 3600.0, 1) if any(метки) else ""),
            "рецепты": ", ".join(р for р in рецепты if р),
        }
        помечены = [с for с in строки
                    if "заглушк" in str(с.get("источник") or "").lower()]
        if помечены:
            заглушки[сущность] = len(помечены)
    строки_систем = []
    for с in системы:
        ид = str(с.get("id") or "")
        строки_систем.append({
            "система": ид, "вид": с.get("kind"), "адрес": с.get("base_url"),
            "роль": с.get("role") or "", "egress": с.get("egress") or "",
            "ключ": "есть" if str(с.get("auth_ref") or "") else "не нужен",
            "заглушка": "да" if "заглушк" in str(с.get("note") or "").lower() else "нет",
            "доступна_отделам": ", ".join(с.get("scope") or []) or "всем",
            "заметка": str(с.get("note") or "")[:200],
        })
    return {"системы": строки_систем, "данные": свежесть, "заглушки": заглушки,
            "итог": {"систем": len(строки_систем),
                     "сущностей_с_данными": len(свежесть),
                     "сущностей_с_заглушками": len(заглушки),
                     "заглушек_всего": sum(заглушки.values())}}
