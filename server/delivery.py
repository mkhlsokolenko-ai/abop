"""Доставка результата навыка В СИСТЕМУ (2026-09-27): шаблон извлечения описывает не только структуру
ответа, но и куда она уходит. Секция `delivery` в template.json:

    "delivery": {
      "system": "redmine", "type": "issue.create",          # система из реестра и тип команды коннектора
      "each": "находки",                                     # (необяз.) массив → по команде на элемент
      "where": {"ранг": ["критично", "существенно"]},        # (необяз.) фильтр элементов по enum-полям
      "limit": 10,                                           # (необяз.) максимум команд за прогон
      "title": "Задача: {{заголовок}}",                      # заголовок HITL-карточки
      "payload": {"subject": "…{{id}}…", "description": "…{{$.итог}}…", "project": "abop"}
    }

Подстановки: `{{путь.к.полю}}` — из элемента (или корня, если `each` нет), `{{$.путь}}` — из корня ответа,
`{{#путь}}` — список маркированными строками. Списки в `{{…}}` склеиваются через «; », объекты — «ключ: значение».
ABOP не отправляет ничего сам: команды становятся HITL-заявками с превью того, что уйдёт; после «да»
команда публикуется в шину и её исполняет коннектор-воркер, а результат (id/ссылка) возвращается событием.
"""
from __future__ import annotations

import html
import re
from typing import Any

_VAR = re.compile(r"\{\{\s*(#?)\s*([^{}]+?)\s*\}\}")
_ONLY_VAR = re.compile(r"^\s*\{\{\s*#?\s*[^{}]+?\s*\}\}\s*$")   # поле целиком = одна подстановка
_ALLOWED = {"system", "type", "each", "where", "limit", "title", "payload", "require"}


def validate_delivery(d: Any, *, systems: set | None = None) -> list[str]:
    """Ошибки секции delivery (пусто — валидна). None/{} — доставки нет, это нормально.

    `systems` — идентификаторы систем РЕЕСТРА. Если переданы, система доставки обязана быть среди
    них: иначе шаблон объявляет отправку туда, куда коннектор не умеет, и ошибка всплывает в очереди
    ошибок шины уже после прогона. Без реестра (оффлайн-проверка шаблонов) этот слой пропускается —
    проверить его там нечем.
    """
    if d in (None, {}):
        return []
    if not isinstance(d, dict):
        return ["delivery: должен быть объектом"]
    errs = [f"delivery: неизвестные ключи {sorted(set(d) - _ALLOWED)}"] if set(d) - _ALLOWED else []
    sysid = str(d.get("system") or "")
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", sysid):
        errs.append("delivery.system: slug системы из реестра")
    elif systems is not None and sysid not in systems:
        errs.append(f"delivery.system: системы «{sysid}» нет в реестре "
                    f"(есть: {', '.join(sorted(systems)[:12])})")
    if not re.fullmatch(r"[a-z0-9_.-]{1,60}", str(d.get("type") or "")):
        errs.append("delivery.type: тип команды коннектора (например issue.create)")
    if d.get("each") is not None and not isinstance(d["each"], str):
        errs.append("delivery.each: путь к массиву строкой")
    if d.get("where") is not None and not (isinstance(d["where"], dict) and all(isinstance(v, list) for v in d["where"].values())):
        errs.append("delivery.where: {поле: [допустимые значения]}")
    if d.get("limit") is not None and not (isinstance(d["limit"], int) and 1 <= d["limit"] <= 50):
        errs.append("delivery.limit: 1..50")
    if d.get("require") is not None and not (isinstance(d["require"], list) and all(isinstance(x, str) for x in d["require"])):
        errs.append("delivery.require: список обязательных ключей payload")
    if isinstance(d.get("require"), list) and isinstance(d.get("payload"), dict):
        miss = [k for k in d["require"] if k not in d["payload"]]
        if miss:
            errs.append(f"delivery.require: ключей нет в payload: {miss}")
    p = d.get("payload")
    if not isinstance(p, dict) or not p:
        errs.append("delivery.payload: объект полей команды (строки с {{подстановками}})")
    return errs


def _get(obj: Any, path: str) -> Any:
    cur = obj
    for seg in [s for s in path.split(".") if s]:
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            cur = cur[int(seg)] if int(seg) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _scalar(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, (int, float, str)):
        return str(v)
    if isinstance(v, list):
        return "; ".join(_scalar(x) for x in v if x not in (None, "", [], {}))
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_scalar(x)}" for k, x in v.items() if x not in (None, "", [], {}))
    return str(v)


