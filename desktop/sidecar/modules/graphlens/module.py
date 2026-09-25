"""Модуль «Граф» (GraphLens) — интерактивный редактор графа агента: палитра→холст→связи→
инспектор→проверка исполнимости→запуск. Граф хранится локально (SQLite), узлы-агенты
запускаются цепочкой через шлюз. Вёрстка — по ДС GraphLens.dc.html.
"""
from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel

from ... import db
from ... import abop_client as abop

MANIFEST = {"id": "graphlens", "title": "Граф", "icon": "graphlens", "ui": "graphlens", "order": 30}
router = APIRouter()

# Палитра блоков (типы узлов) — из макета.
PALETTE = [
    {"kind": "input", "label": "входящие", "glyph": "▸"},
    {"kind": "agent", "label": "агент", "glyph": "🤖"},
    {"kind": "tool", "label": "инструмент", "glyph": "🔧"},
    {"kind": "cond", "label": "условие", "glyph": "◈"},
    {"kind": "wait", "label": "ожидание", "glyph": "⏳"},
    {"kind": "output", "label": "наружу", "glyph": "↗"},
]


class GraphIn(BaseModel):
    name: str = "Новый граф"
    nodes: list = []
    edges: list = []


class RunGraphIn(BaseModel):
    id: int
    task: str


def _row(g: dict) -> dict:
    g["nodes"] = json.loads(g.get("nodes") or "[]")
    g["edges"] = json.loads(g.get("edges") or "[]")
    return g


@router.get("/palette")
def palette() -> list[dict]:
    return PALETTE


@router.get("/graphs")
def graphs() -> list[dict]:
    return [_row(g) for g in db.q("SELECT id,name,nodes,edges FROM graphs ORDER BY updated_at DESC")]


@router.post("/graphs")
def create(body: GraphIn) -> dict:
    gid = db.run("INSERT INTO graphs(name,nodes,edges,updated_at) VALUES(?,?,?,?)",
                 (body.name, json.dumps(body.nodes, ensure_ascii=False), json.dumps(body.edges), db.now()))
    return {"ok": True, "id": gid}


@router.patch("/graphs/{gid}")
def update(gid: int, body: GraphIn) -> dict:
    db.run("UPDATE graphs SET name=?,nodes=?,edges=?,updated_at=? WHERE id=?",
           (body.name, json.dumps(body.nodes, ensure_ascii=False), json.dumps(body.edges), db.now(), gid))
    return {"ok": True}


@router.delete("/graphs/{gid}")
def delete(gid: int) -> dict:
    db.run("DELETE FROM graphs WHERE id=?", (gid,))
    return {"ok": True}


@router.post("/check")
def check(body: GraphIn) -> dict:
    """Проверка исполнимости: доступность компонентов, связность, циклы (как в макете)."""
    nodes = body.nodes or []
    edges = [tuple(e) for e in (body.edges or [])]
    ids = {n["id"] for n in nodes}
    issues = []
    if not nodes:
        return {"ok": False, "issues": ["Холст пуст — добавьте блоки."]}
    # висячие связи
    for a, b in edges:
        if a not in ids or b not in ids:
            issues.append("Есть связь к несуществующему узлу.")
            break
    # связность (слабая): все узлы достижимы из входного/любого
    adj: dict = {n["id"]: set() for n in nodes}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].add(b); adj[b].add(a)
    if len(nodes) > 1:
        seen = set(); stack = [nodes[0]["id"]]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x); stack += [y for y in adj[x] if y not in seen]
        if len(seen) < len(nodes):
            issues.append("Не все блоки связаны (есть изолированные).")
    # циклы (в ориентированном графе)
    dadj: dict = {n["id"]: [] for n in nodes}
    for a, b in edges:
        if a in dadj and b in dadj:
            dadj[a].append(b)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n["id"]: WHITE for n in nodes}
    cyc = [False]

    def dfs(u):
        color[u] = GREY
        for v in dadj[u]:
            if color[v] == GREY:
                cyc[0] = True
            elif color[v] == WHITE:
                dfs(v)
        color[u] = BLACK
    for n in nodes:
        if color[n["id"]] == WHITE:
            dfs(n["id"])
    if cyc[0]:
        issues.append("Обнаружен цикл — поток не завершится.")
    # честность (UX-аудит D-H10): исполняются только узлы «агент», и только с привязкой к каталогу.
    agent_nodes = [n for n in nodes if n.get("kind") == "agent"]
    for n in agent_nodes:
        if not str(n.get("agent_id") or "").strip():
            issues.append(f"Узел «{n.get('label') or 'агент'}» не привязан к агенту из каталога — выберите его в инспекторе.")
    if not agent_nodes:
        issues.append("В графе нет ни одного узла «агент» — запускать нечего.")
    other = [n for n in nodes if n.get("kind") != "agent"]
    warnings = []
    if other:
        kinds = sorted({str(n.get("kind")) for n in other})
        warnings.append(f"Узлы {', '.join(kinds)} ({len(other)}) — разметка потока: при запуске выполняются только узлы «агент».")
    return {"ok": not issues, "issues": issues, "warnings": warnings, "nodes": len(nodes), "edges": len(edges),
            "runnable": len(agent_nodes)}


@router.post("/run")
def run(body: RunGraphIn) -> dict:
    """Запуск графа: агентные узлы в топологическом порядке — цепочкой через шлюз."""
    rows = db.q("SELECT nodes,edges FROM graphs WHERE id=?", (body.id,))
    if not rows:
        return {"ok": False, "error": "not_found"}
    nodes = json.loads(rows[0]["nodes"] or "[]")
    edges = [tuple(e) for e in json.loads(rows[0]["edges"] or "[]")]
    # порядок: топосорт; берём только агентные узлы
    order = [n["id"] for n in nodes]  # простой порядок добавления как fallback
    indeg = {n["id"]: 0 for n in nodes}
    dadj: dict = {n["id"]: [] for n in nodes}
    for a, b in edges:
        if a in indeg and b in indeg:
            dadj[a].append(b); indeg[b] += 1
    if edges:
        q = [i for i in indeg if indeg[i] == 0]; topo = []
        while q:
            x = q.pop(0); topo.append(x)
            for y in dadj[x]:
                indeg[y] -= 1
                if indeg[y] == 0:
                    q.append(y)
        if len(topo) == len(nodes):
            order = topo
    byid = {n["id"]: n for n in nodes}
    agent_nodes = [byid[i] for i in order if byid[i].get("kind") == "agent"]
    if not agent_nodes:
        return {"ok": False, "error": "no_agents", "message": "В графе нет агентных узлов."}
    outputs, prior = [], ""
    try:
        for n in agent_nodes:
            name = n.get("label") or "Агент"
            system = f"Ты — узел графа «{name}»."
            # спека узла-агента берётся из ABOP (единый каталог), fallback — локальная метка
            if n.get("agent_id"):
                try:
                    a = abop.agent(str(n["agent_id"]))
                    if a:
                        steps = a.get("steps") or a.get("skills") or ""
                        if isinstance(steps, list):
                            steps = "\n".join(str(s) for s in steps)
                        system = f"Ты — {a.get('name', name)}. {a.get('description', '') or a.get('role', '')}\nШаги:\n{steps}"
                except abop.AbopError:
                    pass
            prefix = ("Наработки предыдущих узлов:\n" + prior) if prior else ""
            r = abop.chat(prompt=f"Задача: {body.task}\n\n{prefix}", system=system, max_tokens=1000)
            t = r.get("text", "")
            outputs.append({"name": name, "text": t}); prior += f"[{name}]: {t}\n"
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "steps": outputs}
