"""HTTP-клиенты к внешним сервисам, которые оборачивает MCP.

Всё, что уже развёрнуто на server-1 (sLAVA, 201.51.5.24), переиспользуется:
    * LLM + embeddings  -> RouteAI (routerai.ru, OpenAI-совместимый)
    * self-hosted LLM   -> vLLM на арендованной RTX 6000 (модели "local/*")
    * vector search     -> Qdrant (тот же инстанс, отдельные коллекции с префиксом)
    * reranker          -> RouteAI или /rerank живого sLAVA-API
"""
from __future__ import annotations

import httpx

from .config import settings


# ─────────────────────────── LLM (chat) ───────────────────────────

def _route(model: str) -> tuple[str, str, str]:
    """(base_url, api_key, real_model) по имени модели.

    "local/<name>" -> vLLM на LOCAL_LLM_BASE_URL; всё остальное -> RouteAI.
    """
    if model.startswith("local/") or model == "local":
        real = settings.local_llm_model or model.removeprefix("local/")
        return settings.local_llm_base_url, settings.local_llm_api_key, real
    return settings.routeai_base_url, settings.routeai_api_key, model


async def chat(
    messages: list[dict],
    *,
    profile: str = "standard",
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    response_format: dict | None = None,
) -> dict:
    """Chat completion с каскадным fallback по профилю.

    Возвращает {text, model, input_tokens, output_tokens}. Пробегает каскад
    (code->Qwen, research->DeepSeek, ...) — первая ответившая модель выигрывает.
    """
    cascade = [model] if model else settings.cascade_for(profile)
    last_err: Exception | None = None

    for m in cascade:
        base_url, api_key, real_model = _route(m)
        if not base_url:
            continue  # local не сконфигурен — пропускаем
        payload: dict = {
            "model": real_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        # Self-hosted Qwen3.8 — reasoning-модель: гасим "мышление вслух" в контенте,
        # чтобы кодовый агент получал чистый код (vLLM пробрасывает в chat template).
        if base_url == settings.local_llm_base_url and settings.local_llm_base_url:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            async with httpx.AsyncClient(timeout=120) as cli:
                r = await cli.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            usage = data.get("usage", {})
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            # Reasoning-модели (DeepSeek-V4-pro) иногда тратят весь бюджет на "мышление"
            # и возвращают пустой content (не ошибка). Трактуем как провал -> следующая модель.
            if not text.strip():
                last_err = RuntimeError(f"{m}: пустой content (reasoning исчерпал max_tokens)")
                continue
            return {
                "text": text,
                "model": m,
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
                "finish_reason": choice.get("finish_reason", ""),
            }
        except Exception as e:  # noqa: BLE001 — каскад: падаем к следующей модели
            last_err = e
            continue

    raise RuntimeError(f"Все модели каскада {cascade} недоступны: {last_err}")


# ─────────────────────────── Embeddings ───────────────────────────

async def embed(texts: list[str]) -> list[list[float]]:
    """BGE-M3 через RouteAI (OpenAI-совместимый /embeddings)."""
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(
            f"{settings.routeai_base_url}/embeddings",
            headers={"Authorization": f"Bearer {settings.routeai_api_key}"},
            json={"model": settings.embed_model, "input": texts},
        )
        r.raise_for_status()
        data = r.json()
    return [item["embedding"] for item in data["data"]]


# ─────────────────────────── Reranker ───────────────────────────

async def rerank(query: str, documents: list[str], top_n: int) -> list[dict]:
    """Возвращает [{index, score}] по убыванию релевантности.

    backend=routerai -> BGE-reranker через RouteAI; backend=slava -> /rerank sLAVA.
    """
    if settings.reranker_backend == "slava":
        url = f"{settings.slava_api_base_url}/rerank"
        payload = {"query": query, "documents": documents, "top_n": top_n}
        headers = {}
    else:
        url = f"{settings.routeai_base_url}/rerank"
        payload = {
            "model": settings.reranker_model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        headers = {"Authorization": f"Bearer {settings.routeai_api_key}"}

    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    results = data.get("results", data.get("data", []))
    return [
        {"index": item["index"], "score": item.get("relevance_score", item.get("score", 0.0))}
        for item in results
    ]


# ─────────────────────────── Qdrant ───────────────────────────

async def qdrant_ensure(collection: str, dim: int) -> None:
    """Создаёт коллекцию, если её нет (cosine)."""
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.get(_qurl(f"/collections/{collection}"), headers=_qheaders())
        if r.status_code == 200:
            return
        await cli.put(
            _qurl(f"/collections/{collection}"),
            headers=_qheaders(),
            json={"vectors": {"size": dim, "distance": "Cosine"}},
        )


async def qdrant_upsert(collection: str, points: list[dict]) -> None:
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.put(
            _qurl(f"/collections/{collection}/points?wait=true"),
            headers=_qheaders(),
            json={"points": points},
        )
        r.raise_for_status()


async def qdrant_search(collection: str, vector: list[float], limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            _qurl(f"/collections/{collection}/points/search"),
            headers=_qheaders(),
            json={"vector": vector, "limit": limit, "with_payload": True},
        )
        r.raise_for_status()
        return r.json()["result"]


def _qurl(path: str) -> str:
    return f"{settings.qdrant_url}{path}"


def _qheaders() -> dict:
    return {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else {}
