#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка / разборка веб-бандла ABOP (Блок 5: фронт вне JSON-строки).

`webapp/index.html` — самодостаточный dc-runtime бандл: оболочка + `<script type="__bundler/manifest">`
(ресурсы: компоненты .dc.html, React, dc-runtime, шрифты) + `<script type="__bundler/template">`
(главный шаблон: HTML + `<script type="text/x-dc">` с логикой). Раньше любая правка шла через
json.loads → replace → json.dumps. Теперь исходники лежат в `webapp/src/`:

  src/shell.html            — оболочка с плейсхолдерами @@MANIFEST@@ / @@TEMPLATE@@
  src/template.html         — главный шаблон (правится как обычный HTML/JS)
  src/components/*.dc.html  — компоненты (Ape, GraphLens, OpsLens, AdminScreens, Overlays)
  src/vendor/*.js           — react, react-dom, dc-runtime
  src/fonts/*.woff2         — шрифты
  src/manifest.json         — реестр ресурсов: uuid → {file, mime, compressed}

Команды:
  python webapp/build.py extract   # index.html → src/ (одноразово / после чужой правки бандла)
  python webapp/build.py build     # src/ → index.html
  python webapp/build.py check     # собрать во временный буфер и сравнить с index.html (CI)
  python webapp/build.py lint      # node --check для JS главного шаблона и компонентов
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
SRC = ROOT / "src"
RE_MAN = re.compile(r'<script type="__bundler/manifest">(.*?)</script>', re.S)
RE_TPL = re.compile(r'<script type="__bundler/template">(.*?)</script>', re.S)
RE_EXT = re.compile(r'<script type="__bundler/ext_resources">\s*(.*?)\s*</script>', re.S)
EXT = {"text/html": ".dc.html", "text/javascript": ".js", "font/woff2": ".woff2"}


def _names(raw: str) -> dict[str, str]:
    """uuid → читаемое имя файла из ext_resources (Ape.dc.html, react.production.min.js …)."""
    m = RE_EXT.search(raw)
    out = {}
    for it in json.loads(m.group(1)) if m else []:
        base = it["id"].rsplit("/", 1)[-1]
        out[it["uuid"]] = base
    return out


def _js_of_template(tpl: str) -> str:
    mt = re.search(r'<script type="text/x-dc"[^>]*>', tpl)
    return tpl[mt.end():tpl.find("</script>", mt.end())] if mt else ""


def _js_of_component(html: str) -> str:
    m = re.search(r"<script data-dc-script[^>]*>(.*?)</script>", html, re.S)
    return m.group(1) if m else ""


def extract() -> None:
    raw = INDEX.read_text(encoding="utf-8")
    man = json.loads(RE_MAN.search(raw).group(1))
    tpl = json.loads(RE_TPL.search(raw).group(1))
    names = _names(raw)
    for d in ("components", "vendor", "fonts"):
        (SRC / d).mkdir(parents=True, exist_ok=True)
    reg = {}
    used = set()
    for uid, res in man.items():
        mime, comp, data = res["mime"], bool(res.get("compressed")), base64.b64decode(res["data"])
        if comp:
            data = gzip.decompress(data)
        ext = EXT.get(mime, ".bin")
        base = names.get(uid) or (uid[:8] + ext)
        if mime == "text/javascript" and base.endswith(".js") is False:
            base += ".js"
        if mime == "text/html" and not base.endswith(".dc.html"):
            base = base.replace(".html", "") + ".dc.html"
        if mime == "font/woff2":
            base = _font_name(data, uid)
        if base in used:
            base = uid[:8] + "-" + base
        used.add(base)
        sub = "components" if mime == "text/html" else ("vendor" if mime == "text/javascript" else "fonts")
        (SRC / sub / base).write_bytes(data)
        reg[uid] = {"file": f"{sub}/{base}", "mime": mime, "compressed": comp}
    (SRC / "manifest.json").write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    (SRC / "template.html").write_text(tpl, encoding="utf-8")
    mm, mt = RE_MAN.search(raw), RE_TPL.search(raw)
    shell = raw[:mm.start(1)] + "@@MANIFEST@@" + raw[mm.end(1):mt.start(1)] + "@@TEMPLATE@@" + raw[mt.end(1):]
    (SRC / "shell.html").write_text(shell, encoding="utf-8")
    print(f"extract ok: {len(reg)} ресурсов, template {len(tpl)} симв., shell {len(shell)} симв.")


def _font_name(data: bytes, uid: str) -> str:
    """Имя шрифта из CSS шаблона нельзя узнать по байтам — берём uuid как стабильное имя."""
    return uid[:8] + ".woff2"


def assemble() -> str:
    reg = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    man = {}
    for uid, spec in reg.items():
        data = (SRC / spec["file"]).read_bytes()
        if spec.get("compressed"):
            data = gzip.compress(data, mtime=0)
        man[uid] = {"mime": spec["mime"], "compressed": bool(spec.get("compressed")), "data": base64.b64encode(data).decode("ascii")}
    tpl = (SRC / "template.html").read_text(encoding="utf-8")
    shell = (SRC / "shell.html").read_text(encoding="utf-8")
    enc = lambda obj: json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")  # noqa: E731 — как делал bundler
    return shell.replace("@@MANIFEST@@", enc(man)).replace("@@TEMPLATE@@", enc(tpl))


def build() -> None:
    lint()
    out = assemble()
    INDEX.write_text(out, encoding="utf-8")
    print(f"build ok: index.html {len(out)} симв.")


def _decode(raw: str) -> tuple[str, dict[str, bytes]]:
    man = json.loads(RE_MAN.search(raw).group(1))
    tpl = json.loads(RE_TPL.search(raw).group(1))
    res = {}
    for uid, r in man.items():
        d = base64.b64decode(r["data"])
        res[uid] = gzip.decompress(d) if r.get("compressed") else d
    return tpl, res


def check() -> int:
    """Функциональная эквивалентность: шаблон и распакованные ресурсы совпадают (gzip-байты могут отличаться)."""
    cur = INDEX.read_text(encoding="utf-8")
    new = assemble()
    t1, r1 = _decode(cur)
    t2, r2 = _decode(new)
    bad = []
    if t1 != t2:
        bad.append("template.html отличается от index.html")
    for k in set(r1) | set(r2):
        if r1.get(k) != r2.get(k):
            bad.append("ресурс " + k[:8] + " отличается")
    if bad:
        print("CHECK FAIL:\n  " + "\n  ".join(bad)); return 1
    print("check ok: src/ и index.html эквивалентны"); return 0


def lint() -> None:
    node = "node"
    tpl = (SRC / "template.html").read_text(encoding="utf-8")
    items = [("template.html", _js_of_template(tpl))]
    for p in sorted((SRC / "components").glob("*.dc.html")):
        js = _js_of_component(p.read_text(encoding="utf-8"))
        if js.strip():
            items.append((p.name, js))
    for name, js in items:
        tf = Path(tempfile.gettempdir()) / ("_abop_lint_" + name.replace(".", "_") + ".js")
        tf.write_text(js, encoding="utf-8")
        r = subprocess.run([node, "--check", str(tf)], capture_output=True, text=True)
        if r.returncode != 0:
            print("LINT FAIL " + name + ":\n" + r.stderr[:2000]); sys.exit(1)
    print(f"lint ok: {len(items)} JS-блоков")


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if cmd == "extract":
        extract()
    elif cmd == "build":
        build()
    elif cmd == "lint":
        lint()
    elif cmd == "check":
        sys.exit(check())
    else:
        print(__doc__); sys.exit(2)
