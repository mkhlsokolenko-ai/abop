"""Коннекторы рабочих источников — ТОНКИЙ КЛИЕНТ ABOP (отвязано от курсового шлюза).

Каталог = коннекторы + рецепты Data Plane из ABOP (`/api/data/connectors`, `/api/data/recipes`) под JWT
пользователя (ABAC: флаг `allowed` по отделу). Плюс локальные коннекторы, которые работают СРАЗУ под
правами пользователя ОС — читаем то, что доступно человеку на его машине (Office/последние документы).

Локальный файл → canonical text → знания треда (через модуль chat, локальное хранилище сайдкара).
Удалённые системы (CRM/ERP) — делегированный доступ (token-exchange), см. status:planned.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from ... import abop_client as abop

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


@router.get("/list")
def connectors() -> dict:
    """Каталог: (1) коннекторы Data Plane из ABOP, (2) рецепты источник→entity из ABOP,
    (3) локальные коннекторы под правами ОС. ABOP-часть отражает то, что реально видит агент."""
    abop_conns, abop_recipes, abop_error = [], [], None
    try:
        for c in abop.connectors():
            sysinfo = c.get("system") or {}
            note = c.get("adapter") or c.get("target") or ""
            if sysinfo:
                note = f"{sysinfo.get('kind', '')} · {sysinfo.get('egress', '')}".strip(" ·") or note
            abop_conns.append({
                "id": c.get("id"), "title": c.get("title") or c.get("id"),
                "kind": "abop", "note": note,
                "status": "ready" if c.get("allowed", True) else "blocked",
                "allowed": c.get("allowed", True), "access_reason": c.get("access_reason"),
            })
        for r in abop.recipes():
            abop_recipes.append({
                "id": r.get("name") or r.get("id"), "title": r.get("title") or r.get("name"),
                "entity": r.get("entity") or r.get("target"),
                "note": r.get("note") or r.get("source") or "", "status": "ready",
            })
    except abop.AbopError as e:
        abop_error = str(e)

    local = [
        {"id": "local-file", "title": "Локальный файл (Office/текст)", "kind": "local",
         "note": "docx/xlsx/csv/txt/json/md под правами пользователя", "status": "ready"},
        {"id": "recent-files", "title": "Последние рабочие файлы", "kind": "local",
         "note": "недавние документы из ~/Documents и Downloads", "status": "ready"},
    ]
    return {"connectors": abop_conns, "recipes": abop_recipes, "local": local, "abop_error": abop_error}


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
    """Локальный файл → текст под правами ОС. Возвращаем распознанный текст, чтобы приложить его
    в знания треда (локальное хранилище сайдкара, модуль chat) — без курсового RAG-шлюза."""
    try:
        text = _read_local(body.path)
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except Exception as e:  # noqa: BLE001 — например, нет docx/openpyxl в этой сборке
        return {"ok": False, "error": f"read: {e}"}
    if not text.strip():
        return {"ok": False, "error": "empty"}
    return {"ok": True, "name": os.path.basename(body.path), "chars": len(text), "text": text}
