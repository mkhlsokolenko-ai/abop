#!/usr/bin/env python3
"""Проверка связки ABOP: оркестратор → агент-эксперт → тенант знаний (sLAVA) → цитата.

Топология (пре-бокс):
  - LLM оркестратора/агента: routerai (qwen3-30b-a3b-instruct-2507) — контролируемый egress.
  - Знания: sLAVA на сервере-1 (Qdrant + BGE-M3 embed + reranker В ПЕРИМЕТРЕ), коллекция = семья.
  - На боксе позже: --slava-url → локальный sLAVA (ONNX/CPU), --base-url → локальный vLLM. Код тот же.

Цепочка на каждый вопрос:
  1) оркестратор (LLM) route_family → должна выбрать семью (finance)
  2) ретрив: sLAVA /api/v1/query по коллекции семьи → sources[] (чанки+цитаты) + fallback
  3) агент (LLM) по sources формирует ответ С цитатой ИЛИ 'Недостаточно данных' если пусто/fallback
  4) ассерты: ответимый Q → есть цитата и верный факт; ловушка (нет в корпусе) → fallback И агент не выдумал

Запуск:
  python verify_chain.py --slava-url http://127.0.0.1:8080 --collection fam_finance \
    --tenant demo --model qwen/qwen3-30b-a3b-instruct-2507 \
    --api-key "$(cat .orkey)" --llm-url https://routerai.ru/api/v1 \
    --ingest-dir ../data/finance --cases cases_finance.jsonl --out verify_finance.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FALLBACK = "недостаточно данных"


def slava_ensure_collection(cli, url, name, tenant, profile):
    try:
        r = cli.get(url + "/api/v1/collections", timeout=30)
        cols = r.json().get("collections", []) if r.status_code == 200 else []
        if name in cols:
            return "exists"
    except Exception:
        pass
    r = cli.post(url + "/api/v1/collections",
                 json={"name": name, "tenant_id": tenant, "profile": profile}, timeout=60)
    return f"create {r.status_code}"


def slava_ingest(cli, url, name, tenant, path):
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f)}
        data = {"collection": name, "replace": "true"}
        r = cli.post(url + "/api/v1/ingest", files=files, data=data,
                     headers={"X-Tenant-Id": tenant}, timeout=600)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200])


def slava_query(cli, url, name, tenant, q, top_k=5):
    # retry: двойной ssh-джамп изредка рвёт соединение (RemoteProtocolError) на длинных запросах
    last = None
    for attempt in range(4):
        try:
            r = cli.post(url + "/api/v1/query", json={"query": q, "collection": name, "top_k": top_k},
                         headers={"X-Tenant-Id": tenant}, timeout=120)
            r.raise_for_status()
            return r.json()  # {answer, sources[], confidence_heuristic, fallback}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def llm(cli, llm_url, model, key, system, prompt, max_tokens=500, temperature=0.1):
    r = cli.post(llm_url.rstrip("/") + "/chat/completions",
                 headers={"Authorization": "Bearer " + key},
                 json={"model": model, "temperature": temperature, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                 timeout=120)
    r.raise_for_status()
    return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


ROUTE_SYS = ("Определи семью-исполнителя по описанию:\n"
             "- analytics — анализ данных, метрики, процессы\n"
             "- finance — финансы, бухучёт, налоги, отчётность, экономика, реквизиты учётных документов\n"
             "- architecture — ИТ/системная архитектура\n"
             "- management — управление, проекты\n"
             "- research — исследования, обзоры\n"
             "- engineering — программирование, интеграции\n"
             "- critic — критика, риски\n"
             "- decisions — выбор, сравнение вариантов\n"
             "Ответь РОВНО одним словом-меткой семьи, без пояснений.")
AGENT_SYS = ("Ты финансовый агент ABOP. Отвечай ТОЛЬКО по КОНТЕКСТу-источникам ниже. "
             "Каждый факт/число сопровождай ссылкой на источник [n]. "
             "Если ответа в источниках нет — ответь РОВНО: 'Недостаточно данных в базе знаний'. Не выдумывай.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slava-url", required=True)
    ap.add_argument("--collection", default="fam_finance")
    ap.add_argument("--tenant", default="demo")
    ap.add_argument("--profile", default="reglament_ru")
    ap.add_argument("--llm-url", default="https://routerai.ru/api/v1")
    ap.add_argument("--model", default="qwen/qwen3-30b-a3b-instruct-2507")
    ap.add_argument("--api-key", default=os.environ.get("BENCH_API_KEY", ""))
    ap.add_argument("--ingest-dir", default="")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--expect-family", default="finance")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cli = httpx.Client()
    print(f"sLAVA: {args.slava_url} · коллекция={args.collection} · tenant={args.tenant}")
    print("ensure collection:", slava_ensure_collection(cli, args.slava_url, args.collection, args.tenant, args.profile))

    if args.ingest_dir and os.path.isdir(args.ingest_dir):
        docs = [os.path.join(args.ingest_dir, f) for f in sorted(os.listdir(args.ingest_dir))
                if f.lower().endswith((".pdf", ".txt", ".md", ".docx"))]
        for d in docs:
            code, info = slava_ingest(cli, args.slava_url, args.collection, args.tenant, d)
            chunks = info.get("chunks") if isinstance(info, dict) else "?"
            print(f"  ingest {os.path.basename(d)}: {code} · chunks={chunks}")

    cases = [json.loads(x) for x in open(args.cases, encoding="utf-8") if x.strip()]
    rows, passed = [], 0
    for c in cases:
        q, kind = c["q"], c.get("kind", "answerable")  # answerable | trap
        # 1) маршрут
        fam = llm(cli, args.llm_url, args.model, args.api_key, ROUTE_SYS, q, max_tokens=8).strip().lower().split()[0].strip(".»«\"'")
        route_ok = fam == args.expect_family
        # 2) ретрив из тенанта семьи
        t0 = time.time()
        resp = slava_query(cli, args.slava_url, args.collection, args.tenant, q)
        dt = round(time.time() - t0, 2)
        srcs = resp.get("sources", []) or []
        ctx = "\n".join(f"[{i+1}] {(s.get('text') or s.get('snippet') or str(s))[:400]}" for i, s in enumerate(srcs))
        slava_fallback = bool(resp.get("fallback"))
        # 3) агент цитирует
        ans = llm(cli, args.llm_url, args.model, args.api_key, AGENT_SYS,
                  f"ИСТОЧНИКИ:\n{ctx or '(источников нет)'}\n\nВОПРОС: {q}\nОТВЕТ:")
        low = ans.lower()
        # 4) ассерты
        if kind == "trap":
            ok = (FALLBACK in low) and not any(t.lower() in low for t in c.get("trap_must_not_contain", []))
            note = "fallback ок" if ok else ("ВЫДУМКА" if FALLBACK not in low else "протечка ловушки")
        else:
            has_cite = "[" in ans and "]" in ans
            fact_ok = all(m.lower() in low for m in c.get("must_contain", []))
            ok = has_cite and fact_ok and (FALLBACK not in low) and len(srcs) > 0
            note = f"cite={has_cite} факт={fact_ok} src={len(srcs)}"
        passed += ok
        rows.append({"q": q[:60], "kind": kind, "route_ok": route_ok, "slava_fallback": slava_fallback,
                     "n_src": len(srcs), "pass": ok, "note": note, "lat_s": dt})
        print(f"[{'PASS' if ok else 'FAIL'}] {kind:10s} route→{fam}({'✓' if route_ok else '✗'}) src={len(srcs)} {note} · {q[:50]}")

    n = len(cases)
    route_acc = round(100 * sum(r["route_ok"] for r in rows) / n, 1) if n else 0
    rate = round(100 * passed / n, 1) if n else 0
    print("\n" + "=" * 56)
    # Гейт связки = цепочка (ретрив+цитата+анти-галлюцинация). Маршрут — отдельная метрика.
    verdict = passed == n
    print(f"ГЕЙТ СВЯЗКИ (ретрив+цитата+анти-галл): {passed}/{n} = {rate}%  {'✅' if verdict else '❌'}")
    print(f"метрика маршрутизации в семью: {route_acc}%")
    print(f"ВЕРДИКТ: {'✅ СВЯЗКА РАБОТАЕТ end-to-end' if verdict else '❌ есть разрывы — см. FAIL'}")
    print("=" * 56)
    if args.out:
        json.dump({"route_acc": route_acc, "chain_rate": rate, "rows": rows, "verdict": verdict},
                  open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
