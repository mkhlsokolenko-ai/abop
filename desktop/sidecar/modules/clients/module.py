"""Модуль «Клиенты» — реестр обслуживаемых компаний, ТОНКИЙ ПРОКСИ к ABOP.

У аутсорсера в одной установке сотни обслуживаемых компаний. Подключение каждой должно быть
записью в реестре, а не работой с сервером: до 12.11.2026 завести клиента можно было только по
ssh — переменная с паролем в `.env`, строка в файле-источнике, пересоздание контейнера, прогон
рецепта. Веб-экран появился, а десктоп — то место, где живёт тот, кто ведёт клиентов.

Три правила, которые здесь нельзя нарушить, и все три взяты из провалов на живых базах:

1. **Организацию человек ВЫБИРАЕТ из базы, а не набирает.** Прогон делит данные по этому полю, и
   имя, набранное руками, разойдётся с данными на один символ — расчёт даст ноль документов
   молча. Поэтому «Проверить связь» обязательна, и список организаций приходит из самой базы.
2. **Пароль здесь только ПРОЕЗЖАЕТ.** Сайдкар его не хранит, не кладёт в свою SQLite и не пишет в
   журнал. Платформа кладёт секрет в файл своей установки и наружу не отдаёт — ни значение, ни
   путь.
3. **Считает и решает платформа.** Десктоп не повторяет ни проверку связи, ни выбор схемы учёта:
   второй расчёт на клиенте разошёлся бы с первым, и человек не знал бы, какому верить.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ... import abop_client as abop

MANIFEST = {"id": "clients", "title": "Клиенты", "icon": "connectors", "ui": "clients", "order": 15}
router = APIRouter()


def _err(e: "abop.AbopError") -> JSONResponse:
    """Отказ платформы доносим кодом и словами, а не 502 на всё подряд.

    «Пароль не принят» (400 от пробы), «нет такого клиента» (404) и «платформа не ответила» —
    разные новости, и человек по ним идёт в разные места.
    """
    return JSONResponse({"ok": False, "error": e.detail, "status": e.status},
                        status_code=e.status if 400 <= e.status < 600 else 502)


class ПробаIn(BaseModel):
    адрес: str
    пароль: str
    логин: str = ""


class КарточкаIn(BaseModel):
    id: str = ""
    адрес: str
    пароль: str = ""          # пусто при правке существующей карточки — секрет уже в установке
    организация: str = ""
    название: str = ""
    период_с: str = ""
    период_по: str = ""
    схема_учёта: str = ""
    примечание: str = ""


class СостояниеIn(BaseModel):
    состояние: str            # активен | выключен


@router.get("/registry")
def registry(все: int = 0):
    """Реестр клиентов и варианты схем учёта (схемы объявлены наборами правил на сервере)."""
    try:
        return {"ok": True, **(abop.clients(bool(все)) or {})}
    except abop.AbopError as e:
        return _err(e)


@router.post("/probe")
def probe(тело: ПробаIn):
    """Проверка доступа к базе: только чтение. Возвращает организации, из которых выбирают."""
    try:
        return {"ok": True, **(abop.client_probe(тело.адрес, тело.пароль, тело.логин) or {})}
    except abop.AbopError as e:
        return _err(e)


@router.post("/save")
def save(тело: КарточкаIn):
    """Завести клиента или поправить карточку. Связь платформа проверяет всегда сама."""
    карточка = {к: з for к, з in тело.model_dump().items() if str(з or "").strip()}
    try:
        return {"ok": True, **(abop.client_save(карточка) or {})}
    except abop.AbopError as e:
        return _err(e)


@router.post("/refresh/{client_id:path}")
def refresh(client_id: str, начисто: int = 0):
    """Перечитать данные ОДНОГО клиента. На живой базе это до минуты — экран обязан сказать это
    словами до нажатия, а не показывать замерший интерфейс."""
    try:
        return {"ok": True, **(abop.client_refresh(client_id, bool(начисто)) or {})}
    except abop.AbopError as e:
        return _err(e)


@router.post("/state/{client_id:path}")
def state(client_id: str, тело: СостояниеIn):
    """Включить или выключить. Удаления нет намеренно: прогоны и находки остаются историей."""
    try:
        return {"ok": True, **(abop.client_state(client_id, тело.состояние) or {})}
    except abop.AbopError as e:
        return _err(e)
