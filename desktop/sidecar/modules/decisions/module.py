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


class ПоляIn(BaseModel):
    """Правка человека: только значения полей и, если он захотел, причина."""

    поля: dict = {}
    почему: str = ""


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


@router.get("/fields")
def fields(organisation: str = "") -> dict:
    """Поля первички, которые ждут человека. Третий поток раздела, не смешанный с остальными.

    Заявка требует «отправить или нет», находка — «это проблема или норма», поле — «что в
    документе на самом деле». Три разных вопроса: сложить их в один список значило бы заставить
    человека каждый раз угадывать, чего от него хотят.
    """
    try:
        return abop.pervichka_fields(organisation)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)


@router.post("/fields/{document}/confirm")
def fields_confirm(document: str, body: ПоляIn) -> dict:
    """Подтвердить значения полей документа: правка человека и эталон рядом с ней."""
    try:
        return abop.pervichka_confirm(document, body.поля or {}, body.почему or "")
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)


@router.get("/quality")
def quality(organisation: str = "") -> dict:
    """Качество разбора на корпусе заказчика — по его же правкам."""
    try:
        return abop.pervichka_quality(organisation)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex)


@router.get("/findings")
def findings(project: str = "", process: str = "", organisation: str = "") -> dict:
    """Находки реестра, ждущие решения: «это проблема» или «так и должно быть».

    Раньше находка жила внутри прогона: чтобы её найти, надо было помнить, какой прогон её родил.
    Здесь она лежит рядом с доказательством, и решение по ней — одно движение.
    """
    try:
        return abop.findings_open(project, process, organisation)
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex


class НаходкаIn(BaseModel):
    состояние: str = "подтверждено"
    почему: str = ""
    # Дата возврата для состояния «отложено». Пусто у остальных исходов — и это не ошибка.
    до: str = ""


class ПакетIn(BaseModel):
    ids: list = []
    состояние: str = "подтверждено"
    почему: str = ""
    до: str = ""


@router.post("/findings/{finding_id}/decide")
def finding_decide(finding_id: str, body: НаходкаIn) -> dict:
    try:
        return {"ok": True, "result": abop.finding_decide(finding_id, body.состояние,
                                                          body.почему, body.до)}
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex


@router.post("/findings/decide-batch")
def findings_decide_batch(body: ПакетIn) -> dict:
    """Решение по группе находок сразу — то же решение, только не по одной.

    Руководитель смотрит на причину («14 поступлений без счёта-фактуры»), а не на четырнадцать
    её следов. Запись в журнале при этом остаётся по каждой находке: пакет ускоряет нажатие, а
    не упрощает отчётность.
    """
    try:
        return {"ok": True, "result": abop.findings_decide_batch(body.ids, body.состояние,
                                                                 body.почему, body.до)}
    except Exception as ex:  # noqa: BLE001
        raise _ошибка(ex) from ex
