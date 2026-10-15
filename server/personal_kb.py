# -*- coding: utf-8 -*-
"""Личное хранилище человека: своя коллекция в sLAVA + реестр документов в Postgres.

Зачем оно есть. Навык `bft-draft` объявляет вход `{from: "context"}`: расшифровку интервью может
принести ТОЛЬКО человек, вставив её в чат. То же у `icp-interviewer`, `jtbd-formulator`,
`researcher`, `meeting-action-items`. Значит «собери БФТ по интервью с заказчиками» сегодня не на
чем выполнить — и честное «нет данных» тут следствие отсутствия источника, а не плохого поиска.
Личное пространство закрывает именно эту дыру: материал человека лежит на сервере, и агент
находит его сам.

Лестница поиска (порядок задаёт НАВЫК, а не глобальная настройка): личное пространство →
пространство команды (вики/трекер) → корпоративный Data Plane. На каждом уровне свой провенанс.

Правило, которое важнее механики: личные источники допустимы для «сформулировать / составить /
предложить» и НЕДОПУСТИМЫ для «посчитать / сверить / утвердить». Нельзя сверять деньги по личным
заметкам, поэтому личное едет в промпт ОТДЕЛЬНЫМ блоком с этой оговоркой и никогда не смешивается
с нормами и данными: смешанная заметка становится «корпоративной истиной» и уезжает в документ
заказчику.

Изоляция: коллекция на пользователя (`slava_user_<хэш sub>`), реестр — на `user_sub`. Чужое личное
не видит никто, включая администратора: это не вопрос ролей, а вопрос того, зачем пространство
заводили.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import uuid

from . import observability as obs
from . import slava, tenancy
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_docs (
    id          TEXT PRIMARY KEY,
    user_sub    TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'note',
    chars       INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'desktop',
    tenant      TEXT NOT NULL DEFAULT 'default',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS personal_docs_user ON personal_docs (user_sub, created_at DESC);
"""

_MEM: dict[str, list[dict]] = {}          # без БД — в памяти процесса (dev/CLI)
_TEXT: dict[str, str] = {}                # текст документа для выдачи без sLAVA (dev/тесты)
ЛИМИТ_ДОКУМЕНТА = 400_000                # символов: больше — это не заметка, а выгрузка


def _has_pg() -> bool:
    # По настройке, а не по импорту пула: `server.db` тянет psycopg_pool, которого нет в
    # окружении CLI и тестов — и импорт ради проверки ронял бы всё, что сюда заглянет.
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


def collection_for(user_sub: str) -> str:
    """Имя личной коллекции. Хэш, а не адрес: имя коллекции видно в логах sLAVA."""
    h = hashlib.sha256(("abop/personal/" + str(user_sub or "")).encode("utf-8")).hexdigest()[:16]
    return "slava_user_" + h


def _ключ(user_sub: str) -> str:
    return tenancy.текущий() + "/" + str(user_sub or "")


async def add(user_sub: str, name: str, text: str, *, kind: str = "note",
              source: str = "desktop") -> dict:
    """Положить документ в личное пространство. Возвращает карточку документа."""
    user_sub = str(user_sub or "").strip()
    if not user_sub:
        raise ValueError("нет владельца документа: личное пространство адресное")
    text = str(text or "")
    if not text.strip():
        raise ValueError("пустой документ: нечего искать")
    if len(text) > ЛИМИТ_ДОКУМЕНТА:
        raise ValueError("документ больше %d символов — это выгрузка, а не личная заметка"
                         % ЛИМИТ_ДОКУМЕНТА)
    name = (str(name or "").strip() or "заметка")[:200]
    did = "pd-" + uuid.uuid4().hex[:12]
    карточка = {"id": did, "user_sub": user_sub, "name": name, "kind": (kind or "note")[:40],
                "chars": len(text), "source": (source or "desktop")[:40],
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    # Сначала индексируем, потом регистрируем: карточка без индекса обещает поиск, которого нет.
    try:
        await slava.ingest(collection_for(user_sub), name + ".txt", text,
                           tenant="abop", doc_id=did)
    except Exception as ex:  # noqa: BLE001 — хранилище знаний недоступно → документ не принят
        obs.log_event("warning", "personal_kb.ingest_failed",
                      error=f"{type(ex).__name__}: {str(ex)[:200]}")
        if not _has_pg():
            _TEXT[did] = text        # dev без sLAVA: держим текст, чтобы поиск работал локально
        else:
            raise
    else:
        _TEXT[did] = text
    if _has_pg():
        from .db import _conn
        async with _conn() as conn:
            await conn.execute(
                "INSERT INTO personal_docs (id,user_sub,name,kind,chars,source,tenant) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (did, user_sub, карточка["name"], карточка["kind"], карточка["chars"],
                 карточка["source"], tenancy.текущий()))
    else:
        _MEM.setdefault(_ключ(user_sub), []).insert(0, карточка)
    obs.log_event("info", "personal_kb.added", doc=did, chars=карточка["chars"],
                  kind=карточка["kind"])
    return карточка


