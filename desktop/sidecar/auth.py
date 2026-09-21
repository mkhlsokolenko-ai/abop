"""Аутентификация: loopback-PKCE вход через GitHub (порт из CLI `ape`), хранение токенов.

Только stdlib — чтобы сайдкар легко паковался PyInstaller'ом без тяжёлых зависимостей.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

from . import config


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_cfg() -> dict:
    try:
        return json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(cfg: dict) -> None:
    config.ensure_dirs()
    config.CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    try:
        config.CONFIG_FILE.chmod(0o600)
    except Exception:
        pass


def _post_form(url: str, data: bytes) -> dict:
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _store_tokens(tok: dict) -> None:
    cfg = load_cfg()
    cfg["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        cfg["refresh_token"] = tok["refresh_token"]
    cfg["expires_at"] = time.time() + int(tok.get("expires_in", 300)) - 30
    save_cfg(cfg)


def refresh() -> bool:
    cfg = load_cfg()
    rt = cfg.get("refresh_token")
    if not rt:
        return False
    try:
        tok = _post_form(config.KC + "/token", urllib.parse.urlencode({
            "grant_type": "refresh_token", "client_id": config.CLIENT, "refresh_token": rt}).encode())
        _store_tokens(tok)
        return True
    except Exception:
        return False


def token() -> str | None:
    cfg = load_cfg()
    if not cfg.get("access_token"):
        return None
    if time.time() >= cfg.get("expires_at", 0):
        if not refresh():
            return None
        cfg = load_cfg()
    return cfg.get("access_token")


def claims() -> dict:
    try:
        p = load_cfg()["access_token"].split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def login(idp: str = "github", timeout_s: int = 300) -> dict:
    """Запускает loopback-PKCE вход; блокирует до редиректа или таймаута.

    Возвращает {ok, user} либо {ok:False, error}. Открытие браузера — на стороне вызывающего
    (Electron), сюда возвращаем url, если браузер не открылся.
    """
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(16))
    holder: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            holder["code"] = (q.get("code") or [None])[0]
            holder["state"] = (q.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body style='font-family:sans-serif;text-align:center;padding-top:60px;"
                "background:#0B0F14;color:#E6EDF3'><h2>Вход выполнен ✓</h2>"
                "<p>Можно вернуться в приложение и закрыть вкладку.</p></body></html>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    params = {"client_id": config.CLIENT, "response_type": "code", "scope": "openid",
              "redirect_uri": redirect, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", "kc_idp_hint": idp}
    url = config.KC + "/auth?" + urllib.parse.urlencode(params)

    threading.Thread(target=srv.handle_request, daemon=True).start()
    # url отдаём наружу заранее — Electron откроет системный браузер сам
    login.last_url = url  # type: ignore[attr-defined]

    for _ in range(timeout_s):
        if "code" in holder:
            break
        time.sleep(1)
    srv.server_close()
    if not holder.get("code") or holder.get("state") != state:
        return {"ok": False, "error": "Вход не завершён (таймаут или отказ)."}
    try:
        tok = _post_form(config.KC + "/token", urllib.parse.urlencode({
            "grant_type": "authorization_code", "client_id": config.CLIENT,
            "code": holder["code"], "redirect_uri": redirect, "code_verifier": verifier}).encode())
        _store_tokens(tok)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Обмен кода на токен не удался: {e}"}
    return {"ok": True, "user": claims().get("preferred_username") or "студент"}


def logout() -> None:
    cfg = load_cfg()
    for k in ("access_token", "refresh_token", "expires_at"):
        cfg.pop(k, None)
    save_cfg(cfg)
