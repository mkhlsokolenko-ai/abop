"""Сайдкар APE Desktop: локальный FastAPI-движок.

Электрон-оболочка спавнит этот процесс и рендерит ui/ поверх него. Ядро тонкое:
auth + реестр модулей. Вся функциональность (чат, позже OCR/ML, ABOP) — в модулях.

Запуск:  python -m sidecar.app   (порт из APE_SIDECAR_PORT, иначе 8799)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, registry

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)

# Оболочка ходит с localhost — разрешаем локальные origin'ы (file:// и 127.0.0.1).
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── UI-прокси (правильный путь А): сайдкар отдаёт UI десктопа с 127.0.0.1/ui, тянет свежий с ABOP
#    при старте (server-side, без Chromium PNA). UI и API — один origin → PNA не мешает; правки UI
#    прилетают git-деплоем БЕЗ пересборки .exe. Фолбэк — UI, вшитый в сайдкар (офлайн/сбой сети). ──
_UI_CACHE = config.DATA_DIR / "ui-cache"


def _bundled_ui() -> Path | None:
    """UI, вшитый в сайдкар PyInstaller'ом (ui_fallback) — для первого офлайн-запуска."""
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    for cand in (base / "ui_fallback", base / "ui"):
        if cand.is_dir() and (cand / "index.html").exists():
            return cand
    return None


def _sync_ui() -> None:
    """Тянем свежий UI с ABOP (/desktop-ui-bundle) в кэш. Если недоступно и кэш пуст — вшитый фолбэк."""
    try:
        req = urllib.request.Request(config.ABOP.rstrip("/") + "/desktop-ui-bundle")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        files = data.get("files") or {}
        if files:
            tmp = _UI_CACHE.with_suffix(".new")
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            for rel, txt in files.items():
                fp = tmp / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(txt, encoding="utf-8")
            if _UI_CACHE.exists():
                shutil.rmtree(_UI_CACHE, ignore_errors=True)
            tmp.rename(_UI_CACHE)
            print(f"[sidecar] UI обновлён с ABOP: {data.get('version')} ({len(files)} файлов)")
            return
    except Exception as e:  # noqa: BLE001 — офлайн/сбой → фолбэк
        print(f"[sidecar] UI с ABOP не получен ({e}); фолбэк на вшитый/кэш")
    if not (_UI_CACHE / "index.html").exists():
        fb = _bundled_ui()
        if fb:
            _UI_CACHE.mkdir(parents=True, exist_ok=True)
            shutil.copytree(fb, _UI_CACHE, dirs_exist_ok=True)
            print("[sidecar] UI из вшитого фолбэка")


_MODULES = []


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    _UI_CACHE.mkdir(parents=True, exist_ok=True)
    _sync_ui()
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
    return {"authed": True, "user": cl.get("preferred_username"),
            "email": cl.get("email") or "", "name": cl.get("name") or "",
            "department": cl.get("department") or cl.get("family") or "", "roles": roles}


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


# UI десктопа с 127.0.0.1/ui (один origin с /api/* → без PNA). Каталог наполняется в startup (_sync_ui).
_UI_CACHE.mkdir(parents=True, exist_ok=True)
app.mount("/ui", StaticFiles(directory=str(_UI_CACHE), html=True, check_dir=False), name="ui")


def main() -> None:
    import uvicorn
    port = int(os.environ.get("APE_SIDECAR_PORT", "8799"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
