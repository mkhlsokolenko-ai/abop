# -*- coding: utf-8 -*-
"""Переписка: треды и сообщения.

Домен вынесен из монолита (п.23 исследования 08.10). Самодостаточен целиком: один стор, владение
по `sub` из токена, никаких пересечений с прогонами и картой. Карточки прогонов хранятся в `meta`
сообщения — это данные переписки, а не ссылка в чужой домен.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import chat_store
from ..deps import _sub, user

router = APIRouter()

@router.get("/api/chat/threads")
async def chat_threads(u: dict = Depends(user)) -> dict:
    return {"threads": await chat_store.threads(_sub(u))}


@router.post("/api/chat/threads")
async def chat_thread_create(body: dict, u: dict = Depends(user)) -> dict:
    t = await chat_store.create(_sub(u), title=str((body or {}).get("title") or "Новый чат"),
                                profile=str((body or {}).get("profile") or "standard"),
                                skills=(body or {}).get("skills") or [])
    return t


@router.patch("/api/chat/threads/{thread_id}")
async def chat_thread_patch(thread_id: int, body: dict, u: dict = Depends(user)) -> dict:
    t = await chat_store.patch(_sub(u), thread_id, title=(body or {}).get("title"),
                               profile=(body or {}).get("profile"), skills=(body or {}).get("skills"),
                               favorite=(body or {}).get("favorite"))
    if not t:
        raise HTTPException(404, "нет такого чата")
    return t


@router.delete("/api/chat/threads/{thread_id}")
async def chat_thread_delete(thread_id: int, u: dict = Depends(user)) -> dict:
    if not await chat_store.delete(_sub(u), thread_id):
        raise HTTPException(404, "нет такого чата")
    return {"ok": True}


@router.get("/api/chat/threads/{thread_id}/messages")
async def chat_messages(thread_id: int, u: dict = Depends(user)) -> dict:
    if not await chat_store.owns(_sub(u), thread_id):
        raise HTTPException(404, "нет такого чата")
    return {"messages": await chat_store.messages(_sub(u), thread_id)}


@router.post("/api/chat/threads/{thread_id}/messages")
async def chat_message_add(thread_id: int, body: dict, u: dict = Depends(user)) -> dict:
    m = await chat_store.add(_sub(u), thread_id, str((body or {}).get("role") or "user"),
                             str((body or {}).get("content") or ""), (body or {}).get("meta") or {})
    if not m:
        raise HTTPException(404, "нет такого чата")
    return m


@router.patch("/api/chat/threads/{thread_id}/messages/{message_id}")
async def chat_message_patch(thread_id: int, message_id: int, body: dict, u: dict = Depends(user)) -> dict:
    m = await chat_store.patch_meta(_sub(u), thread_id, message_id, (body or {}).get("meta") or {})
    if not m:
        raise HTTPException(404, "нет такого сообщения")
    return m
