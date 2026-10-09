# -*- coding: utf-8 -*-
"""Регламент: разметка текста по ключу НСИ и граф процесса из него.

Домен вынесен из монолита (ADR-052, docs/RASKLADKA_SERVERA.md). Это вход «как должно быть по
документу»: регламент режется на чанки, каждому присваивается ключ НСИ `[раздел][подраздел][пункт]`,
и из ключей собирается граф процесса. Сверка собранного процесса с этим графом живёт рядом с картой
(`routes/process.py`) — там её место, потому что отвечает она про карту, а не про текст.

`_parse_json_array` терпим к обёрткам модели (```json, преамбула, хвост): разметку делает LLM, и
строгий `json.loads` ронял бы приём на первом же вежливом «Вот результат:».

Зависимости наружу: `server/deps` и сторы; `web_api` не импортируется.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import audit_store, clients, reglament_store
from ..deps import require_level, user

from .. import findings
from ..deps import user

router = APIRouter()


def _parse_json_array(text: str) -> list:
    import json as _json
    import re as _re
    t = (text or "").strip()
    m = _re.search(r"\[.*\]", t, _re.S)
    if m:
        t = m.group(0)
    try:
        return _json.loads(t)
    except Exception:  # noqa: BLE001
        return []


@router.post("/api/reglament/ingest")
async def reglament_ingest(body: dict, u: dict = Depends(user)) -> dict:
    """Загрузить регламент и РАЗМЕТИТЬ чанки ЛЛМ (RouteAI DeepSeek v4): каждому — ключ НСИ [P][SS][OOO]
    + метки процесс/подпроцесс/операция; посчитать эмбеддинг (BGE-M3) и сохранить в Postgres.
    Тело: {text, tenant?, replace?}. §регламент-конформанс (reglament-conformance-slava)."""
    require_level(u, "manager")
    text = str((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(422, "нужен text регламента")
    tenant = str((body or {}).get("tenant") or "default").strip() or "default"
    import re as _re
    raw = [c.strip() for c in _re.split(r"\n\s*\n", text) if len(c.strip()) >= 15]
    if not raw:
        raw = [c.strip() for c in text.split("\n") if len(c.strip()) >= 15]
    raw = raw[:40]
    if not raw:
        raise HTTPException(422, "не удалось выделить фрагменты")
    prompt = (
        "Ты размечаешь регламент бизнес-процесса ключами НСИ. Для КАЖДОГО фрагмента присвой "
        "иерархический ключ вида [P][SS][OOO] (P=номер процесса 1..9, SS=подпроцесс 01..99, "
        "OOO=операция 001..999; связанные фрагменты — общий процесс/подпроцесс) и краткие метки. "
        "Верни СТРОГО JSON-массив без пояснений: "
        "[{\"i\":0,\"nsi_key\":\"[1][01][001]\",\"process\":\"...\",\"subprocess\":\"...\",\"op\":\"...\"}].\n\n"
        + "\n".join(f"[{i}] {c[:300]}" for i, c in enumerate(raw)))
    try:
        # deepseek-v4-pro — reasoning-модель: даём запас max_tokens, иначе «мышление» съедает бюджет → пустой content
        resp = await clients.chat(messages=[{"role": "user", "content": prompt}],
                                  model="deepseek/deepseek-v4-pro", max_tokens=4000)
        marks = _parse_json_array(resp.get("text") or "")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"LLM-разметка недоступна: {ex}")
    by_i = {int(m["i"]): m for m in marks if isinstance(m, dict) and m.get("i") is not None}
    embs = await clients.embed(raw)
    if (body or {}).get("replace"):
        await reglament_store.clear(tenant)
    editor = u.get("name") or u.get("sub") or "dev"
    saved = []
    for i, (chunk, emb) in enumerate(zip(raw, embs)):
        m = by_i.get(i, {})
        key = str(m.get("nsi_key") or f"[9][99][{i + 1:03d}]")
        await reglament_store.save_chunk(tenant, key, m.get("process") or "—", m.get("subprocess") or "—",
                                         m.get("op") or chunk[:60], chunk, emb, editor=editor)
        saved.append({"nsi_key": key, "op": m.get("op") or chunk[:60], "process": m.get("process") or "—"})
    await audit_store.record(editor, "reglament.ingest", tenant, {"chunks": len(saved), "model": resp.get("model")})
    return {"tenant": tenant, "chunks": saved, "count": len(saved), "markup_model": resp.get("model")}

def _nsi_parts(key: str) -> list:
    import re as _re
    return _re.findall(r"\[([^\]]+)\]", key or "")


@router.post("/api/reglament/graph/build")
async def reglament_graph_build(body: dict, u: dict = Depends(user)) -> dict:
    """Построить граф-слой регламента: (1) рёбра ИЕРАРХИИ НСИ (contains: процесс→подпроцесс→операция,
    детерминированно из ключа), (2) КРОСС-ЦИТИРОВАНИЕ норм через RouteAI DeepSeek v4 (нарушение одной
    нормы влечёт проверку связанной / обязательное действие — уточнённые декларации). §граф-слой."""
    require_level(u, "manager")
    tenant = str((body or {}).get("tenant") or "default").strip() or "default"
    chunks = await reglament_store.all_for(tenant)
    if not chunks:
        raise HTTPException(422, f"нет регламента для tenant «{tenant}» — сначала /api/reglament/ingest")
    await reglament_store.clear_edges(tenant)
    # (1) иерархия НСИ: в группе по [P][SS] родитель = чанк с минимальным [OOO], остальные — contains
    groups: dict = {}
    for c in chunks:
        p = _nsi_parts(c["nsi_key"])
        pref = "".join(f"[{x}]" for x in p[:2]) if len(p) >= 2 else c["nsi_key"]
        groups.setdefault(pref, []).append(c["nsi_key"])
    hier = 0
    for pref, keys in groups.items():
        keys = sorted(keys)
        parent = keys[0]
        for k in keys[1:]:
            await reglament_store.save_edge(tenant, parent, k, "contains", "иерархия НСИ")
            hier += 1
    # (2) кросс-цитирование норм (DeepSeek v4)
    listing = "\n".join(f"{c['nsi_key']} — {c['op']}: {c['text'][:160]}" for c in chunks)
    prompt = (
        "Ниже операции/нормы регламента (ключ НСИ — текст). Определи КРОСС-ССЫЛКИ: нарушение или "
        "выполнение одной нормы ВЛЕЧЁТ проверку связанной ИЛИ обязательное действие (напр. уточнённая "
        "декларация по НДС/прибыли, проверка договора). Верни СТРОГО JSON-массив без пояснений: "
        "[{\"from\":\"[1][01][002]\",\"to\":\"[1][01][005]\",\"relation\":\"entails\",\"note\":\"почему\"}]. "
        "relation: entails (связанная норма, to=ключ) | action (обязательное действие, to=\"\", note=действие).\n\n"
        + listing)
    xcite = 0
    model = ""
    try:
        resp = await clients.chat(messages=[{"role": "user", "content": prompt}],
                                  model="deepseek/deepseek-v4-pro", max_tokens=8000)  # reasoning + граф-вывод
        model = resp.get("model", "")
        for e in _parse_json_array(resp.get("text") or ""):
            if isinstance(e, dict) and e.get("from"):
                await reglament_store.save_edge(tenant, str(e["from"]), str(e.get("to") or ""),
                                                str(e.get("relation") or "entails"), str(e.get("note") or "")[:200])
                xcite += 1
    except Exception as ex:  # noqa: BLE001 — кросс-цитирование опционально
        model = f"(LLM недоступен: {ex})"
    await audit_store.record(u.get("name") or u.get("sub") or "dev", "reglament.graph", tenant,
                             {"hierarchy": hier, "xcite": xcite})
    return {"tenant": tenant, "nodes": len(chunks), "hierarchy_edges": hier, "xcite_edges": xcite, "markup_model": model}


@router.get("/api/reglament")
async def reglament_list(tenant: str = "default", u: dict = Depends(user)) -> dict:
    """Размеченный регламент (чанки + ключи НСИ, без эмбеддингов)."""
    reg = await reglament_store.all_for(tenant)
    return {"tenant": tenant, "chunks": [{"nsi_key": r["nsi_key"], "process": r["process"],
            "subprocess": r["subprocess"], "op": r["op"], "text": r["text"]} for r in reg]}


@router.get("/api/audit1c/norms/{name}")
async def audit1c_norm(name: str, u: dict = Depends(user)):
    """Текст нормы из экспертного справочника demo/audit1c/norms (markdown) — источник «Чем грозит / Что проверить»."""
    from fastapi.responses import PlainTextResponse
    import re as _re
    if not _re.match(r"^[\w\-]+\.md$", name):
        raise HTTPException(404, "нет такой нормы")
    p = findings.NORMS_DIR / name
    if not p.exists():
        raise HTTPException(404, "нет такой нормы")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
