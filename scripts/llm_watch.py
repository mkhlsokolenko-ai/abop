# -*- coding: utf-8 -*-
"""Сторож LLM-бокса (урок 08.10–19.11: бокс молча умер, прод 40 дней платил RouteAI).

Проверки: кредит Vast, состояние инстанса, доступность /v1/models из контейнера.
Проблема — Telegram всем привязанным через «Кабинет»; повторный алерт только если
состояние изменилось (файл состояния /tmp/llm_watch_state.json).

Запуск по крону НА ХОСТЕ:
  */30 * * * * V=$(cat /root/.vast_api_key) && docker exec -e VAST_KEY="$V" \
    abop-webapi python /app/scripts/llm_watch.py >> /tmp/llm_watch.log 2>&1
"""
import json
import os
import sys
import urllib.request

VAST_ID = int(os.environ.get("VAST_INSTANCE", "59895017"))
ПОРОГ_КРЕДИТА = float(os.environ.get("VAST_CREDIT_MIN", "5"))
СВОЙСТВО = "/tmp/llm_watch_state.json"
BOX = os.environ.get("LOCAL_LLM_BASE_URL", "").rstrip("/")


def vast_v0(ключ, путь):
    req = urllib.request.Request("https://console.vast.ai/api/v0" + путь,
                                 headers={"Authorization": "Bearer " + ключ})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def vast(ключ, путь, метод="GET", тело=None):
    req = urllib.request.Request("https://console.vast.ai/api/v1" + путь,
                                 headers={"Authorization": "Bearer " + ключ},
                                 method=метод,
                                 data=json.dumps(тело).encode() if тело else None)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def читать_проблемы() -> list:
    проблемы = []
    ключ = os.environ.get("VAST_KEY", "")
    if not ключ:
        return ["VAST_KEY не передан — сторож не может проверить кредит и состояние"]
    try:
        # кредит живёт на v0 (v1 отдаёт 404 — проверено 19.11)
        u = vast_v0(ключ, "/users/current/")
        кредит = float(u.get("credit") or 0)
        if кредит < ПОРОГ_КРЕДИТА:
            проблемы.append(f"кредит Vast {кредит:.2f}$ < порога {ПОРОГ_КРЕДИТА}$ — бокс скоро встанет")
    except Exception as e:
        проблемы.append(f"кредит Vast не проверился: {type(e).__name__} {str(e)[:80]}")
    try:
        инс = vast(ключ, "/instances/").get("instances") or []
        наш = next((i for i in инс if i.get("id") == VAST_ID), None)
        if not наш:
            проблемы.append(f"инстанс {VAST_ID} на аккаунте НЕ НАЙДЕН — бокса нет вовсе")
        elif str(наш.get("cur_state")) != "running":
            проблемы.append(f"инстанс {VAST_ID} в состоянии {наш.get('cur_state')} — не running")
    except Exception as e:
        проблемы.append(f"состояние инстанса не проверилось: {type(e).__name__} {str(e)[:80]}")
    if BOX:
        try:
            urllib.request.urlopen(BOX + "/models", timeout=12).read(20)
        except Exception as e:
            проблемы.append(f"бокс {BOX} не отвечает: {type(e).__name__} {str(e)[:60]}")
    return проблемы


def послать(текст: str) -> int:
    токен = os.environ.get("ABOP_BOT_SECRET", "")
    if not токен:
        return 0
    адресаты = []
    try:
        sys.path.insert(0, "/app")
        from server.db import _conn

        async def ид():
            async with _conn() as conn:
                cur = await conn.execute(
                    "SELECT external_id FROM channel_tokens WHERE channel='telegram' "
                    "AND external_id <> ''")
                строки = await cur.fetchall()
                return [r[0] for r in строки]
        адресаты = asyncio_run(ид())
    except Exception as e:
        print("bindings не прочитались:", type(e).__name__, str(e)[:80])
    отправлено = 0
    for чат in адресаты:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{токен}/sendMessage",
                data=json.dumps({"chat_id": чат, "text": текст}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
            отправлено += 1
        except Exception as e:
            print("не отправилось в", чат, ":", type(e).__name__, str(e)[:60])
    return отправлено


def asyncio_run(ко):
    import asyncio
    return asyncio.run(ко)


def main() -> int:
    проблемы = читать_проблемы()
    прежнее = []
    try:
        with open(СВОЙСТВО, encoding="utf-8") as f:
            прежнее = json.load(f)
    except Exception:
        pass
    if проблемы == прежнее:
        print("без изменений:", len(проблемы), "проблем")
        return 0
    if проблемы:
        послать("ABOP · сторож LLM-бокса\n— " + "\n— ".join(проблемы))
        print("АЛЕРТ:", "; ".join(проблемы))
    elif прежнее:
        послать("ABOP · сторож LLM-бокса: всё восстановилось — бокс отвечает, кредит в норме")
        print("восстановилось")
    with open(СВОЙСТВО, "w", encoding="utf-8") as f:
        json.dump(проблемы, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
