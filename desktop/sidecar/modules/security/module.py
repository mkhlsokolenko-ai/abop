"""Модуль «Безопасность» (AdminScreens): RBAC-политика, периметр агента, эскалация,
ре-аттестация, аудит ИБ. Пока структура/предпросмотр — реальный энфорс на шлюзе (роли Keycloak).
Отдаёт скелет политики для UI; правки будут писаться в аудит при подключении RBAC.
"""
from __future__ import annotations

from fastapi import APIRouter

from ... import gateway

MANIFEST = {"id": "security", "title": "Безопасность", "icon": "security", "ui": "security", "order": 80}
router = APIRouter()


@router.get("/me")
def me() -> dict:
    """Реальные права текущего пользователя со шлюза (RBAC по ролям Keycloak)."""
    try:
        return {"ok": True, **gateway.portal_get("/api/my/permissions")}
    except gateway.AuthRequired:
        return {"ok": False, "error": "auth_required"}
    except gateway.GatewayError as e:
        return {"ok": False, "error": str(e)}


@router.get("/policy")
def policy() -> dict:
    # Скелет по макету AdminScreens. Реальные роли придут из Keycloak, энфорс — на шлюзе.
    return {
        "roles": [
            {"role": "manager", "allowed": ["chat", "agents:run", "export"], "denied": ["admin", "connectors:write"]},
            {"role": "analyst", "allowed": ["chat", "agents:run", "data:read", "export"], "denied": ["admin"]},
            {"role": "lecturer", "allowed": ["*"], "denied": []},
        ],
        "perimeter": "Агент не может получить прав больше, чем у роли, которая его собрала. "
                     "Проверяется при сборке и деплое, вручную не редактируется.",
        "escalation": {"threshold": "высокая цена ошибки / выход наружу", "note": "конфиг на сервере, изменение → аудит ИБ"},
        "reattest": {"period_days": 90, "note": "очередь ре-аттестации прав; просрочка → блок"},
        "audit_note": "Хэш-цепочка: каждая запись ссылается на предыдущую; правка задним числом видна сверкой.",
        "enforced": False,  # станет True при подключении RBAC на шлюзе
    }
