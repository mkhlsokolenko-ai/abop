"""Клиент sLAVA API (RAG-платформа на сервере-2) — загрузка/поиск знания ПО СЕМЬЯМ.

ABOP (сервер-1) ходит к sLAVA через relay (slava-relay.service: 201.51.5.24:8000 → 127.0.0.1:8000);
sLAVA фронтит Qdrant. Знание/регламент каждой семьи — в свою коллекцию slava_fam_<family>
(изоляция корпусов; гейт на MCP-шлюзе тем же access-слоем). См. agent-rbac-mcp-gateway,
reglament-conformance-slava. Конфиг базы: settings.slava_api_base_url (на проде → relay).

/api/v1/ingest (multipart: file, collection, replace, x-tenant-id) — чанкинг+эмбеддинг+upsert в Qdrant.
/api/v1/query ({query, collection, top_k}) — векторный поиск с ререйком.
"""
from __future__ import annotations

import re

import httpx

from .config import settings


def _base() -> str:
    return (settings.slava_api_base_url or "").rstrip("/")


def fam_collection(family: str) -> str:
    """Имя family-коллекции sLAVA (совпадает с уже поднятыми slava_fam_<family>)."""
    k = re.sub(r"[^a-z0-9_]", "_", (family or "shared").lower())
    return "slava_fam_" + (k if k and k != "_" else "shared")


async def ingest(collection: str, filename: str, content, *, tenant: str = "abop",
                 replace: bool = False, doc_id: str | None = None) -> dict:
    """Загрузить документ в коллекцию sLAVA (чанкинг+эмбеддинг+upsert). content — str/bytes."""
    body = content.encode("utf-8") if isinstance(content, str) else content
    files = {"file": (filename, body, "text/plain")}
    data = {"collection": collection, "replace": "true" if replace else "false"}
    if doc_id:
        data["doc_id"] = doc_id
    async with httpx.AsyncClient(timeout=180) as cli:
        r = await cli.post(f"{_base()}/api/v1/ingest", files=files, data=data,
                           headers={"x-tenant-id": tenant})
        r.raise_for_status()
        return r.json()


async def query(collection: str, text: str, *, top_k: int = 5, tenant: str = "abop") -> dict:
    """Поиск по коллекции sLAVA (векторный + ререйк). Возвращает ответ платформы."""
    async with httpx.AsyncClient(timeout=90) as cli:
        r = await cli.post(f"{_base()}/api/v1/query",
                           json={"query": text, "collection": collection, "top_k": top_k},
                           headers={"x-tenant-id": tenant})
        r.raise_for_status()
        return r.json()


async def collections() -> dict:
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.get(f"{_base()}/api/v1/collections")
        r.raise_for_status()
        return r.json()
