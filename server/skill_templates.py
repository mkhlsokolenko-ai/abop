"""Шаблоны извлечения навыков из репозитория (Блок «шаблоны для скиллов», 2026-09-27).

`skills/<sid>/template.json` = {"name", "instruction", "json_schema"} — подробная JSON Schema результата
навыка + инструкция-парсер. При старте сервера шаблоны сеются в `schema_templates` как builtin с id = sid
(репозиторий — источник правды; правка в UI поверх builtin перекрывается при следующем старте только если
шаблон в репо изменился — см. `seed`). Рантайм привязывает шаблон к навыку по умолчанию, если у навыка нет
явного `schema_template_id` (UI-правка в skill_store имеет приоритет).

Схемы пишутся под strict structured output (vLLM xgrammar): только type/properties/required/items/enum/
additionalProperties:false, без $ref/pattern/format. Числа — строками с единицей там, где важен источник
(«194 000,00 ₽»), чтобы модель не округляла и не «считала».
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SKILLS_DIR = Path(os.environ.get("APE_SKILLS_DIR") or (Path(__file__).resolve().parent.parent / "skills"))


def load_all() -> dict[str, dict]:
    """{sid: template} для всех навыков, у которых есть template.json (валидный JSON-объект со схемой)."""
    out: dict[str, dict] = {}
    if not SKILLS_DIR.exists():
        return out
    for p in sorted(SKILLS_DIR.glob("*/template.json")):
        sid = p.parent.name
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — битый шаблон не должен ронять старт
            continue
        sch = t.get("json_schema") or {}
        if not isinstance(sch, dict) or not sch.get("properties"):
            continue
        out[sid] = {"name": t.get("name") or sid, "instruction": t.get("instruction") or "",
                    "json_schema": sch, "fingerprint": hashlib.sha1(json.dumps(t, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]}
    return out


async def seed(schema_store) -> int:
    """Сеет/обновляет builtin-шаблоны из репо. Возвращает число записанных."""
    n = 0
    have = {t["id"]: t for t in await schema_store.all()}
    for sid, t in load_all().items():
        cur = have.get(sid)
        # не перекрываем ручную правку в UI (builtin=False у сохранённых из редактора) и не пишем без изменений
        if cur and not cur.get("builtin"):
            continue
        marker = "[repo:" + t["fingerprint"] + "]"
        if cur and marker in (cur.get("instruction") or ""):
            continue
        await schema_store.save(sid, {"name": t["name"], "json_schema": t["json_schema"],
                                      "instruction": t["instruction"].rstrip() + "\n" + marker},
                                editor="repo", builtin=True)
        n += 1
    return n


def describe_for_prompt(template: dict) -> str:
    """Короткое словесное описание полей схемы для инструкции модели (что класть в каждое поле)."""
    sch = (template or {}).get("json_schema") or {}
    lines = []

    def walk(node: dict, prefix: str, depth: int) -> None:
        if depth > 3 or not isinstance(node, dict):
            return
        props = node.get("properties") or {}
        for k, v in props.items():
            if not isinstance(v, dict):
                continue
            d = v.get("description") or ""
            t = v.get("type") or ""
            en = v.get("enum")
            lines.append(f"{prefix}{k} ({t}{' ∈ ' + '|'.join(map(str, en)) if en else ''}): {d}")
            if t == "object":
                walk(v, prefix + k + ".", depth + 1)
            elif t == "array" and isinstance(v.get("items"), dict) and v["items"].get("type") == "object":
                walk(v["items"], prefix + k + "[].", depth + 1)
    walk(sch, "", 0)
    return "\n".join(lines[:60])
