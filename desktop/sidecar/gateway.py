"""Клиент курсового MCP-шлюза (JSON-RPC / streamable-http), порт из CLI `ape`.

Инструменты шлюза: chat (профиль code/research/standard), rag_index, rag_search.
Модули сайдкара ходят в модели ТОЛЬКО через этот файл — единая точка и единый JWT.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request

from . import auth, config


class GatewayError(RuntimeError):
    pass


class AuthRequired(GatewayError):
    pass


def portal_get(path: str, timeout: int = 20) -> dict:
    """Authed GET к portal_api (REST), под JWT пользователя. Для RBAC/permissions и пр."""
    tok = auth.token()
    if not tok:
        raise AuthRequired("Не выполнен вход")
    req = urllib.request.Request(config.PORTAL + path, headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise GatewayError(f"portal {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise GatewayError(str(e)) from e


def portal_post(path: str, body: dict, timeout: int = 30) -> dict:
    """Authed POST (JSON) к portal_api под JWT пользователя."""
    tok = auth.token()
    if not tok:
        raise AuthRequired("Не выполнен вход")
    req = urllib.request.Request(config.PORTAL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise GatewayError("нет права (403)") from e
        raise GatewayError(f"portal {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise GatewayError(str(e)) from e


def call(tool: str, args: dict, timeout: int = 120) -> dict:
    tok = auth.token()
    if not tok:
        raise AuthRequired("Не выполнен вход")
    sid = {"v": None}

    def rpc(method, params=None, notify=False, retry=True):
        nonlocal tok
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = 1
        if params is not None:
            body["params"] = params
        hdr = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": "Bearer " + tok}
        if sid["v"]:
            hdr["mcp-session-id"] = sid["v"]
        req = urllib.request.Request(config.MCP, data=json.dumps(body).encode(), headers=hdr)
        last = None
        for attempt in range(4):
            try:
                r = urllib.request.urlopen(req, timeout=timeout)
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and retry and auth.refresh():
                    tok = auth.token() or tok
                    return rpc(method, params, notify, retry=False)
                raise GatewayError(f"Шлюз вернул {e.code}") from e
            except (ssl.SSLError, urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
        else:
            raise GatewayError(f"Сеть нестабильна, шлюз недоступен: {last}")
        if not sid["v"] and r.headers.get("mcp-session-id"):
            sid["v"] = r.headers.get("mcp-session-id")
        if notify:
            return None
        raw = r.read().decode()
        if "text/event-stream" in (r.headers.get("Content-Type") or ""):
            raw = "".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
        return json.loads(raw)

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "ape-desktop", "version": config.APP_VERSION}})
    rpc("notifications/initialized", notify=True)
    res = rpc("tools/call", {"name": tool, "arguments": args})
    result = (res or {}).get("result", {})
    # FastMCP отдаёт structured content в structuredContent, иначе — в content[0].text
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content") or []
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except Exception:
            return {"text": content[0]["text"]}
    return result
