"""Заливка шаблонов навыков в БД работающего ABOP через API — без пересборки образа.

  python scripts/push_templates.py --base https://abop.example --user admin.abop --password '...'
  python scripts/push_templates.py --base http://127.0.0.1:8091 --only audit1c-rank,invest1c-verdict --force

Читает skills/*/template.json, проверяет strict-совместимость локально (scripts/check_templates.py той же логикой),
логинится (POST /api/auth/login) и шлёт POST /api/schema-templates/import {source:"repo"}.
Ручные правки в БД (builtin=false) без --force не перезаписываются. Пароль можно дать через ABOP_ADMIN_PASSWORD.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.delivery import validate_delivery  # noqa: E402
from server.skill_templates import validate_schema  # noqa: E402


def _req(base: str, path: str, body: dict | None = None, token: str = "") -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path}: HTTP {e.code} {e.read().decode()[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--user", default=os.environ.get("ABOP_ADMIN_USER", "admin.abop"))
    ap.add_argument("--password", default=os.environ.get("ABOP_ADMIN_PASSWORD", ""))
    ap.add_argument("--only", default="", help="через запятую: id навыков")
    ap.add_argument("--force", action="store_true", help="перезаписать и ручные правки (builtin=false)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    only = {s.strip() for s in a.only.split(",") if s.strip()}
    items, bad = [], {}
    for p in sorted((ROOT / "skills").glob("*/template.json")):
        sid = p.parent.name
        if only and sid not in only:
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        errs = validate_schema(raw.get("json_schema")) + validate_delivery(raw.get("delivery"))
        if errs or not raw.get("instruction"):
            bad[sid] = errs or ["нужна instruction"]
            continue
        items.append({"id": sid, **{k: raw[k] for k in ("name", "instruction", "json_schema", "max_tokens", "delivery") if k in raw}})
    print(f"локально валидны: {len(items)}, с ошибками: {len(bad)}")
    for sid, errs in bad.items():
        print("  FAIL", sid, "|", "; ".join(errs[:3]))
    if bad:
        return 1
    if a.dry_run:
        return 0
    token = ""
    if a.password:
        token = _req(a.base, "/api/auth/login", {"username": a.user, "password": a.password}).get("access_token", "")
        if not token:
            raise SystemExit("логин не дал access_token")
    total = {"imported": [], "skipped": [], "errors": {}}
    for i in range(0, len(items), 50):
        r = _req(a.base, "/api/schema-templates/import", {"templates": items[i:i + 50], "source": "repo", "force": a.force}, token)
        total["imported"] += r.get("imported", []); total["skipped"] += r.get("skipped", []); total["errors"].update(r.get("errors", {}))
    print(f"импортировано: {len(total['imported'])}, пропущено (ручные правки): {total['skipped']}, ошибки: {total['errors']}")
    return 0 if not total["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
