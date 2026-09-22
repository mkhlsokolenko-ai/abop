"""Тонкий клиент ABOP Web API — сайдкар проксирует запросы к ABOP /api/* под JWT пользователя.

ABOP — единственный источник истины по агентам/прогонам/HITL/данным; сайдкар лишь пробрасывает
Bearer-токен пользователя (auth.token()) и адаптирует ответы под локальный API. RBAC/ABAC решает
ABOP на сервере. См. docs/ADR_DESKTOP_ABOP_SMYCHKA.md (Фаза 1 — тонкий прокси).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import auth, config


class AbopError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"ABOP {status}: {detail}")
        self.status = status
        self.detail = detail


def _req(method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict | list:
    """Вызов ABOP /api/* с JWT пользователя. Пробрасывает статус/ошибку наверх (не глушим молча)."""
    url = config.ABOP.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    tok = auth.token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:  # noqa: BLE001
            pass
        raise AbopError(e.code, str(detail)[:400]) from None
    except Exception as e:  # noqa: BLE001 — сеть/таймаут → понятная ошибка наверх
        raise AbopError(0, f"{type(e).__name__}: {e}") from None


# ── операции, которые нужны desktop-модулю agents ──
def me() -> dict:
    return _req("GET", "/api/me", timeout=20)


def agents(contract: str = "") -> list:
    q = ("?contract=" + urllib.request.quote(contract)) if contract else ""
    r = _req("GET", "/api/agents" + q, timeout=30)
    return (r or {}).get("agents", []) if isinstance(r, dict) else (r or [])


def agent(agent_id: str) -> dict:
    return _req("GET", "/api/agents/" + urllib.request.quote(agent_id), timeout=30)


def skills() -> list:
    r = _req("GET", "/api/skills", timeout=30)
    return (r or {}).get("skills", []) if isinstance(r, dict) else (r or [])


def families() -> list:
    """Ростер Семья→Роль→Навык — палитра для конструктора цепочки агентов."""
    r = _req("GET", "/api/families", timeout=30)
    return (r or {}).get("families", []) if isinstance(r, dict) else (r or [])


def recipes() -> list:
    """Рецепты Data Plane (источник→entity) — какие данные доступны агенту."""
    r = _req("GET", "/api/data/recipes", timeout=30)
    return (r or {}).get("recipes", []) if isinstance(r, dict) else (r or [])


def author(family: str, skills: list, name: str = "", member: str = "") -> dict:
    """Создать агента-цепочку БЕЗ контракта из выбранных навыков (простые менеджерские агенты).
    Конверт/автономия — консервативно из навыков. manager+."""
    body = {"family": family, "skills": skills}
    if name:
        body["name"] = name
    if member:
        body["member"] = member
    return _req("POST", "/api/agents/author", body, timeout=60)


def prepare(scenario: str) -> dict:
    return _req("POST", "/api/demo/prepare", {"scenario": scenario}, timeout=60)


def run(agent_id: str = "", scenario: str = "", context: str = "", no_cache: bool = False) -> dict:
    """Запуск прогона. Если задан scenario — сначала demo/prepare (идемпотентно). context (выделенный
    текст/файл из host-системы) прокидывается в тело как подсказка (ABOP решит, как использовать)."""
    aid = agent_id
    if not aid and scenario:
        prep = prepare(scenario)
        aid = prep.get("agent_id", "")
    body: dict = {"agent_id": aid}
    if context:
        body["context"] = context[:20000]
    if no_cache:
        body["no_cache"] = True
    return _req("POST", "/api/runs", body, timeout=300)


def runs(agent_id: str = "") -> list:
    q = ("?agent_id=" + urllib.request.quote(agent_id)) if agent_id else ""
    r = _req("GET", "/api/runs" + q, timeout=30)
    return (r or {}).get("runs", []) if isinstance(r, dict) else (r or [])


def hitl_queue() -> list:
    r = _req("GET", "/api/hitl/queue", timeout=20)
    return (r or {}).get("queue", []) if isinstance(r, dict) else (r or [])


def hitl_approve(item_id: str, decision: str = "approve", reason: str = "") -> dict:
    return _req("POST", "/api/hitl/" + urllib.request.quote(item_id) + "/approve",
                {"decision": decision, "reason": reason}, timeout=60)


def health() -> dict:
    return _req("GET", "/api/health", timeout=10)
