"""Модуль «Чат» — тред №1 оболочки.

Возможности MVP: треды/темы + история (локальный SQLite), выбор профиля (code/ask/standard),
скиллы из ABOP (подмешиваются в system), вложения (полный текст в контекст диалога),
память треда (последние реплики в контексте), мультиагенты (передача задачи по ролям).

LLM — через ABOP `/api/chat` (тот же self-host каскад, что и у агентов) под JWT пользователя.
ОТВЯЗАНО от курсового шлюза (MCP/portal). Добавление фич = правка ЭТОГО модуля, не ядра.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ... import auth, db
from ... import abop_client as abop

MANIFEST = {"id": "chat", "title": "Чат", "icon": "chat", "ui": "chat", "order": 10}

router = APIRouter()

# Краткие подсказки скиллов (v1). Позже — загрузка полной методики из skills/<id>/SKILL.md.
SKILLS = {
    "email-draft": "Пиши деловые письма по структуре: цель, контекст, просьба, дедлайн.",
    "icp-interviewer": "Помогай раскрыть портрет клиента: триггер, костыль, успех, готовность платить.",
    "devils-advocate": "Жёстко критикуй идею: без похвал, каждое возражение с аргументом.",
    "idea-scorer": "Оценивай идею по рубрике: боль, данные, выполнимость, экономика.",
    "finance-report": "Собирай финсводку: только числа из источника, без выдумок.",
}
BASE_SYSTEM = "Ты — рабочий ИИ-ассистент. Отвечай по делу, без воды. Если данных нет — скажи прямо, не выдумывай."
# Пресеты ролей для мультиагентов — пользователь выбирает, кого подключить под задачу.
ROLE_PRESETS = {
    "researcher": ("Ресёрчер", "Собери факты и контекст по задаче. Только проверяемое, помечай неуверенность."),
    "analyst": ("Аналитик", "На основе фактов найди закономерности и выводы. Структурируй."),
    "critic": ("Критик", "Проверь выводы на прочность: где натяжки, чего не хватает, что перепроверить."),
    "writer": ("Редактор", "Собери итог в чистый структурированный ответ для делового читателя."),
    "planner": ("Планировщик", "Разложи задачу на шаги с ответственными и сроками."),
    "finance": ("Финансист", "Посчитай экономику: затраты, эффект, риски. Числа только из данных."),
}
DEFAULT_ROLES = ["researcher", "analyst", "critic"]


class ThreadIn(BaseModel):
    title: str = "Новый чат"
    profile: str = "standard"
    skills: list[str] = []
    favorite: int = 0


class SendIn(BaseModel):
    prompt: str


class AttachIn(BaseModel):
    name: str = "документ"
    documents: list[str]


class AttachFileIn(BaseModel):
    name: str = "документ"
    data_b64: str        # содержимое файла в base64 (для бинарных: pdf/docx/xlsx)


class AgentsIn(BaseModel):
    task: str
    roles: list[str] = []       # id из ROLE_PRESETS (быстрые пресеты)
    agent_ids: list[int] = []   # id из каталога (модуль agents) — приоритетнее roles


class ExportIn(BaseModel):
    format: str = "md"      # md | docx | xlsx (pdf делает Electron через printToPDF)


def _downloads() -> Path:
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-. ]", "_", name or "").strip()[:60] or "chat"


def _sid(thread_id: int) -> str:
    return f"desktop-thread-{thread_id}"


def _system_for(skills: list[str]) -> str:
    hints = [f"[{s}] {SKILLS[s]}" for s in skills if s in SKILLS]
    return BASE_SYSTEM + ("\n\nАктивные методики:\n" + "\n".join(hints) if hints else "")


def _history(thread_id: int, limit: int = 6) -> str:
    rows = db.q("SELECT role,content FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                (thread_id, limit))
    rows = list(reversed(rows))
    return "\n".join(f"{'Ты' if r['role']=='user' else 'Ассистент'}: {r['content']}" for r in rows)


# ── скиллы для UI: тянем из ABOP (единый каталог навыков), локальные — как fallback ──
@router.get("/skills")
def skills() -> list[dict]:
    try:
        items = abop.skills()
        out = []
        for s in items:
            sid = s.get("id") or s.get("slug") or s.get("name")
            if not sid:
                continue
            hint = s.get("summary") or s.get("description") or s.get("title") or ""
            out.append({"id": sid, "hint": hint})
        if out:
            return out
    except abop.AbopError:
        pass
    return [{"id": k, "hint": v} for k, v in SKILLS.items()]


# ── треды ──
@router.get("/threads")
def list_threads() -> list[dict]:
    rows = db.q("SELECT id,title,profile,skills,favorite,updated_at FROM threads "
                "ORDER BY favorite DESC, updated_at DESC")
    for r in rows:
        r["skills"] = [s for s in (r["skills"] or "").split(",") if s]
    return rows


@router.post("/threads")
def create_thread(body: ThreadIn) -> dict:
    ts = db.now()
    tid = db.run("INSERT INTO threads(title,profile,skills,created_at,updated_at) VALUES(?,?,?,?,?)",
                 (body.title, body.profile, ",".join(body.skills), ts, ts))
    return {"id": tid, "title": body.title, "profile": body.profile, "skills": body.skills}


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: int) -> dict:
    db.run("DELETE FROM messages WHERE thread_id=?", (thread_id,))
    db.run("DELETE FROM threads WHERE id=?", (thread_id,))
    return {"ok": True}


@router.get("/threads/{thread_id}/messages")
def messages(thread_id: int) -> list[dict]:
    rows = db.q("SELECT id,role,content,meta,created_at FROM messages WHERE thread_id=? ORDER BY id",
                (thread_id,))
    for r in rows:
        r["meta"] = json.loads(r["meta"] or "{}")
    return rows


@router.patch("/threads/{thread_id}")
def update_thread(thread_id: int, body: ThreadIn) -> dict:
    db.run("UPDATE threads SET title=?,profile=?,skills=?,favorite=?,updated_at=? WHERE id=?",
           (body.title, body.profile, ",".join(body.skills), int(body.favorite), db.now(), thread_id))
    return {"ok": True}


@router.post("/threads/{thread_id}/autotitle")
def autotitle(thread_id: int) -> dict:
    """Авто-название темы по первым репликам (короткий вызов модели)."""
    msgs = db.q("SELECT content FROM messages WHERE thread_id=? AND role='user' ORDER BY id LIMIT 2",
                (thread_id,))
    if not msgs:
        return {"ok": False, "error": "empty"}
    seed = "\n".join(m["content"] for m in msgs)[:800]
    try:
        r = abop.chat(
            prompt=f"Придумай короткое название темы чата (3-5 слов, без кавычек и точки) по началу диалога:\n{seed}",
            system="Верни ТОЛЬКО название, без пояснений.", max_tokens=30)
    except abop.AbopError:
        return {"ok": False, "error": "abop"}
    title = (r.get("text", "") or "").strip().strip('"').splitlines()[0][:60] or "Новый чат"
    db.run("UPDATE threads SET title=?,updated_at=? WHERE id=?", (title, db.now(), thread_id))
    return {"ok": True, "title": title}


# ── вложения → полный текст в контекст диалога (локально, без курсового RAG-шлюза) ──
@router.post("/threads/{thread_id}/attach")
def attach(thread_id: int, body: AttachIn) -> dict:
    """Прикрепить документ к треду. Полный текст СОХРАНЯЕТСЯ и идёт в контекст диалога (как в обычных
    чатах — модель «видит» файл сразу). Хранение локальное (SQLite сайдкара)."""
    full = "\n\n".join(body.documents)
    chars = len(full)
    aid = db.run("INSERT INTO attachments(thread_id,name,chars,chunks,content,created_at) VALUES(?,?,?,?,?,?)",
                 (thread_id, body.name, chars, 0, full, db.now()))
    return {"ok": True, "id": aid, "indexed": 0, "name": body.name, "chars": chars}


def _extract_text(name: str, raw: bytes) -> str:
    """Извлечь текст из файла по расширению: pdf (pypdf), docx (python-docx), xlsx (openpyxl),
    txt/md/csv/json — как текст. Неизвестное — попытка decode. Ошибки не глушим молча."""
    import io
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if ext == "docx":
        from docx import Document
        doc = Document(io.BytesIO(raw))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for t in doc.tables:  # текст из таблиц Word
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts).strip()
    if ext == "xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"[Лист: {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out).strip()
    # текстовые форматы
    return raw.decode("utf-8", "replace").strip()


@router.post("/threads/{thread_id}/attach-file")
def attach_file(thread_id: int, body: AttachFileIn) -> dict:
    """Прикрепить БИНАРНЫЙ файл (pdf/docx/xlsx) — сайдкар извлекает текст и кладёт его в контекст
    диалога (модель видит содержимое). RAG-индексация — дополнительно, best-effort."""
    import base64
    try:
        raw = base64.b64decode(body.data_b64)
        text = _extract_text(body.name, raw)
    except Exception as e:  # noqa: BLE001 — вернуть понятную ошибку, не 500
        return {"ok": False, "error": f"не удалось извлечь текст: {type(e).__name__}: {e}"}
    if not text:
        return {"ok": False, "error": "в файле не найден текстовый слой (возможно скан — нужен OCR)"}
    aid = db.run("INSERT INTO attachments(thread_id,name,chars,chunks,content,created_at) VALUES(?,?,?,?,?,?)",
                 (thread_id, body.name, len(text), 0, text, db.now()))
    return {"ok": True, "id": aid, "indexed": 0, "name": body.name, "chars": len(text)}


@router.get("/threads/{thread_id}/files")
def files(thread_id: int) -> list[dict]:
    """Проиндексированные вложения треда — чтобы видеть, что уже в базе знаний."""
    return db.q("SELECT id,name,chars,chunks,created_at FROM attachments WHERE thread_id=? ORDER BY id DESC",
                (thread_id,))


@router.delete("/threads/{thread_id}/files/{att_id}")
def del_file(thread_id: int, att_id: int) -> dict:
    # из локального списка убираем; из Qdrant чанки живут по TTL сессии (чистится шлюзом)
    db.run("DELETE FROM attachments WHERE id=? AND thread_id=?", (att_id, thread_id))
    return {"ok": True}


@router.get("/agent-roles")
def agent_roles() -> list[dict]:
    return [{"id": k, "name": v[0], "brief": v[1], "default": k in DEFAULT_ROLES}
            for k, v in ROLE_PRESETS.items()]


# ── экспорт треда в файл в «Загрузки» (md/docx/xlsx; pdf делает Electron) ──
@router.post("/threads/{thread_id}/export")
def export_thread(thread_id: int, body: ExportIn) -> dict:
    th = db.q("SELECT title FROM threads WHERE id=?", (thread_id,))
    if not th:
        return {"ok": False, "error": "no_thread"}
    title = th[0]["title"] or "chat"
    msgs = db.q("SELECT role,content,meta FROM messages WHERE thread_id=? ORDER BY id", (thread_id,))
    base, out, fmt = _safe(title), _downloads(), body.format.lower()
    try:
        if fmt == "md":
            p = out / (base + ".md")
            lines = [f"# {title}\n"]
            for m in msgs:
                who = "🧑 Вы" if m["role"] == "user" else "🤖 Ассистент"
                lines.append(f"\n## {who}\n\n{m['content']}\n")
            p.write_text("\n".join(lines), encoding="utf-8")
        elif fmt == "docx":
            from docx import Document
            doc = Document()
            doc.add_heading(title, 0)
            for m in msgs:
                doc.add_heading("Вы" if m["role"] == "user" else "Ассистент", level=2)
                doc.add_paragraph(m["content"])
            p = out / (base + ".docx")
            doc.save(str(p))
        elif fmt == "xlsx":
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "chat"
            ws.append(["Роль", "Сообщение", "Модель", "Стоимость ₽"])
            for m in msgs:
                meta = json.loads(m["meta"] or "{}")
                ws.append([m["role"], m["content"], meta.get("model", ""), meta.get("cost_rub", "")])
            p = out / (base + ".xlsx")
            wb.save(str(p))
        else:
            return {"ok": False, "error": "bad_format"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(p)}


# ── отправка сообщения ──
@router.post("/threads/{thread_id}/send")
def send(thread_id: int, body: SendIn) -> dict:
    th = db.q("SELECT profile,skills FROM threads WHERE id=?", (thread_id,))
    if not th:
        return {"ok": False, "error": "no_thread"}
    profile = th[0]["profile"] or "standard"
    skills = [s for s in (th[0]["skills"] or "").split(",") if s]
    db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
           (thread_id, "user", body.prompt, "{}", db.now()))

    # контекст из вложений: полный текст файла (модель ВИДИТ файл) + история + вопрос
    prompt = _build_prompt(thread_id, body.prompt)
    try:
        res = abop.chat(prompt=prompt, profile=profile, system=_system_for(skills), max_tokens=1500)
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}

    text = res.get("text", "")
    meta = {"model": res.get("model"),
            "input_tokens": res.get("input_tokens"), "output_tokens": res.get("output_tokens")}
    mid = db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
                 (thread_id, "assistant", text, json.dumps(meta, ensure_ascii=False), db.now()))
    db.run("UPDATE threads SET updated_at=? WHERE id=?", (db.now(), thread_id))
    return {"ok": True, "id": mid, "content": text, "meta": meta}


def _sse(d: dict) -> str:
    return "data: " + json.dumps(d, ensure_ascii=False) + "\n\n"


_ATTACH_BUDGET = 24000   # символов вложений в контекст (полный текст небольших файлов; большие → +RAG)


def _attach_context(thread_id: int, user_prompt: str) -> str:
    """Контекст из прикреплённых файлов: полный текст (в пределах бюджета) — модель ВИДИТ файл сразу,
    как в обычных чатах. Для больших/множественных файлов добавляем релевантные чанки из RAG."""
    atts = db.q("SELECT name,content,chars FROM attachments WHERE thread_id=? ORDER BY id", (thread_id,))
    if not atts:
        return ""
    total = sum(a["chars"] for a in atts)
    parts = []
    if total <= _ATTACH_BUDGET:
        # всё влезает — кладём документы ЦЕЛИКОМ (полный контекст)
        for a in atts:
            if a["content"]:
                parts.append(f"=== ФАЙЛ: {a['name']} ===\n{a['content']}")
    else:
        # не влезает — начало каждого файла (бюджет символов). Семантический RAG вынесен в ABOP
        # Data Plane для агентных прогонов; в свободном чате кладём голову документов.
        per = max(1500, _ATTACH_BUDGET // (len(atts) + 1))
        for a in atts:
            if a["content"]:
                head = a["content"][:per]
                parts.append(f"=== ФАЙЛ: {a['name']} (фрагмент) ===\n{head}")
    return ("ПРИЛОЖЕННЫЕ ДОКУМЕНТЫ (используй их при ответе):\n" + "\n\n".join(parts) + "\n\n") if parts else ""


def _build_prompt(thread_id: int, user_prompt: str) -> str:
    """Контекст из вложений (полный текст/фрагменты+RAG) + история + текущий вопрос."""
    ctx = _attach_context(thread_id, user_prompt)
    hist = _history(thread_id)
    return ctx + (f"История:\n{hist}\n\n" if hist else "") + f"Ты: {user_prompt}"


# ── «стриминг»: ABOP /api/chat пока не стримит, поэтому берём полный ответ и режем на куски,
#    отдавая их как delta — для UI это выглядит как постепенная печать (эффект тот же). ──
@router.post("/threads/{thread_id}/send-stream")
def send_stream(thread_id: int, body: SendIn) -> StreamingResponse:
    th = db.q("SELECT profile,skills FROM threads WHERE id=?", (thread_id,))

    def gen():
        if not th:
            yield _sse({"error": "no_thread"}); return
        if not auth.token():
            yield _sse({"error": "auth_required"}); return
        profile = th[0]["profile"] or "standard"
        skills = [s for s in (th[0]["skills"] or "").split(",") if s]
        db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
               (thread_id, "user", body.prompt, "{}", db.now()))
        prompt = _build_prompt(thread_id, body.prompt)
        full, meta = "", {}
        try:
            r = abop.chat(prompt=prompt, profile=profile, system=_system_for(skills), max_tokens=1500)
            full = r.get("text", "") or ""
            meta = {"model": r.get("model"), "input_tokens": r.get("input_tokens"),
                    "output_tokens": r.get("output_tokens")}
            # нарезка на «дельты» ~48 символов — псевдо-стрим для плавной печати в UI
            for i in range(0, len(full), 48):
                yield _sse({"delta": full[i:i + 48]})
        except abop.AbopError as e:
            yield _sse({"error": str(e)})
        if full:
            db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
                   (thread_id, "assistant", full, json.dumps(meta, ensure_ascii=False), db.now()))
            db.run("UPDATE threads SET updated_at=? WHERE id=?", (db.now(), thread_id))
        yield _sse({"done": True, "meta": meta})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def _catalog_system(a: dict) -> str:
    """system-prompt агента из каталога (методика в полях)."""
    parts = [f"Ты — {a['name']}."]
    if a.get("description"):
        parts.append(a["description"])
    if a.get("steps"):
        parts.append("Методика (шаги):\n" + a["steps"])
    if a.get("dod"):
        parts.append("Definition of Done:\n" + a["dod"])
    if a.get("antipatterns"):
        parts.append("Избегай (анти-паттерны):\n" + a["antipatterns"])
    hints = [f"[{s}] {SKILLS[s]}" for s in (a.get("skills") or "").split(",") if s in SKILLS]
    if hints:
        parts.append("Методики-скиллы:\n" + "\n".join(hints))
    return "\n\n".join(parts)


# ── мультиагенты: каталог (agent_ids) ИЛИ быстрые роли (roles), цепочкой в текущий тред ──
@router.post("/threads/{thread_id}/agents")
def agents(thread_id: int, body: AgentsIn) -> dict:
    # specs: список (имя, system) — из каталога приоритетно, иначе из пресетов
    specs = []
    if body.agent_ids:
        for aid in body.agent_ids:
            rows = db.q("SELECT name,description,skills,steps,dod,antipatterns FROM agents WHERE id=?", (aid,))
            if rows:
                specs.append((rows[0]["name"], _catalog_system(rows[0])))
    if not specs:
        roles = [r for r in (body.roles or DEFAULT_ROLES) if r in ROLE_PRESETS] or DEFAULT_ROLES
        specs = [(ROLE_PRESETS[r][0], f"Ты — {ROLE_PRESETS[r][0]}. {ROLE_PRESETS[r][1]}") for r in roles]
    names = ", ".join(n for n, _ in specs)
    db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
           (thread_id, "user", f"[агенты: {names}] {body.task}", "{}", db.now()))
    outputs, prior = [], ""
    try:
        for name, system in specs:
            prefix = ("Наработки предыдущих ролей:\n" + prior) if prior else ""
            r = abop.chat(prompt=f"Задача: {body.task}\n\n{prefix}", system=system, max_tokens=1200)
            t = r.get("text", "")
            outputs.append(f"### {name}\n{t}")
            prior += f"\n[{name}]: {t}\n"
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}
    combined = "\n\n".join(outputs)
    mid = db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
                 (thread_id, "assistant", combined, json.dumps({"agents": names}), db.now()))
    db.run("UPDATE threads SET updated_at=? WHERE id=?", (db.now(), thread_id))
    return {"ok": True, "id": mid, "content": combined}
