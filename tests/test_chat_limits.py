# -*- coding: utf-8 -*-
"""Длина свободного ответа в чате: решает сервер, а не клиент; обрыв по лимиту виден дальше по цепочке.

23.11: сайдкар запрашивал у /api/chat/stream 1500 токенов, и длинный ответ обрывался на полуслове
(4069 знаков, оборван на «…57. Проведение пер»), а finish_reason из ответа модели никто не читал —
стрим заканчивался так же, как полный ответ."""
import asyncio
import dataclasses
import os
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))
for _k in ("KEYCLOAK_JWKS_URI", "KEYCLOAK_JWKS_INTERNAL", "ABOP_EXTRA_JWKS", "PG_DSN", "DATABASE_URL", "ABOP_PG_DSN"):
    os.environ.pop(_k, None)
os.environ.setdefault("ABOP_RUN_WORKERS", "0")
os.environ["ABOP_DEV_AUTH"] = "1"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from server import web_api
    with TestClient(web_api.app) as c:
        yield c


def test_стрим_берёт_лимит_сервера_а_не_клиента(client, monkeypatch):
    from server import clients
    from server.config import settings

    вызов = {}

    async def поддельный(**kw):
        вызов.update(kw)
        yield {"delta": "При"}
        yield {"done": True, "model": "local/qwen3-30b-a3b", "finish_reason": "stop"}

    monkeypatch.setattr(clients, "chat_stream", поддельный)
    r = client.post("/api/chat/stream", json={"prompt": "привет", "max_tokens": 1500})
    assert r.status_code == 200, r.text[:200]
    assert вызов["max_tokens"] == settings.chat_max_tokens, "клиент снова диктует длину ответа"
    assert settings.chat_max_tokens >= 4096, "лимит свободного ответа снова режет длинные ответы"
    assert '"finish_reason": "stop"' in r.text, "признак завершения не доходит до того, кто слушает стрим"


def test_клиент_доносит_обрыв_по_лимиту(monkeypatch):
    """Модель оборвалась по max_tokens: в последнем куске стрима finish_reason=length. Прежде его
    выбрасывали, и обрыв выглядел законченным ответом."""
    from server import clients
    from server.config import settings

    строки = [
        'data: {"choices": [{"delta": {"content": "При"}}]}',
        "",
        'data: {"choices": [{"delta": {"content": "вет"}, "finish_reason": null}]}',
        "",
        'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}',
        "",
        'data: {"choices": [], "usage": {"completion_tokens": 6}}',
        "",
        "data: [DONE]",
        'data: {"choices": [{"delta": {"content": "после [DONE]"}}]}',   # дальше стрим не читается
    ]
    отправлено = {}

    class _Ответ:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        async def aiter_lines():
            for s in строки:
                yield s

    class _Поток:
        async def __aenter__(self):
            return _Ответ()

        async def __aexit__(self, *a):
            return False

    class _Клиент:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):
            отправлено.update(url=url, json=json or {})
            return _Поток()

    # `settings` — замороженный dataclass: подменяем не поля, а сам объект в модуле клиента.
    свои = dataclasses.replace(settings, local_llm_base_url="http://box/v1", local_llm_model="qwen3-30b-a3b")
    monkeypatch.setattr(clients.httpx, "AsyncClient", _Клиент)
    monkeypatch.setattr(clients, "settings", свои)
    было = dict(clients._LLM_OVERRIDE)          # адрес бокса решают настройки, а не чужое переопределение
    clients.set_llm_override({})

    async def идти():
        return [ev async for ev in clients.chat_stream(
            [{"role": "user", "content": "вопрос"}], model="local/qwen3-30b-a3b", max_tokens=8192)]

    try:
        события = asyncio.run(идти())
    finally:
        clients.set_llm_override(было)
    assert события == [{"delta": "При"}, {"delta": "вет"},
                       {"done": True, "model": "local/qwen3-30b-a3b", "finish_reason": "length"}]
    assert отправлено["url"] == "http://box/v1/chat/completions"
    assert отправлено["json"]["max_tokens"] == 8192


def test_сайдкар_и_чат_доносят_обрыв_до_человека():
    сайдкар = КОРЕНЬ / "desktop" / "sidecar"
    клиент = (сайдкар / "abop_client.py").read_text(encoding="utf-8")
    участок = клиент[клиент.index("def chat_stream"):клиент.index("# ── операции")]
    assert '"max_tokens"' not in участок, "длину свободного ответа решает сервер — сайдкар её не задаёт"
    модуль = (сайдкар / "modules" / "chat" / "module.py").read_text(encoding="utf-8")
    тело = модуль[модуль.index("def send_stream"):модуль.index("def _catalog_system")]
    assert "max_tokens" not in тело, "стрим чата снова запрашивает свою длину ответа"
    assert 'meta["finish_reason"]' in тело, "обрыв ответа не доходит от сервера до чата"
    панель = (КОРЕНЬ / "desktop" / "ui" / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")
    assert 'asst.meta.finish_reason === "length"' in панель and "meta.cut" in панель, \
        "обрыв ответа не подписан в переписке"
