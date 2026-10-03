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

from fastapi import APIRouter, HTTPException
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
    persona: str = ""       # «Персона и постоянные инструкции» из Кабинета — попадает в system (D-H7)


class NoteIn(BaseModel):
    """Служебное сообщение ассистента с meta (результат цепочки/решение/уведомление) — чтобы карточки
    переживали смену раздела и перезапуск (UX-аудит D-H3: раньше жили только в памяти панели)."""
    content: str = ""
    meta: dict = {}


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


class RunAgentIn(BaseModel):
    agent_id: str               # реальный ABOP-агент (mailtasks/invest1c/bft/authored…)
    context: str = ""           # подсказка из треда (задача / выделенный текст / ссылка)
    no_cache: bool = False
    deliver: str = ""           # дерево решений: '' все каналы | 'chat' только в чат | канал (redmine/email)


class ExportIn(BaseModel):
    format: str = "md"      # md | docx | xlsx (pdf делает Electron через printToPDF)


def _downloads() -> Path:
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-. ]", "_", name or "").strip()[:60] or "chat"


def _sid(thread_id: int) -> str:
    return f"desktop-thread-{thread_id}"


def _system_for(skills: list[str], persona: str = "") -> str:
    hints = [f"[{s}] {SKILLS[s]}" for s in skills if s in SKILLS]
    out = BASE_SYSTEM + ("\n\nАктивные методики:\n" + "\n".join(hints) if hints else "")
    persona = (persona or "").strip()[:1500]
    if persona:
        out += "\n\nО пользователе и его постоянные инструкции (учитывай всегда):\n" + persona
    return out


