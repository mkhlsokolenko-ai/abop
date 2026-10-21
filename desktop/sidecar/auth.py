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
    # Удача снимает пометки о сбое: иначе интерфейс продолжал бы просить вход у вошедшего.
    for k in ("нужен_вход", "почему", "когда", "пауза_до"):
        cfg.pop(k, None)
    save_cfg(cfg)


# Обновление токена — одно в полёте. Прежде каждый вызывающий звал Keycloak сам: в логе стенда
# стоят пары запросов на обновление в 1–9 мс друг от друга, а при включённой ротации refresh-токена
# второй предъявляет уже погашенный — «Token is not active». На медленной связи (VPN) окно гонки
# шире, и промах становится правилом.
_ЗАМОК_ОБНОВЛЕНИЯ = threading.Lock()

# Пауза после сетевого сбоя: не превращать офлайн в долбёжку. За шесть часов стенд принял 2524
# неудачных обновления и ни одного удачного — это не диагностика, это шум, в котором тонет причина.
ПАУЗА_ПОСЛЕ_СБОЯ_С = 15


def _сессия_жива(cfg: dict) -> bool:
    return bool(cfg.get("access_token")) and time.time() < cfg.get("expires_at", 0)


def _сессия_мертва(почему: str) -> None:
    """Keycloak отказал окончательно: токены стираем, причину храним — её покажет интерфейс.

    Разница между «нет сети» и «сессия кончилась» для человека принципиальна: первое пройдёт само,
    второе требует войти заново. Молча отдавать None в обоих случаях значит показывать одно и то же
    пустое окно и ждать, что пользователь догадается.
    """
    cfg = load_cfg()
    for k in ("access_token", "refresh_token", "expires_at", "пауза_до"):
        cfg.pop(k, None)
    cfg["нужен_вход"] = True
    cfg["почему"] = почему
    cfg["когда"] = time.time()
    save_cfg(cfg)


def состояние() -> dict:
    """Что показать человеку: вошёл ли, нужно ли входить заново и почему."""
    cfg = load_cfg()
    return {"нужен_вход": bool(cfg.get("нужен_вход")),
            "почему": str(cfg.get("почему") or ""),
            "ждём_до": float(cfg.get("пауза_до") or 0),
            "запомнили": bool(cfg.get("запомнили"))}


def _почему_отказ(ex: urllib.error.HTTPError) -> tuple:
    """Разбирает отказ Keycloak: (окончательный ли, текст для человека)."""
    код, текст = "", ""
    try:
        тело = json.loads(ex.read().decode("utf-8", "replace"))
        код = str(тело.get("error") or "")
        текст = str(тело.get("error_description") or "")
    except Exception:  # noqa: BLE001 — тело может быть пустым или не JSON
        pass
    окончательный = ex.code in (400, 401) or код in (
        "invalid_grant", "invalid_token", "unauthorized_client", "invalid_client")
    понятно = {"invalid_grant": "срок сессии истёк или вход был выполнен в другом месте",
               "invalid_token": "сервер больше не признаёт сохранённый токен",
               "unauthorized_client": "клиенту «%s» запрещено обновлять токен" % config.CLIENT,
               "invalid_client": "клиент «%s» не настроен в Keycloak" % config.CLIENT}
    причина = понятно.get(код) or текст or ("Keycloak ответил HTTP %s" % ex.code)
    return окончательный, причина


