"""Модуль «Папка под присмотром» — новый файл в папке сам становится задачей агента.

Зачем это в десктопе, а не в вебе: папка — местная. Акты приходят в «Входящие» на диск, выписки
выгружаются в папку банка, сканы падают из МФУ. Веб-клиент физически не может это заметить;
десктоп может, и это одно из двух, чего веб не умеет по определению (второе — уведомления ОС).

Четыре решения, за которые пришлось выбирать между удобством и честностью, и каждое выбрано в
пользу честности:

1. **Файлы, которые лежали до присмотра, не берутся.** Указать на «Загрузки» — обычное дело, а там
   сотня файлов; взять их все значит потратить деньги заказчика на сто прогонов, о которых он не
   просил. Они помечаются «был до присмотра» и видны в журнале, а взять их можно кнопкой — явно.
2. **За один заход берётся не больше `ПРЕДЕЛ_ЗА_ЗАХОД` файлов**, и остаток назван числом. Выгрузка
   в пятьсот файлов не должна превращаться в пятьсот прогонов в одну минуту.
3. **Файл, который ещё копируется, не берётся.** Размер должен совпасть в двух заходах подряд:
   иначе агент получит половину документа и честно разберёт половину.
4. **Отказ виден.** Скан без текстового слоя, запертый файл, упавший запуск — всё пишется в журнал
   строкой с причиной. Присмотр, молча не берущий файлы, хуже отсутствия присмотра: человек
   считает, что работа идёт.

Область (установка + человек + вендорский переход) — та же, что у вложений чата: папка заказчика
не должна присматриваться, когда вошли в другую установку.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from ... import abop_client as abop
from ... import auth, db
from ..chat import module as chat
from ..chat import store

MANIFEST = {"id": "watch", "title": "Присмотр", "icon": "watch", "ui": "watch", "order": 55}
router = APIRouter()

# Читаем то, из чего умеем достать текст (`chat._extract_text`). Остальное — не «ошибка», а
# «не наш файл»: картинки идут в «Распознавание», и присмотр о них молчит намеренно.
ЧИТАЕМ = ("pdf", "docx", "xlsx", "txt", "md", "csv", "json")
ПРЕДЕЛ_ЗА_ЗАХОД = 5
ИНТЕРВАЛ = 20.0            # с; папка — не очередь, чаще смысла нет
ПРЕДЕЛ_РАЗМЕРА = 20 * 1024 * 1024

_размеры: dict[str, int] = {}      # отпечаток пути → размер в прошлом заходе (докопировка)
_поток: threading.Thread | None = None
_последний: dict = {"at": 0.0, "взято": 0, "папок": 0, "почему": ""}


def _отпечаток(p: Path, st: os.stat_result) -> str:
    """Путь + размер + время правки. Изменённый файл — новая работа, и его возьмут снова."""
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


def _подходит(имя: str, маска: str) -> bool:
    ext = (имя.rsplit(".", 1)[-1] if "." in имя else "").lower()
    если = [m.strip().lstrip(".").lower() for m in (маска or "").split(",") if m.strip()]
    return ext in (если or list(ЧИТАЕМ))


def _папки() -> list[dict]:
    return db.q("SELECT * FROM watched WHERE scope=? ORDER BY id DESC", (store.область(),))


def _отметить(folder_id: int, имя: str, отпечаток: str, состояние: str,
              примечание: str = "", job_id: str = "") -> None:
    db.run("INSERT OR REPLACE INTO watched_seen(folder_id,fingerprint,name,state,note,job_id,at) "
           "VALUES(?,?,?,?,?,?,?)",
           (folder_id, отпечаток, имя, состояние, примечание[:400], job_id, db.now()))


def _видели(folder_id: int) -> set[str]:
    return {r["fingerprint"] for r in
            db.q("SELECT fingerprint FROM watched_seen WHERE folder_id=?", (folder_id,))}


def _чат(папка: dict) -> int:
    """Чат, куда приходят результаты папки. Один на папку: иначе ответы теряются среди переписки."""
    tid = int(папка.get("thread_id") or 0)
    if tid and db.q("SELECT id FROM threads WHERE id=?", (tid,)):
        return tid
    имя = "Присмотр: " + Path(папка["path"]).name
    tid = db.run("INSERT INTO threads(title,profile,skills,created_at,updated_at) VALUES(?,?,?,?,?)",
                 (имя, "standard", "", db.now(), db.now()))
    db.change("UPDATE watched SET thread_id=? WHERE id=?", (tid, папка["id"]))
    return tid


def _взять(папка: dict, файл: Path, отпечаток: str) -> tuple[str, str]:
    """Один файл → вложение в чат папки → прогон агента через очередь.

    Возвращает «состояние, примечание». Ошибка здесь — это строка в журнале, а не исключение:
    один нечитаемый файл не должен останавливать присмотр за остальными.
    """
    try:
        raw = файл.read_bytes()
    except OSError as e:
        return "не вышло", f"файл не открылся: {type(e).__name__}"
    try:
        текст = chat._extract_text(файл.name, raw)
    except Exception as e:                       # noqa: BLE001 — битый pdf не повод падать
        return "не вышло", f"текст не извлёкся: {type(e).__name__}: {e}"
    if not текст.strip():
        return "не вышло", "нет текстового слоя — похоже на скан, нужно «Распознавание»"

    tid = _чат(папка)
    db.run("INSERT INTO attachments(thread_id,name,chars,chunks,content,scope,created_at) "
           "VALUES(?,?,?,?,?,?,?)",
           (tid, файл.name, len(текст), 0, текст, store.область(), db.now()))
    db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
           (tid, "user", f"Файл из папки под присмотром: {файл.name}", "{}", db.now()))
    try:
        r = abop.run_async(agent_id=папка["agent_id"],
                           context=f"Документ «{файл.name}» из папки {папка['path']}:\n\n{текст}",
                           no_cache=False, deliver=False)
    except abop.AbopError as e:
        return "не вышло", f"прогон не запустился: {e.detail or e}"
    job = str((r or {}).get("job_id") or "")
    if job:
        # То же задание догона, что у чата: результат доедет, даже если приложение закрыли.
        db.run("INSERT OR REPLACE INTO pending_runs(job_id,thread_id,agent_id,scope,created_at) "
               "VALUES(?,?,?,?,?)", (job, tid, папка["agent_id"], store.область(), db.now()))
    return "взят", ""


def _заход() -> dict:
    """Один обход всех папок текущей области. Возвращает сводку — её показывает интерфейс."""
    if not auth.token():
        # Присмотр привязан к области (установка + человек). Пока не вошли, области нет, и
        # присматривать «за кого-то» нельзя. Это состояние, а не ошибка, — так и сообщаем.
        return {"at": db.now(), "взято": 0, "папок": 0, "почему": "присмотр ждёт входа в ABOP"}
    взято, папок, почему = 0, 0, ""
    for папка in _папки():
        if not int(папка.get("active") or 0):
            continue
        папок += 1
        корень = Path(папка["path"])
        if not корень.is_dir():
            почему = почему or f"папка не найдена: {папка['path']}"
            continue
        видели = _видели(папка["id"])
        новые = []
        try:
            записи = sorted(корень.iterdir(), key=lambda p: p.name)
        except OSError as e:
            почему = почему or f"папку не прочитать: {type(e).__name__}"
            continue
        for p in записи:
            if not p.is_file() or not _подходит(p.name, папка.get("mask") or ""):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            отп = _отпечаток(p, st)
            if отп in видели:
                continue
            if st.st_size > ПРЕДЕЛ_РАЗМЕРА:
                _отметить(папка["id"], p.name, отп, "не вышло",
                          f"файл больше {ПРЕДЕЛ_РАЗМЕРА // (1024 * 1024)} МБ")
                continue
            # Докопировка: размер обязан совпасть в двух заходах подряд. Иначе агент получит
            # половину документа и честно разберёт половину.
            ключ = str(p.resolve())
            if _размеры.get(ключ) != st.st_size:
                _размеры[ключ] = st.st_size
                continue
            новые.append((p, отп))
        остаток = max(0, len(новые) - ПРЕДЕЛ_ЗА_ЗАХОД)
        for p, отп in новые[:ПРЕДЕЛ_ЗА_ЗАХОД]:
            состояние, примечание = _взять(папка, p, отп)
            _отметить(папка["id"], p.name, отп, состояние, примечание)
            if состояние == "взят":
                взято += 1
        if остаток:
            почему = почему or f"взято {ПРЕДЕЛ_ЗА_ЗАХОД}, ещё {остаток} возьмём следующим заходом"
    return {"at": db.now(), "взято": взято, "папок": папок, "почему": почему}


def _цикл() -> None:
    while True:
        try:
            _последний.update(_заход())
        except Exception as e:                   # noqa: BLE001 — присмотр не роняет сайдкар
            _последний.update({"at": db.now(), "почему": f"{type(e).__name__}: {e}"})
        time.sleep(ИНТЕРВАЛ)


def запустить() -> None:
    """Поднять присмотр. Вызывается один раз при старте сайдкара."""
    global _поток
    if _поток and _поток.is_alive():
        return
    _поток = threading.Thread(target=_цикл, daemon=True, name="watch")
    _поток.start()


# ── ручки ──────────────────────────────────────────────────────────────────────────────────────

class ПапкаIn(BaseModel):
    path: str
    agent_id: str
    agent_name: str = ""
    mask: str = ""
    # Брать ли то, что уже лежит в папке. По умолчанию НЕТ: указать на «Загрузки» — обычное дело, а
    # сто прогонов «на всякий случай» человек не заказывал.
    take_existing: bool = False


@router.get("/list")
def list_folders() -> dict:
    папки = []
    for f in _папки():
        журнал = db.q("SELECT name,state,note,job_id,at FROM watched_seen WHERE folder_id=? "
                      "ORDER BY at DESC LIMIT 12", (f["id"],))
        счёт = {r["state"]: r["n"] for r in db.q(
            "SELECT state, COUNT(*) AS n FROM watched_seen WHERE folder_id=? GROUP BY state",
            (f["id"],))}
        папки.append({**f, "exists": Path(f["path"]).is_dir(), "журнал": журнал, "счёт": счёт})
    return {"folders": папки, "last": dict(_последний), "interval": ИНТЕРВАЛ,
            "limit": ПРЕДЕЛ_ЗА_ЗАХОД, "reads": list(ЧИТАЕМ),
            "authed": bool(auth.token())}


@router.post("/add")
def add_folder(body: ПапкаIn) -> dict:
    путь = Path(str(body.path or "").strip())
    if not путь.is_dir():
        return {"ok": False, "error": f"папки нет: {путь}"}
    if not str(body.agent_id or "").strip():
        return {"ok": False, "error": "не выбран агент — присмотр без агента ничего не делает"}
    fid = db.run("INSERT INTO watched(path,agent_id,agent_name,mask,thread_id,scope,active,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)",
                 (str(путь), body.agent_id, body.agent_name or body.agent_id, body.mask or "",
                  0, store.область(), 1, db.now()))
    пропущено = 0
    if not body.take_existing:
        # Помечаем «был до присмотра»: файл не взят и не потерян — он виден в журнале, и его
        # можно взять кнопкой. Промолчать значило бы оставить человека в догадках.
        for p in sorted(путь.iterdir(), key=lambda x: x.name):
            if p.is_file() and _подходит(p.name, body.mask or ""):
                try:
                    _отметить(fid, p.name, _отпечаток(p, p.stat()), "был до присмотра",
                              "лежал в папке, когда присмотр включили")
                    пропущено += 1
                except OSError:
                    continue
    return {"ok": True, "id": fid, "skipped": пропущено}


@router.post("/{folder_id}/pause")
def pause(folder_id: int, on: int = 0) -> dict:
    db.change("UPDATE watched SET active=? WHERE id=? AND scope=?",
              (1 if on else 0, folder_id, store.область()))
    return {"ok": True, "active": bool(on)}


@router.post("/{folder_id}/take-existing")
def take_existing(folder_id: int) -> dict:
    """Взять то, что лежало до присмотра: снимаем пометку, и следующий заход их подхватит."""
    n = db.change("DELETE FROM watched_seen WHERE folder_id=? AND state=?",
                  (folder_id, "был до присмотра"))
    return {"ok": True, "released": n}


@router.delete("/{folder_id}")
def remove(folder_id: int) -> dict:
    db.change("DELETE FROM watched_seen WHERE folder_id=?", (folder_id,))
    db.change("DELETE FROM watched WHERE id=? AND scope=?", (folder_id, store.область()))
    return {"ok": True}


@router.post("/scan")
def scan_now() -> dict:
    """Обойти папки сейчас, не дожидаясь очередного захода."""
    _последний.update(_заход())
    return dict(_последний)