def _history(thread_id: int, limit: int = 6) -> str:
    rows = db.q("SELECT role,content FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                (thread_id, limit))
    rows = list(reversed(rows))
    return "\n".join(f"{'Ты' if r['role']=='user' else 'Ассистент'}: {r['content']}" for r in rows)


class MatchIn(BaseModel):
    q: str


@router.post("/match")
def match(body: MatchIn) -> dict:
    """Подбор агента под задачу (дерево решений чата): лексика+семантика на стороне ABOP.

    Если описания не хватает, ABOP возвращает не пустоту, а вопросы — их и показываем: по двум словам
    выбирать исполнителя нельзя, под такое описание подходит десяток навыков.
    """
    try:
        return abop.match_full(body.q)
    except abop.AbopError:
        return {"matches": []}


class PlanIn(BaseModel):
    q: str
    slots: dict | None = None


class PlanBuildIn(BaseModel):
    steps: list
    name: str = ""


@router.post("/plan")
def plan(body: PlanIn) -> dict:
    """План из НАВЫКОВ под задачу — когда готового агента нет.

    Отдаём как есть, вместе с «чего не хватает»: пустой план это ответ, а не ошибка. Он говорит, что
    в каталоге нет исполнителя или в среде нет данных, — и это честнее, чем собрать красивую цепочку
    и упасть на середине.
    """
    try:
        return abop.plan_auto(body.q, body.slots or {})
    except abop.AbopError as e:
        return {"ok": False, "steps": [], "missing": [], "error": str(e)}


@router.post("/plan/build")
def plan_build(body: PlanBuildIn) -> dict:
    """Собрать агентов и цепочку по плану. Права проверяет ABOP: сборка — уровень manager."""
    try:
        return abop.plan_build(body.steps, body.name)
    except abop.AbopError as e:
        raise HTTPException(502, str(e)) from e


_CH_RU = {"redmine": "Redmine", "email": "почта", "yandex": "почта", "bookstack": "BookStack (вики)",
          "yougile": "YouGile", "pdf": "PDF", "file": "файл"}


# ── каталог реальных ABOP-агентов для запуска из чата (чат = среда управления пользователя) ──
_AG_CACHE: dict = {"at": 0.0, "tok": None, "data": []}
_AG_TTL = 60.0   # с; шторка/дерево решений дёргают каталог часто — N+1 к ABOP не должен повторяться (D-H11)


def _agent_card(a: dict) -> dict:
    desc, systems = "", []
    try:
        full = abop.agent(a.get("id") or "")
        nodes = (full.get("graph") or {}).get("nodes") or []
        sk = [n.get("skill") for n in nodes if n.get("kind") == "skill" and n.get("skill")]
        desc = "Навыки: " + ", ".join(sk[:4]) if sk else ""
        ents = sorted({str(n.get("entity")) for n in nodes if n.get("entity")})
        chans = [_CH_RU.get((n.get("out") or {}).get("channel"), (n.get("out") or {}).get("channel"))
                 for n in nodes if n.get("kind") in ("output", "out")]
        systems = [c for c in chans if c] + [e for e in ents if e]
    except abop.AbopError:
        pass
    return {"id": a.get("id"), "name": a.get("name"), "family": a.get("family"), "role": a.get("role"),
            "autonomy_max": a.get("autonomy_max"), "outward": bool(a.get("outward")),
            "description": desc, "owner": bool(a.get("owner")), "systems": sorted(set(systems))}


@router.get("/abop-agents")
def abop_agents() -> list[dict]:
    """Агенты ABOP, доступные пользователю (ABAC), с кратким описанием (что делает) и системами
    (входные данные + каналы доставки) — для карточки агента в сайдбаре чата.
    Детали агентов тянем параллельно и кэшируем на минуту (было: N+1 последовательно при каждом открытии)."""
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    tok = auth.token()
    if _AG_CACHE["data"] and _AG_CACHE["tok"] == tok and _t.time() - _AG_CACHE["at"] < _AG_TTL:
        return _AG_CACHE["data"]
    try:
        base = abop.agents()
        with ThreadPoolExecutor(max_workers=8) as ex:
            out = list(ex.map(_agent_card, base))
        _AG_CACHE.update({"at": _t.time(), "tok": tok, "data": out})
        return out
    except abop.AbopError:
        # Раньше ошибка (в том числе «нужен вход») отдавалась пустым списком с кодом 200, и
        # интерфейс писал «У вас пока нет своих агентов» вместо предложения войти.
        if _AG_CACHE["data"]:
            return _AG_CACHE["data"]
        raise


def _run_summary(agent_id: str, run: dict) -> dict:
    """Компактная сводка прогона для карточки в чате (общая для sync и async путей)."""
    run = run.get("run", run) if isinstance(run, dict) else run
    findings = [ (b.get("text") or "") for b in (run.get("board") or []) if b.get("kind") == "finding" ]
    # hitl_id обязателен: подтверждение в чате идёт строго по заявке (D-C3), а не «всё pending агента»
    delivery = [ {"channel": d.get("channel"), "to": d.get("to"), "mode": d.get("mode"),
                  "hitl_id": d.get("hitl_id"), "title": d.get("title"), "subject": d.get("subject"),
                  "result": (d.get("result") or "")[:200]}
                 for d in (run.get("delivery") or []) ]
    return {
        "agent_id": agent_id,
        "agent_name": (run.get("agent") or {}).get("name") if isinstance(run.get("agent"), dict) else run.get("agent_name"),
        "run_id": run.get("run_id") or run.get("id") or (run.get("saved") or {}).get("id"),
        "trace_id": run.get("trace_id"),
        "cached": run.get("cached"),
        "findings": findings[:8],
        "findings_total": (run.get("findings_summary") or {}).get("total") or len(findings),
        "investigations_total": (run.get("investigations_summary") or {}).get("total"),
        "delivery": delivery,
        "verdict": run.get("verdict") if isinstance(run.get("verdict"), dict) else None,
        "tokens": int(((run.get("run_metrics") or {}).get("cost") or {}).get("input_tokens") or 0)
                  + int(((run.get("run_metrics") or {}).get("cost") or {}).get("output_tokens") or 0),
        # Прогон, обрезанный лимитом, выглядел как обычный: находок меньше — и непонятно почему.
        "budget_stopped": [str(x) for x in (((run.get("run_metrics") or {}).get("budget") or {}).get("stopped_skills") or [])],
        # Расхождения между ветвями: сколько закрыто правилом и что ждёт решения человека. Молчать об
        # этом нельзя — иначе человек видит одну цифру и не знает, что о ней спорили.
        "arbitration": (lambda a: {"total": a.get("total"), "by_rule": a.get("by_rule"),
                                   "needs_human": a.get("needs_human"), "open": a.get("open") or [],
                                   "note": a.get("note")} if a and a.get("total") else None)(
            (run.get("run_metrics") or {}).get("arbitration") or {}),
    }


def _persist_run(thread_id: int, agent_id: str, summary: dict) -> None:
    line = f"[агент {agent_id}] находок: {summary['findings_total']}"
    db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
           (thread_id, "assistant", line, json.dumps({"run_agent": summary}, ensure_ascii=False), db.now()))
    db.run("UPDATE threads SET updated_at=? WHERE id=?", (db.now(), thread_id))