def _bullets(v: Any) -> str:
    if isinstance(v, list):
        return "\n".join("- " + _scalar(x) for x in v if x not in (None, "", [], {}))
    if isinstance(v, dict):
        return "\n".join(f"- {k}: {_scalar(x)}" for k, x in v.items() if x not in (None, "", [], {}))
    return _scalar(v)


def render(text: str, item: Any, root: Any) -> str:
    """Подстановка {{…}} в строке шаблона. Неизвестные пути → пусто (ничего не выдумываем)."""
    def sub(m: re.Match) -> str:
        bullets, path = m.group(1) == "#", m.group(2).strip()
        src, p = (root, path[2:]) if path.startswith("$.") else (item, path)
        v = _get(src, p)
        return _bullets(v) if bullets else _scalar(v)
    return _VAR.sub(sub, text or "")


def _passes(item: Any, where: dict | None) -> bool:
    for k, allowed in (where or {}).items():
        v = _get(item, k)
        if isinstance(v, list):
            if not any(x in allowed for x in v):
                return False
        elif v not in allowed:
            return False
    return True


def build_commands(spec: dict, struct: dict, *, skill: str = "") -> list[dict]:
    """Команды коннектора из структурированного ответа навыка по секции delivery."""
    if validate_delivery(spec) or not isinstance(struct, dict):
        return []
    items: list[Any] = [struct]
    if spec.get("each"):
        arr = _get(struct, spec["each"])
        items = [x for x in arr if isinstance(x, dict)] if isinstance(arr, list) else []
    items = [x for x in items if _passes(x, spec.get("where"))][: int(spec.get("limit") or 10)]
    # Обязательные поля: явный require + поля, чьё значение в шаблоне ЦЕЛИКОМ одна подстановка
    # (например "to": "{{кому}}"): пусто в ответе навыка — отправлять нечего, команду не создаём,
    # иначе коннектор получит битую команду и уронит её в DLQ («payload.to пустой»).
    tpl_payload = spec.get("payload") or {}
    required = {k for k in (spec.get("require") or []) if k in tpl_payload}
    required |= {k for k, v in tpl_payload.items() if isinstance(v, str) and _ONLY_VAR.match(v)}
    out: list[dict] = []
    skipped: list[dict] = []
    for it in items:
        payload = {k: (render(v, it, struct) if isinstance(v, str) else v) for k, v in tpl_payload.items()}
        payload = {k: v for k, v in payload.items() if v not in ("", None)}
        miss = sorted(k for k in required if k not in payload)
        if miss or not payload:
            skipped.append({"reason": ("пустые обязательные поля: " + ", ".join(miss)) if miss else "нечего отправлять",
                            "item": _scalar(it.get("id") or "")[:80] if isinstance(it, dict) and it is not struct else ""})
            continue
        title = render(spec.get("title") or "", it, struct).strip() or f"{spec['system']}/{spec['type']}"
        ref = _scalar(it.get("id") or it.get("заголовок") or it.get("название") or "") if it is not struct else ""
        out.append({"system": spec["system"], "type": spec["type"], "payload": payload, "title": title[:200],
                    "source": {"skill": skill, "item": ref[:80]}})
    if skipped:
        out.append({"_skipped": skipped, "system": spec["system"], "type": spec["type"]})
    return out


def split_skipped(cmds: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разделить результат build_commands на команды и пропуски (пустые обязательные поля)."""
    return [c for c in cmds if not c.get("_skipped")], [s for c in cmds if c.get("_skipped") for s in c["_skipped"]]


def preview_html(cmd: dict) -> str:
    """Превью того, что уйдёт в систему — для HITL-карточки (десктоп и веб показывают как есть, без скриптов)."""
    p = cmd.get("payload") or {}
    src = cmd.get("source") or {}
    head = (f"<p><b>{html.escape(cmd.get('system', ''))} · {html.escape(cmd.get('type', ''))}</b>"
            f" · навык <code>{html.escape(src.get('skill') or '')}</code>"
            + (f" · элемент <code>{html.escape(src.get('item') or '')}</code>" if src.get("item") else "") + "</p>")
    rows = []
    for k, v in p.items():
        if k == "description" or (isinstance(v, str) and "\n" in v):
            rows.append(f"<p><b>{html.escape(str(k))}:</b></p><pre style=\"white-space:pre-wrap\">{html.escape(str(v))[:6000]}</pre>")
        else:
            rows.append(f"<p><b>{html.escape(str(k))}:</b> {html.escape(str(v))[:500]}</p>")
    return head + "".join(rows) + "<p style=\"color:#6b7280;font-size:12px\">После подтверждения команда уйдёт в шину, её исполнит коннектор; результат (номер/ссылка) вернётся в эту карточку.</p>"
