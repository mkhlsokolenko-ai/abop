# -*- coding: utf-8 -*-
"""История чата у вошедшего пользователя живёт на сервере, у гостя — локально.

Требование владельца: «нужно, чтобы при входе с любой машины пользователь видел свою историю,
хранение в стейт-машине — просто, но вообще не переживёт отчистку кеша». Локальный SQLite сайдкара
и есть такая стейт-машина. Поэтому вошедший работает с `chat_threads`/`chat_messages` в Postgres
ABOP, а локальная база остаётся для работы без входа.

Проверяем ровно то, что может сломаться незаметно:
1. вошёл → чтение и запись уходят на сервер (а не в локальную базу);
2. не вошёл → всё работает локально (гостевой режим не должен падать);
3. meta карточек прогонов переживает хранение в обоих режимах — на ней держатся решения по HITL;
4. ошибка сервера не превращается в «истории нет»: она доходит до интерфейса как ошибка.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

DESKTOP = pathlib.Path(__file__).resolve().parents[1] / "desktop"
sys.path.insert(0, str(DESKTOP))


@pytest.fixture()
def srv():
    """Сервер «на месте»: подменяем клиента ABOP на память процесса."""
    pytest.importorskip("sidecar.app", reason="сайдкар десктопа недоступен")
    from sidecar import abop_client, auth
    from sidecar.modules.chat import store

    state = {"threads": {}, "msgs": {}, "seq": 0}

    def _create(title, profile, skills):
        state["seq"] += 1
        t = {"id": state["seq"], "title": title, "profile": profile, "skills": list(skills),
             "favorite": False, "updated_at": f"2026-10-04T10:0{state['seq']}:00"}
        state["threads"][t["id"]] = t
        state["msgs"][t["id"]] = []
        return t

    def _add(tid, role, content, meta=None):
        state["seq"] += 1
        m = {"id": state["seq"], "role": role, "content": content, "meta": dict(meta or {})}
        state["msgs"].setdefault(int(tid), []).append(m)
        return m

    def _meta(tid, mid, meta):
        for m in state["msgs"].get(int(tid), []):
            if m["id"] == int(mid):
                ra = dict((m["meta"] or {}).get("run_agent") or {})
                ra.update((meta or {}).get("run_agent") or {})
                m["meta"].update({k: v for k, v in (meta or {}).items() if k != "run_agent"})
                if ra:
                    m["meta"]["run_agent"] = ra
                return {"id": mid, "meta": m["meta"]}
        return {}

    def _patch(tid, fields):
        t = state["threads"][int(tid)]
        for k, v in (fields or {}).items():
            if v is not None:
                t[k] = v
        return t

    def _delete(tid):
        state["threads"].pop(int(tid), None)
        state["msgs"].pop(int(tid), None)
        return {"ok": True}

    saved = {k: getattr(abop_client, k) for k in
             ("chat_threads", "chat_thread_create", "chat_thread_patch", "chat_thread_delete",
              "chat_messages", "chat_message_add", "chat_message_meta")}
    saved_token = auth.token
    abop_client.chat_threads = lambda: list(state["threads"].values())
    abop_client.chat_thread_create = _create
    abop_client.chat_thread_patch = _patch
    abop_client.chat_thread_delete = _delete
    abop_client.chat_messages = lambda tid: list(state["msgs"].get(int(tid), []))
    abop_client.chat_message_add = _add
    abop_client.chat_message_meta = _meta
    auth.token = lambda: "jwt-пользователя"
    yield store, state
    for k, v in saved.items():
        setattr(abop_client, k, v)
    auth.token = saved_token


@pytest.fixture()
def local(tmp_path):
    """Гостевой режим: токена нет, база — временная (личную базу разработчика не трогаем)."""
    pytest.importorskip("sidecar.app", reason="сайдкар десктопа недоступен")
    from sidecar import auth, config, db
    from sidecar.modules.chat import store
    saved_db, saved_token = config.DB_FILE, auth.token
    config.DB_FILE = tmp_path / "ape.db"
    auth.token = lambda: None
    db.init()
    yield store
    config.DB_FILE, auth.token = saved_db, saved_token


def test_вошёл_история_уходит_на_сервер(srv):
    store, state = srv
    assert store.remote() is True
    t = store.create("Разбор почты", "standard", ["mail-triage"])
    store.add(t["id"], "user", "посмотри входящие")
    store.add(t["id"], "assistant", "[агент mailtasks] находок: 3", {"run_agent": {"run_id": "r-1"}})
    assert len(state["msgs"][t["id"]]) == 2, "сообщения не доехали на сервер"
    msgs = store.messages(t["id"])
    assert msgs[1]["meta"]["run_agent"]["run_id"] == "r-1", "карточка прогона потеряла meta"
    assert [x["title"] for x in store.threads()] == ["Разбор почты"]


def test_решение_по_заявке_переживает_хранение(srv):
    """На meta держится HITL: без неё после переоткрытия чата кнопка «Подтвердить» снова активна."""
    store, _ = srv
    t = store.create("Письмо подрядчику", "standard", [])
    mid = store.add(t["id"], "assistant", "карточка", {"run_agent": {"run_id": "r-2"}})
    store.set_meta(t["id"], mid, {"run_agent": {"hitl_done": "approve"}})
    ra = store.messages(t["id"])[0]["meta"]["run_agent"]
    assert ra["hitl_done"] == "approve" and ra["run_id"] == "r-2", "слияние meta потеряло поля"


def test_гость_работает_локально(local):
    store = local
    assert store.remote() is False
    t = store.create("Без входа", "standard", ["idea-scorer"])
    store.add(t["id"], "user", "оцени идею")
    mid = store.add(t["id"], "assistant", "оценка", {"run_agent": {"run_id": "r-3"}})
    store.set_meta(t["id"], mid, {"run_agent": {"hitl_done": "reject"}})
    msgs = store.messages(t["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert [m["content"] for m in msgs] == ["оцени идею", "оценка"]
    assert msgs[-1]["meta"]["run_agent"] == {"run_id": "r-3", "hitl_done": "reject"}
    assert store.thread(t["id"])["skills"] == ["idea-scorer"]
    store.rename(t["id"], "Оценка идеи")
    assert store.thread(t["id"])["title"] == "Оценка идеи"
    store.delete(t["id"])
    assert store.threads() == []


def test_сбой_сервера_не_выглядит_как_пустая_история(srv):
    """Молчаливый фолбэк в локальную базу показал бы ЧУЖУЮ (прошлую) историю и дописал бы сообщения
    не туда. Поэтому ошибка поднимается наверх — интерфейс покажет «не загрузилось · повторить»."""
    store, _ = srv
    from sidecar import abop_client

    def boom():
        raise abop_client.AbopError(0, "ConnectionRefused")

    abop_client.chat_threads = boom
    with pytest.raises(abop_client.AbopError):
        store.threads()


def test_область_вложений_разделена():
    """Идентификаторы чатов на сервере и локально означают разные чаты: локальные вложения и задания
    догона помечены областью, иначе чат сервера №3 показал бы файлы локального чата №3."""
    root = pathlib.Path(__file__).resolve().parents[1]
    mod = (root / "desktop" / "sidecar" / "modules" / "chat" / "module.py").read_text(encoding="utf-8")
    dbp = (root / "desktop" / "sidecar" / "db.py").read_text(encoding="utf-8")
    assert "scope TEXT NOT NULL DEFAULT 'local'" in dbp, "нет миграции области хранения"
    assert "WHERE thread_id=? AND scope=?" in mod, "вложения читаются без области"
    assert "FROM pending_runs WHERE thread_id=? AND scope=?" in mod, "догон прогонов без области"
