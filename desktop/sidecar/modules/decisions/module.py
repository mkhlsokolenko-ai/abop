"""Модуль «Решения» — очередь того, что ждёт слова человека, и ничего больше.

Решения жили внутри чата: очередь показывалась карточкой в переписке, и из «Прогонов» или
«Кабинета» о висящих письмах узнать было нельзя — счётчик в рейле появился, а места, куда по нему
пойти, не было. Это и есть раздел «Решения» (PRD 5.4, DIZAJN 5.4).

Три решения этого модуля:

1. **Переключателя «режим руководителя» нет.** Раздел один и показывает то, что адресовано
   человеку: У2 — свой прогон, У3 — владельцу операции. Руководитель тоже ставит задачи, и
   прыгать между двумя приложениями ради одного подтверждения он не должен.
2. **Отказ называется «оставить черновиком», а не «отклонить».** Отказ здесь не отказ, а выбор
   обратимого пути: письмо остаётся готовым и ждёт 30 дней. Слово «отклонить» врало бы о
   последствии.
3. **Сайдкар только проксирует.** Право решать считает сервер (`routes/hitl._можно_решать`):
   уровень действия, адресность и отдел — не дело интерфейса. Десктоп показывает отказ словами,
   а не прячет кнопку.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import abop_client as abop

MANIFEST = {"id": "decisions", "title": "Решения", "icon": "decisions", "ui": "decisions",
            "order": 15}
router = APIRouter()


class РешениеIn(BaseModel):
    decision: str = "approve"
    reason: str = ""


def _ошибка(ex: Exception) -> HTTPException:
    """Отказ сервера пересказываем как есть: он знает, почему нельзя, а мы — нет."""
    код = int(getattr(ex, "code", 0) or 0)
    return HTTPException(код if 400 <= код < 600 else 502, str(ex)[:400])


@router.get("/queue")
def queue(mine: str = "1") -> dict:
    """Решения, ждущие человека. По умолчанию — только мои: адресованные мне идут первыми."""
    try:
        return abop.hitl_queue_mine(mine=mine)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex


@router.get("/item/{item_id}")
def item(item_id: str) -> dict:
    """Полный предмет решения: кому, тема, текст письма целиком.

    Подтверждать невидимое нельзя — это уже ловил UX-аудит 25.09 (D-C3): письмо подтверждали, не
    видя его текста.
    """
    try:
        return abop.hitl_item(item_id)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex


@router.post("/item/{item_id}/decide")
def decide(item_id: str, body: РешениеIn) -> dict:
    try:
        return {"ok": True, "result": abop.hitl_approve(item_id, body.decision, body.reason)}
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex
