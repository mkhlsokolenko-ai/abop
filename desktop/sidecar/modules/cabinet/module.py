"""Модуль «Кабинет» — расход/квота и (позже) RBAC + рабочие источники.

v1: usage через шлюз (my_usage → недельные токены/остаток/стоимость). RBAC и коннекторы
рабочих источников (Word/Excel/ИС) — заглушки статуса, разворачиваются в отдельные модули.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ... import abop_client as abop

MANIFEST = {"id": "cabinet", "title": "Кабинет", "icon": "cabinet", "ui": "cabinet", "order": 90}

router = APIRouter()


class АдресIn(BaseModel):
    адрес: str


@router.get("/abop")
def abop_address() -> dict:
    """Адрес установки ABOP, откуда он взят и что будет, если его сменить."""
    from ... import config  # noqa: PLC0415
    источник = config.источник_адреса()
    return {"ok": True, "адрес": config.адрес_abop(), "источник": источник,
            "умолчание": config.ABOP_ПО_УМОЛЧАНИЮ,
            # Переменная среды сильнее настройки: поле, значение которого молча перекрыто,
            # выглядит сломанным, поэтому экран обязан об этом сказать.
            "перекрыт_средой": источник == "переменная среды"}


@router.post("/abop")
def abop_address_save(тело: АдресIn) -> dict:
    """Сменить адрес ABOP — ТОЛЬКО после успешной пробы.

    Сохранить непроверенный адрес значит оставить человека без связи и без понимания, почему:
    экран будет говорить «ABOP недоступен», а причина — опечатка в адресе, которую он сам же и
    внёс. Поэтому сначала `GET {адрес}/api/health`, и лишь потом запись.
    """
    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    from ... import auth, config  # noqa: PLC0415

    адрес = str(тело.адрес or "").strip().rstrip("/")
    if not адрес:
        return {"ok": False, "error": "пустой адрес"}
    if not адрес.startswith(("http://", "https://")):
        return {"ok": False, "error": "адрес должен начинаться с http:// или https://"}
    try:
        адрес.encode("ascii")
    except UnicodeEncodeError:
        # urllib на нелатинский адрес падает UnicodeEncodeError, и человек видел бы
        # «'ascii' codec can't encode characters» вместо понятного отказа.
        return {"ok": False, "error": "в адресе нелатинские символы: домен на кириллице нужно "
                                      "записать в punycode (xn--…)"}
    try:
        with urllib.request.urlopen(адрес + "/api/health", timeout=5) as r:
            тело_ответа = _json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "по адресу %s ответ HTTP %d — это не ABOP или он закрыт"
                                      % (адрес, e.code)}
    except urllib.error.URLError as e:
        return {"ok": False, "error": "%s не отвечает: %s" % (адрес, str(getattr(e, "reason", e))[:120])}
    except Exception as e:  # noqa: BLE001 — незнакомый отказ тоже обязан быть назван
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    if not isinstance(тело_ответа, dict) or "ok" not in тело_ответа and "status" not in тело_ответа:
        # Ответил кто-то другой: подменять адрес установки на случайный сервер нельзя.
        return {"ok": False, "error": "по адресу %s ответил не ABOP" % адрес}
    cfg = auth.load_cfg()
    cfg["abop_base_url"] = адрес
    auth.save_cfg(cfg)
    return {"ok": True, "адрес": config.адрес_abop(), "источник": config.источник_адреса(),
            "перекрыт_средой": config.источник_адреса() == "переменная среды",
            "дальше": "адрес действует сразу — перезапускать приложение не нужно"}


@router.get("/guides")
def guides() -> dict:
    """Руководства поставки, которые отдаёт ABOP. Ссылки делаем абсолютными: десктоп открывает их
    во внешнем браузере, а относительный путь там не разрешится."""
    from ... import config  # noqa: PLC0415 — локальный импорт: реестр модулей не должен тянуть конфиг
    try:
        data = abop._req("GET", "/api/guides")
    except Exception as e:  # noqa: BLE001 — отсутствие руководств не должно ронять кабинет
        return {"guides": [], "error": str(e)[:200]}
    base = config.ABOP.rstrip("/")
    out = []
    for g in ((data or {}).get("guides") or []) if isinstance(data, dict) else []:
        out.append({**g,
                    "html": (base + g["html"]) if g.get("html") else "",
                    "pdf": (base + g["pdf"]) if g.get("pdf") else ""})
    return {"guides": out}


@router.get("/usage")
def usage() -> dict:
    """Профиль пользователя из ABOP (отдел/роли/уровень). Детальный расход токенов/стоимость —
    в наблюдаемости ABOP (Grafana/метрики), а не в курсовом шлюзе."""
    try:
        r = abop.me()
        usr = (r or {}).get("user", r) or {}
        h = {}
        try:
            h = abop.health()
        except abop.AbopError:
            h = {}
        return {"ok": True, "user": usr, "department": usr.get("department"),
                "roles": usr.get("roles") or [], "level": usr.get("level"),
                "runtime": {"llm": h.get("llm"), "status": h.get("status") or h.get("ok")}}
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}


@router.get("/billing")
def billing() -> dict:
    """Счётчик токенов/квота: реальные токены из RunMetrics + остаток (токен-квота ABOP)."""
    try:
        return abop.billing()
    except abop.AbopError as e:
        return {"error": str(e)}


@router.post("/telegram/code")
def telegram_code() -> dict:
    """Код на привязку Telegram. Сам токен канала десктоп НЕ получает и не хранит: его выдаёт ABOP
    боту в обмен на этот код. Так в приложении не появляется ещё одного секрета."""
    try:
        return abop.channel_code("telegram")
    except abop.AbopError as e:
        return {"ok": False, "error": e.detail or str(e), "status": e.status}


@router.get("/channels")
def channels() -> dict:
    """Чем человек привязан. Видно, когда каналом пользовались: забытая привязка — открытая дверь."""
    try:
        return abop.channels()
    except abop.AbopError as e:
        return {"ok": False, "error": e.detail or str(e), "привязки": []}


@router.delete("/channels/{binding}")
def channel_revoke(binding: str) -> dict:
    try:
        return abop.channel_revoke(binding)
    except abop.AbopError as e:
        return {"ok": False, "error": e.detail or str(e)}


@router.get("/sources")
def sources() -> dict:
    """Рабочие источники: подключены в ABOP Data Plane (см. модуль «Источники»); локальные файлы —
    под правами ОС. RBAC/ABAC — на стороне ABOP (realm abop)."""
    return {"connectors": [
        {"id": "abop-data", "title": "ABOP Data Plane (коннекторы + рецепты)", "status": "ready"},
        {"id": "local-office", "title": "Локальные файлы Office (Word/Excel)", "status": "ready"},
        {"id": "recent-files", "title": "Последние рабочие файлы", "status": "ready"},
    ], "rbac": {"status": "enforced", "note": "роли/доступ к инструментам и источникам — RBAC/ABAC ABOP (realm abop)"}}
