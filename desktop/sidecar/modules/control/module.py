"""Модуль «Лента контроля» — рабочее место руководителя: смотреть и управлять полномочиями.

Руководитель здесь **не согласует**. Отправку КП подтверждает тот, в чьи обязанности она входит
(ADR-060); сюда он приходит за другим: что уже ушло наружу, где работа встала, кто что себе
разрешил. Единственное действие на экране — снять разрешение: отправленное не вернуть, и управлять
можно только тем, что будет можно дальше.

Сайдкар только проксирует: сборку ленты и права считает сервер (`server/control_feed`,
`routes/control`). Отказ по правам пересказываем словами сервера, а не прячем раздел — спрятанный
раздел учит человека, что продукт «что-то недоговаривает».
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import abop_client as abop

MANIFEST = {"id": "control", "title": "Лента контроля", "icon": "control", "ui": "control",
            "order": 18}
router = APIRouter()


def _ошибка(ex: Exception) -> HTTPException:
    код = int(getattr(ex, "code", 0) or getattr(ex, "status", 0) or 0)
    return HTTPException(код if 400 <= код < 600 else 502, str(ex)[:400])


@router.get("/feed")
def feed(days: int = 7) -> dict:
    try:
        return abop.control_feed(days=days)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex


@router.delete("/permissions/{binding}")
def revoke(binding: str) -> dict:
    """Снять постоянное разрешение. Необратимым не является: человек может привязать канал снова."""
    try:
        return abop.control_revoke(binding)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex
