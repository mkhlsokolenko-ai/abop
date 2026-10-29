# -*- coding: utf-8 -*-
"""Периоды закрытия: список, расчёт, закрытие, переоткрытие и точка отсчёта.

Ручки обслуживают рабочее место расчётчика (решение владельца 28.10.2026: «закрывает расчётчик»).
Главная мысль в `server/periods.py`: период — объект со состоянием, а не параметр дат, потому что
«закрыть октябрь» надо куда-то записать.

Уровня `manager` здесь нет намеренно: закрытие периода — рядовая работа расчётчика, а не решение
руководителя. Зато записано, КТО закрыл: ответственность именная.
"""
from __future__ import annotations

import asyncio as _asyncio

from fastapi import APIRouter, Depends, HTTPException

from cli import ape

from .. import periods as периоды
from ..deps import user

router = APIRouter()


def _кто(u: dict) -> str:
    return str(u.get("name") or u.get("sub") or "dev")


@router.get("/api/periods")
async def periods_list(organisation: str = "", month: str = "", mine: int = 0,
                       u: dict = Depends(user)) -> dict:
    """Периоды: свои или по организации. `mine=1` — только те, где я ответственный.

    Имена параметров латиницей: кириллическое имя уезжает процентной кодировкой у каждого
    клиента по-своему, и путается оно не у нас, а у того, кто пишет панель.
    """
    кто = _кто(u)
    строки = await _asyncio.to_thread(периоды.список, organisation, month,
                                      кто if mine else "")
    сводка = await _asyncio.to_thread(периоды.сводка, кто if mine else "", month)
    # Назначенные организации отдаём отдельно: по части из них ещё НЕ СЧИТАЛИ, периода нет, и в
    # списке периодов их быть не может. Для портфеля это главные строки — «не начинали» на фирме,
    # которую ты ведёшь, важнее закрытых. Без этого поля портфель показывал бы только начатое и
    # выглядел бы благополучнее, чем есть.
    мои = await _asyncio.to_thread(периоды.мои_организации, кто)
    return {"периоды": строки, "сводка": сводка, "мои_организации": мои,
            "почему_нет_назначений": ("" if мои else
                                      "вам не назначено ни одной организации: «чьё это» живёт в "
                                      "данных (org_assignment), и пока назначений нет, портфель "
                                      "собирается по тем фирмам, где уже считали")}


@router.get("/api/periods/{organisation}/{month}")
async def period_one(organisation: str, month: str, u: dict = Depends(user)) -> dict:
    """Один период со всеми признаками и сравнением с точкой отсчёта."""
    з = await _asyncio.to_thread(периоды.получить, organisation, month)
    изм = await _asyncio.to_thread(периоды.изменения, organisation, month)
    if not з:
        return {"период": None, "изменения": изм,
                "почему": "по этой организации за этот месяц ещё не считали — периода нет"}
    return {"период": dict(з, **периоды.признаки(з)), "изменения": изм, "почему": ""}


@router.post("/api/periods/{organisation}/{month}/calc")
async def period_calc(organisation: str, month: str, body: dict | None = None,
                      u: dict = Depends(user)) -> dict:
    """Посчитать организацию: прогнать рецепты вертикали ПО НЕЙ и перевести период в «считается».

    Тело: `{"рецепты": ["finance_inbox", …]}` — какие именно прогнать. Пусто — ошибка, а не
    «прогнать всё»: «посчитать всё» по четырёмстам фирмам стоит в сотни раз дороже, и решать это
    молчанием нельзя.
    """
    кто = _кто(u)
    рецепты = [str(р) for р in ((body or {}).get("рецепты") or []) if str(р).strip()]
    if not рецепты:
        raise HTTPException(422, "нужно назвать рецепты: «посчитать всё» молчанием не включается")
    try:
        период = await _asyncio.to_thread(периоды.начать_расчёт, organisation, month, кем=кто)
    except ValueError as ex:
        raise HTTPException(409, str(ex))

    отчёт, сбои = [], []
    for имя in рецепты:
        try:
            сущность, записано, снято, брак = await _asyncio.to_thread(
                ape.data_run, имя, False, {"организация": organisation})
            отчёт.append({"рецепт": имя, "сущность": сущность, "записано": записано,
                          "отброшено": снято, "невалидно": брак})
        except Exception as ex:  # noqa: BLE001 — сбой одного рецепта не отменяет остальные
            сбои.append({"рецепт": имя, "почему": str(ex)[:300]})
    период = await _asyncio.to_thread(периоды.на_проверку, organisation, month, кем=кто)
    # Линию снимаем ПЕРВЫМ расчётом: позже она уже не «что было до нас».
    период = await _asyncio.to_thread(периоды.снять_линию, organisation, month, кем=кто)
    return {"период": dict(период, **периоды.признаки(период)), "отчёт": отчёт, "сбои": сбои,
            "организация": organisation,
            "почему": "" if отчёт else "ни один рецепт не отработал — считать было нечем"}


@router.post("/api/periods/{organisation}/{month}/close")
async def period_close(organisation: str, month: str, u: dict = Depends(user)) -> dict:
    """Закрыть период. При открытых находках отвечает отказом с причиной, а не тихо не делает."""
    try:
        з = await _asyncio.to_thread(периоды.закрыть, organisation, month, кем=_кто(u))
    except ValueError as ex:
        raise HTTPException(409, str(ex))
    return {"период": dict(з, **периоды.признаки(з))}


@router.post("/api/periods/{organisation}/{month}/reopen")
async def period_reopen(organisation: str, month: str, body: dict | None = None,
                        u: dict = Depends(user)) -> dict:
    """Переоткрыть период. Причина обязательна: это пересдача, а не правка опечатки."""
    try:
        з = await _asyncio.to_thread(периоды.переоткрыть, organisation, month, кем=_кто(u),
                                     почему=str((body or {}).get("почему") or ""))
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    return {"период": dict(з, **периоды.признаки(з))}
