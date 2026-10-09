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

from .. import clients
from ..deps import user
from .runs import _rate_check

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


# Свободный ответ в чате — это РАЗГОВОР, а не работа. Без этой рамки модель берётся отчитываться за
# систему: 04.10 в чате появились «цепочка выполнена», «отчёт собран в PDF», имя файла и путь
# «docs/…» — ничего из этого не существовало. Это худший вид отказа: не ошибка, а правдоподобный
# отчёт о несделанном, под которым стоит подпись модели и ноль рублей.
# Рамка добавляется НА СЕРВЕРЕ, а не в клиенте: правило про границы рантайма обязано действовать в
# любом канале, а не только в той версии десктопа, которую успели обновить.
CHAT_GUARD = (
    "Ты отвечаешь ТЕКСТОМ и не выполняешь действий: не запускаешь агентов, не собираешь отчёты, "
    "не создаёшь файлы, не отправляешь письма и не заводишь задачи. Никогда не утверждай, что "
    "что-то выполнено, собрано, сохранено, прикреплено или отправлено, и не выдумывай имена файлов, "
    "пути, ссылки и идентификаторы. Если просят сделать работу — дай текстом то, что можешь дать "
    "сам (разбор, план, вопросы по существу), а про запуск исполнителя скажи ОДНОЙ короткой "
    "строкой в конце: «запустить исполнителя — команда /work». НИКОГДА не выводи команду с "
    "текстом задачи внутри и не цитируй задачу человека обратно вместо ответа: человек уже "
    "написал её, переписывать её ему не нужно. Команды набирает человек, не ты. "
    "За готовым отчётом прогона отправляй к команде «/report» — она соберёт настоящий файл. "
    "О том, что уже сделано, человек узнаёт из карточек прогона, а не из твоего пересказа. "
    # Qwen на длинной системной рамке иногда вставляет иероглифы в середину русской фразы —
    # проверено живым ответом. Язык объявляем явно, это дешевле, чем чистить вывод.
    "Отвечай по-русски."
)


@router.post("/api/chat")
async def chat_freeform(body: dict, u: dict = Depends(user)) -> dict:
    """Свободный LLM-ответ под JWT пользователя — единый рантайм-канал для desktop-чата,
    цепочек графа и распознавания (тот же self-host каскад, что и у агентов). Отвязка от
    курсового шлюза: desktop больше НЕ ходит в MCP/portal курса. Rate-limit по пользователю."""
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(422, "нужен prompt")
    actor = u.get("name") or u.get("sub") or "dev"
    if not _rate_check(actor):
        raise HTTPException(429, "слишком часто — попробуйте чуть позже")
    system = str((body or {}).get("system") or "").strip()
    context = str((body or {}).get("context") or "").strip()
    profile = str((body or {}).get("profile") or "standard").strip() or "standard"
    max_tokens = min(int((body or {}).get("max_tokens") or 1200), 4096)
    messages: list[dict] = [{"role": "system", "content": CHAT_GUARD}]
    if system:
        messages.append({"role": "system", "content": system})
    user_content = (f"Контекст:\n{context}\n\n" if context else "") + prompt
    messages.append({"role": "user", "content": user_content})
    try:
        resp = await clients.chat(messages=messages, profile=profile, max_tokens=max_tokens)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"LLM недоступен: {ex}")
    return {"text": resp.get("text") or "", "model": resp.get("model"),
            "input_tokens": resp.get("input_tokens"), "output_tokens": resp.get("output_tokens")}


@router.post("/api/chat/stream")
async def chat_freeform_stream(body: dict, u: dict = Depends(user)):
    """Стриминг свободного ответа (SSE): токены-дельты по мере генерации — настоящий стрим для
    desktop-чата (не псевдо-нарезка на сайдкаре). Формат строк: `data: {"delta": "..."}` и финал
    `data: {"done": true, "model": ...}`. Тот же self-host каскад, что и /api/chat."""
    import json as _json
    from fastapi.responses import StreamingResponse
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(422, "нужен prompt")
    actor = u.get("name") or u.get("sub") or "dev"
    if not _rate_check(actor):
        raise HTTPException(429, "слишком часто — попробуйте чуть позже")
    system = str((body or {}).get("system") or "").strip()
    context = str((body or {}).get("context") or "").strip()
    profile = str((body or {}).get("profile") or "standard").strip() or "standard"
    max_tokens = min(int((body or {}).get("max_tokens") or 1500), 4096)
    messages: list[dict] = [{"role": "system", "content": CHAT_GUARD}]
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": (f"Контекст:\n{context}\n\n" if context else "") + prompt})

    async def gen():
        try:
            async for ev in clients.chat_stream(messages=messages, profile=profile, max_tokens=max_tokens):
                yield "data: " + _json.dumps(ev, ensure_ascii=False) + "\n\n"
        except Exception as ex:  # noqa: BLE001
            yield "data: " + _json.dumps({"error": f"LLM: {ex}"}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
