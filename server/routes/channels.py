# -*- coding: utf-8 -*-
"""Привязка внешних каналов (бот в мессенджере) к человеку и её отзыв.

Четыре ручки и один запрет, который важнее всех четырёх.

* `POST /api/channels/{channel}/code` — человек, вошедший в десктоп или веб, просит одноразовый код;
* `POST /api/channels/{channel}/exchange` — **бот** меняет код на узкий токен ABOP. Единственная
  ручка, которую вызывает не человек, поэтому она защищена секретом бота;
* `GET /api/channels` — свои привязки: что к чему привязано и когда последний раз использовалось;
* `DELETE /api/channels/{binding}` — отзыв. Мгновенный: токен перестаёт действовать до рестарта.

**Имена параметров пути — латиницей, и это не вкус.** Starlette распознаёт параметр только по
ASCII-идентификатору: `{канал}` он экранирует как ЛИТЕРАЛ, маршрут регистрируется и не совпадает
никогда — без ошибки и предупреждения. Поймано тестом, который получил 405 от статики вместо
ответа ручки; сторож на этот класс стоит в `tests/test_routes_inventory.py`.

**Запрет: каналом нельзя выпустить ещё один канал.** Иначе утёкший токен бота превращается в
бесконечную цепочку привязок, и отзыв одной ничего не решает. Проверка стоит в выдаче кода, и это
единственное место, где она возможна, — потому что именно там заявляется личность.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .. import audit_store, channel_tokens
from .. import observability as obs
from ..deps import _sub, user

router = APIRouter()

# Секрет бота: без него ручка обмена кода открыта всем, и код из восьми знаков подбирают
# перебором. Пусто ⇒ обмен выключен совсем: лучше неработающий канал, чем открытый.
_СЕКРЕТ = os.getenv("ABOP_BOT_SECRET", "")
if _СЕКРЕТ and not _СЕКРЕТ.isascii():
    # Секрет едет заголовком HTTP, а заголовки — latin-1: на кириллическом секрете бот падает на
    # отправке, не на проверке, и причина выглядит как «сеть». Лучше сказать сразу.
    raise RuntimeError("ABOP_BOT_SECRET должен быть только из ASCII: он уходит заголовком HTTP. "
                       "Сгенерируйте так: python -c \"import secrets;print(secrets.token_urlsafe(32))\"")
# Частота обмена: даже с секретом перебор кода не должен быть дешёвым.
_ОКНО = 60.0
_ПРЕДЕЛ = 10
_ПОПЫТКИ: list[float] = []


def _частота_в_норме() -> bool:
    сейчас = time.time()
    _ПОПЫТКИ[:] = [t for t in _ПОПЫТКИ if сейчас - t < _ОКНО]
    if len(_ПОПЫТКИ) >= _ПРЕДЕЛ:
        return False
    _ПОПЫТКИ.append(сейчас)
    return True


@router.post("/api/channels/{channel}/code")
async def выдать_код(channel: str, u: dict = Depends(user)) -> dict:
    """Одноразовый код на привязку канала. Действует 15 минут и один раз."""
    if u.get("channel"):
        # Канал не выпускает каналы: иначе один утёкший токен плодит привязки без конца, и отзыв
        # перестаёт быть отзывом.
        raise HTTPException(403, "привязать канал можно только из приложения или веба, не из бота")
    к = channel_tokens.нормализовать_канал(channel)
    if not к:
        raise HTTPException(400, f"неизвестный канал: {channel}")
    итог = await channel_tokens.выдать_код(u, к)
    await audit_store.record(u.get("name") or _sub(u), "channel.code", к, {"канал": к})
    obs.log_event("info", "channel.code", channel=к, who=u.get("name") or _sub(u))
    return итог


@router.post("/api/channels/{channel}/exchange")
async def обменять_код(channel: str, body: dict, request: Request,
                       x_bot_secret: str = Header(default="")) -> dict:
    """Код → токен канала. Зовёт БОТ, а не человек, поэтому защита тут не JWT, а секрет бота.

    Ответ про неподходящий код всегда один и тот же, независимо от причины: по разнице в ответах
    код подбирают.
    """
    if not _СЕКРЕТ:
        raise HTTPException(503, "канал не настроен: не задан ABOP_BOT_SECRET")
    if x_bot_secret != _СЕКРЕТ:
        obs.log_event("warning", "channel.exchange_bad_secret", channel=channel,
                      peer=str(getattr(request.client, "host", "") or "")[:40])
        raise HTTPException(403, "секрет бота не подходит")
    if not _частота_в_норме():
        raise HTTPException(429, f"слишком много попыток обмена: предел {_ПРЕДЕЛ} за {int(_ОКНО)} с")
    код = str((body or {}).get("code") or "").strip().upper()
    куда = str((body or {}).get("external_id") or "").strip()
    итог = await channel_tokens.обменять_код(код, channel, куда)
    if not итог.get("ok"):
        obs.log_event("warning", "channel.exchange_failed", channel=channel)
        raise HTTPException(400, итог.get("почему") or "код не подходит")
    obs.log_event("info", "channel.bound", channel=channel, who=итог.get("кто"),
                  tenant=итог.get("арендатор"))
    return итог


@router.get("/api/channels")
async def мои_привязки(u: dict = Depends(user)) -> dict:
    """Свои привязки. Видно, когда каналом пользовались: забытая привязка — это открытая дверь."""
    return {"привязки": await channel_tokens.мои(_sub(u))}


@router.delete("/api/channels/{binding}")
async def отозвать(binding: str, u: dict = Depends(user)) -> dict:
    """Отзыв своей привязки. Чужую не отзывают: для этого есть удаление человека целиком."""
    ok = await channel_tokens.отозвать(_sub(u), binding)
    if not ok:
        raise HTTPException(404, "привязки нет или она уже отозвана")
    await audit_store.record(u.get("name") or _sub(u), "channel.revoke", binding, {})
    obs.log_event("info", "channel.revoked", id=binding, who=u.get("name") or _sub(u))
    return {"ok": True}
