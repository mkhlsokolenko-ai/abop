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


_ALLOWED_KEYS = {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}


def fingerprint(raw: dict) -> str:
    """Отпечаток файла шаблона (как лежит в репо) — маркер [repo:…] делает посев идемпотентным."""
    return hashlib.sha1(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def validate_schema(schema: dict) -> list[str]:
    """Strict-совместимость (vLLM xgrammar / OpenAI strict): только разрешённые ключи, у объектов
    additionalProperties:false и required = все свойства, у массивов items. Возвращает список ошибок."""
    errs: list[str] = []

    def walk(node, path: str) -> None:
        if not isinstance(node, dict):
            errs.append(f"{path}: не объект"); return
        bad = set(node) - _ALLOWED_KEYS
        if bad:
            errs.append(f"{path}: запрещённые ключи {sorted(bad)}")
        t = node.get("type")
        if t == "object":
            if node.get("additionalProperties") is not False:
                errs.append(f"{path}: нужен additionalProperties:false")
            props = node.get("properties") or {}
            if set(node.get("required") or []) != set(props):
                errs.append(f"{path}: required должен перечислять все свойства")
            for k, v in props.items():
                walk(v, f"{path}.{k}")
        elif t == "array":
            if "items" not in node:
                errs.append(f"{path}: массив без items")
            else:
                walk(node["items"], f"{path}[]")
        elif t not in ("string", "integer", "number", "boolean"):
            errs.append(f"{path}: недопустимый type {t!r}")
    if not isinstance(schema, dict) or not (schema.get("properties") or {}):
        return ["json_schema: нужен объект с properties"]
    walk(schema, "$")
    return errs[:20]


def _card(sid: str, raw: dict) -> dict | None:
    sch = raw.get("json_schema") or {}
    if not isinstance(sch, dict) or not sch.get("properties"):
        return None
    return {"name": raw.get("name") or sid, "instruction": raw.get("instruction") or "",
            "json_schema": sch, "max_tokens": int(raw.get("max_tokens") or 0) or None,
            "delivery": raw.get("delivery") if isinstance(raw.get("delivery"), dict) and raw.get("delivery") else None,
            "slots": raw.get("slots") if isinstance(raw.get("slots"), list) else [],
            "inputs": raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {},
            "produces": raw.get("produces") if isinstance(raw.get("produces"), (dict, list)) else {},
            "fingerprint": fingerprint(raw)}


def load_one(sid: str) -> dict | None:
    """Шаблон одного навыка из репо (для «сбросить к версии репо»)."""
    p = SKILLS_DIR / sid / "template.json"
    if not p.is_file():
        return None
    try:
        return _card(sid, json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return None


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
        card = _card(sid, t)
        if card:
            out[sid] = card
    return out


async def upsert(schema_store, sid: str, card: dict, *, editor: str = "repo", builtin: bool = True) -> dict:
    """Записать шаблон в БД (источник правды в рантайме). builtin=True — управляется репо/импортом
    (маркер [repo:<fingerprint>] в инструкции делает посев идемпотентным); builtin=False — ручная правка,
    посев её не трогает."""
    instr = (card.get("instruction") or "").rstrip()
    if builtin and card.get("fingerprint"):
        instr += "\n[repo:" + card["fingerprint"] + "]"
    return await schema_store.save(sid, {"name": card.get("name") or sid, "json_schema": card["json_schema"],
                                         "instruction": instr, "max_tokens": card.get("max_tokens"),
                                         "delivery": card.get("delivery"), "slots": card.get("slots") or [],
                                         "inputs": card.get("inputs") or {}, "produces": card.get("produces") or {}},
                                   editor=editor, builtin=builtin)


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
        if (cur and marker in (cur.get("instruction") or "") and (cur.get("max_tokens") or None) == t.get("max_tokens")
                and (cur.get("delivery") or None) == (t.get("delivery") or None)
                and (cur.get("slots") or []) == (t.get("slots") or [])
                and (cur.get("inputs") or {}) == (t.get("inputs") or {})
                and (cur.get("produces") or {}) == (t.get("produces") or {})):
            continue
        await upsert(schema_store, sid, t, editor="repo", builtin=True)
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


def strip_markers(instruction: str) -> str:
    """Инструкция без служебных маркеров [repo:…]/[max_tokens:…] — то, что видит модель и редактор."""
    import re
    return re.sub(r"\n?\[(repo|max_tokens):[^\]]*\]", "", instruction or "").rstrip()


def max_tokens_of(template: dict) -> int | None:
    """Колонка max_tokens (БД) или маркер [max_tokens:N] в инструкции (старые посевы)."""
    import re
    if (template or {}).get("max_tokens"):
        return int(template["max_tokens"])
    m = re.search(r"\[max_tokens:(\d+)\]", (template or {}).get("instruction") or "")
    return int(m.group(1)) if m else None
