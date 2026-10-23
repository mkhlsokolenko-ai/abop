"""Вертикали — поставка представления и его точка отсчёта, из приложения, а не из консоли.

До этого раздела вертикаль ставили ручкой API: человек, который продаёт и внедряет, не должен для
этого открывать терминал. Здесь он видит то же, что видит платформа: что лежит в поставке, годен ли
пакет, чего не хватает в системе, установлена ли вертикаль и какая активна.

Четыре действия и ни одного молчаливого:

* **поставить** — отчёт по каждому пункту словом «поставлен / обновлён / пропущен» и причиной;
  правку методолога чужая установка не затирает;
* **нулевой прогон** — точка отсчёта: сколько записей пришло по каждому источнику и какие находки
  были НА ВХОДЕ. Без этого списка через месяц не отличить «мы нашли и починили» от «оно и так
  было найдено до нас»;
* **база** — чтение той самой точки отсчёта;
* **сделать активной** — переключение представления; пустой идентификатор возвращает широкий режим.

Ручки тонкие: вся проверка (манифест против содержимого, требуемое против системы) живёт на
платформе. Дублировать её здесь значило бы завести второе мнение о годности пакета.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import abop_client as abop

MANIFEST = {"id": "verticals", "title": "Вертикали", "icon": "abop", "ui": "verticals",
            "order": 85}
router = APIRouter()


def _ошибка(ex: Exception) -> HTTPException:
    """Отказ платформы доносим словами и кодом, а не «что-то пошло не так»."""
    код = int(getattr(ex, "code", 0) or 0)
    return HTTPException(код if 400 <= код < 600 else 502, str(ex)[:400])


@router.get("/list")
def list_verticals() -> dict:
    """Каталог поставки и состояние системы: годен, установлена, чего не хватает."""
    try:
        каталог = abop.vertical_list()
        активная = (abop.vertical_active() or {}).get("активная") or {}
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)
    return {"вертикали": каталог.get("вертикали") or [], "активная": активная}


@router.post("/{vid}/install")
def install(vid: str) -> dict:
    """Поставить вертикаль. Идемпотентно: повторная установка не плодит версии."""
    try:
        return abop.vertical_install(vid)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)


@router.post("/{vid}/zero-scan")
def zero_scan(vid: str) -> dict:
    """Нулевой прогон: прочитать источники вертикали и зафиксировать точку отсчёта."""
    try:
        return abop.vertical_zero_scan(vid)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)


@router.get("/{vid}/baseline")
def baseline(vid: str) -> dict:
    """Та самая точка отсчёта: с ней сравнивают через месяц."""
    try:
        return abop.vertical_baseline(vid)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)


@router.post("/{vid}/activate")
def activate(vid: str) -> dict:
    """Сделать активной. `-` снимает выбор: оболочка возвращается в широкий режим."""
    try:
        return abop.vertical_activate(vid)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)
