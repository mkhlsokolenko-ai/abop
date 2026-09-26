#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Нагрузочный тест очереди ABOP (Блок 3): N пользователей × M прогонов одного агента.

Пользователи создаются в Keycloak (realm abop) через admin API (KC_ADMIN_USER/KC_ADMIN_PASS),
логинятся через /api/auth/login, ставят прогоны в очередь (202) и поллят /api/runs/jobs/{id}.
Метрики: ожидание в очереди (created→started), исполнение (started→finished), p50/p95, ошибки,
RSS и глубина очереди по /api/runs/queue в течение теста, стоимость LLM по /api/runs/{id}.

  python bench/load_test.py --base http://127.0.0.1:8091 --users 20 --runs 3 --agent authored.v6 --out bench/load_YYYY.json
Запускать с сервера-1 (Keycloak 127.0.0.1:8811 доступен только локально).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import statistics
import sys
import time

import httpx

KC = os.getenv("KEYCLOAK_ADMIN_BASE") or "http://127.0.0.1:8811"
REALM = "abop"
PW = "Load1234!"


def _iso(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def kc_admin_token(c: httpx.AsyncClient) -> str:
    r = await c.post(KC + "/realms/master/protocol/openid-connect/token",
                     data={"client_id": "admin-cli", "grant_type": "password",
                           "username": os.getenv("KC_ADMIN_USER", "admin"), "password": os.environ["KC_ADMIN_PASS"]})
    r.raise_for_status()
    return r.json()["access_token"]


async def ensure_users(c: httpx.AsyncClient, n: int, department: str) -> list[str]:
    at = await kc_admin_token(c)
    h = {"Authorization": "Bearer " + at}
    roles = {x["name"]: x for x in (await c.get(KC + f"/admin/realms/{REALM}/roles", headers=h)).json()}
    names = []
    for i in range(1, n + 1):
        u = f"load{i:02d}"
        names.append(u)
        r = await c.get(KC + f"/admin/realms/{REALM}/users?username={u}&exact=true", headers=h)
        if r.json():
            continue
        body = {"username": u, "enabled": True, "attributes": {"department": [department]},
                "credentials": [{"type": "password", "value": PW, "temporary": False}]}
        r = await c.post(KC + f"/admin/realms/{REALM}/users", headers=h, json=body)
        r.raise_for_status()
        uid = (await c.get(KC + f"/admin/realms/{REALM}/users?username={u}&exact=true", headers=h)).json()[0]["id"]
        if "analyst" in roles:
            await c.post(KC + f"/admin/realms/{REALM}/users/{uid}/role-mappings/realm", headers=h, json=[roles["analyst"]])
    return names


async def delete_users(c: httpx.AsyncClient, names: list[str]) -> int:
    at = await kc_admin_token(c)
    h = {"Authorization": "Bearer " + at}
    n = 0
    for u in names:
        r = await c.get(KC + f"/admin/realms/{REALM}/users?username={u}&exact=true", headers=h)
        for x in r.json():
            await c.delete(KC + f"/admin/realms/{REALM}/users/{x['id']}", headers=h)
            n += 1
    return n


async def _retry(fn, tries: int = 5):
    last = None
    for k in range(tries):
        try:
            return await fn()
        except (httpx.TransportError, httpx.HTTPStatusError) as ex:  # сеть/5xx под нагрузкой — повтор с паузой
            last = ex
            await asyncio.sleep(1.5 * (k + 1))
    raise last


async def login(c: httpx.AsyncClient, base: str, u: str) -> str:
    async def go():
        r = await c.post(base + "/api/auth/login", json={"username": u, "password": PW})
        r.raise_for_status()
        return r.json()["access_token"]
    return await _retry(go)


async def one_user(c: httpx.AsyncClient, base: str, u: str, agent: str, runs: int, ctx: str, out: list, timeout: int):
    tok = await login(c, base, u)
    h = {"Authorization": "Bearer " + tok}
    jobs = []
    for k in range(runs):
        t0 = time.perf_counter()
        r = await _retry(lambda: c.post(base + "/api/runs?async=1", headers=h,
                                        json={"agent_id": agent, "context": f"{ctx} · {u} · #{k+1} · {time.time():.0f}", "async": True}))
        rec = {"user": u, "n": k + 1, "submit_ms": round((time.perf_counter() - t0) * 1000, 1), "http": r.status_code}
        if r.status_code == 202:
            rec["job_id"] = r.json()["job_id"]; rec["position"] = r.json().get("position")
        else:
            rec["error"] = r.text[:200]
        jobs.append(rec)
    deadline = time.time() + timeout
    pending = {j["job_id"] for j in jobs if j.get("job_id")}
    while pending and time.time() < deadline:
        await asyncio.sleep(5)
        for j in jobs:
            jid = j.get("job_id")
            if not jid or jid not in pending:
                continue
            try:
                r = await c.get(base + f"/api/runs/jobs/{jid}", headers=h)
            except httpx.TransportError:
                continue
            if r.status_code != 200:
                continue
            d = r.json()
            if d["status"] in ("done", "failed", "cancelled"):
                pending.discard(jid)
                j.update(status=d["status"], run_id=d.get("run_id"), error=d.get("error"),
                         created_at=d.get("created_at"), started_at=d.get("started_at"), finished_at=d.get("finished_at"))
                ca, sa, fa = _iso(d.get("created_at")), _iso(d.get("started_at")), _iso(d.get("finished_at"))
                if ca and sa:
                    j["wait_s"] = round((sa - ca).total_seconds(), 1)
                if sa and fa:
                    j["exec_s"] = round((fa - sa).total_seconds(), 1)
                if d.get("run_id"):
                    rr = await c.get(base + f"/api/runs/{d['run_id']}", headers=h)
                    if rr.status_code == 200:
                        j["rub"] = ((rr.json().get("run_metrics") or {}).get("cost") or {}).get("rub")
    for j in jobs:
        if j.get("job_id") and "status" not in j:
            j["status"] = "timeout"
    out.extend(jobs)


async def sampler(c: httpx.AsyncClient, base: str, admin_tok: str, samples: list, stop: asyncio.Event):
    h = {"Authorization": "Bearer " + admin_tok}
    while not stop.is_set():
        try:
            r = await c.get(base + "/api/runs/queue", headers=h)
            if r.status_code == 200:
                d = r.json()
                samples.append({"t": time.time(), "rss_mb": d.get("rss_mb"), "by_status": d.get("by_status"),
                                "pressure": d.get("memory_pressure")})
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(5)


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, round((p / 100) * (len(xs) - 1))))
    return xs[k]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8091")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--agent", default="authored.v6")
    ap.add_argument("--department", default="management")
    ap.add_argument("--context", default="Нагрузочный тест очереди: коротко составь план дня из 3 пунктов")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--admin-user", default="admin.abop")
    ap.add_argument("--admin-pass", default="Demo1234!")
    ap.add_argument("--keep-users", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=16, max_keepalive_connections=8)) as c:
        users = await ensure_users(c, a.users, a.department)
        print(f"users ready: {len(users)}")
        r = await c.post(a.base + "/api/auth/login", json={"username": a.admin_user, "password": a.admin_pass})
        admin_tok = r.json()["access_token"]
        q0 = (await c.get(a.base + "/api/runs/queue", headers={"Authorization": "Bearer " + admin_tok})).json()
        samples: list = []
        stop = asyncio.Event()
        st = asyncio.create_task(sampler(c, a.base, admin_tok, samples, stop))
        out: list = []
        t0 = time.time()
        await asyncio.gather(*[one_user(c, a.base, u, a.agent, a.runs, a.context, out, a.timeout) for u in users])
        total_s = round(time.time() - t0, 1)
        stop.set(); await st
        if not a.keep_users:
            print("users deleted:", await delete_users(c, users))
    waits = [j["wait_s"] for j in out if j.get("wait_s") is not None]
    execs = [j["exec_s"] for j in out if j.get("exec_s") is not None]
    rubs = [j["rub"] for j in out if isinstance(j.get("rub"), (int, float))]
    rss = [s["rss_mb"] for s in samples if s.get("rss_mb")]
    summary = {
        "when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "base": a.base, "agent": a.agent,
        "users": a.users, "runs_per_user": a.runs, "submitted": len(out),
        "accepted_202": sum(1 for j in out if j.get("http") == 202),
        "done": sum(1 for j in out if j.get("status") == "done"), "failed": sum(1 for j in out if j.get("status") == "failed"),
        "timeout": sum(1 for j in out if j.get("status") == "timeout"),
        "wall_s": total_s, "throughput_runs_per_min": round(60 * sum(1 for j in out if j.get("status") == "done") / max(1, total_s), 2),
        "wait_s": {"p50": pct(waits, 50), "p95": pct(waits, 95), "max": max(waits) if waits else None},
        "exec_s": {"p50": pct(execs, 50), "p95": pct(execs, 95), "max": max(execs) if execs else None},
        "submit_ms": {"p50": pct([j["submit_ms"] for j in out], 50), "p95": pct([j["submit_ms"] for j in out], 95)},
        "cost_rub": {"total": round(sum(rubs), 2), "per_run": round(statistics.mean(rubs), 3) if rubs else None},
        "rss_mb": {"start": q0.get("rss_mb"), "max": max(rss) if rss else None, "end": rss[-1] if rss else None,
                   "soft_limit": q0.get("mem_soft_mb"), "pressure_samples": sum(1 for s in samples if s.get("pressure"))},
        "workers": q0.get("workers"), "user_concurrent": q0.get("user_concurrent"),
        "errors": [j.get("error") for j in out if j.get("error")][:5],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "jobs": out, "samples": samples}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
