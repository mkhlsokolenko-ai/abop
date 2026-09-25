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


def chat_stream(prompt: str, system: str = "", profile: str = "standard", max_tokens: int = 1500):
    """Проксирует SSE-стрим ABOP /api/chat/stream: генератор yield-ит строки SSE как есть
    (`data: {...}`). Настоящий стрим токенов (не псевдо-нарезка). Bearer из auth.token()."""
    url = config.ABOP.rstrip("/") + "/api/chat/stream"
    headers = {"Content-Type": "application/json"}
    tok = auth.token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    payload = {"prompt": prompt, "profile": profile, "max_tokens": max_tokens}
    if system:
        payload["system"] = system
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise AbopError(e.code, str(detail)[:400]) from None
    except Exception as e:  # noqa: BLE001
        raise AbopError(0, f"{type(e).__name__}: {e}") from None
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            try:
                yield json.loads(chunk)
            except Exception:  # noqa: BLE001
                continue


# ── операции, которые нужны desktop-модулю agents ──
def chat(prompt: str, system: str = "", context: str = "", profile: str = "standard",
         max_tokens: int = 1200) -> dict:
    """Свободный LLM-ответ ABOP (тот же self-host каскад, что и у агентов) под JWT пользователя.
    Единый рантайм-канал для desktop-чата/цепочек графа/распознавания вместо курсового шлюза."""
    body = {"prompt": prompt, "profile": profile, "max_tokens": max_tokens}
    if system:
        body["system"] = system
    if context:
        body["context"] = context
    return _req("POST", "/api/chat", body, timeout=120)


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


def connectors() -> list:
    """Коннекторы Data Plane ABOP (источники + резолв эндпоинта из реестра систем, флаг allowed по ABAC)."""
    r = _req("GET", "/api/data/connectors", timeout=30)
    return (r or {}).get("connectors", []) if isinstance(r, dict) else (r or [])


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


def run(agent_id: str = "", scenario: str = "", context: str = "", no_cache: bool = False,
        deliver: str = "") -> dict:
    """Запуск прогона. Если задан scenario — сначала demo/prepare (идемпотентно). context (выделенный
    текст/файл из host-системы) прокидывается в тело как подсказка. deliver (дерево решений чата):
    '' — все каналы; 'chat' — только в чат; имя канала — только он."""
    aid = agent_id
    if not aid and scenario:
        prep = prepare(scenario)
        aid = prep.get("agent_id", "")
    body: dict = {"agent_id": aid}
    if context:
        body["context"] = context[:20000]
    if no_cache:
        body["no_cache"] = True
    if deliver:
        body["deliver"] = deliver
    return _req("POST", "/api/runs", body, timeout=300)


def runs(agent_id: str = "") -> list:
    q = ("?agent_id=" + urllib.request.quote(agent_id)) if agent_id else ""
    r = _req("GET", "/api/runs" + q, timeout=30)
    return (r or {}).get("runs", []) if isinstance(r, dict) else (r or [])


def add_trigger(agent_id: str, cron: str, title: str = "", deliver: str = "chat",
                enabled: bool = True, hitl: bool = False) -> dict:
    """Сделать задачу агента регулярной: добавить триггер-расписание (новая версия агента)."""
    body = {"cron": cron, "deliver": deliver, "enabled": enabled, "hitl": hitl}
    if title:
        body["title"] = title
    return _req("POST", "/api/agents/" + urllib.request.quote(agent_id) + "/triggers", body, timeout=60)


def match_agents(q: str) -> list:
    """Подбор агента под задачу (лексика+семантика). Топ-агенты со score/каналами."""
    r = _req("POST", "/api/agents/match", {"q": q}, timeout=30)
    return (r or {}).get("matches", []) if isinstance(r, dict) else (r or [])


def my_schedules() -> list:
    """Расписания текущего пользователя (агент/cron/вкл/доставка/последний прогон)."""
    r = _req("GET", "/api/triggers/mine", timeout=30)
    return (r or {}).get("schedules", []) if isinstance(r, dict) else (r or [])


def del_trigger(agent_id: str, trigger_id: str) -> dict:
    return _req("DELETE", "/api/agents/" + urllib.request.quote(agent_id) + "/triggers/"
                + urllib.request.quote(trigger_id), timeout=60)


def hitl_queue() -> list:
    r = _req("GET", "/api/hitl/queue", timeout=20)
    return (r or {}).get("queue", []) if isinstance(r, dict) else (r or [])


def billing() -> dict:
    """Расход/квота: реальные токены из RunMetrics + остаток (токен-квота). cost≈0 на self-host,
    но токены списываются с квоты — пользователь видит, сколько ещё может отработать."""
    r = _req("GET", "/api/billing", timeout=30)
    return r if isinstance(r, dict) else {}


# ── Цепочки агентов (pipelines): линейный конвейер выход→контекст ──
def pipelines() -> list:
    r = _req("GET", "/api/pipelines", timeout=30)
    return (r or {}).get("pipelines", []) if isinstance(r, dict) else (r or [])


def save_pipeline(name: str, steps: list, pid: str = "") -> dict:
    return _req("POST", "/api/pipelines", {"name": name, "steps": steps, "id": pid})


def del_pipeline(pid: str) -> dict:
    return _req("DELETE", "/api/pipelines/" + urllib.request.quote(pid), timeout=30)


def del_agent(agent_id: str) -> dict:
    """Идемпотентное удаление «моего» агента (все версии) — сервер убирает его целиком."""
    return _req("DELETE", "/api/agents/" + urllib.request.quote(agent_id), timeout=30)


def run_pipeline(pid: str, context: str = "") -> dict:
    return _req("POST", "/api/pipelines/" + urllib.request.quote(pid) + "/run",
                {"context": context}, timeout=600)


def hitl_approve(item_id: str, decision: str = "approve", reason: str = "") -> dict:
    return _req("POST", "/api/hitl/" + urllib.request.quote(item_id) + "/approve",
                {"decision": decision, "reason": reason}, timeout=60)


def health() -> dict:
    return _req("GET", "/api/health", timeout=10)