def refresh() -> bool:
    with _ЗАМОК_ОБНОВЛЕНИЯ:
        cfg = load_cfg()
        # Пока ждали замок, сосед мог уже обновить — тогда идти в Keycloak незачем и вредно.
        if _сессия_жива(cfg):
            return True
        rt = cfg.get("refresh_token")
        if not rt:
            return False
        if time.time() < cfg.get("пауза_до", 0):
            return False
        try:
            tok = _post_form(config.KC + "/token", urllib.parse.urlencode({
                "grant_type": "refresh_token", "client_id": config.CLIENT,
                "refresh_token": rt}).encode())
        except urllib.error.HTTPError as ex:
            окончательный, причина = _почему_отказ(ex)
            if окончательный:
                _сессия_мертва("Войдите заново: " + причина)
            else:
                cfg["пауза_до"] = time.time() + ПАУЗА_ПОСЛЕ_СБОЯ_С
                cfg["почему"] = "Сервер входа отвечает ошибкой: " + причина
                save_cfg(cfg)
            return False
        except Exception as ex:  # noqa: BLE001 — сеть, прокси, VPN: состояние, а не поломка
            # Токен НЕ стираем: офлайн кончится, и сессия должна пережить его.
            cfg["пауза_до"] = time.time() + ПАУЗА_ПОСЛЕ_СБОЯ_С
            cfg["почему"] = "Нет связи с сервером входа (%s)" % type(ex).__name__
            save_cfg(cfg)
            return False
        _store_tokens(tok)
        return True


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


СТРАНИЦА_ОК = (
    "<html><head><meta charset='utf-8'></head>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:60px;"
    "background:#0B0F14;color:#E6EDF3'><h2>Вход выполнен ✓</h2>"
    "<p>Можно вернуться в приложение и закрыть вкладку.</p></body></html>")


def _страница_отказа(почему: str) -> bytes:
    return ("<html><head><meta charset='utf-8'></head>"
            "<body style='font-family:sans-serif;text-align:center;padding-top:60px;"
            "background:#0B0F14;color:#E6EDF3'><h2>Вход не завершён</h2>"
            f"<p style='color:#fbbf24'>{почему}</p>"
            "<p>Вернитесь в приложение и попробуйте снова.</p></body></html>").encode()


