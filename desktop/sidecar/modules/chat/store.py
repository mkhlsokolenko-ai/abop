"""Где лежит переписка: на сервере ABOP (вошедший пользователь) или локально (гостевой режим).

Владелец сформулировал требование так: «история чатов надо хранить в БД… нужно, чтобы при входе с
любой машины пользователь видел свою историю, хранение в стейт-машине — просто, но вообще не
переживёт отчистку кеша». Локальный SQLite сайдкара — ровно такая стейт-машина: своя на каждой
машине, умирает вместе с переустановкой. Поэтому у вошедшего пользователя переписка живёт в
Postgres ABOP (`chat_threads`/`chat_messages`, владение по `sub` из JWT), а локальная база остаётся
для работы без входа.

Выбор хранилища ЧЕСТНЫЙ и детерминированный: есть токен — сервер, нет — локально. Молча
подменять одно другим нельзя: идентификаторы чатов в двух хранилищах означают разные чаты, и
«тихий фолбэк» показал бы человеку чужую (прошлую) историю и дописал бы сообщения не туда. Если
сервер недоступен, ошибка уходит в интерфейс как ошибка — там уже есть «Список чатов не
загрузился · Повторить».

Вложения и очередь догона прогонов остаются локальными (это файлы и задания этой машины), но
хранятся с пометкой области: иначе чат сервера №3 показал бы файлы локального чата №3.
"""
from __future__ import annotations

import json

from ... import abop_client as abop
from ... import auth, db

SRV, LOCAL = "srv", "local"


def mode() -> str:
    """Где сейчас хранится переписка. Вход в ABOP переключает хранилище — это ожидаемо: человек
    входит именно за тем, чтобы увидеть свою историю с любой машины."""
    return SRV if auth.token() else LOCAL


def remote() -> bool:
    return mode() == SRV


# ── чаты ──
def threads() -> list[dict]:
    if remote():
        r = abop.chat_threads()
        out = []
        for t in r:
            out.append({"id": t.get("id"), "title": t.get("title") or "Новый чат",
                        "profile": t.get("profile") or "standard",
                        "skills": list(t.get("skills") or []),
                        "favorite": 1 if t.get("favorite") else 0,
                        "updated_at": t.get("updated_at")})
        # Два устойчивых сорта вместо одного ключа: избранное сверху, внутри — свежие первыми.
        out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        out.sort(key=lambda x: -x["favorite"])
        return out
    rows = db.q("SELECT id,title,profile,skills,favorite,updated_at FROM threads "
                "ORDER BY favorite DESC, updated_at DESC")
    for r in rows:
        r["skills"] = [s for s in (r["skills"] or "").split(",") if s]
    return rows


def create(title: str, profile: str, skills: list[str]) -> dict:
    if remote():
        t = abop.chat_thread_create(title, profile, list(skills or []))
        return {"id": t.get("id"), "title": t.get("title") or title,
                "profile": t.get("profile") or profile, "skills": list(t.get("skills") or skills or [])}
    ts = db.now()
    tid = db.run("INSERT INTO threads(title,profile,skills,created_at,updated_at) VALUES(?,?,?,?,?)",
                 (title, profile, ",".join(skills or []), ts, ts))
    return {"id": tid, "title": title, "profile": profile, "skills": list(skills or [])}


def thread(tid: int) -> dict | None:
    """Шапка чата: профиль, навыки, название. Нужна почти каждой ручке, поэтому отдельно."""
    if remote():
        for t in threads():
            if int(t["id"]) == int(tid):
                return t
        return None
    rows = db.q("SELECT id,title,profile,skills,favorite FROM threads WHERE id=?", (tid,))
    if not rows:
        return None
    r = rows[0]
    r["skills"] = [s for s in (r["skills"] or "").split(",") if s]
    return r


