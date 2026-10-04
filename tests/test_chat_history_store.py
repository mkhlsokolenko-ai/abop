# -*- coding: utf-8 -*-
"""История чатов принадлежит человеку, а не машине.

Переписка жила в локальной базе сайдкара: на каждой машине своя, чистка кэша или новый ноутбук
означали «истории не было». А в чате принимаются решения и запускаются агенты — в карточках лежат
ссылки на прогоны, вердикты и подтверждения внешних действий. Это рабочий журнал, и он обязан
переживать машину.

Владение — по `sub` из JWT. Чужую переписку нельзя ни прочитать, ни испортить: проверка стоит в
хранилище, а не в интерфейсе.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k in ("KEYCLOAK_JWKS_URI", "ABOP_EXTRA_JWKS", "PG_DSN"):
    os.environ.pop(k, None)
os.environ["ABOP_DEV_AUTH"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from server import chat_store, web_api  # noqa: E402

CLIENT = TestClient(web_api.app)


def test_чат_и_сообщения_живут_в_хранилище():
    async def flow():
        t = await chat_store.create("user-1", title="Разбор почты")
        await chat_store.add("user-1", t["id"], "user", "разбери почту")
        await chat_store.add("user-1", t["id"], "assistant", "[агент mailtasks] находок: 3",
                             {"run_agent": {"run_id": "run-7", "findings_total": 3}})
        return t, await chat_store.messages("user-1", t["id"]), await chat_store.threads("user-1")
    t, msgs, threads = asyncio.run(flow())
    assert t["title"] == "Разбор почты" and len(msgs) == 2
    assert msgs[1]["meta"]["run_agent"]["run_id"] == "run-7", "meta карточки прогона не сохранилась"
    assert any(x["id"] == t["id"] for x in threads)


def test_чужую_переписку_не_видно_и_не_испортить():
    async def flow():
        t = await chat_store.create("user-1", title="Моё")
        seen = await chat_store.threads("user-2")
        msgs = await chat_store.messages("user-2", t["id"])
        added = await chat_store.add("user-2", t["id"], "user", "подсматриваю")
        patched = await chat_store.patch("user-2", t["id"], title="переименовал")
        killed = await chat_store.delete("user-2", t["id"])
        return seen, msgs, added, patched, killed
    seen, msgs, added, patched, killed = asyncio.run(flow())
    assert all(x["title"] != "Моё" for x in seen), "чужой чат виден в списке"
    assert msgs == [] and added is None and patched is None and killed is False


def test_решение_по_подтверждению_доживает_до_перечитывания():
    """Иначе после переоткрытия чата кнопка «Подтвердить» снова активна, а ссылка на задачу исчезла."""
    async def flow():
        t = await chat_store.create("user-3")
        m = await chat_store.add("user-3", t["id"], "assistant", "[агент x]",
                                 {"run_agent": {"run_id": "r1", "findings_total": 1}})
        await chat_store.patch_meta("user-3", t["id"], m["id"], {"run_agent": {"hitl_done": "approve"}})
        return await chat_store.messages("user-3", t["id"])
    msgs = asyncio.run(flow())
    ra = msgs[-1]["meta"]["run_agent"]
    assert ra["hitl_done"] == "approve" and ra["run_id"] == "r1", "дописали meta, потеряв прежние поля"


def test_ручки_истории_работают_под_своим_пользователем():
    r = CLIENT.post("/api/chat/threads", json={"title": "Через API"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert CLIENT.post(f"/api/chat/threads/{tid}/messages",
                       json={"role": "user", "content": "привет"}).status_code == 200
    got = CLIENT.get(f"/api/chat/threads/{tid}/messages").json()["messages"]
    assert [m["content"] for m in got] == ["привет"]
    lst = CLIENT.get("/api/chat/threads").json()["threads"]
    assert any(t["id"] == tid for t in lst)
    assert CLIENT.patch(f"/api/chat/threads/{tid}", json={"title": "Переименован"}).json()["title"] == "Переименован"
    assert CLIENT.delete(f"/api/chat/threads/{tid}").json()["ok"] is True
    assert CLIENT.get(f"/api/chat/threads/{tid}/messages").status_code == 404


def test_схема_объявлена_и_привязана_к_владельцу():
    s = chat_store.SCHEMA
    assert "chat_threads" in s and "chat_messages" in s
    assert "user_sub" in s and "ON DELETE CASCADE" in s, "удаление чата должно убирать его сообщения"
    assert "chat_threads_user_idx" in s, "список чатов пользователя без индекса будет медленным"
