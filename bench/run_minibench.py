#!/usr/bin/env python3
"""ABOP мини-бенч моделей — APE как гейт проверки MoE-кандидатов под одну 32ГБ карту.

Идея: не доверять чужим лидербордам, а проверить КАЖДОГО кандидата на том, что реально
делает движок ABOP под governance: строгий JSON-tool (ReAct), вывод/сверка DoD,
анти-галлюцинация с cite ("недостаточно данных" вместо выдумки), русский, латентность/токены.

Портируемо: любой OpenAI-совместимый /chat/completions — наш MCP-шлюз, RouteAI,
OpenRouter, локальный vLLM. Грейдеры детерминированы (совпадают с гейтами cli/ape.py),
LLM-судья опционален.

Запуск:
    python run_minibench.py minibench_ru.jsonl \
        --base-url https://openrouter.ai/api/v1 --model qwen/qwen3-30b-a3b-instruct \
        --api-key $KEY --out report_qwen.json

Сравнение кандидатов: прогнать по каждому endpoint, свести report_*.json.
Гейт: модель ПРОХОДИТ, только если все измерения ≥ порога И анти-галлюцинация (hard) чиста.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import httpx

try:  # Windows-консоль (cp1251) не тянет юникод в выводе — форсируем UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- Пороги гейта (перед арендой бокса). cite — HARD-constraint (CLAUDE.md §анти-галлюцинация). ---
GATE = {
    "cite": 0.90,       # HARD: доля корректных cite/fallback; выдумка = провал
    "json_tool": 0.90,  # строгий JSON + верный инструмент в ReAct
    "route": 0.75,      # выбор семьи/роли
    "dod": 0.70,        # структура Definition of Done + вердикт
    "ru": 0.70,         # следование инструкции на русском
}

# Опциональные тарифы (₽/Mtok) для оценки стоимости — заполнить под провайдера.
PRICE = {"in": None, "out": None}


def _json_first(text: str):
    """Первый сбалансированный JSON-объект из текста (модель может обернуть в ```). Копия cli/ape.py."""
    s = text.find("{")
    while s != -1:
        depth = 0; instr = False; esc = False
        for i in range(s, len(text)):
            c = text[i]
            if instr:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': instr = False
            else:
                if c == '"': instr = True
                elif c == "{": depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[s:i + 1])
                        except Exception:
                            break
        s = text.find("{", s + 1)
    return None


def call(client, base_url, model, api_key, system, prompt, temperature, max_tokens, timeout):
    """OpenAI-совместимый вызов. Возвращает (text, in_tok, out_tok, latency_s, error)."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
    t0 = time.time()
    try:
        r = client.post(url, headers=headers, json=payload, timeout=timeout)
        dt = time.time() - t0
        r.raise_for_status()
        d = r.json()
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        usage = d.get("usage") or {}
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), dt, None
    except Exception as e:  # noqa: BLE001
        return "", 0, 0, time.time() - t0, str(e)


def grade(case: dict, answer: str) -> tuple[bool, str]:
    """Детерминированный грейдер под измерение. Возвращает (pass, note)."""
    g = case.get("grader", "contains")
    exp = case.get("expect", {})
    low = answer.lower()

    if g == "json_tool":
        obj = _json_first(answer)
        if obj is None:
            return False, "не JSON"
        keysets = exp.get("one_of_keys", [["tool", "args"], ["final"]])
        if not any(all(k in obj for k in ks) for ks in keysets):
            return False, f"нет ключей {keysets}"
        if "expect_tool" in exp and obj.get("tool") != exp["expect_tool"]:
            return False, f"tool={obj.get('tool')}≠{exp['expect_tool']}"
        return True, "ok"

    if g == "cite_present":
        for m in exp.get("must_contain", []):
            if m.lower() not in low:
                return False, f"нет цитаты {m!r}"
        for m in exp.get("must_not_contain", []):
            if m.lower() in low:
                return False, f"лишнее {m!r}"
        return True, "ok"

    if g == "cite_absent":  # анти-галлюцинация: нет данных → fallback, не выдумывать
        fb = exp.get("fallback", "недостаточно")
        if fb.lower() not in low:
            return False, "нет fallback-фразы"
        for trap in exp.get("trap_must_not_contain", []):
            if trap.lower() in low:
                return False, f"ВЫДУМКА: {trap!r}"
        return True, "ok"

    if g == "route_label":
        got = answer.strip().lower().strip(".»«\"'")
        got = got.split()[0] if got else ""
        return got == str(exp.get("label", "")).lower(), f"→{got}"

    if g == "dod_struct":
        obj = _json_first(answer)
        if obj is None:
            return False, "не JSON"
        items = obj.get(exp.get("list_key", "dod"), [])
        if not isinstance(items, list) or len(items) < exp.get("min_items", 3):
            return False, f"DoD пунктов {len(items) if isinstance(items, list) else 0}"
        if "verdict_key" in exp and obj.get(exp["verdict_key"]) != exp.get("verdict"):
            return False, f"вердикт {obj.get(exp['verdict_key'])}≠{exp.get('verdict')}"
        return True, "ok"

    if g == "contains_any":  # принять любой из синонимов (устойчиво к перефразировке)
        opts = exp if isinstance(exp, list) else exp.get("any", [])
        hit = next((o for o in opts if o.lower() in low), None)
        return hit is not None, (f"~{hit}" if hit else "нет синонимов")

    if g == "equals":
        return low.strip() == str(exp).strip().lower(), "eq"
    # contains
    return str(exp).lower() in low, "contains"


def pct(vals):
    return round(100 * sum(vals) / len(vals), 1) if vals else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--base-url", default=os.environ.get("BENCH_BASE_URL", ""))
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default=os.environ.get("BENCH_API_KEY", ""))
    ap.add_argument("--temperature", type=float, default=0.1)  # CLAUDE.md: T≤0.1
    ap.add_argument("--max-tokens", type=int, default=700)
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--price-in", type=float, default=None, help="₽/Mtok вход")
    ap.add_argument("--price-out", type=float, default=None, help="₽/Mtok выход")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not args.base_url:
        sys.exit("нужен --base-url (OpenAI-совместимый endpoint)")
    cases = [json.loads(x) for x in open(args.dataset, encoding="utf-8") if x.strip()]
    dims: dict[str, list] = {}
    lats: list[float] = []
    tok_in = tok_out = 0
    rows = []

    with httpx.Client() as client:
        for i, c in enumerate(cases):
            text, ti, to, dt, err = call(
                client, args.base_url, args.model, args.api_key,
                c.get("system", ""), c["input"], args.temperature, args.max_tokens, args.timeout,
            )
            lats.append(dt); tok_in += ti; tok_out += to
            if err:
                ok, note = False, f"ERR {err[:60]}"
            else:
                ok, note = grade(c, text)
            dim = c.get("dim", "misc")
            dims.setdefault(dim, []).append(ok)
            rows.append({"id": c.get("id", i), "dim": dim, "pass": ok, "note": note, "lat_s": round(dt, 2)})
            print(f"[{'PASS' if ok else 'FAIL'}] {dim:10s} {str(c.get('id', i))[:34]:34s} {note}")

    per_dim = {d: pct(v) for d, v in dims.items()}
    p_in = args.price_in if args.price_in is not None else PRICE["in"]
    p_out = args.price_out if args.price_out is not None else PRICE["out"]
    est_cost = None
    if p_in is not None and p_out is not None:
        est_cost = round(tok_in / 1e6 * p_in + tok_out / 1e6 * p_out, 3)

    # Гейт: каждое измерение ≥ порога; cite — hard.
    gate_rows, passed_gate = [], True
    for d, thr in GATE.items():
        got = per_dim.get(d)
        if got is None:
            gate_rows.append((d, "—", thr, "нет кейсов")); continue
        ok = got >= thr * 100
        if not ok:
            passed_gate = False
        gate_rows.append((d, got, thr * 100, "✓" if ok else "✗ провал" + (" (HARD)" if d == "cite" else "")))

    lat_sorted = sorted(lats)
    p50 = round(statistics.median(lat_sorted), 2) if lat_sorted else 0
    p95 = round(lat_sorted[max(0, int(len(lat_sorted) * 0.95) - 1)], 2) if lat_sorted else 0

    print("\n" + "=" * 60)
    print(f"МОДЕЛЬ: {args.model}")
    print(f"Измерения: " + " · ".join(f"{d}={v}%" for d, v in per_dim.items()))
    print("Гейт (порог):")
    for d, got, thr, verdict in gate_rows:
        print(f"  {d:10s} {str(got):>6}% ≥ {thr:.0f}%  {verdict}")
    print(f"Латентность: p50={p50}s p95={p95}s · токенов in/out={tok_in}/{tok_out}"
          + (f" · ~{est_cost}₽" if est_cost is not None else ""))
    print(f"ВЕРДИКТ ГЕЙТА: {'✅ ПРОХОДИТ — можно арендовать бокс под неё' if passed_gate else '❌ НЕ ПРОХОДИТ'}")
    print("=" * 60)

    report = {
        "model": args.model, "base_url": args.base_url, "n_cases": len(cases),
        "per_dim": per_dim, "gate": {d: {"got": g, "thr": t, "ok": "✓" in v} for d, g, t, v in gate_rows},
        "passed_gate": passed_gate, "latency": {"p50": p50, "p95": p95},
        "tokens": {"in": tok_in, "out": tok_out}, "est_cost_rub": est_cost, "rows": rows,
    }
    if args.out:
        json.dump(report, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"→ {args.out}")
    sys.exit(0 if passed_gate else 1)


if __name__ == "__main__":
    main()
