"""Движок триггеров ABOP: стартовые события агентов + политика инстанциации (spawn).

Триггеры живут в графе агента (kind:'trigger', поле `trig`). Этот модуль: извлекает их, матчит
cron, подписывается на события систем реестра (systems_store), исполняет политику spawn
(count/per-item/dedup/HITL-на-создание, автономия ≤ потолок контракта) и крутит фоновый
планировщик. Реальный запуск прогона — через переданный run_executor (web_api.execute_agent_run),
чтобы не плодить циклический импорт.

Безопасность (реальный планировщик на общем стенде): фаерит ТОЛЬКО `trig.enabled==true`, min-interval
на триггер, глобальный кап фаеров за тик, дедуп по курсору событий. Kill-switch ABOP_SCHEDULER=0.
См. persistence-localstorage-hole, agent-rbac-mcp-gateway (реестр систем).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re

from . import access, agent_store, contract_store, systems_store, trigger_store

A_LEVELS = ["A0", "A1", "A2", "A3", "A4"]
TICK_SEC = int(os.getenv("ABOP_SCHEDULER_TICK", "45"))
MIN_INTERVAL_SEC = int(os.getenv("ABOP_SCHEDULER_MIN_INTERVAL", "60"))
MAX_FIRES_PER_TICK = int(os.getenv("ABOP_SCHEDULER_MAX_FIRES", "3"))
SCHEDULER_ON = os.getenv("ABOP_SCHEDULER", "1") != "0"

_DEFAULT_PATH = {"mailpit": "/api/v1/messages", "redmine": "/issues.json", "nocodb": "", "twenty": "/rest/opportunities"}


def triggers_of(agent: dict) -> list[dict]:
    """Стартовые события из графа агента: [{id, title, trig{...}}]."""
    out = []
    for n in (agent.get("graph") or {}).get("nodes") or []:
        if n.get("kind") == "trigger":
            out.append({"id": n.get("id"), "title": n.get("title"), "trig": n.get("trig") or {}})
    return out


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(iso) -> dt.datetime | None:
    if not iso:
        return None
    if isinstance(iso, dt.datetime):
        return iso if iso.tzinfo else iso.replace(tzinfo=dt.timezone.utc)
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def cron_due(cron: str, now: dt.datetime, last: dt.datetime | None) -> bool:
    """Минимальный матчер: '*/N' (каждые N минут — для теста), 'HH:MM' или '5-field M H * * *'
    (ежедневно в это время)."""
    cron = (cron or "").strip()
    if not cron:
        return False
    m = re.match(r"^\*/(\d+)$", cron)
    if m:
        n = max(1, int(m.group(1)))
        return last is None or (now - last).total_seconds() >= n * 60
    hh = mm = None
    m = re.match(r"^(\d{1,2}):(\d{2})$", cron)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
    else:
        parts = cron.split()
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            mm, hh = int(parts[0]), int(parts[1])
    if hh is None:
        return False
    target = now.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
    return now >= target and (last is None or last < target)


async def _poll_count(system: dict, path: str) -> int | None:
    """Опросить REST-систему реестра и вернуть счётчик элементов (детекция новых). Best-effort,
    секрет из env по auth_ref (не логируем)."""
    base = (system or {}).get("base_url") or ""
    if not base:
        return None
    url = base.rstrip("/") + "/" + (path or "").lstrip("/")
    headers = {}
    ref = (system or {}).get("auth_ref")
    val = os.getenv(ref) if ref else None
    if val:
        headers["X-Redmine-API-Key" if system.get("id") == "redmine" else "Authorization"] = (
            val if system.get("id") == "redmine" else "Bearer " + val)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(url, headers=headers)
            r.raise_for_status()
            d = r.json()
        if isinstance(d, dict):
            for k in ("total", "messages_count", "total_count", "count"):
                if isinstance(d.get(k), int):
                    return d[k]
            for k in ("messages", "issues", "records", "data"):
                if isinstance(d.get(k), list):
                    return len(d[k])
        if isinstance(d, list):
            return len(d)
    except Exception:  # noqa: BLE001 — источник недоступен → тихо пропускаем тик
        return None
    return None


async def fire(agent: dict, trig_node: dict, reason: str, run_executor) -> dict:
    """Исполнить политику spawn: автономия ≤ потолок; HITL-на-создание → pending (не запускаем);
    иначе — реальный прогон через run_executor. Пишет фаер в trigger_store."""
    trig = trig_node.get("trig") or {}
    tid = trig_node.get("id")
    spawn = trig.get("spawn") or {}
    contract = await contract_store.get(agent.get("contract_audit_id") or "") or \
        {"intake": {"autonomy_ceiling": agent.get("autonomy_max") or "A2"}, "bundle": {}}
    ceiling = (contract.get("intake") or {}).get("autonomy_ceiling") or "A2"
    want = spawn.get("autonomy_max") or agent.get("autonomy_max") or "A1"
    wi = A_LEVELS.index(want) if want in A_LEVELS else 1
    ci = A_LEVELS.index(ceiling) if ceiling in A_LEVELS else 2
    if wi > ci:  # ADR-013: автономия spawn выше конверта контракта — блок
        await trigger_store.record_fire(agent["id"], tid, trig.get("type"), None, "skipped",
                                        f"автономия spawn {want} > потолок {ceiling}")
        return {"status": "skipped", "note": f"autonomy {want} > ceiling {ceiling}"}
    if spawn.get("hitl"):  # регулируемое создание: ждёт подтверждения человека, прогон НЕ запускаем
        await trigger_store.record_fire(agent["id"], tid, trig.get("type"), None, "pending_hitl", reason)
        return {"status": "pending_hitl", "note": "ожидает подтверждения создания (HITL)"}
    out = await run_executor(agent, contract, "trigger:" + (trig.get("type") or "?"), trigger=trig_node)
    rid = out["saved"]["id"]
    await trigger_store.record_fire(agent["id"], tid, trig.get("type"), rid, "fired", reason)
    return {"status": "fired", "run_id": rid, "note": reason}


async def fire_manual(agent_id: str, trigger_id: str, run_executor) -> dict:
    """Ручной запуск триггера (кнопка/тест). Игнорирует enabled/min-interval, чтит spawn/HITL."""
    agent = await agent_store.get(agent_id)
    if not agent:
        return {"status": "error", "note": "нет агента"}
    tn = next((t for t in triggers_of(agent) if t["id"] == trigger_id), None)
    if not tn:
        return {"status": "error", "note": "нет триггера в графе"}
    return await fire(agent, tn, "ручной запуск", run_executor)


async def _latest_versions() -> list[dict]:
    """Только ПОСЛЕДНЯЯ версия каждого агента (по contract_audit_id). Триггеры живут в графе, а
    add/del триггера плодит версии — старые версии НЕ должны фаерить и НЕ должны показывать расписания
    (иначе дубли писем и «удаление не работает»: расписание всплывает из старой версии)."""
    latest: dict = {}
    for a in await agent_store.list_for(None):
        aid = a.get("contract_audit_id") or a.get("id")
        cur = latest.get(aid)
        if not cur or (a.get("version") or 0) > (cur.get("version") or 0):
            latest[aid] = a
    return list(latest.values())


async def list_triggers() -> list[dict]:
    """Триггеры по ПОСЛЕДНИМ версиям агентов + последний фаер (для наблюдаемости/UI)."""
    out = []
    for a in await _latest_versions():
        full = await agent_store.get(a["id"])
        if not full:
            continue
        for tn in triggers_of(full):
            trig = tn.get("trig") or {}
            last = await trigger_store.last_fire_at(full["id"], tn["id"])
            out.append({"agent_id": full["id"], "agent": full.get("name"), "trigger_id": tn["id"],
                        "title": tn.get("title"), "type": trig.get("type"), "enabled": bool(trig.get("enabled")),
                        "cron": trig.get("cron"), "source": trig.get("source"),
                        "spawn": trig.get("spawn") or {}, "last_fire": last.isoformat() if last else None})
    return out


async def _tick(run_executor) -> None:
    fired = 0
    for a in await _latest_versions():   # только последние версии — старые не фаерят (нет дублей)
        if fired >= MAX_FIRES_PER_TICK:
            break
        full = await agent_store.get(a["id"])
        if not full:
            continue
        for tn in triggers_of(full):
            if fired >= MAX_FIRES_PER_TICK:
                break
            trig = tn.get("trig") or {}
            if not trig.get("enabled"):
                continue  # фаерим только явно включённые триггеры (безопасность на общем стенде)
            last_dt = _parse(await trigger_store.last_fire_at(full["id"], tn["id"]))
            if last_dt and (_now() - last_dt).total_seconds() < MIN_INTERVAL_SEC:
                continue
            ttype = trig.get("type")
            if ttype == "schedule" and cron_due(trig.get("cron"), _now(), last_dt):
                await fire(full, tn, f"расписание {trig.get('cron')}", run_executor)
                fired += 1
            elif ttype == "event":
                sysid = trig.get("source")
                system = await systems_store.get(sysid) if sysid else None
                if not system:
                    continue
                # ABAC: агент вправе слушать эту систему? (семья агента ∈ scope системы) — иначе отказ+аудит
                key = access.scope_key(family=full.get("family"))
                ok, reason = access.can_reach_system(key, system)
                if not ok:
                    await trigger_store.record_fire(full["id"], tn["id"], "event", None, "skipped",
                                                    f"нет доступа к «{sysid}»: {reason}")
                    await access.audit_denial("agent:" + full["id"], key, sysid, "event", reason)
                    continue
                cnt = await _poll_count(system, trig.get("path") or _DEFAULT_PATH.get(sysid, ""))
                if cnt is None:
                    continue
                ckey = f"{full['id']}:{tn['id']}:{sysid}"
                prev = await trigger_store.get_cursor(ckey)
                await trigger_store.set_cursor(ckey, str(cnt))
                if prev is not None and cnt > int(prev):
                    await fire(full, tn, f"новых в {sysid}: {cnt - int(prev)}", run_executor)
                    fired += 1


_LEADER_LOCK_KEY = 0x41424f50  # 'ABOP' — advisory-lock планировщика (фаерит только реплика-лидер)


async def _try_become_leader():
    """Захватить session-level advisory-lock (эта реплика = лидер планировщика). Держим на выделенном
    коннекте; при его падении Postgres освобождает lock → другая реплика перехватывает лидерство.
    Возвращает коннект-держатель или None (не лидер / нет PG)."""
    from .config import settings
    if not settings.pg_dsn:
        return None
    try:
        import psycopg
        conn = await psycopg.AsyncConnection.connect(settings.pg_dsn, autocommit=True)
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (_LEADER_LOCK_KEY,))
        row = await cur.fetchone()
        if row and row[0]:
            from . import observability as obs
            obs.log_event("info", "scheduler.leader_acquired")
            return conn
        await conn.close()
    except Exception:  # noqa: BLE001 — не смогли захватить → не лидер, попробуем позже
        return None
    return None


async def scheduler_loop(run_executor) -> None:
    """Фоновый планировщик (раз в TICK). Kill-switch ABOP_SCHEDULER=0. Leader-election через
    Postgres advisory-lock: тикает ТОЛЬКО реплика-лидер → нет двойного запуска триггеров на 2+ репликах."""
    if not SCHEDULER_ON:
        return
    from .config import settings
    single = not settings.pg_dsn  # без PG — одна реплика, всегда лидер
    leader = None                 # коннект-держатель advisory-lock (эта реплика — лидер)
    while True:
        try:
            if not single:
                if leader is not None and getattr(leader, "closed", False):
                    leader = None  # потеряли коннект → потеряли лидерство
                if leader is None:
                    leader = await _try_become_leader()  # пробуем перехватить лидерство
            if single or leader is not None:
                await _tick(run_executor)
        except Exception:  # noqa: BLE001 — планировщик не должен падать
            pass
        await asyncio.sleep(TICK_SEC)