def _run_context(thread_id: int, body: "RunAgentIn") -> str:
    ctx = (body.context or "").strip()
    if not ctx:
        rows = db.q("SELECT content FROM messages WHERE thread_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                    (thread_id,))
        ctx = rows[0]["content"] if rows else ""
    att = _attach_context(thread_id, ctx)
    return (att + ctx).strip()


@router.post("/threads/{thread_id}/run-agent")
def run_agent(thread_id: int, body: RunAgentIn) -> dict:
    """Запуск реального ABOP-агента из чата — ЧЕРЕЗ ОЧЕРЕДЬ (гейт масштабирования): ABOP отвечает 202
    с job_id, UI поллит /run-job/{job_id}; долгого HTTP-запроса больше нет, второй запуск того же
    пользователя ждёт в очереди, а не конкурирует. Старый ABOP (201 сразу) обрабатывается как раньше."""
    full_ctx = _run_context(thread_id, body)
    try:
        r = abop.run_async(agent_id=body.agent_id, context=full_ctx, no_cache=body.no_cache, deliver=body.deliver)
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}
    if isinstance(r, dict) and r.get("done"):
        summary = _run_summary(body.agent_id, r["run"])
        _persist_run(thread_id, body.agent_id, summary)
        return {"ok": True, "done": True, "run": summary}
    if r.get("job_id"):
        # запоминаем задание: даже если опрос оборвётся, результат доедет в чат при следующем открытии
        db.run("INSERT OR REPLACE INTO pending_runs (job_id, thread_id, agent_id, created_at) VALUES (?,?,?,?)",
               (str(r["job_id"]), thread_id, body.agent_id, db.now()))
    return {"ok": True, "done": False, "job_id": r.get("job_id"), "status": r.get("status"),
            "position": r.get("position"), "deduped": bool(r.get("deduped"))}


@router.get("/threads/{thread_id}/run-job/{job_id}")
def run_job_status(thread_id: int, job_id: str, agent_id: str = "") -> dict:
    """Один шаг поллинга. done → карточка сохраняется в тред и возвращается run; failed/cancelled → ошибка."""
    try:
        j = abop.run_job(job_id)
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}
    st = j.get("status")
    if st == "done" and j.get("run"):
        aid = agent_id or j.get("agent_id") or ""
        summary = _run_summary(aid, j["run"])
        _persist_run(thread_id, aid, summary)
        db.run("DELETE FROM pending_runs WHERE job_id=?", (job_id,))
        return {"ok": True, "done": True, "run": summary}
    if st in ("failed", "cancelled"):
        db.run("DELETE FROM pending_runs WHERE job_id=?", (job_id,))
        return {"ok": False, "done": True, "status": st, "error": j.get("error") or ("прогон отменён" if st == "cancelled" else "прогон не выполнен")}
    return {"ok": True, "done": False, "status": st, "position": j.get("position") or 0, "progress": j.get("progress")}


@router.get("/threads/{thread_id}/catchup")
def catchup(thread_id: int) -> dict:
    """Догнать прогоны, чей опрос оборвался: дописать готовые карточки, сообщить о ещё идущих.
    Вызывается при открытии чата — без этого результат пропадал вместе с закрытой вкладкой."""
    rows = db.q("SELECT job_id, agent_id FROM pending_runs WHERE thread_id=? ORDER BY created_at", (thread_id,))
    added, running = 0, []
    for r in rows:
        try:
            j = abop.run_job(r["job_id"])
        except abop.AbopError:
            continue                      # сервер недоступен — задание останется в очереди догона
        st = j.get("status")
        if st == "done" and j.get("run"):
            _persist_run(thread_id, r["agent_id"] or j.get("agent_id") or "", _run_summary(r["agent_id"], j["run"]))
            db.run("DELETE FROM pending_runs WHERE job_id=?", (r["job_id"],))
            added += 1
        elif st in ("failed", "cancelled"):
            db.run("DELETE FROM pending_runs WHERE job_id=?", (r["job_id"],))
        else:
            running.append({"job_id": r["job_id"], "agent_id": r["agent_id"], "status": st})
    return {"ok": True, "added": added, "running": running}


@router.post("/threads/{thread_id}/run-job/{job_id}/cancel")
def run_job_cancel(thread_id: int, job_id: str) -> dict:
    try:
        return {"ok": True, **(abop.cancel_job(job_id) or {})}
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}


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
            title = s.get("title") or sid                      # русское название навыка
            hint = s.get("short") or s.get("summary") or s.get("description") or ""
            out.append({"id": sid, "title": title, "hint": hint})
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


@router.post("/threads/{thread_id}/note")
def add_note(thread_id: int, body: NoteIn) -> dict:
    """Сохранить служебное сообщение ассистента (карточка цепочки/решения/уведомление) в историю треда."""
    if not db.q("SELECT id FROM threads WHERE id=?", (thread_id,)):
        return {"ok": False, "error": "no_thread"}
    mid = db.run("INSERT INTO messages(thread_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
                 (thread_id, "assistant", body.content or "", json.dumps(body.meta or {}, ensure_ascii=False), db.now()))
    db.run("UPDATE threads SET updated_at=? WHERE id=?", (db.now(), thread_id))
    return {"ok": True, "id": mid}


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
class MetaIn(BaseModel):
    meta: dict = {}


