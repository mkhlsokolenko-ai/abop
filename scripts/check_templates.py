"""Проверка template.json на strict-совместимость: python check_tpl.py skills/<sid>/template.json ..."""
import json, sys
ALLOWED = {"type","properties","required","items","enum","additionalProperties","description"}
def walk(node, path, errs):
    if not isinstance(node, dict): errs.append(f"{path}: not object"); return
    bad = set(node) - ALLOWED
    if bad: errs.append(f"{path}: forbidden keys {sorted(bad)}")
    t = node.get("type")
    if t == "object":
        if node.get("additionalProperties") is not False: errs.append(f"{path}: additionalProperties must be false")
        props = node.get("properties") or {}
        req = node.get("required") or []
        if set(req) != set(props): errs.append(f"{path}: required != properties ({sorted(set(props)-set(req))} missing / {sorted(set(req)-set(props))} extra)")
        for k, v in props.items(): walk(v, f"{path}.{k}", errs)
    elif t == "array":
        if "items" not in node: errs.append(f"{path}: array without items")
        else: walk(node["items"], f"{path}[]", errs)
    elif t not in ("string","integer","number","boolean"):
        errs.append(f"{path}: bad type {t!r}")
ok = True
for p in sys.argv[1:]:
    errs = []
    try:
        t = json.load(open(p, encoding="utf-8"))
        for k in ("name","instruction","json_schema"):
            if not t.get(k): errs.append(f"missing {k}")
        if t.get("json_schema"): walk(t["json_schema"], "$", errs)
        if not (400 <= len(json.dumps(t["json_schema"], ensure_ascii=False)) <= 12000): errs.append("schema size out of range")
        mt = t.get("max_tokens")
        if mt is not None and not (1500 <= int(mt) <= 8000): errs.append("max_tokens out of range")
    except Exception as e: errs.append(f"load: {e}")
    print(("OK   " if not errs else "FAIL ") + p + ("" if not errs else "\n   " + "\n   ".join(errs)))
    ok = ok and not errs
sys.exit(0 if ok else 1)