def patch(tid: int, *, title: str, profile: str, skills: list[str], favorite: int) -> None:
    if remote():
        abop.chat_thread_patch(tid, {"title": title, "profile": profile,
                                     "skills": list(skills or []), "favorite": bool(favorite)})
        return
    db.run("UPDATE threads SET title=?,profile=?,skills=?,favorite=?,updated_at=? WHERE id=?",
           (title, profile, ",".join(skills or []), int(favorite), db.now(), tid))


def rename(tid: int, title: str) -> None:
    if remote():
        abop.chat_thread_patch(tid, {"title": title})
        return
    db.run("UPDATE threads SET title=?,updated_at=? WHERE id=?", (title, db.now(), tid))


def delete(tid: int) -> None:
    if remote():
        abop.chat_thread_delete(tid)
        return
    db.run("DELETE FROM messages WHERE thread_id=?", (tid,))
    db.run("DELETE FROM threads WHERE id=?", (tid,))


# ── сообщения ──
def messages(tid: int, limit: int = 400) -> list[dict]:
    """Переписка целиком, старые сверху. `meta` — словарь (карточки прогонов, решения, отметки)."""
    if remote():
        out = []
        for m in abop.chat_messages(tid):
            out.append({"id": m.get("id"), "role": m.get("role") or "assistant",
                        "content": m.get("content") or "", "meta": dict(m.get("meta") or {}),
                        "created_at": m.get("created_at")})
        return out[-limit:]
    rows = db.q("SELECT id,role,content,meta,created_at FROM messages WHERE thread_id=? ORDER BY id",
                (tid,))
    for r in rows:
        try:
            r["meta"] = json.loads(r["meta"] or "{}")
        except Exception:  # noqa: BLE001 — битая meta не повод терять сообщение
            r["meta"] = {}
    return rows[-limit:]


def add(tid: int, role: str, content: str, meta: dict | None = None) -> int:
    """Сообщение в переписку. Возвращает идентификатор — по нему интерфейс потом правит meta."""
    if remote():
        m = abop.chat_message_add(tid, role, content or "", dict(meta or {}))
        return int(m.get("id") or 0)
    mid = db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
                 (tid, role, content or "", json.dumps(dict(meta or {}), ensure_ascii=False), db.now()))
    db.run("UPDATE threads SET updated_at=? WHERE id=?", (db.now(), tid))
    return mid


def set_meta(tid: int, mid: int, patch_meta: dict) -> dict:
    """Дописать поля в meta сообщения (решение по заявке, номер заведённой задачи).

    Слияние одинаковое в обоих хранилищах: `run_agent` дополняется, остальные поля заменяются.
    """
    if remote():
        m = abop.chat_message_meta(tid, mid, dict(patch_meta or {}))
        return dict(m.get("meta") or {})
    rows = db.q("SELECT meta FROM messages WHERE id=? AND thread_id=?", (mid, tid))
    if not rows:
        return {}
    try:
        meta = json.loads(rows[0]["meta"] or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    p = dict(patch_meta or {})
    ra = dict(meta.get("run_agent") or {})
    ra.update(p.get("run_agent") or {})
    meta.update({k: v for k, v in p.items() if k != "run_agent"})
    if ra:
        meta["run_agent"] = ra
    db.run("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), mid))
    return meta


def last_user_text(tid: int) -> str:
    if remote():
        for m in reversed(messages(tid)):
            if m["role"] == "user":
                return m["content"]
        return ""
    rows = db.q("SELECT content FROM messages WHERE thread_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                (tid,))
    return rows[0]["content"] if rows else ""


def first_user_texts(tid: int, n: int = 2) -> list[str]:
    if remote():
        return [m["content"] for m in messages(tid) if m["role"] == "user"][:n]
    rows = db.q("SELECT content FROM messages WHERE thread_id=? AND role='user' ORDER BY id LIMIT ?",
                (tid, n))
    return [r["content"] for r in rows]


def tail(tid: int, limit: int = 10) -> list[dict]:
    """Хвост переписки для контекста модели: последние реплики вместе с meta."""
    return messages(tid)[-limit:]
