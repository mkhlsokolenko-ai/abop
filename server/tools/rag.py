"""RAG-инструменты: индексация и поиск по личной коллекции студента.

Pipeline из недели 9: chunk -> embed(BGE-M3) -> Qdrant -> search -> rerank(BGE).
Коллекция у каждого студента своя (префикс ape_ + его id), изолирована от корпуса sLAVA.
"""
from __future__ import annotations

import hashlib

from ..auth import current_student
from ..clients import embed, qdrant_ensure, qdrant_search, qdrant_upsert, rerank
from ..config import settings
from ..db import log_usage
from ..pricing import cost_rub


def _collection(student_id: str) -> str:
    short = hashlib.sha1(student_id.encode()).hexdigest()[:12]
    return settings.collection(f"stu_{short}")


def register(mcp) -> None:
    @mcp.tool
    async def rag_index(documents: list[str], session_id: str) -> dict:
        """Проиндексировать документы (чанки) в свою коллекцию Qdrant.

        documents: список текстовых чанков (стратегию чанкинга выбираешь сам —
        см. скилл rag-architect). Возвращает число проиндексированных точек.
        """
        student = current_student()
        col = _collection(student.db_id)
        await qdrant_ensure(col, settings.embed_dim)
        vectors = await embed(documents)
        points = [
            {
                "id": int(hashlib.sha1(f"{i}:{doc[:64]}".encode()).hexdigest()[:15], 16),
                "vector": vec,
                "payload": {"text": doc},
            }
            for i, (doc, vec) in enumerate(zip(documents, vectors))
        ]
        await qdrant_upsert(col, points)
        await log_usage(
            student_id=student.db_id, username=student.username, session_id=session_id,
            kind="embed", model=settings.embed_model,
            input_tokens=sum(len(d) // 4 for d in documents), output_tokens=0,
            cost_rub=cost_rub(settings.embed_model, sum(len(d) // 4 for d in documents), 0),
        )
        return {"indexed": len(points), "collection": col}

    @mcp.tool
    async def rag_search(query: str, session_id: str, top_k: int = 5, rerank_top: int = 20) -> dict:
        """Поиск по своей коллекции: embed -> vector search -> rerank.

        Сначала достаём rerank_top кандидатов вектором, затем reranker (BGE) поднимает
        precision и оставляет top_k. Возвращает найденные чанки с оценками.
        """
        student = current_student()
        col = _collection(student.db_id)
        qvec = (await embed([query]))[0]
        hits = await qdrant_search(col, qvec, limit=rerank_top)
        if not hits:
            return {"results": [], "note": "Коллекция пуста или ничего не найдено."}
        docs = [h["payload"]["text"] for h in hits]
        ranked = await rerank(query, docs, top_n=top_k)
        results = [
            {"text": docs[r["index"]], "score": r["score"]} for r in ranked
        ]
        await log_usage(
            student_id=student.db_id, username=student.username, session_id=session_id,
            kind="rerank", model=settings.reranker_model,
            input_tokens=len(query) // 4, output_tokens=0, cost_rub=0.0,
        )
        return {"results": results}
