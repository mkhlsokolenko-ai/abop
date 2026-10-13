"""Локальное хранилище приложения (SQLite): треды и сообщения чата.

Держим на устройстве — приватно и работает офлайн для истории. Вложения/RAG уходят в
курсовые S3+Qdrant через шлюз; здесь только метаданные и текст сообщений.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'standard',
    skills TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    role TEXT NOT NULL,          -- user | assistant | system
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id);
CREATE TABLE IF NOT EXISTS pending_runs (
    job_id TEXT PRIMARY KEY,     -- задание в очереди ABOP
    thread_id INTEGER NOT NULL,  -- чат, куда вернуть карточку
    agent_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_thread ON pending_runs(thread_id);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    chars INTEGER NOT NULL DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',   -- полный текст вложения → идёт в контекст диалога (не только RAG)
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_thread ON attachments(thread_id);
-- Графы агентов (GraphLens): узлы/связи в JSON. Один граф = один сценарий.
CREATE TABLE IF NOT EXISTS graphs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    nodes TEXT NOT NULL DEFAULT '[]',   -- [{id,kind,label,x,y,agent_id?}]
    edges TEXT NOT NULL DEFAULT '[]',   -- [[fromId,toId], ...]
    updated_at REAL NOT NULL
);
-- Пользовательский каталог агентов (конструктор): методика в человекочитаемых полях.
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '',        -- csv id скиллов
    steps TEXT NOT NULL DEFAULT '',         -- шаги, по строке
    dod TEXT NOT NULL DEFAULT '',           -- definition of done, по строке
    antipatterns TEXT NOT NULL DEFAULT '',  -- анти-паттерны, по строке
    profile TEXT NOT NULL DEFAULT 'standard',
    outward INTEGER NOT NULL DEFAULT 0,      -- действует наружу → требует approve/deny
    created_at REAL NOT NULL
);
-- Папки под присмотром: новый файл в папке сам становится задачей агента.
CREATE TABLE IF NOT EXISTS watched (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT '',
    mask TEXT NOT NULL DEFAULT '',          -- расширения через запятую; пусто = всё, что умеем читать
    thread_id INTEGER NOT NULL DEFAULT 0,   -- чат, куда приходит результат
    scope TEXT NOT NULL DEFAULT 'local',    -- та же область, что у вложений: установка + человек
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
-- Что присмотр уже видел. Отпечаток = путь + размер + время правки: один и тот же файл не
-- запускает агента дважды, а ИЗМЕНЁННЫЙ файл — это новая работа, и его возьмут снова.
CREATE TABLE IF NOT EXISTS watched_seen (
    folder_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,                    -- взят | не вышло | был до присмотра
    note TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL,
    PRIMARY KEY (folder_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_seen_folder ON watched_seen(folder_id, at DESC);
-- Время человека у экрана по прогону (ADR-064). Живёт ЗДЕСЬ, а не на сервере, потому что сервер
-- этого не знает и знать не может: он не видит, сколько карточка была открыта. Число личное —
-- «сколько времени на это потратили ВЫ», — и общего смысла у него нет.
CREATE TABLE IF NOT EXISTS attention (
    run_id TEXT PRIMARY KEY,
    sec    REAL NOT NULL DEFAULT 0,
    at     REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    c = sqlite3.connect(config.DB_FILE)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        # мягкие миграции для уже установленных копий (CREATE IF NOT EXISTS не добавляет колонки)
        try:
            c.execute("ALTER TABLE threads ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        try:
            c.execute("ALTER TABLE agents ADD COLUMN outward INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE attachments ADD COLUMN content TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # Область чата: переписка вошедшего живёт на сервере ABOP, у гостя — здесь. Идентификаторы
        # чатов в двух хранилищах означают РАЗНЫЕ чаты, поэтому локальные вложения и задания догона
        # помечаем областью: иначе чат сервера №3 показал бы файлы прежнего локального чата №3.
        for tbl in ("attachments", "pending_runs"):
            try:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN scope TEXT NOT NULL DEFAULT 'local'")
            except sqlite3.OperationalError:
                pass
        c.commit()


def q(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with _conn() as c:
        cur = c.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def run(sql: str, args: tuple = ()) -> int:
    """Выполнить и вернуть rowid ВСТАВЛЕННОЙ строки. Для UPDATE/DELETE смысла не имеет — там
    `change()`: `lastrowid` после них отдаёт прошлую вставку этого соединения, и «перенесено N»
    было бы просто неправдой."""
    with _conn() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def change(sql: str, args: tuple = ()) -> int:
    """Выполнить UPDATE/DELETE и вернуть ЧИСЛО затронутых строк."""
    with _conn() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0


def now() -> float:
    return time.time()
