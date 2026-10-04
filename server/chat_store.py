"""История чатов пользователя в Postgres — чтобы она была у человека, а не у машины.

Переписка жила в локальной базе сайдкара: на каждой машине своя, и чистка кэша или новый ноутбук
означали «истории не было». Для продукта, где в чате принимаются решения и запускаются агенты, это
потеря рабочего журнала, а не неудобство: в карточках прогонов лежат ссылки на прогоны, вердикты и
подтверждения внешних действий.

Ключ владения — `sub` из JWT (стабильный идентификатор пользователя Keycloak), как в
`userdata_store`. Чужую переписку не отдаём и не правим: проверка владельца стоит в каждом запросе, а
не в интерфейсе.

chat_threads{id, user_sub, title, profile, skills JSONB, favorite, created_at, updated_at}
chat_messages{id, thread_id, role, content, meta JSONB, created_at}

Сообщение несёт `meta` целиком: карточка прогона, решение оркестратора, отметка отмены. Именно из
meta история берёт факты прогонов — без них модель слепа к результатам своего же разговора.
Без DSN — фолбэк в память процесса (тесты, локальный запуск без Postgres).
"""
from __future__ import annotations

import json
import time

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_threads (
    id          BIGSERIAL PRIMARY KEY,
    user_sub    TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT 'Новый чат',
    profile     TEXT NOT NULL DEFAULT 'standard',
    skills      JSONB NOT NULL DEFAULT '[]'::jsonb,
    favorite    BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_threads_user_idx ON chat_threads (user_sub, updated_at DESC);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   BIGINT NOT NULL REFERENCES chat_threads (id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_thread_idx ON chat_messages (thread_id, id);
"""

# Фолбэк без Postgres: тот же вид данных, но живёт только в процессе.
_MEM_THREADS: dict[int, dict] = {}
_MEM_MSGS: dict[int, list[dict]] = {}
_SEQ = {"thread": 0, "msg": 0}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        for chunk in SCHEMA.split(";"):
            stmt = chunk.strip()
            if stmt:
                await conn.execute(stmt)


def _thread_row(r) -> dict:
    return {"id": r[0], "title": r[1], "profile": r[2], "skills": list(r[3] or []),
            "favorite": bool(r[4]), "created_at": r[5].isoformat() if r[5] else None,
            "updated_at": r[6].isoformat() if r[6] else None}


async def threads(user_sub: str, limit: int = 100) -> list[dict]:
    """Чаты пользователя, свежие сверху. Чужие не показываем — отбор по владельцу в запросе."""
    if not _has_pg():
        rows = [t for t in _MEM_THREADS.values() if t["user_sub"] == user_sub]
        rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return [{k: v for k, v in t.items() if k != "user_sub"} for t in rows[:limit]]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,title,profile,skills,favorite,created_at,updated_at FROM chat_threads "
            "WHERE user_sub=%s ORDER BY updated_at DESC LIMIT %s", (user_sub, limit))
        return [_thread_row(r) for r in await cur.fetchall()]


async def create(user_sub: str, title: str = "Новый чат", profile: str = "standard",
                 skills: list | None = None) -> dict:
    if not _has_pg():
        _SEQ["thread"] += 1
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        t = {"id": _SEQ["thread"], "user_sub": user_sub, "title": title or "Новый чат",
             "profile": profile or "standard", "skills": list(skills or []), "favorite": False,
             "created_at": now, "updated_at": now}
        _MEM_THREADS[t["id"]] = t
        _MEM_MSGS[t["id"]] = []
        return {k: v for k, v in t.items() if k != "user_sub"}
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "INSERT INTO chat_threads (user_sub,title,profile,skills) VALUES (%s,%s,%s,%s) "
            "RETURNING id,title,profile,skills,favorite,created_at,updated_at",
            (user_sub, title or "Новый чат", profile or "standard", json.dumps(list(skills or []))))
        return _thread_row(await cur.fetchone())


async def owns(user_sub: str, thread_id: int) -> bool:
    """Чей это чат. Проверяется в КАЖДОМ запросе: право на чтение чужой переписки не выдаётся
    интерфейсом, а подтверждается владельцем в базе."""
    if not _has_pg():
        t = _MEM_THREADS.get(int(thread_id))
        return bool(t and t["user_sub"] == user_sub)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT 1 FROM chat_threads WHERE id=%s AND user_sub=%s",
                                 (int(thread_id), user_sub))
        return bool(await cur.fetchone())


async def patch(user_sub: str, thread_id: int, **fields) -> dict | None:
    """Правка названия, профиля, набора навыков, пометки «избранное»."""
    if not await owns(user_sub, thread_id):
        return None
    allowed = {k: v for k, v in fields.items() if k in ("title", "profile", "skills", "favorite") and v is not None}
    if not allowed:
        return None
    if not _has_pg():
        t = _MEM_THREADS[int(thread_id)]
        t.update(allowed)
        t["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return {k: v for k, v in t.items() if k != "user_sub"}
    sets, args = [], []
    for k, v in allowed.items():
        sets.append(f"{k}=%s")
        args.append(json.dumps(list(v)) if k == "skills" else v)
    args += [int(thread_id), user_sub]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            f"UPDATE chat_threads SET {', '.join(sets)}, updated_at=now() WHERE id=%s AND user_sub=%s "
            "RETURNING id,title,profile,skills,favorite,created_at,updated_at", tuple(args))
        r = await cur.fetchone()
    return _thread_row(r) if r else None


async def delete(user_sub: str, thread_id: int) -> bool:
    if not await owns(user_sub, thread_id):
        return False
    if not _has_pg():
        _MEM_THREADS.pop(int(thread_id), None)
        _MEM_MSGS.pop(int(thread_id), None)
        return True
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("DELETE FROM chat_threads WHERE id=%s AND user_sub=%s", (int(thread_id), user_sub))
    return True


async def messages(user_sub: str, thread_id: int, limit: int = 400) -> list[dict]:
    if not await owns(user_sub, thread_id):
        return []
    if not _has_pg():
        return list(_MEM_MSGS.get(int(thread_id)) or [])[-limit:]
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,role,content,meta,created_at FROM chat_messages WHERE thread_id=%s "
            "ORDER BY id LIMIT %s", (int(thread_id), limit))
        return [{"id": r[0], "role": r[1], "content": r[2], "meta": dict(r[3] or {}),
                 "created_at": r[4].isoformat() if r[4] else None} for r in await cur.fetchall()]


async def add(user_sub: str, thread_id: int, role: str, content: str,
              meta: dict | None = None) -> dict | None:
    """Сообщение в переписку. `meta` сохраняется целиком: из него история берёт факты прогонов."""
    if not await owns(user_sub, thread_id):
        return None
    if not _has_pg():
        _SEQ["msg"] += 1
        m = {"id": _SEQ["msg"], "role": role, "content": content or "", "meta": dict(meta or {}),
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _MEM_MSGS.setdefault(int(thread_id), []).append(m)
        _MEM_THREADS[int(thread_id)]["updated_at"] = m["created_at"]
        return m
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "INSERT INTO chat_messages (thread_id,role,content,meta) VALUES (%s,%s,%s,%s) "
            "RETURNING id,role,content,meta,created_at",
            (int(thread_id), role, content or "", json.dumps(dict(meta or {}), ensure_ascii=False)))
        r = await cur.fetchone()
        await conn.execute("UPDATE chat_threads SET updated_at=now() WHERE id=%s", (int(thread_id),))
    return {"id": r[0], "role": r[1], "content": r[2], "meta": dict(r[3] or {}),
            "created_at": r[4].isoformat() if r[4] else None}


async def patch_meta(user_sub: str, thread_id: int, message_id: int, meta: dict) -> dict | None:
    """Дописать поля в meta сообщения: решение по подтверждению, номер заведённой задачи, отмена.

    Иначе решение жило только во вкладке: после переоткрытия чата кнопка «Подтвердить» снова была
    активна, а ссылка на заведённую задачу исчезала.
    """
    if not await owns(user_sub, thread_id):
        return None
    if not _has_pg():
        for m in _MEM_MSGS.get(int(thread_id)) or []:
            if int(m["id"]) == int(message_id):
                ra = dict((m["meta"] or {}).get("run_agent") or {})
                ra.update((meta or {}).get("run_agent") or {})
                m["meta"].update({k: v for k, v in (meta or {}).items() if k != "run_agent"})
                if ra:
                    m["meta"]["run_agent"] = ra
                return m
        return None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT meta FROM chat_messages WHERE id=%s AND thread_id=%s",
                                 (int(message_id), int(thread_id)))
        r = await cur.fetchone()
        if not r:
            return None
        cur_meta = dict(r[0] or {})
        ra = dict(cur_meta.get("run_agent") or {})
        ra.update((meta or {}).get("run_agent") or {})
        cur_meta.update({k: v for k, v in (meta or {}).items() if k != "run_agent"})
        if ra:
            cur_meta["run_agent"] = ra
        await conn.execute("UPDATE chat_messages SET meta=%s WHERE id=%s",
                           (json.dumps(cur_meta, ensure_ascii=False), int(message_id)))
    return {"id": int(message_id), "meta": cur_meta}
