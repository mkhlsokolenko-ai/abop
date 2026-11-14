# -*- coding: utf-8 -*-
"""Общая доска: долгая память человека и отдела.

Факт на доске имеет автора-человека, даже если текст готовил агент: решение вынести
что-то в общую память принимает человек. Доска переживает прогоны и видна отделу.

Слои (шаг 4 «Отделов агентов», docs/OTDELY_AGENTOV.md §5): working → validated → cross → user.
working пишут исполнители (агенты и люди); продвигать в validated/cross — решение супервайзера,
а пока роль не собрана (шаг 5) — человека manager+. Переиспользование расчётов берёт только
validated/cross по ТОЧНОМУ ключу (организация+период+правило+версия политики) — черновик чужого
отдела не истина, «похожее» не переиспользуем вовсе (замер эмбеддера 13.11: 83 % в первое место).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import audit_store, blackboard

from ..deps import _sub, can_see_family, require_level, user

from .runs import _board_scope

router = APIRouter()


@router.get("/api/board")
async def board_read(kind: str = "user", family: str = "", stale: bool = False,
                     слой: str = "", u: dict = Depends(user)) -> dict:
    """Факты доски: личной или отдела. Просроченные не отдаём, пока не попросят явно.
    `слой` — фильтр по ступени доверия; без него отдаём все слои."""
    scope = _board_scope(kind, u, family)
    rows = await blackboard.load(scope, include_stale=bool(stale),
                                 слой=(слой if слой in blackboard.СЛОИ else ""))
    return {"scope": scope, "facts": rows, "count": len(rows)}


@router.post("/api/board/facts")
async def board_put(body: dict, u: dict = Depends(user)) -> dict:
    """Положить факт на доску руками: «вынести на общую доску».

    Автором ставим человека, а не агента: это его утверждение, даже если взято из прогона.
    Человек может положить сразу в любой слой — он и есть супервайзер для своих решений,
    пока та роль не собрана (шаг 5). Агенты пишут в working через движок прогона.
    """
    key = str((body or {}).get("key") or "").strip()
    if not key:
        raise HTTPException(422, "нужен ключ факта")
    слой = str((body or {}).get("слой") or "working")
    if слой not in blackboard.СЛОИ:
        raise HTTPException(422, "слой: один из " + ", ".join(blackboard.СЛОИ))
    scope = _board_scope(str((body or {}).get("kind") or "user"), u, str((body or {}).get("family") or ""))
    row = await blackboard.put_fact(
        scope, key, (body or {}).get("value"),
        author=u.get("name") or _sub(u), note=str((body or {}).get("note") or ""),
        ttl_sec=int((body or {}).get("ttl_sec") or blackboard.DEFAULT_TTL_SEC),
        run_id=str((body or {}).get("run_id") or ""), слой=слой)
    await audit_store.record(u.get("name") or _sub(u), "board.put", scope,
                             {"key": key, "run_id": row.get("run_id"), "ttl_sec": row.get("ttl_sec"),
                              "слой": слой})
    return {"ok": True, "scope": scope, "fact": row}


@router.post("/api/board/facts/{key}/publish")
async def board_publish(key: str, body: dict, u: dict = Depends(user)) -> dict:
    """Продвинуть запись по слоям: working → validated → cross → user.

    Это вердикт супервайзера над чужой работой. Роль супервайзера собирается на шаге 5;
    до тех пор продвигает человек manager+ — и это названо прямо, не замаскировано.
    Автор и значение записи не меняются: продвижение — не новая правда, а признание её годной.
    """
    require_level(u, "manager")
    kind = str((body or {}).get("kind") or "")
    family = str((body or {}).get("family") or "")
    if kind != "user" and family and not can_see_family(u, family):
        raise HTTPException(403, "доска другого отдела")
    слой = str((body or {}).get("слой") or "")
    if слой not in blackboard.СЛОИ:
        raise HTTPException(422, "слой: один из " + ", ".join(blackboard.СЛОИ))
    scope = _board_scope(kind, u, family)
    row = await blackboard.опубликовать(scope, key, слой, кем=u.get("name") or _sub(u))
    if not row:
        raise HTTPException(404, "записи с таким ключом на этой доске нет")
    await audit_store.record(u.get("name") or _sub(u), "board.publish", scope,
                             {"key": key, "слой": слой, "автор_записи": row.get("author")})
    return {"ok": True, "fact": row}


@router.get("/api/board/reuse")
async def board_reuse(организация: str = "", период: str = "", правило: str = "",
                      версия: str = "", family: str = "",
                      u: dict = Depends(user)) -> dict:
    """Переиспользование расчёта по точному ключу из слоёв validated/cross доски отдела.

    Экономит прогон и гарантирует ту же истину: ключ называется правкой правилом
    (организация+период+правило+версия политики), эмбеддинги тут не участвуют. Черновики
    (working) и протухшее не отдаются — переиспользуем только признанное и свежее.
    """
    if not family:
        raise HTTPException(422, "нужна семья отдела, чью доску проверяем")
    if not can_see_family(u, family):
        raise HTTPException(403, "доска другого отдела")
    if not (организация or период or правило):
        raise HTTPException(422, "нужны организация, период и правило — ключ обязан быть точным")
    ключ = blackboard.ключ_расчёта(организация, период, правило, версия)
    наш = await blackboard.переиспользование(ключ, [family])
    if not наш:
        return {"reuse": False, "ключ": ключ,
                "почему": "признанного артефакта (validated/cross) по этому ключу нет — считать заново"}
    return {"reuse": True, "ключ": ключ, "артефакт": наш}


@router.delete("/api/board/facts/{key}")
async def board_drop(key: str, kind: str = "user", family: str = "", author: str = "",
                     u: dict = Depends(user)) -> dict:
    scope = _board_scope(kind, u, family)
    n = await blackboard.drop_fact(scope, key, author)
    await audit_store.record(u.get("name") or _sub(u), "board.drop", scope, {"key": key})
    return {"ok": True, "removed": n}
