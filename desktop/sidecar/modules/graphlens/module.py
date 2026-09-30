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
    name: str = ""      # имя цепочки, в которую превращается граф


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
    rows = db.q("SELECT name,nodes,edges FROM graphs WHERE id=?", (body.id,))
    if not rows:
        return {"ok": False, "error": "not_found"}
    _gname = rows[0]["name"] or ""
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
    # Узел без привязанного агента исполнять нечем. Раньше такой узел всё равно «работал» — модель
    # отвечала от его имени, и это выглядело результатом.
    bound = [n for n in agent_nodes if n.get("agent_id")]
    skipped = [str(n.get("label") or n.get("id")) for n in agent_nodes if not n.get("agent_id")]
    if not bound:
        return {"ok": False, "error": "no_agent_id",
                "message": "Ни у одного узла не выбран агент. Откройте узел и привяжите агента — "
                           "граф исполняется настоящими агентами, а не пересказом модели."}

    # Граф → цепочка ABOP: узел-агент становится шагом, ребро — зависимостью «после».
    ids = {n["id"] for n in bound}
    steps = []
    for i, n in enumerate(bound, 1):
        after = [str(a) for a, b in edges if b == n["id"] and a in ids]
        st = {"id": "n" + str(i), "agent_id": str(n["agent_id"]), "deliver": n.get("deliver") or "chat"}
        if after:
            # имена шагов свои, поэтому переводим идентификаторы узлов в имена шагов
            idx = {x["id"]: "n" + str(j) for j, x in enumerate(bound, 1)}
            st["after"] = [idx[a] for a in after if a in idx]
        steps.append(st)
    if len(steps) < 2:
        # Один шаг цепочкой не бывает — запускаем агента напрямую, это тот же настоящий прогон.
        try:
            r = abop.run_async(agent_id=steps[0]["agent_id"], context=body.task, no_cache=True,
                               deliver=steps[0].get("deliver") or "chat")
        except abop.AbopError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "kind": "run", "job_id": r.get("job_id"), "status": r.get("status"),
                "skipped": skipped,
                "message": "Запущен прогон агента. Результат появится в разделе «Прогоны»."}
    try:
        pl = abop._req("POST", "/api/pipelines",
                       {"name": (body.name or _gname or "Граф из десктопа"), "steps": steps}, timeout=60)
        # Асинхронно: цепочка уходит в очередь заданиями, окно не висит и результат не теряется,
        # если пользователь закроет раздел.
        started = abop._req("POST", "/api/pipelines/" + str(pl.get("id")) + "/run?async=1",
                            {"context": body.task}, timeout=60)
    except abop.AbopError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "kind": "pipeline", "pipeline_id": pl.get("id"),
            "job_id": (started or {}).get("job_id"), "steps_total": len(steps), "skipped": skipped,
            "message": f"Цепочка из {len(steps)} шагов запущена настоящим движком ABOP. "
                       "Ход и результат — в разделе «Прогоны»."}