async def documents(user_sub: str, limit: int = 200) -> list[dict]:
    """Реестр личных документов человека (без текста)."""
    user_sub = str(user_sub or "")
    if not user_sub:
        return []
    if not _has_pg():
        return list(_MEM.get(_ключ(user_sub)) or [])[:limit]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,user_sub,name,kind,chars,source,created_at FROM personal_docs "
            "WHERE user_sub=%s AND tenant=%s ORDER BY created_at DESC LIMIT %s",
            (user_sub, tenancy.текущий(), int(limit)))
        rows = await cur.fetchall()
    return [{"id": r[0], "user_sub": r[1], "name": r[2], "kind": r[3], "chars": r[4],
             "source": r[5], "created_at": r[6].isoformat() if r[6] else ""} for r in rows]


async def remove(user_sub: str, doc_id: str) -> bool:
    """Убрать документ из реестра личного пространства (владелец — только сам человек)."""
    user_sub, doc_id = str(user_sub or ""), str(doc_id or "")
    if not (user_sub and doc_id):
        return False
    _TEXT.pop(doc_id, None)
    if not _has_pg():
        было = _MEM.get(_ключ(user_sub)) or []
        стало = [d for d in было if d.get("id") != doc_id]
        _MEM[_ключ(user_sub)] = стало
        return len(стало) != len(было)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM personal_docs WHERE id=%s AND user_sub=%s AND tenant=%s",
                                 (doc_id, user_sub, tenancy.текущий()))
        return bool(cur.rowcount)


def _куски(текст: str, запрос: str, сколько: int) -> list[str]:
    """Отбор абзацев по словам запроса — запасной поиск, когда sLAVA недоступна.

    Это не замена вектора: это способ не терять личный материал на стенде без sLAVA, где иначе
    агент скажет «нет данных», хотя человек документ принёс.
    """
    слова = [с.lower() for с in re.findall(r"\w{4,}", запрос or "")][:12]
    абзацы = [а.strip() for а in re.split(r"\n\s*\n", текст) if а.strip()]
    если_без_слов = абзацы[:сколько]
    if not слова:
        return если_без_слов
    весом = sorted(((sum(1 for с in слова if с in а.lower()), -i, а)
                    for i, а in enumerate(абзацы)), reverse=True)
    отобрано = [а for вес, _, а in весом if вес > 0][:сколько]
    return отобрано or если_без_слов


async def search(user_sub: str, запрос: str, *, top_k: int = 4) -> list[dict]:
    """Поиск по личному пространству. Возвращает [{текст, документ}] — с именем документа.

    Имя документа едет вместе с текстом нарочно: человек должен видеть, какая его заметка повлияла
    на вывод, иначе личное знание неотличимо от выдумки модели.
    """
    user_sub = str(user_sub or "")
    if not user_sub:
        return []
    реестр = {d["id"]: d for d in await documents(user_sub)}
    if not реестр:
        return []
    куски: list[dict] = []
    try:
        res = await slava.query(collection_for(user_sub), запрос or "личные материалы",
                                top_k=top_k, tenant="abop")
        for s in (res.get("sources") or []):
            текст = str(s.get("text") or "").strip()
            if not текст:
                continue
            did = str(s.get("doc_id") or s.get("document_id") or "")
            имя = (реестр.get(did) or {}).get("name") or str(s.get("filename") or "личная заметка")
            куски.append({"текст": текст, "документ": имя})
    except Exception as ex:  # noqa: BLE001 — sLAVA недоступна → ищем по тексту, но говорим об этом
        obs.log_event("warning", "personal_kb.query_failed",
                      error=f"{type(ex).__name__}: {str(ex)[:200]}")
    if not куски:
        for did, карточка in реестр.items():
            текст = _TEXT.get(did)
            if not текст:
                continue
            for кусок in _куски(текст, запрос, max(1, top_k // max(1, len(реестр)))):
                куски.append({"текст": кусок, "документ": карточка.get("name") or "личная заметка"})
    return куски[:top_k]


def блок_для_промпта(куски: list[dict]) -> str:
    """Личное — отдельным блоком с оговоркой: иначе заметка уезжает в документ как факт организации."""
    if not куски:
        return ""
    тело = "\n---\n".join("[%s] %s" % (к.get("документ") or "личная заметка", str(к.get("текст") or "")[:800])
                          for к in куски[:6])
    return ("=== ЛИЧНОЕ ПРОСТРАНСТВО ПОЛЬЗОВАТЕЛЯ (его собственные материалы) ===\n"
            "Это личные материалы человека, а не данные учётных систем. Опираться на них можно, "
            "чтобы СФОРМУЛИРОВАТЬ, СОСТАВИТЬ или ПРЕДЛОЖИТЬ; СЧИТАТЬ, СВЕРЯТЬ и УТВЕРЖДАТЬ по ним "
            "нельзя. Ссылаясь, называй документ в квадратных скобках и не выдавай его содержимое за "
            "факт организации.\n" + тело + "\n\n")


def карточка_для_ui(d: dict) -> dict:
    """То, что показывает интерфейс: без sub владельца — он тут и так один."""
    return {"id": d.get("id"), "name": d.get("name"), "kind": d.get("kind"),
            "chars": d.get("chars"), "source": d.get("source"),
            "created_at": d.get("created_at")}


def дамп_для_отладки(user_sub: str) -> str:
    return json.dumps({"collection": collection_for(user_sub)}, ensure_ascii=False)
