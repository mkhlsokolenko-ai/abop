"""Сайдкар APE Desktop: локальный FastAPI-движок.

Электрон-оболочка спавнит этот процесс и рендерит ui/ поверх него. Ядро тонкое:
auth + реестр модулей. Вся функциональность (чат, позже OCR/ML, ABOP) — в модулях.

Запуск:  python -m sidecar.app   (порт из APE_SIDECAR_PORT, иначе 8799)
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, registry

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)

# Оболочка ходит с того же origin (127.0.0.1/ui) либо с file:// (origin "null" — офлайн-фолбэк).
# Сайдкар прикладывает JWT пользователя к каждому запросу в ABOP, поэтому «*» недопустим
# (UX-аудит 25.09 D-C4): только loopback и file://.
app.add_middleware(
    CORSMiddleware, allow_origins=["null"], allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["*"], allow_headers=["*"],
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
        binary = data.get("binary") or {}
        if files:
            tmp = _UI_CACHE.with_suffix(".new")
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            for rel, txt in files.items():
                fp = tmp / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(txt, encoding="utf-8")
            # шрифты/иконки: base64 → байты (D-C6). Старый ABOP без «binary» → берём из вшитого UI,
            # чтобы кэш никогда не оставался без шрифтов (иначе UI рендерился в Segoe UI).
            for rel, b64 in binary.items():
                fp = tmp / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(base64.b64decode(b64))
            if not binary:
                fb = _bundled_ui()
                if fb and (fb / "fonts").is_dir():
                    shutil.copytree(fb / "fonts", tmp / "fonts", dirs_exist_ok=True)
            if _UI_CACHE.exists():
                shutil.rmtree(_UI_CACHE, ignore_errors=True)
            tmp.rename(_UI_CACHE)
            # Версию кэша кладём рядом с файлами: после перезапуска надо знать, что лежит,
            # не пересобирая бандл заново.
            try:
                (_UI_CACHE / ".version").write_text(str(data.get("version") or ""), encoding="utf-8")
            except OSError:
                pass
            print(f"[sidecar] UI обновлён с ABOP: {data.get('version')} ({len(files)} файлов, {len(binary)} бинарных)")
            return True
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
    # Модуль вправе иметь фоновую работу (присмотр за папкой). Поднимаем её здесь, по одному
    # правилу для всех: сбой одного фона не должен мешать запуску приложения целиком.
    for m in _MODULES:
        пуск = getattr(m, "запустить", None)
        if not callable(пуск):
            continue
        try:
            пуск()
        except Exception as e:  # noqa: BLE001
            print(f"[sidecar] фон модуля '{m.MANIFEST['id']}' не запущен: {e}")


# ── ядро: здоровье, аутентификация, список модулей ──
_ABOP_PING: dict = {"at": 0.0, "ok": False, "error": ""}


def _abop_state() -> dict:
    """Доступен ли сам ABOP (сервер), а не только локальный сайдкар. Кэш 8 с — UI пингует часто."""
    import time as _t
    from . import abop_client as _abop
    if _t.time() - float(_ABOP_PING.get("at") or 0) < 8:
        return {"ok": _ABOP_PING["ok"], "error": _ABOP_PING["error"]}
    try:
        _abop.health()
        _ABOP_PING.update({"at": _t.time(), "ok": True, "error": ""})
    except Exception as ex:  # noqa: BLE001 — офлайн не ошибка приложения, это состояние
        _ABOP_PING.update({"at": _t.time(), "ok": False, "error": str(ex)[:200]})
    return {"ok": _ABOP_PING["ok"], "error": _ABOP_PING["error"]}


@app.get("/api/health")
def health() -> dict:
    st = _abop_state()
    сессия = auth.состояние()
    return {"ok": True, "app": config.APP_NAME, "version": config.APP_VERSION,
            "authed": bool(auth.token()), "abop": st["ok"], "abop_error": st["error"],
            "abop_url": config.ABOP,
            "нужен_вход": сессия["нужен_вход"], "почему": сессия["почему"],
            "запомнили": сессия["запомнили"]}


@app.get("/api/modules")
def modules() -> list[dict]:
    """Разделы приложения: модули сборки плюс панели АКТИВНОЙ вертикали.

    Панель вертикали приезжает бандлом как обычный модуль (`modules/<id>/panel.js`), но в списке
    разделов появляется только пока её вертикаль активна: иначе чужой экран висел бы в рейле у
    всех. Сбой запроса профиля не ломает список — приложение остаётся таким, какое есть.
    """
    свои = [m.MANIFEST for m in _MODULES]
    try:
        from . import abop_client as abop
        профиль = (abop.vertical_active() or {}).get("активная") or {}
    except Exception:  # noqa: BLE001 — офлайн: без панелей вертикали, но со всеми своими
        return свои
    известные = {m["id"] for m in свои}
    for п in (профиль.get("панели") or []):
        ид = str(п.get("id") or "")
        if not ид or ид in известные:
            continue
        свои.append({"id": ид, "title": п.get("title") or ид, "icon": п.get("icon") or "vertical",
                     "ui": ид, "order": int(п.get("order") or 50), "вертикаль": профиль.get("id")})
    return свои


@app.get("/api/panel-data/{path:path}")
def panel_data(path: str) -> dict:
    """Чтение данных платформы для панели вертикали (только GET, только белый список путей)."""
    from . import abop_client as abop
    try:
        return {"ok": True, "data": abop.panel_read(path)}
    except Exception as ex:  # noqa: BLE001
        код = int(getattr(ex, "code", 0) or 0)
        raise HTTPException(код if 400 <= код < 600 else 502, str(ex)[:400]) from ex


@app.post("/api/panel-action/{path:path}")
def panel_action(path: str, тело: dict | None = None) -> dict:
    """Действие панели вертикали на платформе (только POST, только свой белый список).

    Появилось для пульта «Сверки» (28.10.2026): расчётчику нужно считать свою организацию и
    закрывать её период, а панель до этого умела только читать. Чтение и действие разведены по
    разным спискам намеренно — см. `abop_client.ПАНЕЛЯМ_ДЕЙСТВИЯ`.
    """
    from . import abop_client as abop
    try:
        return {"ok": True, "data": abop.panel_action(path, тело or {})}
    except Exception as ex:  # noqa: BLE001
        код = int(getattr(ex, "code", 0) or 0)
        raise HTTPException(код if 400 <= код < 600 else 502, str(ex)[:400]) from ex


@app.get("/api/auth/me")
def me() -> dict:
    if not auth.token():
        # Почему не вошли — такой же ответ, как «не вошли». Прежде интерфейс показывал одну и ту
        # же кнопку входа и при офлайне, и при кончившейся сессии, а панели в это время получали
        # 401 от сервера: со стороны это выглядело как «токен не приходит».
        сессия = auth.состояние()
        return {"authed": False, "roles": [],
                "нужен_вход": сессия["нужен_вход"], "почему": сессия["почему"],
                "запомнили": False}
    cl = auth.claims()
    roles = (cl.get("realm_access") or {}).get("roles") or []
    # оставляем только осмысленные для RBAC (без служебных keycloak-ролей)
    roles = [r for r in roles if not r.startswith("default-roles") and r not in
             ("offline_access", "uma_authorization")]
    # уровень доступа считаем так же, как сервер (analyst < manager < support < admin): интерфейс
    # показывал кнопку подтверждения всем, а отказ по правам прилетал уже после клика
    level = next((lv for lv in ("admin", "support", "manager", "analyst") if lv in roles), "analyst")
    return {"authed": True, "level": level, "can_approve": level != "analyst",
            "user": cl.get("preferred_username"),
            "email": cl.get("email") or "", "name": cl.get("name") or "",
            "department": cl.get("department") or cl.get("family") or "", "roles": roles,
            **_арендатор()}


def _арендатор() -> dict:
    """Чьи данные на экране — СО СЛОВ СЕРВЕРА.

    Считать арендатора из токена здесь нельзя: правило вычисления живёт на сервере
    (`tenancy.из_заявки`: tenant → org → organization), и местная копия разошлась бы с ним молча.
    Ровно так уже случилось с уровнем доступа — интерфейс показывал кнопку, которой не было прав.

    Нет связи — так и говорим: «неизвестно». Показать «свой» наугад значит обещать, что на экране
    данные вашей организации, не зная этого.
    """
    from . import abop_client as abop
    try:
        u = ((abop.me() or {}).get("user") or {})
    except abop.AbopError:
        return {"tenant": "", "tenant_own": "", "tenant_known": False,
                "tenant_can_switch": False, "tenant_asked": abop.просим_арендатора()}
    действует = str(u.get("tenant_active") or u.get("tenant") or "")
    свой = str(u.get("tenant_own") or u.get("tenant") or "")
    return {"tenant": действует, "tenant_own": свой, "tenant_known": bool(действует),
            # Переключать вправе только поддержка вендора — и решает это сервер. Здесь только
            # подсказка интерфейсу, показывать ли переключатель: отказ всё равно придёт с сервера.
            "tenant_can_switch": str(u.get("level") or "") in ("admin", "support"),
            "tenant_asked": abop.просим_арендатора()}


@app.post("/api/auth/tenant")
def set_tenant(body: dict | None = None) -> dict:
    """Посмотреть установку клиента (вендор) или вернуться к своей.

    Возвращаем то, что ДЕЙСТВУЕТ по мнению сервера, а не то, что попросили: переход сервер вправе
    не пустить, и в этом случае интерфейс обязан показать «свой», иначе он соврёт.
    """
    from . import abop_client as abop
    abop.смотреть_у((body or {}).get("tenant"))
    # Область локальных таблиц (вложения, задания догона) считается на каждом обращении и уже
    # включает запрошенного арендатора — сбрасывать нечего: вложения клиента не смешаются со своими.
    return _арендатор()


@app.post("/api/auth/login")
def do_login(body: dict | None = None) -> dict:
    """Блокирующий loopback-PKCE вход; браузер открываем на стороне сайдкара.

    `запомнить` (по умолчанию да) просит offline-сессию: без неё вход живёт сутки, и утром
    приложение встречает человека просьбой войти заново.
    """
    запомнить = bool((body or {}).get("запомнить", True))
    holder: dict = {}

    def _открыть(url: str) -> None:
        # Браузер открываем по адресу ЭТОЙ попытки. Прежде адрес читался из атрибута
        # `auth.login.last_url`, который переживал попытку: на второй вход опрос находил прошлый
        # адрес с прошлым портом мгновенно, браузер уходил туда, и страница /callback падала —
        # того порта уже не существовало.
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — браузер мог не открыться; адрес вернём ответом
            pass

    def _run():
        holder["res"] = auth.login(on_url=_открыть, запомнить=запомнить)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join()
    res = holder.get("res", {"ok": False, "error": "internal"})
    if res.get("ok"):
        # Старые вложения помечены просто «srv» — без указания установки. Переносим их на
        # текущую при первом же входе: иначе человек потеряет файлы в своих же чатах, а
        # раздавать их всем установкам нельзя (см. store.перенести_старые_области).
        try:
            from .modules.chat import store as _чат
            перенесено = _чат.перенести_старые_области()
            if перенесено:
                res["перенесено_вложений"] = перенесено
        except Exception:  # noqa: BLE001 — перенос не повод не пустить человека в приложение
            pass
    return res


@app.post("/api/auth/logout")
def do_logout() -> dict:
    """Выход. Отвечает, удалось ли закрыть сессию НА СЕРВЕРЕ: с долгоживущей сессией это не
    формальность — не отозванный токен остаётся действующим ключом к рабочим системам."""
    итог = auth.logout() or {}
    return {"ok": True, "отозван": bool(итог.get("отозван")), "почему": итог.get("почему", "")}


def _версия_кэша() -> str:
    """Какая версия интерфейса лежит в кэше сейчас."""
    try:
        return (_UI_CACHE / ".version").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _версия_сервера() -> tuple[str, str]:
    """Какая версия интерфейса на сервере. Возвращает (версия, почему пусто)."""
    try:
        req = urllib.request.Request(config.ABOP.rstrip("/") + "/desktop-ui-bundle")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return str(data.get("version") or ""), ""
    except Exception as ex:  # noqa: BLE001 — офлайн это состояние, а не ошибка приложения
        return "", "сервер недоступен (%s)" % type(ex).__name__


@app.get("/api/ui/version")
def ui_version() -> dict:
    """Версия интерфейса в кэше и на сервере.

    Нужна, чтобы окно могло узнать об обновлении интерфейса, НЕ перезапуская приложение: оно
    живёт в трее неделями, а синхронизация бандла раньше случалась только при старте — поэтому
    правки «Путём А» доезжали лишь после перезапуска, и выглядело это как «ничего не изменилось».
    """
    кэш = _версия_кэша()
    сервер, почему = _версия_сервера()
    return {"кэш": кэш, "сервер": сервер, "почему": почему,
            "изменилось": bool(сервер and кэш and сервер != кэш)}


@app.post("/api/ui/sync")
def ui_sync() -> dict:
    """Перетянуть интерфейс с сервера прямо сейчас. Отвечает, что было и что стало."""
    было = _версия_кэша()
    ок = bool(_sync_ui())
    стало = _версия_кэша()
    return {"ok": ок, "было": было, "стало": стало, "изменилось": bool(стало and стало != было),
            "почему": "" if ок else "сервер недоступен — остался прежний интерфейс"}


@app.get("/api/vertical")
def vertical() -> dict:
    """Профиль активной вертикали для оболочки.

    Офлайн — не ошибка приложения, а состояние: отдаём пустой профиль, и оболочка остаётся
    полной. Прятать разделы из-за того, что сервер недоступен, значит врать о возможностях.
    """
    from . import abop_client as abop
    try:
        return abop.vertical_active()
    except Exception as ex:  # noqa: BLE001
        return {"активная": None, "доступные": [], "почему": "нет связи с ABOP (%s)"
                % type(ex).__name__}


@app.post("/api/vertical/activate")
def vertical_switch(body: dict | None = None) -> dict:
    """Сменить представление. Право решает сервер: десктоп только передаёт выбор."""
    from . import abop_client as abop
    try:
        return abop.vertical_activate(str((body or {}).get("id") or ""))
    except Exception as ex:  # noqa: BLE001
        код = int(getattr(ex, "code", 0) or 0)
        raise HTTPException(код if 400 <= код < 600 else 502, str(ex)[:400]) from ex


# UI десктопа с 127.0.0.1/ui (один origin с /api/* → без PNA). Каталог наполняется в startup (_sync_ui).
_UI_CACHE.mkdir(parents=True, exist_ok=True)
app.mount("/ui", StaticFiles(directory=str(_UI_CACHE), html=True, check_dir=False), name="ui")


def main() -> None:
    import uvicorn
    port = int(os.environ.get("APE_SIDECAR_PORT", "8799"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
