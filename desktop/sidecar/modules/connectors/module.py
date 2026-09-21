"""Коннекторы рабочих источников. Локальные (файлы Office/последние документы) работают
СРАЗУ под правами пользователя ОС — читаем то, что доступно человеку на его машине.
Удалённые системы (CRM/ERP) — через делегированный OAuth/token-exchange Keycloak (см. status:planned).

Локальный коннектор: файл → canonical text → знания треда (RAG через шлюз).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from ... import gateway

MANIFEST = {"id": "connectors", "title": "Источники", "icon": "connectors", "ui": "connectors", "order": 70}
router = APIRouter()

_TEXT_EXT = (".txt", ".md", ".csv", ".json", ".log")


class IngestIn(BaseModel):
    path: str
    session_id: str = "desktop-connectors"


def _read_local(path: str) -> str:
    """Читает локальный файл под правами пользователя. docx/xlsx — если есть либы, иначе текст."""
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise FileNotFoundError(str(p))
    ext = p.suffix.lower()
    if ext == ".docx":
        from docx import Document
        return "\n".join(par.text for par in Document(str(p)).paragraphs if par.text.strip())
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(str(p), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"# {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append("\t".join(cells))
        return "\n".join(out)
    if ext in _TEXT_EXT:
        return p.read_text(encoding="utf-8", errors="replace")
    # прочее — пробуем как текст
    return p.read_text(encoding="utf-8", errors="replace")


class ExchangeReq(BaseModel):
    target: str
    audience: str = ""


# Удалённые системы для делегированного доступа (token-exchange от имени пользователя).
REMOTE = [
    {"id": "onedrive", "title": "OneDrive / SharePoint", "note": "документы Microsoft 365"},
    {"id": "gsuite", "title": "Google Drive", "note": "документы Google Workspace"},
    {"id": "crm", "title": "CRM компании", "note": "сделки/контакты по вашим правам"},
]


@router.get("/list")
def connectors() -> dict:
    """Каталог коннекторов: локальные под правами ОС; удалённые — делегированный доступ."""
    remote = [{"id": r["id"], "title": r["title"], "kind": "remote", "note": r["note"],
               "status": "delegated"} for r in REMOTE]
    return {"connectors": [
        {"id": "local-file", "title": "Локальный файл (Office/текст)", "kind": "local",
         "note": "docx/xlsx/csv/txt/json/md под правами пользователя", "status": "ready"},
        {"id": "recent-files", "title": "Последние рабочие файлы", "kind": "local",
         "note": "недавние документы из ~/Documents и Downloads", "status": "ready"},
        *remote,
    ]}


@router.post("/connect")
def connect(body: ExchangeReq) -> dict:
    """Подключить удалённую систему через делегированный доступ (token-exchange на шлюзе)."""
    try:
        r = gateway.portal_post("/api/connectors/exchange", {"target": body.target, "audience": body.audience})
    except gateway.AuthRequired:
        return {"ok": False, "error": "auth_required"}
    except gateway.GatewayError as e:
        return {"ok": False, "error": str(e)}
    return r


def _walk(root: Path, depth: int, acc: list) -> None:
    """Устойчивый обход до заданной глубины (пропускаем недоступное/длинные пути Windows)."""
    if depth < 0:
        return
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    exts = _TEXT_EXT + (".docx", ".xlsx", ".pdf")
    for e in entries:
        try:
            if e.is_file() and os.path.splitext(e.name)[1].lower() in exts:
                acc.append((e.stat().st_mtime, e.path, e.name))
            elif e.is_dir() and not e.name.startswith("."):
                _walk(Path(e.path), depth - 1, acc)
        except OSError:
            continue


@router.get("/recent")
def recent(limit: int = 20) -> dict:
    """Последние файлы пользователя (Documents/Downloads/Desktop) — под правами ОС, глубина 2."""
    roots = [Path.home() / "Documents", Path.home() / "Downloads", Path.home() / "Desktop"]
    files: list = []
    for root in roots:
        if root.exists():
            _walk(root, 2, files)
    files.sort(reverse=True)
    return {"files": [{"path": f[1], "name": f[2]} for f in files[:limit]]}


@router.post("/ingest")
def ingest(body: IngestIn) -> dict:
    """Локальный файл → текст (под правами ОС) → знания через RAG-индекс шлюза."""
    try:
        text = _read_local(body.path)
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except Exception as e:  # noqa: BLE001 — например, нет docx/openpyxl в этой сборке
        return {"ok": False, "error": f"read: {e}"}
    if not text.strip():
        return {"ok": False, "error": "empty"}
    try:
        r = gateway.call("rag_index", {"documents": [text], "session_id": body.session_id})
    except gateway.AuthRequired:
        return {"ok": False, "error": "auth_required"}
    except gateway.GatewayError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "name": os.path.basename(body.path), "indexed": r.get("indexed", 1), "chars": len(text)}
