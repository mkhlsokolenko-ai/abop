"""Проверка template.json на strict-совместимость (та же логика, что в API импорта):
  python scripts/check_templates.py skills/*/template.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.delivery import validate_delivery  # noqa: E402
from server.skill_contract import field_warnings, validate_contract  # noqa: E402
from server.skill_templates import validate_schema  # noqa: E402

ok = True
for p in sys.argv[1:]:
    errs: list[str] = []
    try:
        t = json.loads(Path(p).read_text(encoding="utf-8"))
        for k in ("name", "instruction", "json_schema"):
            if not t.get(k):
                errs.append(f"нет {k}")
        if t.get("json_schema"):
            # Реестр систем тут недоступен: оффлайн-проверка не знает, что развёрнуто на стенде.
            # Существование системы доставки проверит сервер при сохранении шаблона.
            errs += validate_schema(t["json_schema"]) + validate_delivery(t.get("delivery"))
            if not (400 <= len(json.dumps(t["json_schema"], ensure_ascii=False)) <= 12000):
                errs.append("размер схемы вне 400..12000")
        # контракт навыка: вход, выход и сквозной ключ — по ним считается покрытие при сборке
        errs += validate_contract(t)
        mt = t.get("max_tokens")
        if mt is not None and not (1500 <= int(mt) <= 8000):
            errs.append("max_tokens вне 1500..8000")
    except Exception as e:  # noqa: BLE001
        errs.append(f"load: {e}")
    print(("OK   " if not errs else "FAIL ") + p + ("" if not errs else "\n   " + "\n   ".join(errs)))
    ok = ok and not errs
sys.exit(0 if ok else 1)
