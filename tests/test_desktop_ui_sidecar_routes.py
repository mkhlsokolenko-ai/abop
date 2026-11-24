# -*- coding: utf-8 -*-
"""Каждый запрос интерфейса десктопа обязан находить маршрут в сайдкаре.

23.11.2026 выяснилось, что окно «Заявки методолога» с 20.11 зовёт `/api/methodology/*` прямо в
сайдкар — маршрутов там нет, «Применить» и пересчёт отвечали 404, и никто не замечал неделю
(docs/AUDIT_DESKTOP_2026-11-23.md, п. 2.1). Маршруты сервера при этом были: терялся именно стык
«интерфейс → сайдкар», а его не сверял ни один тест.

Сторож разбирает вызовы `api(...)` и `fetch(...)` в `desktop/ui` (литералы, константы вида
`const M = "/api/modules/…"`, шаблоны) и требует, чтобы путь подходил под маршрут сайдкара.
Метод не сверяется: он часто стоит дальше по тексту вызова, и его разбор давал бы ложные тревоги.

Путь, собранный в переменной (`api(D)`), до конца не раскрыть: такие вызовы заканчиваются на `/`
и пропускаются. Если нужен иной способ написать «без маршрута» — это ошибка, а не исключение.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
UI = КОРЕНЬ / "desktop" / "ui"
SIDECAR = КОРЕНЬ / "desktop" / "sidecar"
IDENT = r"[A-Za-z_А-Яа-яЁё][A-Za-z_0-9А-Яа-яЁё]*"


def norm(path: str) -> str:
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return re.sub(r"/+", "/", path) or "/"


def route_re(route: str):
    parts = re.split(r"\{\}", norm(route))
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


def sidecar_routes():
    routes = []
    for f in sorted(list((SIDECAR / "modules").glob("*/module.py")) + [SIDECAR / "app.py"]):
        text = f.read_text(encoding="utf-8")
        m = re.search(r'"id"\s*:\s*"([a-z_]+)"', text)
        prefix = ("/api/modules/" + m.group(1)) if (f.name == "module.py" and m) else ""
        for mm in re.finditer(r'@(router|app)\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']', text):
            routes.append((mm.group(2).upper(), norm(prefix + mm.group(3)), f.parent.name))
    return routes


def first_arg(text: str, start: int) -> str:
    depth, quote, i = 0, "", start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return text[start:i]


def parse(expr: str, consts: dict, query_vars: set) -> str:
    expr = re.sub(r"`([^`]*)`", lambda m: '"' + m.group(1) + '"', expr)
    parts, depth, buf = [], 0, ""
    for ch in expr:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "+" and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    parts.append(buf.strip())
    path = ""
    for p in parts:
        if not p:
            continue
        if p[:1] in "fF" and p[1:2] in "\"'":
            p = p[1:]
        m = re.fullmatch(r'"([^"]*)"', p)
        if m:
            path += m.group(1)
        elif p in consts:
            path += consts[p]
        elif p.split(".")[0] in ("ctx", "API", "window", "config"):
            continue
        elif p in query_vars:
            continue
        elif p.startswith("(") and '"?' in p:
            continue
        else:
            path += "{}"
    return path


def ui_calls():
    calls = []
    for f in sorted(list((UI / "modules").glob("*/panel.js")) + list((UI / "core").glob("*.js"))):
        text = f.read_text(encoding="utf-8")
        consts = {m.group(1): m.group(2) for m in
                  re.finditer(r"^\s*const\s+(" + IDENT + r")\s*=\s*\"(/api/[^\"]*)\"", text, re.M)}
        query_vars = set()
        for m in re.finditer(r"(?:const|let|var)\s+(" + IDENT + r")\s*=\s*([^\n;]*)", text):
            tail = m.group(2)
            if ('"?' in tail or "`?" in tail) and "/api/" not in tail:
                query_vars.add(m.group(1))
        for m in re.finditer(r"\b(api|fetch)\(", text):
            path = parse(first_arg(text, m.end()), consts, query_vars)
            if "/api/" not in path:
                continue
            path = norm(path[path.index("/api"):])
            where = f"{f.parent.name}/{f.name}:{text.count(chr(10), 0, m.start()) + 1}"
            calls.append((path, where))
    return calls


def test_каждый_запрос_интерфейса_имеет_маршрут_в_сайдкаре():
    regexes = [route_re(r) for _, r, _ in sidecar_routes()]
    lost = [(p, w) for p, w in ui_calls() if not p.endswith("/") and not any(r.match(p) for r in regexes)]
    assert not lost, "запросы интерфейса, у которых нет маршрута в сайдкаре (это 404):\n" + "\n".join(
        f"  {p}  {w}" for p, w in lost)


def test_сторож_вообще_видит_запросы():
    routes = sidecar_routes()
    calls = ui_calls()
    assert len(routes) > 100 and len(calls) > 150, (len(routes), len(calls))
    assert any(p == "/api/modules/agents/methodology/patrol" for p, _ in calls)