@router.patch("/threads/{thread_id}/messages/{message_id}/meta")
def patch_message_meta(thread_id: int, message_id: int, body: MetaIn):
    """Дописать поля в meta сообщения (карточка прогона): решение по заявке, номер созданной задачи.
    Без этого решение жило только в памяти вкладки: после переоткрытия чата кнопка «Подтвердить»
    снова была активна, а ссылка на заведённую задачу исчезала."""
    rows = db.q("SELECT meta FROM messages WHERE id=? AND thread_id=?", (message_id, thread_id))
    if not rows:
        raise HTTPException(404, "нет такого сообщения")
    try:
        meta = json.loads(rows[0]["meta"] or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    patch = body.meta or {}
    ra = dict(meta.get("run_agent") or {})
    ra.update(patch.get("run_agent") or {})
    meta.update({k: v for k, v in patch.items() if k != "run_agent"})
    if ra:
        meta["run_agent"] = ra
    db.run("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), message_id))
    return {"ok": True, "meta": meta}


def _run_text(meta: dict) -> str:
    """Текст карточки прогона для выгрузки: находки, доставка, решение по подтверждению.
    Без этого в Word и Excel уезжала строка вида «[агент X] находок: 12» вместо самих находок."""
    ra = (meta or {}).get("run_agent") or {}
    pr = (meta or {}).get("pipeline_result") or {}
    parts: list[str] = []
    if ra:
        head = ra.get("agent_name") or ra.get("agent_id") or "агент"
        v = ra.get("verdict") or {}
        parts.append("Агент: %s%s" % (head, " — пройден" if v.get("ok") else (" — есть замечания" if v else "")))
        fnd = ra.get("findings") or []
        total = ra.get("findings_total") or len(fnd)
        if fnd:
            parts.append("Находки (%d из %d):" % (len(fnd), total))
            parts += ["  - " + str(f).strip() for f in fnd]
        for d in ra.get("delivery") or []:
            mode = {"awaiting_hitl": "ждёт подтверждения", "real": "отправлено",
                    "dry_run": "черновик", "denied": "доступ закрыт"}.get(d.get("mode"), d.get("mode") or "")
            parts.append("Доставка: %s%s — %s" % (d.get("channel") or "", (" → " + d["to"]) if d.get("to") else "", mode))
        if ra.get("hitl_done"):
            parts.append("Решение: %s" % ("подтверждено" if ra["hitl_done"] == "approve" else "отклонено"))
        for r in (ra.get("cmd_results") or {}).values():
            parts.append("Результат: %s%s" % (r.get("text") or "", (" · " + r["url"]) if r.get("url") else ""))
        if ra.get("run_id"):
            parts.append("Прогон: %s" % ra["run_id"])
    for st in (pr.get("steps") or []):
        parts.append("Шаг «%s»: %s" % (st.get("agent_name") or st.get("agent_id") or "", str(st.get("text") or "").strip()[:2000]))
    return "\n".join(parts)


def _export_text(m: dict) -> str:
    """Сообщение для выгрузки: обычный текст, а для карточки прогона — её содержимое."""
    try:
        meta = json.loads(m["meta"] or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    body = _run_text(meta)
    return (m["content"] + ("\n\n" + body if body else "")) if body else m["content"]


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
                lines.append(f"\n## {who}\n\n{_export_text(m)}\n")
            p.write_text("\n".join(lines), encoding="utf-8")
        elif fmt == "docx":
            from docx import Document
            doc = Document()
            doc.add_heading(title, 0)
            for m in msgs:
                doc.add_heading("Вы" if m["role"] == "user" else "Ассистент", level=2)
                doc.add_paragraph(_export_text(m))
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
                ws.append([m["role"], _export_text(m), meta.get("model", ""), meta.get("cost_rub", "")])
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
        res = abop.chat(prompt=prompt, profile=profile, system=_system_for(skills, body.persona), max_tokens=1500)
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


# ── НАСТОЯЩИЙ стриминг: проксируем SSE-дельты от ABOP /api/chat/stream (токены по мере генерации).
#    Пользователь видит ответ как в обычных чатах — печать в реальном времени. ──
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
            for ev in abop.chat_stream(prompt=prompt, profile=profile, system=_system_for(skills, body.persona), max_tokens=1500):
                if ev.get("delta"):
                    full += ev["delta"]
                    yield _sse({"delta": ev["delta"]})
                elif ev.get("done"):
                    meta = {"model": ev.get("model")}
                elif ev.get("error"):
                    yield _sse({"error": ev["error"]})
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
