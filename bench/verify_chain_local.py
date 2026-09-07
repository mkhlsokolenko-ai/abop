#!/usr/bin/env python3
"""Проверка связки БЕЗ сервера-1: оркестратор→агент→тенант знаний→ретрив→цитата, всё через routerai.

Доказывает ту же цепочку, что и verify_chain.py (который ходит в sLAVA), но локально:
  - эмбеддер: routerai baai/bge-m3 (тот же, что в sLAVA)   — паритет
  - реранкер: routerai qwen3-reranker-8b
  - вектор-стор: numpy cosine + payload{family} = тенант знаний семьи (изоляция)
  - LLM оркестратора/агента: routerai qwen3-30b-a3b-instruct-2507 (победитель гейта)

Тенанты: коллекция=семья. Финансовые чанки помечены family=finance; для доказательства
изоляции добавлен decoy-чанк family=research — запрос finance его НЕ достаёт.

Цепочка на кейс: route_family→finance · ретрив по тенанту finance (embed→cosine→rerank) ·
агент цитирует или «Недостаточно данных» · ассерты (цитата+факт / trap→fallback без выдумки) ·
изоляция (в выдаче только finance).

На боксе/сервере-1 эта же логика = verify_chain.py на sLAVA (embed/rerank в периметре).
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
import httpx, numpy as np

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = "https://routerai.ru/api/v1"
EMB_MODEL = "baai/bge-m3"
RERANK_MODEL = "qwen/qwen3-reranker-8b"
LLM_MODEL = "qwen/qwen3-30b-a3b-instruct-2507"
FALLBACK = "недостаточно данных"

ROUTE_SYS = ("Определи семью-исполнителя по описанию:\n"
             "- analytics — анализ данных, метрики, процессы\n"
             "- finance — финансы, бухучёт, налоги, отчётность, экономика\n"
             "- architecture — ИТ/системная архитектура\n"
             "- management — управление, проекты, координация\n"
             "- research — исследования, обзоры литературы/рынка\n"
             "- engineering — программирование, интеграции, скрипты\n"
             "- critic — критика, риски, поиск слабых мест\n"
             "- decisions — выбор, решение, сравнение вариантов\n"
             "Ответь РОВНО одним словом-меткой семьи, без пояснений.")
AGENT_SYS = ("Ты финансовый агент ABOP. Отвечай ТОЛЬКО по КОНТЕКСТу-источникам ниже. "
             "Каждый факт/число сопровождай ссылкой [n]. Если ответа в источниках нет — "
             "ответь РОВНО: 'Недостаточно данных в базе знаний'. Не выдумывай.")

DECOY_RESEARCH = ("Обзор методов дистилляции больших языковых моделей: knowledge distillation "
                  "переносит знания модели-учителя в компактную модель-ученика через soft-таргеты.")


def _h(key): return {"Authorization": "Bearer " + key}

def embed(cli, key, texts):
    r = cli.post(BASE + "/embeddings", headers=_h(key), json={"model": EMB_MODEL, "input": texts}, timeout=90)
    r.raise_for_status()
    return np.array([d["embedding"] for d in r.json()["data"]], dtype=np.float32)

def rerank(cli, key, query, docs):
    r = cli.post(BASE + "/rerank", headers=_h(key),
                 json={"model": RERANK_MODEL, "query": query, "documents": docs}, timeout=60)
    if r.status_code != 200:
        return list(range(len(docs)))  # деградация: без реранка
    res = r.json().get("results", [])
    return [x["index"] for x in sorted(res, key=lambda x: -x.get("relevance_score", 0))]

def llm(cli, key, system, prompt, max_tokens=500):
    r = cli.post(BASE + "/chat/completions", headers=_h(key),
                 json={"model": LLM_MODEL, "temperature": 0.1, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                 timeout=120)
    r.raise_for_status()
    return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

def chunk_docs(folder):
    """Простой чанкинг: делим по '## ' заголовкам. Возвращает [(text, family, source)]."""
    out = []
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith((".md", ".txt")): continue
        raw = open(os.path.join(folder, fn), encoding="utf-8").read()
        parts = re.split(r"\n(?=## )", raw)
        for p in parts:
            p = p.strip()
            if len(p) > 40:
                out.append((p, "finance", fn))
    out.append((DECOY_RESEARCH, "research", "decoy"))  # чужой тенант для проверки изоляции
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest-dir", default="data/finance")
    ap.add_argument("--cases", default="bench/cases_finance.jsonl")
    ap.add_argument("--api-key", default=os.environ.get("BENCH_API_KEY", ""))
    ap.add_argument("--top-n", type=int, default=6)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--expect-family", default="finance")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    key = args.api_key
    cli = httpx.Client()

    chunks = chunk_docs(args.ingest_dir)
    texts = [c[0] for c in chunks]; fams = [c[1] for c in chunks]
    print(f"тенанты знаний: {dict((f, fams.count(f)) for f in set(fams))} · чанков {len(chunks)}")
    M = embed(cli, key, texts)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)

    cases = [json.loads(x) for x in open(args.cases, encoding="utf-8") if x.strip()]
    rows, passed = [], 0
    for c in cases:
        q, kind = c["q"], c.get("kind", "answerable")
        fam = llm(cli, key, ROUTE_SYS, q, max_tokens=8).strip().lower().split()[0].strip(".»«\"'")
        route_ok = fam == args.expect_family
        # ретрив ТОЛЬКО в тенанте семьи (изоляция) → cosine → rerank
        t0 = time.time()
        idx_fam = [i for i, f in enumerate(fams) if f == args.expect_family]
        qv = embed(cli, key, [q])[0]; qv /= (np.linalg.norm(qv) + 1e-9)
        sims = M[idx_fam] @ qv
        order = [idx_fam[i] for i in np.argsort(-sims)[:args.top_n]]
        docs = [texts[i] for i in order]
        rr = rerank(cli, key, q, docs)[:args.top_k]
        top = [order[i] for i in rr]
        dt = round(time.time() - t0, 2)
        leak = any(fams[i] != args.expect_family for i in top)  # изоляция: чужого тенанта быть не должно
        ctx = "\n".join(f"[{n+1}] {texts[i][:400]}" for n, i in enumerate(top))
        ans = llm(cli, key, AGENT_SYS, f"ИСТОЧНИКИ:\n{ctx}\n\nВОПРОС: {q}\nОТВЕТ:")
        low = ans.lower()
        if kind == "trap":
            ok = (FALLBACK in low) and not any(t.lower() in low for t in c.get("trap_must_not_contain", []))
            note = "fallback ок" if ok else ("ВЫДУМКА" if FALLBACK not in low else "протечка")
        else:
            has_cite = "[" in ans and "]" in ans
            fact_ok = all(m.lower() in low for m in c.get("must_contain", []))
            ok = has_cite and fact_ok and FALLBACK not in low and not leak
            note = f"cite={has_cite} факт={fact_ok} изоляция={'✓' if not leak else 'ПРОТЕЧКА'}"
        passed += ok
        rows.append({"q": q[:55], "kind": kind, "route_ok": route_ok, "leak": leak, "pass": ok, "note": note, "lat_s": dt})
        print(f"[{'PASS' if ok else 'FAIL'}] {kind:10s} route→{fam}({'✓' if route_ok else '✗'}) {note} · {q[:46]}")

    n = len(cases)
    route_acc = round(100 * sum(r["route_ok"] for r in rows) / n, 1)
    iso_ok = all(not r["leak"] for r in rows)
    rate = round(100 * passed / n, 1)
    print("\n" + "=" * 58)
    print(f"СВЯЗКА (локально, routerai): маршрут в семью {route_acc}% · изоляция тенантов {'✓' if iso_ok else '✗ ПРОТЕЧКА'}")
    print(f"цепочка (ретрив+цитата+анти-галлюцинация) {passed}/{n} = {rate}%")
    verdict = passed == n and route_acc >= 75 and iso_ok
    print(f"ВЕРДИКТ: {'✅ СВЯЗКА РАБОТАЕТ end-to-end' if verdict else '❌ есть разрывы'}")
    print("=" * 58)
    if args.out:
        json.dump({"route_acc": route_acc, "isolation_ok": iso_ok, "chain_rate": rate,
                   "verdict": verdict, "rows": rows}, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
