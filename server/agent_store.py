"""Хранилище AgentVersion — собранные на канве агенты (ADR-024 жизненный цикл).

AgentVersion{id, name, contract_audit_id, version, status, graph{nodes,edges}, autonomy_max}.
status: draft → tested → deployed → retired (пока сохраняем draft). Привязан к ContractSet
(audit_id) — автономия агента ограничена его DeploymentContract (ADR-013, проверка в assembly).
Персистентность — Postgres; без DSN — фолбэк в память (как contract_store).
"""
from __future__ import annotations

import json

from . import tenancy
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_versions (
    id            TEXT PRIMARY KEY,             -- <audit_id>.v<N>
    name          TEXT NOT NULL,
    contract_audit_id TEXT NOT NULL,             -- 'authored' для агентов, собранных без контракта
    version       INT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',-- draft|tested|deployed|retired
    autonomy_max  TEXT,                          -- фактический потолок автономии графа
    graph         JSONB NOT NULL,                -- {nodes[], edges[]}
    family        TEXT,                          -- ADR-032: семья агента
    role          TEXT,                          -- ADR-032: член-роль семьи
    transitions   JSONB,                         -- ADR-032: become-переходы (роли той же семьи)
    source        TEXT DEFAULT 'contract',       -- contract|authored
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_av_contract ON agent_versions (contract_audit_id);
-- миграция существующих таблиц (идемпотентно):
ALTER TABLE agent_versions ADD COLUMN IF NOT EXISTS family TEXT;
ALTER TABLE agent_versions ADD COLUMN IF NOT EXISTS role TEXT;
ALTER TABLE agent_versions ADD COLUMN IF NOT EXISTS transitions JSONB;
ALTER TABLE agent_versions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'contract';
ALTER TABLE agent_versions ADD COLUMN IF NOT EXISTS verification JSONB;
-- Арендатор (шаг 5): агент принадлежит компании. Уже заведённые достаются арендатору
-- по умолчанию — той компании, для которой система и работала.
ALTER TABLE agent_versions ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_av_tenant ON agent_versions (tenant, contract_audit_id);
"""
# Арендатор в ПЕРВИЧНОМ ключе: «omninet-showcase-2026.v1» у двух заказчиков совпадёт.
SCHEMA += tenancy.миграция_ключа("agent_versions", "id")

_MEM: dict[str, dict] = {}


def _has_pg() -> bool:
    return bool(settings.pg_dsn)


async def init() -> None:
    if not _has_pg():
        return
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(SCHEMA)


async def next_version(audit_id: str) -> int:
    if not _has_pg():
        return 1 + sum(1 for a in _MEM.values() if a["contract_audit_id"] == audit_id
                       and a.get("tenant") == tenancy.текущий())
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM agent_versions "
            "WHERE contract_audit_id=%s AND tenant=%s",
            (audit_id, tenancy.текущий()))
        return int((await cur.fetchone())[0])


_ON_SAVE = []   # хуки после сохранения агента (словарь лексем и т.п.); ошибки хука не ломают сохранение


def on_save(fn) -> None:
    """Подписка на сохранение агента: сервер пересчитывает словарь лексем нового агента."""
    if fn not in _ON_SAVE:
        _ON_SAVE.append(fn)


async def _fire_saved(row: dict) -> None:
    import asyncio
    for fn in list(_ON_SAVE):
        try:
            r = fn(row)
            if asyncio.iscoroutine(r):
                await r
        except Exception:  # noqa: BLE001
            pass


async def save(*, name: str, audit_id: str, version: int, graph: dict,
               autonomy_max: str, created_by: str = "dev",
               family: str = "", role: str = "", transitions=None, source: str = "contract",
               verification: dict | None = None) -> dict:
    aid = f"{audit_id}.v{version}"
    row = {"id": aid, "name": name, "contract_audit_id": audit_id, "version": version,
           "status": "draft", "autonomy_max": autonomy_max, "graph": graph, "created_by": created_by,
           "family": family, "role": role, "transitions": transitions or [], "source": source,
           "verification": verification, "tenant": tenancy.текущий()}
    if not _has_pg():
        import datetime as _dt
        row["created_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _MEM[tenancy.ключ_памяти(aid)] = row
        await _fire_saved(row)
        return row
    from .db import _conn
    async with _conn() as conn:
        await conn.execute(
            "INSERT INTO agent_versions (id,name,contract_audit_id,version,status,autonomy_max,graph,family,role,transitions,source,created_by,verification,tenant) "
            "VALUES (%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (aid, name, audit_id, version, autonomy_max, json.dumps(graph),
             family, role, json.dumps(transitions or []), source, created_by,
             json.dumps(verification) if verification is not None else None,
             tenancy.текущий()))
    out = await get(aid) or row
    await _fire_saved(out)
    return out


async def latest(audit_id: str) -> dict | None:
    """Последняя (по версии) НЕ retired версия агента контракта — для upsert draft."""
    if not _has_pg():
        rows = [a for a in _MEM.values()
                if a["contract_audit_id"] == audit_id and a.get("status") != "retired"
                and a.get("tenant") == tenancy.текущий()]
        rows.sort(key=lambda a: a.get("version", 0), reverse=True)
        return rows[0] if rows else None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,version,status FROM agent_versions "
            "WHERE contract_audit_id=%s AND tenant=%s AND status<>'retired' "
            "ORDER BY version DESC LIMIT 1", (audit_id, tenancy.текущий()))
        r = await cur.fetchone()
    return {"id": r[0], "version": r[1], "status": r[2]} if r else None


async def save_draft(*, name: str, audit_id: str, graph: dict, autonomy_max: str,
                     created_by: str = "dev", family: str = "", role: str = "",
                     transitions=None, source: str = "contract",
                     verification: dict | None = None) -> dict:
    """UPSERT draft-версии (ADR-024): автосейв/сборка НЕ плодит версии — перезаписывает
    последний draft. Новая версия — только осознанным Пересмотром (revise → save()).
    Если последняя версия НЕ draft (tested/deployed) → создаёт новую (next_version)."""
    last = await latest(audit_id)
    if last and last.get("status") == "draft":
        aid = last["id"]; version = last["version"]
        if not _has_pg():
            a = _MEM.get(tenancy.ключ_памяти(aid)) or {}
            a.update({"graph": graph, "autonomy_max": autonomy_max, "name": name,
                      "family": family, "role": role, "verification": verification,
                      "tenant": tenancy.текущий()})
            _MEM[tenancy.ключ_памяти(aid)] = a
            return a
        from .db import _conn
        async with _conn() as conn:
            # Верификация конверта пересчитывается на каждое сохранение: граф изменился — значит
            # прежний вердикт относится уже не к нему.
            await conn.execute(
                "UPDATE agent_versions SET graph=%s, autonomy_max=%s, name=%s, family=%s, role=%s, "
                "verification=%s WHERE id=%s AND tenant=%s",
                (json.dumps(graph), autonomy_max, name, family, role,
                 json.dumps(verification) if verification is not None else None, aid,
                 tenancy.текущий()))
        out = await get(aid) or {"id": aid, "version": version}
        await _fire_saved(out)
        return out
    version = await next_version(audit_id)
    return await save(name=name, audit_id=audit_id, version=version, graph=graph,
                      autonomy_max=autonomy_max, created_by=created_by, family=family, role=role,
                      transitions=transitions, source=source, verification=verification)


async def get(agent_id: str) -> dict | None:
    """Агент по id — ТОЛЬКО свой. Чужой отвечает «нет такого», а не 403: существование чужого
    агента само по себе сведение о другой компании."""
    if not _has_pg():
        a = _MEM.get(tenancy.ключ_памяти(agent_id))
        return a if a and a.get("tenant") == tenancy.текущий() else None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,name,contract_audit_id,version,status,autonomy_max,graph,created_by,created_at,"
            "family,role,transitions,source,verification FROM agent_versions "
            "WHERE id=%s AND tenant=%s", (agent_id, tenancy.текущий()))
        r = await cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "contract_audit_id": r[2], "version": r[3], "status": r[4],
            "autonomy_max": r[5], "graph": r[6], "created_by": r[7],
            "created_at": r[8].isoformat() if r[8] else None,
            "family": r[9], "role": r[10], "transitions": r[11] or [], "source": r[12] or "contract",
            "verification": r[13]}


async def list_for(audit_id: str | None = None, limit: int = 100, archived: bool = False) -> list[dict]:
    """archived=False — активные (не retired); archived=True — Лимб (только retired, ADR-024)."""
    if not _has_pg():
        items = [a for a in _MEM.values()
                 if (not audit_id or a["contract_audit_id"] == audit_id)
                 and a.get("tenant") == tenancy.текущий()]
        items = [a for a in items if (a.get("status") == "retired") == archived]
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return [_brief(a) for a in items[:limit]]
    from .db import _conn
    # Фильтр по арендатору — в САМОМ тексте запроса, а не первым элементом списка условий:
    # список можно переставить или собрать иначе, и проверка раскладки (tests/test_tenancy.py)
    # этого уже не увидит. Условие, которое нельзя потерять, должно быть видно глазами.
    conds, args = [], [tenancy.текущий()]
    if audit_id:
        conds.append("contract_audit_id=%s"); args.append(audit_id)
    conds.append("status = 'retired'" if archived else "status <> 'retired'")
    where = "WHERE tenant=%s AND " + " AND ".join(conds)
    async with _conn() as conn:
        cur = await conn.execute(
            "SELECT id,name,contract_audit_id,version,status,autonomy_max,created_at,"
            "family,role,source,verification FROM agent_versions WHERE tenant=%s "
            f"{where.replace('WHERE tenant=%s AND ', 'AND ')} ORDER BY created_at DESC LIMIT %s",
            (*args, limit))
        rows = await cur.fetchall()
    return [{"id": r[0], "name": r[1], "contract_audit_id": r[2], "version": r[3], "status": r[4],
             "autonomy_max": r[5], "created_at": r[6].isoformat() if r[6] else None,
             "family": r[7], "role": r[8], "source": r[9] or "contract", "verification": r[10]} for r in rows]


async def set_status(agent_id: str, status: str) -> dict | None:
    """Сменить статус агента (ADR-024: draft→tested→deployed→retired). retired = Лимб/архив."""
    if not _has_pg():
        a = _MEM.get(tenancy.ключ_памяти(agent_id))
        if a and a.get("tenant") != tenancy.текущий():
            return None
        if a:
            a["status"] = status
        return a
    from .db import _conn
    async with _conn() as conn:
        await conn.execute("UPDATE agent_versions SET status=%s WHERE id=%s AND tenant=%s",
                           (status, agent_id, tenancy.текущий()))
    return await get(agent_id)


async def delete(agent_id: str) -> bool:
    """Полное удаление AgentVersion (жёсткое, в обход Лимба). Для черновиков/ошибок."""
    if not _has_pg():
        a = _MEM.get(tenancy.ключ_памяти(agent_id))
        if not a or a.get("tenant") != tenancy.текущий():
            return False
        return _MEM.pop(tenancy.ключ_памяти(agent_id), None) is not None
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute("DELETE FROM agent_versions WHERE id=%s AND tenant=%s",
                                 (agent_id, tenancy.текущий()))
    return getattr(cur, "rowcount", 0) > 0


def _brief(a: dict) -> dict:
    return {"id": a["id"], "name": a["name"], "contract_audit_id": a["contract_audit_id"],
            "version": a["version"], "status": a["status"], "autonomy_max": a.get("autonomy_max"),
            "created_at": a.get("created_at"), "family": a.get("family"), "role": a.get("role"),
            "source": a.get("source", "contract"), "verification": a.get("verification")}


async def арендаторы() -> list[str]:
    """Арендаторы, у которых есть живые агенты. СКВОЗНОЙ запрос — и это осознанно.

    Нужен планировщику: он работает вне запроса, контекста арендатора у него нет, и если бы он
    просто читал `list_for`, то видел бы только арендатора по умолчанию — расписания остальных
    компаний МОЛЧА не запускались бы. Тихое неисполнение хуже ошибки: никто не заметит.

    Единственный законный сквозной читатель вместе с `claim` очереди. Оба названы в
    tests/test_tenancy.py как исключения из сторожа раскладки.
    """
    if not _has_pg():
        return sorted({a.get("tenant") or tenancy.ПО_УМОЛЧАНИЮ for a in _MEM.values()
                       if a.get("status") != "retired"})
    from .db import _conn
    async with _conn() as conn:
        cur = await conn.execute(          # tenant: сквозной перечень арендаторов для планировщика
            "SELECT DISTINCT tenant FROM agent_versions WHERE status <> 'retired' ORDER BY tenant")
        return [r[0] for r in await cur.fetchall()]