def login(idp: str = "github", timeout_s: int = 300, on_url=None,
          запомнить: bool = True) -> dict:
    """Запускает loopback-PKCE вход; блокирует до редиректа или таймаута.

    `on_url(url)` вызывается СРАЗУ, как адрес готов, — так вызывающий открывает браузер именно на
    этой попытке. Прежде адрес передавался атрибутом `login.last_url`, и он переживал попытку: на
    второй вход маршрут находил прошлый адрес с прошлым портом, браузер уходил туда, а того порта
    уже не было — страница `/callback` падала с «не удаётся подключиться». Общего состояния между
    попытками здесь больше нет.

    Возвращает {ok, user} либо {ok:False, error}.
    """
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(16))
    holder: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            код = (q.get("code") or [None])[0]
            ошибка = (q.get("error_description") or q.get("error") or [None])[0]
            if код:
                holder["code"] = код
                holder["state"] = (q.get("state") or [None])[0]
            elif ошибка:
                holder["error"] = str(ошибка)[:300]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            # Отказ в Keycloak (закрыли окно, нет прав) должен читаться с экрана, а не выглядеть
            # как успешный вход, после которого приложение почему-то не вошло.
            self.wfile.write(СТРАНИЦА_ОК.encode() if код else _страница_отказа(
                ошибка or "Keycloak не прислал код авторизации."))

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    srv.timeout = 1
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    # «Запомнить меня» — это offline-сессия Keycloak. Обычная живёт 8 часов простоя и 24 часа
    # максимум: приложение, закрытое на ночь, утром встречает человека просьбой войти заново, и
    # именно это выглядело как «токен не приходит». Offline-токен это переживает.
    #
    # Цена честная: на диске остаётся долгоживущий ключ к рабочим системам. Поэтому файл настроек
    # с правами 600, а выход ОТЗЫВАЕТ токен на сервере — иначе «вышел» означало бы только
    # «забыл локально», а ключ продолжал бы действовать.
    область = "openid offline_access" if запомнить else "openid"
    params = {"client_id": config.CLIENT, "response_type": "code", "scope": область,
              "redirect_uri": redirect, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", "kc_idp_hint": idp}
    url = config.KC + "/auth?" + urllib.parse.urlencode(params)

    # Сервер слушает, ПОКА не придёт код. Прежде он обрабатывал ровно один запрос
    # (`handle_request` в потоке): любое обращение браузера до кода — предварительное соединение,
    # `/favicon.ico`, повтор страницы — съедало единственный заход, и настоящий `/callback`
    # получал отказ соединения.
    стоп = threading.Event()

    def _слушать():
        while not стоп.is_set() and not holder.get("code"):
            srv.handle_request()          # srv.timeout=1 → цикл проверяет флаг раз в секунду

    поток = threading.Thread(target=_слушать, daemon=True)
    поток.start()
    if callable(on_url):
        on_url(url)
    login.last_url = url  # type: ignore[attr-defined]   # совместимость: прежние вызывающие

    for _ in range(timeout_s):
        if holder.get("code") or holder.get("error"):
            break
        time.sleep(1)
    # Сначала дожидаемся, пока слушающий поток выйдет, и только потом закрываем сокет. Закрытие
    # «из-под» потока, сидящего в select, на Windows даёт WinError 10038 (операция на объекте, не
    # являющемся сокетом) — поймано тестом входа.
    стоп.set()
    поток.join(timeout=3)
    srv.server_close()
    if holder.get("error"):
        return {"ok": False, "error": f"Keycloak отказал во входе: {holder['error']}"}
    if not holder.get("code"):
        return {"ok": False, "error": ("Вход не завершён: код авторизации не пришёл. Проверьте, что "
                                       f"{config.KC.split('/realms')[0]} доступен с этого компьютера "
                                       "(VPN, прокси, брандмауэр).")}
    if holder.get("state") != state:
        return {"ok": False, "error": "Ответ не совпал с запросом входа (state) — попробуйте снова."}
    try:
        tok = _post_form(config.KC + "/token", urllib.parse.urlencode({
            "grant_type": "authorization_code", "client_id": config.CLIENT,
            "code": holder["code"], "redirect_uri": redirect, "code_verifier": verifier}).encode())
        _store_tokens(tok)
        cfg = load_cfg()
        # Что Keycloak ВЫДАЛ, а не что мы просили: offline-область могла быть не разрешена
        # клиенту, и обещать «запомнили» в этом случае — ложь, которая вскроется через сутки.
        cfg["запомнили"] = "offline" in str(tok.get("scope") or область)
        save_cfg(cfg)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Обмен кода на токен не удался: {e}"}
    return {"ok": True, "user": claims().get("preferred_username") or "студент",
            "запомнили": bool(load_cfg().get("запомнили"))}


def logout() -> dict:
    """Выход: сначала отозвать сессию на сервере, потом забыть локально.

    С долгоживущей offline-сессией «забыл локально» — это не выход: ключ остаётся действующим на
    сервере, и украденный файл настроек продолжает работать. Поэтому сначала выход в Keycloak, и
    человеку честно говорится, удалось ли это: если нет (офлайн), сессию надо закрыть в браузере.
    """
    cfg = load_cfg()
    rt = cfg.get("refresh_token")
    отозван, почему = False, ""
    if rt:
        try:
            _post_form(config.KC + "/logout", urllib.parse.urlencode({
                "client_id": config.CLIENT, "refresh_token": rt}).encode())
            отозван = True
        except urllib.error.HTTPError as ex:
            # 400 на отзыв значит, что сессии уже нет — для выхода это успех, а не сбой.
            отозван = ex.code in (400, 404)
            почему = "" if отозван else "сервер ответил HTTP %s" % ex.code
        except Exception as ex:  # noqa: BLE001 — офлайн: локально выйдем, на сервере нет
            почему = "нет связи с сервером входа (%s)" % type(ex).__name__
    for k in ("access_token", "refresh_token", "expires_at", "нужен_вход", "почему", "когда",
              "пауза_до", "запомнили"):
        cfg.pop(k, None)
    save_cfg(cfg)
    return {"отозван": отозван, "почему": почему}
