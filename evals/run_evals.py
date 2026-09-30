"""Регрессия ABOP на эталонных запросах: что среда делает с фразами, какими их пишут люди.

Проверяем не текст ответа — он у модели каждый раз свой, — а проверяемые инварианты: кто подобран,
тот ли предмет работы, покрыт ли вход навыка, какие поля результата обязаны быть заполнены. Плюс цена
и время, потому что лишний вопрос это трение, а работа не по тому проекту — потеря доверия.

Два режима:
    python evals/run_evals.py                      # без модели: подбор, предмет, покрытие входов
    python evals/run_evals.py --live               # ещё и прогоны: поля результата, цена, время

Адрес стенда: --base http://127.0.0.1:8099 (по умолчанию). Отчёт: evals/last_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "cases.json")
REPORT = os.path.join(HERE, "last_report.json")


def call(base: str, path: str, body=None, method="GET", timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def safe(base: str, path: str, body=None, method="GET", timeout=900):
    try:
        return call(base, path, body, method, timeout)
    except Exception as e:  # noqa: BLE001 — недоступный эндпоинт это результат проверки, а не сбой прогона
        return {"_error": f"{type(e).__name__}: {str(e)[:160]}"}


# ── Проверки ────────────────────────────────────────────────────────────────────────────────

def check_match(base: str, case: dict, agents: dict) -> dict:
    """Кого среда выбрала на фразу и насколько уверенно.

    Если на стенде нет ни одного агента с ожидаемым навыком, подбор проверять не на чем: это состояние
    стенда, а не качество подбора. Такой случай помечаем отдельно, чтобы он не портил метрику молча.
    """
    exp = case["ожидается"]
    want_skill = exp.get("навык")
    if want_skill and want_skill not in agents:
        return {"ok": True, "skip": True,
                "note": f"на стенде нет агента с навыком «{want_skill}» — подбор проверять не на чем"}
    r = safe(base, "/api/agents/match", {"q": case["фраза"]}, "POST", timeout=120)
    if "_error" in r:
        return {"ok": False, "note": "подбор недоступен: " + r["_error"]}
    # Правило достаточности: на короткую фразу среда обязана доспросить, а не подобрать исполнителя.
    if exp.get("просит_уточнить"):
        su = r.get("sufficiency") or {}
        asked = bool(r.get("need_more")) and bool(su.get("вопросы"))
        return {"ok": asked or not (r.get("matches") or []),
                "note": ("задан вопрос: " + su["вопросы"][0][:60]) if asked else
                        ("подобран агент там, где описания не хватает" if r.get("matches") else "исполнителя нет")}
    items = r.get("matches") or r.get("agents") or []
    top = items[0] if items else {}
    score = float(top.get("score") or 0)
    fam = str(top.get("family") or "")
    want_fam = exp.get("семья")
    weak = score < 0.35
    if exp.get("низкая_уверенность"):
        # тут правильным ответом является ЧЕСТНОЕ «не уверен», а не попадание в семью
        return {"ok": bool(weak or not items), "score": score, "family": fam,
                "note": "низкая уверенность показана" if weak or not items else "выбран агент там, где уверенности нет"}
    if not items:
        return {"ok": False, "score": 0, "note": "агент не подобран"}
    return {"ok": (not want_fam) or fam == want_fam, "score": score, "family": fam,
            "agent": top.get("name") or top.get("id"),
            "note": "" if (not want_fam or fam == want_fam) else f"ожидалась семья «{want_fam}», выбрана «{fam}»"}


def check_slot(base: str, case: dict) -> dict:
    """Предмет работы: объявлен ли слот у навыка и разрешается ли названное значение."""
    exp = case["ожидается"]
    want = exp.get("предмет")
    skill = exp.get("навык")
    if not want:
        return {"ok": True, "note": "предмет не требуется"}
    if not skill:
        return {"ok": True, "note": "навык не задан — проверять нечего"}
    sl = safe(base, f"/api/skills/{urllib.parse.quote(skill)}/slots")
    slots = (sl or {}).get("slots") or []
    if not slots:
        return {"ok": False, "note": f"у навыка «{skill}» не объявлен предмет работы"}
    slot = slots[0]
    val = want.get("значение")
    if val in (None, "*"):
        return {"ok": True, "slot": slot.get("name"), "note": "предмет запрашивается у человека"}
    res = safe(base, f"/api/resolve/{urllib.parse.quote(slot.get('entity') or '')}?q={urllib.parse.quote(str(val))}")
    cands = (res or {}).get("candidates") or []
    hit = any(str((c.get("record") or {}).get("id") or "") == str(val) for c in cands)
    return {"ok": hit, "slot": slot.get("name"), "candidates": len(cands),
            "note": "" if hit else f"значение «{val}» не разрешилось в сущности «{slot.get('entity')}»"}


def check_coverage(base: str, case: dict) -> dict:
    """Покрытие входов навыка: исполним ли он без ручной правки."""
    skill = case["ожидается"].get("навык")
    if not skill:
        return {"ok": True, "note": "навык не задан"}
    c = safe(base, f"/api/skills/{urllib.parse.quote(skill)}/contract")
    if "_error" in c:
        return {"ok": False, "note": "контракт недоступен: " + c["_error"]}
    inputs = (c.get("inputs") or {}).get("required") or []
    ents = {str(i.get("entity")) for i in inputs if i.get("from") == "data" and i.get("entity")}
    if not ents:
        return {"ok": True, "note": "вход из данных не требуется"}
    have = {e["entity"]: e.get("rows") or 0 for e in (safe(base, "/api/data/entities").get("entities") or [])}
    empty = sorted(e for e in ents if not have.get(e))
    return {"ok": not empty, "entities": sorted(ents),
            "note": "" if not empty else "нет данных в сущностях: " + ", ".join(empty)}


def run_live(base: str, case: dict, agent_id: str) -> dict:
    """Настоящий прогон: поля результата, цена и время."""
    exp = case["ожидается"]
    body = {"agent_id": agent_id, "context": case["фраза"], "use_cache": False, "deliver": "chat"}
    want = (exp.get("предмет") or {}).get("значение")
    if want and want != "*":
        body["scope"] = {(exp["предмет"].get("поле") or "проект"): want}
    t0 = time.perf_counter()
    out = safe(base, "/api/runs", body, "POST", timeout=900)
    sec = round(time.perf_counter() - t0, 1)
    if "_error" in out:
        return {"ok": False, "note": "прогон не выполнен: " + out["_error"], "sec": sec}
    res = out.get("result") or out
    run_id = out.get("run_id") or (out.get("saved") or {}).get("id") or ""
    full = safe(base, "/api/runs/" + urllib.parse.quote(run_id)) if run_id else {}
    payload = (full or {}).get("payload") or full or res
    fields = set()
    for so in (payload.get("skill_outputs") or []):
        fields |= set((so.get("structured") or {}).keys())
    fields |= {k for k in payload if payload.get(k)}
    want_fields = exp.get("поля_результата") or []
    missing = [f for f in want_fields if f not in fields]
    cost = ((payload.get("run_metrics") or {}).get("cost") or {}).get("rub") or 0
    return {"ok": not missing, "sec": sec, "rub": cost, "run_id": run_id,
            "note": "" if not missing else "не заполнено: " + ", ".join(missing)}


# ── Прогон набора ───────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--live", action="store_true", help="ещё и настоящие прогоны (нужна модель)")
    ap.add_argument("--only", default="", help="один случай по id")
    a = ap.parse_args()

    with open(CASES, encoding="utf-8") as f:
        spec = json.load(f)
    cases = [c for c in spec["cases"] if not a.only or c["id"] == a.only]

    # Список агентов отдаёт карточки без графа — состав навыков берём из карточки каждого агента.
    agents = {}
    for ag in (safe(a.base, "/api/agents").get("agents") or []):
        full = safe(a.base, "/api/agents/" + urllib.parse.quote(ag["id"]))
        for n in (((full or {}).get("graph") or {}).get("nodes") or []):
            if n.get("skill"):
                agents.setdefault(n["skill"], ag["id"])

    rows, secs, rubs = [], [], []
    for c in cases:
        r = {"id": c["id"], "фраза": c["фраза"][:70]}
        r["подбор"] = check_match(a.base, c, agents)
        r["предмет"] = check_slot(a.base, c)
        r["покрытие"] = check_coverage(a.base, c)
        if a.live and c["ожидается"].get("навык") and agents.get(c["ожидается"]["навык"]):
            r["прогон"] = run_live(a.base, c, agents[c["ожидается"]["навык"]])
            if r["прогон"].get("sec"):
                secs.append(r["прогон"]["sec"])
            if r["прогон"].get("rub"):
                rubs.append(r["прогон"]["rub"])
        rows.append(r)

    def share(key):
        vals = [r[key]["ok"] for r in rows if key in r and not r[key].get("skip")]
        return round(100 * sum(vals) / len(vals)) if vals else 0

    metrics = {
        "случаев": len(rows),
        "подбор_верный_%": share("подбор"),
        "предмет_верный_%": share("предмет"),
        "исполнимость_%": share("покрытие"),
        "уточнений_среднее": round(statistics.mean(
            [(c["ожидается"].get("уточнений") or 0) for c in cases]), 2) if cases else 0,
        "время_среднее_с": round(statistics.mean(secs), 1) if secs else None,
        "стоимость_средняя_руб": round(statistics.mean(rubs), 3) if rubs else None,
    }
    if a.live:
        metrics["прогон_полный_%"] = share("прогон")

    report = {"at": time.strftime("%Y-%m-%d %H:%M"), "base": a.base, "live": a.live,
              "metrics": metrics, "cases": rows}
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print("=" * 78)
    for r in rows:
        bad = [k for k in ("подбор", "предмет", "покрытие", "прогон") if k in r and not r[k]["ok"]]
        mark = "✗" if bad else "✓"
        print(f"{mark} {r['id']:<20} {r['фраза']}")
        for k in ("подбор", "предмет", "покрытие", "прогон"):
            if k in r and r[k].get("note"):
                print(f"    {k}: {r[k]['note']}")
    print("=" * 78)
    for k, v in metrics.items():
        print(f"{k}: {v}")
    print("отчёт:", REPORT)
    # Ненулевой код возврата, если хоть одна проверка провалилась: набор пригоден для CI.
    return 1 if any(not r[k]["ok"] for r in rows for k in ("подбор", "предмет", "покрытие", "прогон") if k in r) else 0


sys.exit(main())
