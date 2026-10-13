"""Очередь HITL-подтверждений — Postgres + mem. Human-in-the-loop для действий наружу.

Когда OUT-узел (или action-навык) помечен `hitl`, прогон НЕ отправляет наружу сразу, а кладёт
ЗАЯВКУ в очередь (state=pending) с уже готовым артефактом (отчёт+конфиг канала). Оператор видит
очередь (GET /api/hitl/queue), подтверждает/отклоняет (POST /api/hitl/{id}/approve) → при approve
выполняется РЕАЛЬНАЯ доставка. Так «Под HITL» перестаёт быть вечным ожиданием. ABAC по семье агента.

hitl_items{id PK, run_id, agent_id, family, node, title, channel, to_addr, payload JSONB,
           state, requested_by, decided_by, reason, created_at, decided_at}.
"""
from __future__ import annotations

import json
import uuid

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS hitl_items (
    id           TEXT PRIMARY KEY,
    run_id       TEXT,
    agent_id     TEXT,
    family       TEXT,
    node         TEXT,
    title        TEXT,
    channel      TEXT,
    to_addr      TEXT,
    payload      JSONB NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT,
    decided_by   TEXT,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_hitl_state ON hitl_items (state, created_at DESC);
"""

_MEM: dict[str, dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def create(*, run_id: str, agent_id: str, family: str, node: str, title: str,
                 channel: str, to_addr: str, payload: dict, requested_by: str) -> dict:
    item = {"id": "hitl-" + uuid.uuid4().hex[:12], "run_id": run_id, "agent_id": agent_id,
            "family": family, "node": node, "title": title, "channel": channel, "to_addr": to_addr,
            "payload": payload, "state": "pending", "requested_by": requested_by}
    if not _has_pg():
        _MEM[item["id"]] = item
        return item
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO hitl_items (id,run_id,agent_id,family,node,title,channel,to_addr,payload,"
            "state,requested_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)",
            (item["id"], run_id, agent_id, family, node, title, channel, to_addr,
             json.dumps(payload, ensure_ascii=False), requested_by))
    return item


async def get(item_id: str) -> dict | None:
    if not _has_pg():
        return _MEM.get(item_id)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,run_id,agent_id,family,node,title,channel,to_addr,payload,state,requested_by,"
            "decided_by,reason FROM hitl_items WHERE id=%s", (item_id,))
        r = await cur.fetchone()
    if not r:
        return None
    keys = ["id", "run_id", "agent_id", "family", "node", "title", "channel", "to_addr", "payload",
            "state", "requested_by", "decided_by", "reason"]
    return dict(zip(keys, r))


async def list_pending() -> list[dict]:
    """Ждущие решения заявки. Несут то, что нужно карточке: КТО просил, что лежит готовым, уровень.

    Прежде список отдавал только «что и куда», и по нему нельзя было ни отобрать свои решения, ни
    показать «не отправлено»: карточка показывала ожидание вместо готового документа.
    """
    if not _has_pg():
        итог = []
        for v in _MEM.values():
            if v.get("state") != "pending":
                continue
            пз = v.get("payload") or {}
            итог.append({**{k: v.get(k) for k in
                            ("id", "run_id", "agent_id", "family", "node", "title", "channel",
                             "to_addr", "requested_by", "created_at")},
                         "черновик": пз.get("черновик") or "", "уровень": пз.get("уровень"),
                         "основание": пз.get("основание") or "", "решает": пз.get("решает") or ""})
        return итог
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,run_id,agent_id,family,node,title,channel,to_addr,created_at,requested_by,"
            "payload->>'черновик',payload->>'уровень',payload->>'основание',payload->>'решает' "
            "FROM hitl_items WHERE state='pending' ORDER BY created_at DESC LIMIT 100")
        rows = await cur.fetchall()
    keys = ["id", "run_id", "agent_id", "family", "node", "title", "channel", "to_addr",
            "created_at", "requested_by", "черновик", "уровень", "основание", "решает"]
    return [dict(zip(keys, r)) for r in rows]


async def просрочить_черновики(старше_сек: int | None = None) -> int:
    """Черновики старше срока уходят в `expired`. Возвращает число убранных.

    **Почему закрытие, а не удаление.** Черновик — работа агента и след решения, которого не
    приняли; стереть его значит потерять ответ на вопрос «что мы не отправили и почему». Он
    перестаёт лежать в «Результатах», но остаётся в журнале.

    Срок — 30 дней (`levels.ЖИВЁТ_ЧЕРНОВИК`, решение владельца 13.10). Один UPDATE, идемпотентный:
    реплик может быть несколько, и выбирать среди них главную ради уборки незачем.
    """
    from . import levels

    срок = int(старше_сек if старше_сек is not None else levels.ЖИВЁТ_ЧЕРНОВИК)
    причина = f"решение не принято за {срок // 86400} дн. — черновик закрыт фоновой уборкой"
    if not _has_pg():
        import time as _t

        убрано = 0
        for v in _MEM.values():
            родился = float((v.get("payload") or {}).get("живёт_с") or 0)
            if v.get("state") == "pending" and родился and _t.time() - родился > срок:
                v.update({"state": "expired", "decided_by": "система", "reason": причина})
                убрано += 1
        return убрано
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "UPDATE hitl_items SET state='expired', decided_by='система', reason=%s, "
            "decided_at=now() WHERE state='pending' "
            "AND created_at < now() - (%s || ' seconds')::interval",
            (причина, str(срок)))
        return int(getattr(cur, "rowcount", 0) or 0)


async def уборка_loop(период: int = 3600) -> None:
    """Фоновая уборка просроченных черновиков. Сбой одного круга не должен ронять цикл."""
    import asyncio

    while True:
        try:
            убрано = await просрочить_черновики()
            if убрано:
                from . import observability as obs

                obs.inc("abop_hitl_expired_total", убрано)
        except Exception:  # noqa: BLE001 — уборка не критична, цикл обязан выжить
            pass
        await asyncio.sleep(период)


async def update_payload(item_id: str, patch: dict) -> None:
    """Дописать поля в payload заявки (id команды в шине, результат коннектора) — без смены state."""
    if not _has_pg():
        if item_id in _MEM:
            _MEM[item_id]["payload"] = {**(_MEM[item_id].get("payload") or {}), **patch}
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("UPDATE hitl_items SET payload = payload || %s::jsonb WHERE id=%s",
                           (json.dumps(patch, ensure_ascii=False), item_id))


async def find_by_command(command_id: str) -> dict | None:
    """Заявка, чья команда ушла в шину под этим id (ответ коннектора command.done/failed → карточка)."""
    if not command_id:
        return None
    if not _has_pg():
        return next((v for v in _MEM.values() if (v.get("payload") or {}).get("command_id") == command_id), None)
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("SELECT id FROM hitl_items WHERE payload->>'command_id' = %s ORDER BY created_at DESC LIMIT 1",
                                 (command_id,))
        r = await cur.fetchone()
    return await get(r[0]) if r else None


async def purge(pending_only: bool = True) -> int:
    """Снять заявки на подтверждение. По умолчанию только ожидающие.

    Заявка — это превью того, что уйдёт наружу. Накопившиеся с проверок превью на показе всплывают
    чужими и смазывают кульминацию, поэтому перед демо очередь обнуляют.
    """
    if not _has_pg():
        ids = [k for k, v in _MEM.items() if not pending_only or v.get("state") == "pending"]
        for k in ids:
            _MEM.pop(k, None)
        return len(ids)
    from .db import _conn
    sql = "DELETE FROM hitl_items" + (" WHERE state = 'pending'" if pending_only else "")
    async with _conn() as conn:
        cur = await conn.execute(sql)
        return int(cur.rowcount or 0)


async def decide(item_id: str, state: str, decided_by: str, reason: str = "") -> None:
    if not _has_pg():
        if item_id in _MEM:
            _MEM[item_id].update({"state": state, "decided_by": decided_by, "reason": reason})
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "UPDATE hitl_items SET state=%s, decided_by=%s, reason=%s, decided_at=now() WHERE id=%s",
            (state, decided_by, reason, item_id))
