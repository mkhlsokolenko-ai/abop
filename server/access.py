"""ABAC-гейт доступа АГЕНТОВ (и людей) к системам реестра — least-privilege на MCP-шлюзе.

Идея владельца (2026-09-15): агент-аналитик не должен видеть данные архитектуры; проверку прав
делаем на MCP-шлюзе (server/tools/*) поверх реестра систем (systems_store.scope). Ключ доступа —
`department`/`family` из идентичности (JWT-claim department у людей, agent.family у агентов).
Плюс изоляция вектора: Qdrant-тенант НА СЕМЬЮ (family → своя коллекция), чтобы RAG-корпуса не
пересекались. Один механизм → изоляция и эндпоинтов, и знания. См. agent-rbac-mcp-gateway.

Переиспользуется: инструменты MCP-шлюза (rag/chat), исполнитель прогона (execute_agent_run),
Web API (эндпоинты проверки/манифеста). Отказы пишем в audit_log (наблюдаемость least-privilege).
"""
from __future__ import annotations

import re

from . import systems_store


def scope_key(*, department: str | None = None, family: str | None = None) -> str:
    """Ключ ABAC-области. admin/support приходят с department='*' (сквозной доступ)."""
    return (department or family or "*") or "*"


def can_reach_system(key: str, system: dict) -> tuple[bool, str]:
    """Вправе ли область `key` (отдел/семья) обращаться к системе? Пустой scope системы ⇒ всем;
    '*' (admin/support) ⇒ везде; иначе key ∈ system.scope. Возвращает (allowed, причина)."""
    if not system:
        return False, "нет такой системы"
    if systems_store.allowed_for(system, key):
        scope = system.get("scope") or []
        if not scope:
            return True, "публичная (scope пуст)"
        if key == "*":
            return True, "сквозной доступ (admin/support)"
        return True, f"«{key}» в scope {scope}"
    return False, f"«{key}» вне scope {system.get('scope')}"


async def manifest(key: str) -> dict:
    """Least-privilege манифест для области `key`: какие системы доступны/закрыты + Qdrant-тенант.
    Наблюдаемость и основа гейта на шлюзе."""
    allowed, denied = [], []
    for s in await systems_store.all():
        ok, reason = can_reach_system(key, s)
        (allowed if ok else denied).append({"id": s["id"], "kind": s["kind"], "egress": s["egress"],
                                            "scope": s.get("scope") or [], "reason": reason})
    return {"scope": key, "allowed": allowed, "denied": denied,
            "qdrant_tenant": qdrant_tenant(key), "allowed_ids": [a["id"] for a in allowed]}


def qdrant_tenant(key: str) -> str:
    """Коллекция/тенант Qdrant НА СЕМЬЮ (изоляция RAG-корпуса). Совпадает с поднятыми в sLAVA
    slava_fam_<family>. '*' ⇒ общий. Имя безопасно для Qdrant."""
    k = re.sub(r"[^a-z0-9_]", "_", (key or "shared").lower())
    return "slava_fam_" + (k if k and k != "_" else "shared")


def can_reach_family(department: str | None, family: str) -> bool:
    """ABAC-изоляция знания: отдел вправе читать/писать корпус семьи? admin/support ('*') ⇒ везде;
    иначе department должен совпасть с family. Так аналитик не читает вектор-корпус архитектуры."""
    return department in ("*", None) or department == family


async def audit_denial(actor: str, key: str, system_id: str, action: str, reason: str) -> None:
    """Записать отказ доступа в аудит ИБ (least-privilege observability)."""
    try:
        from . import audit_store
        await audit_store.record(actor or "agent", "access.deny", system_id,
                                 {"scope": key, "action": action, "reason": reason}, severity="warn")
    except Exception:  # noqa: BLE001 — аудит опционален, гейт не валим
        pass
