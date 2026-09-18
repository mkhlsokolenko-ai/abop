"""Langfuse — трассировка LLM-вызовов прогона (Observability Фаза 0, best-effort).

Один trace на прогон + generation на каждый навык (модель/токены/латентность/ошибка). Включается,
когда заданы LANGFUSE_URL + LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY (langfuse на server-2 —
переиспользуем); иначе no-op (каркас готов, ключи добавляются в env позже — как у OUT-каналов).
НИКОГДА не роняет прогон; ошибку отправки не глушим молча — пишем в observability-лог.
См. docs/CONCEPT_SCALING_OBSERVABILITY.md §3.6.
"""
from __future__ import annotations

import base64
import datetime as dt

from . import observability as obs
from .config import settings


def enabled() -> bool:
    return bool(settings.langfuse_url and settings.langfuse_public_key and settings.langfuse_secret_key)


def _ts() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def emit_run(trace_id: str, run_id: str, agent: dict, result: dict) -> None:
    """Отправить трейс прогона в langfuse (trace + generation на навык). Best-effort."""
    if not enabled():
        return
    tid = trace_id or run_id
    try:
        import httpx
        v = result.get("verdict") or {}
        rm = (result.get("run_metrics") or {})
        events = [{
            "id": tid + "-t", "type": "trace-create", "timestamp": _ts(),
            "body": {"id": tid, "name": "agent.run", "userId": result.get("started_by"),
                     "metadata": {"run_id": run_id, "agent": agent.get("id"), "family": agent.get("family"),
                                  "verdict_ok": v.get("ok"), "autonomy_used": v.get("autonomy_used"),
                                  "cost_rub": (rm.get("cost") or {}).get("rub"),
                                  "total_ms": (rm.get("timings") or {}).get("total_ms"),
                                  "soft_errors": len(result.get("soft_errors") or [])}},
        }]
        for i, f in enumerate(result.get("findings") or []):
            if not (isinstance(f, dict) and f.get("model")):
                continue
            gid = f"{tid}-g{i}"
            events.append({
                "id": gid, "type": "generation-create", "timestamp": _ts(),
                "body": {"id": gid, "traceId": tid, "name": f.get("skill") or "skill",
                         "model": f.get("model"),
                         "usage": {"input": f.get("input_tokens") or 0,
                                   "output": f.get("output_tokens") or 0, "unit": "TOKENS"},
                         "level": ("ERROR" if f.get("error") else "DEFAULT"),
                         "statusMessage": f.get("error"),
                         "metadata": {"ms": f.get("ms"), "entities": f.get("entities")}},
            })
        auth = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(settings.langfuse_url.rstrip("/") + "/api/public/ingestion",
                               headers={"Authorization": "Basic " + auth,
                                        "Content-Type": "application/json"},
                               json={"batch": events})
            if r.status_code >= 300:
                obs.log_event("warn", "langfuse.reject", status=r.status_code, run_id=run_id)
    except Exception as ex:  # noqa: BLE001 — телеметрия опциональна, но не молча
        obs.log_event("warn", "langfuse.error", run_id=run_id, error=f"{type(ex).__name__}: {ex}")
