"""Сайдкар APE Desktop: локальный FastAPI-движок.

Электрон-оболочка спавнит этот процесс и рендерит ui/ поверх него. Ядро тонкое:
auth + реестр модулей. Вся функциональность (чат, позже OCR/ML, ABOP) — в модулях.

Запуск:  python -m sidecar.app   (порт из APE_SIDECAR_PORT, иначе 8799)
"""
from __future__ import annotations

import os
import threading
import webbrowser

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import auth, config, db, registry

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)

# Оболочка ходит с localhost — разрешаем локальные origin'ы (file:// и 127.0.0.1).
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_MODULES = []


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    db.init()
    global _MODULES
    _MODULES = registry.discover()
    for m in _MODULES:
        app.include_router(m.router, prefix=f"/api/modules/{m.MANIFEST['id']}")
    print(f"[sidecar] модули: {[m.MANIFEST['id'] for m in _MODULES]}")


# ── ядро: здоровье, аутентификация, список модулей ──
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": config.APP_NAME, "version": config.APP_VERSION,
            "authed": bool(auth.token())}


@app.get("/api/modules")
def modules() -> list[dict]:
    return [m.MANIFEST for m in _MODULES]


@app.get("/api/auth/me")
def me() -> dict:
    if not auth.token():
        return {"authed": False, "roles": []}
    cl = auth.claims()
    roles = (cl.get("realm_access") or {}).get("roles") or []
    # оставляем только осмысленные для RBAC (без служебных keycloak-ролей)
    roles = [r for r in roles if not r.startswith("default-roles") and r not in
             ("offline_access", "uma_authorization")]
    return {"authed": True, "user": cl.get("preferred_username"), "roles": roles}


@app.post("/api/auth/login")
def do_login() -> dict:
    """Блокирующий loopback-PKCE вход; браузер открываем на стороне сайдкара."""
    holder: dict = {}

    def _run():
        holder["res"] = auth.login()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # дождёмся, пока auth.login выставит url, и откроем браузер
    for _ in range(50):
        url = getattr(auth.login, "last_url", None)
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass
            break
        threading.Event().wait(0.1)
    t.join()
    return holder.get("res", {"ok": False, "error": "internal"})


@app.post("/api/auth/logout")
def do_logout() -> dict:
    auth.logout()
    return {"ok": True}


def main() -> None:
    import uvicorn
    port = int(os.environ.get("APE_SIDECAR_PORT", "8799"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
