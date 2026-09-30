"""Арбитраж расхождений между ветвями: правилом, навыком-арбитром или человеком.

Зачем. Доска (blackboard.py) честно хранит оба вывода, когда ветви разошлись, но сводка должна дать
один ответ — иначе человек получает две цифры и решает сам, а это ровно та работа, которую мы обещали
снять. Раньше расхождение решалось молча: в слиянии выживало значение последней ветви.

Три способа разрешить спор, по возрастанию цены:
  • правило — дешёвое и объяснимое: большинство ветвей, наиболее тяжёлый статус, число по краю,
    свежесть, наличие первоисточника;
  • навык-арбитр — когда правило не применимо, но спор можно разобрать по существу;
  • человек — когда решение меняет смысл результата: срок, деньги, оценка риска.

Порядок важен: правило не должно «решать» там, где оно лишь скрывает спор. Поэтому правило применяется
только если оно в этом споре действительно различает варианты, а иначе спор идёт выше — вплоть до
человека, и тогда это видно в результате как незакрытое расхождение.
"""
from __future__ import annotations

import json

# Тяжесть значений: спор о статусе разрешается в сторону проблемы, а не в сторону благополучия.
# Иначе арбитраж превращается в машинку, которая прячет срывы: из «просрочен» и «в работе» молча
# выбирает «в работе», и отчёт выглядит лучше, чем есть.
_SEVERITY: list[tuple[tuple[str, ...], int]] = [
    (("сорван", "сорвано", "провален", "критично", "критический", "критическая", "просрочен",
      "просрочено", "просрочена", "нарушен", "нарушено"), 5),
    (("риск", "риск срыва", "под угрозой", "угроза", "высокий", "high", "a"), 4),
    (("в работе", "выполняется", "частично", "средний", "medium", "b"), 3),
    (("запланирован", "запланировано", "не начат", "ожидает", "низкий", "low", "c"), 2),
    (("выполнен", "выполнено", "завершён", "завершено", "закрыт", "закрыто", "ок", "ok", "нет", "d"), 1),
]
_NUM_HINTS = ("часы", "человекочасы", "трудозатраты", "сумма", "стоимость", "дней", "days",
              "перерасход", "отклонение", "процент")
_SOURCE_HINTS = ("источник", "провенанс", "документ", "ссылка", "норма", "нормы", "основание", "первоисточник")
# Поля, где машинное решение недопустимо: цена ошибки — срок ввода в эксплуатацию или деньги.
_HUMAN_FIELDS = ("срок", "дата", "дедлайн", "ввод_в_эксплуатацию", "стоимость", "сумма", "бюджет",
                 "решение", "мероприятие", "мероприятия")


def severity(value) -> int:
    """Насколько значение «тяжёлое». 0 — шкала не распознана, правило тяжести не применимо."""
    s = str(value or "").strip().lower()
    if not s:
        return 0
    for words, w in _SEVERITY:
        for w_ in words:
            if s == w_ or s.startswith(w_) or w_ in s:
                return w
    return 0


def _num(value):
    try:
        return float(str(value).replace(",", ".").replace(" ", "").replace(" ", ""))
    except Exception:  # noqa: BLE001
        return None


def _field_value(variant: dict, field: str):
    v = variant.get("value")
    if isinstance(v, dict):
        return v.get(field)
    return v


def _authors(variant: dict) -> list[str]:
    return [str(a) for a in (variant.get("authors") or ([variant["author"]] if variant.get("author") else []))]


def _provenance_weight(variant: dict) -> int:
    """Сколько признаков первоисточника у варианта: ссылка на документ, норму, основание."""
    v = variant.get("value")
    if not isinstance(v, dict):
        return 0
    n = 0
    for k, val in v.items():
        if val in (None, "", [], {}):
            continue
        kl = str(k).lower()
        if any(h in kl for h in _SOURCE_HINTS):
            n += 1
    return n


# ── Правила ──────────────────────────────────────────────────────────────────────────────────

def by_majority(variants: list[dict], field: str = "") -> tuple | None:
    """Большинство ветвей. Ничья решением не является: спор идёт выше."""
    counts = [(len(_authors(v)), v) for v in variants]
    counts.sort(key=lambda kv: -kv[0])
    if len(counts) < 2 or counts[0][0] <= counts[1][0]:
        return None
    v = counts[0][1]
    val = _field_value(v, field) if field else v.get("value")
    return val, f"за этот вариант {counts[0][0]} ветви из {sum(c for c, _ in counts)}: " + ", ".join(_authors(v))


def by_severity(variants: list[dict], field: str = "") -> tuple | None:
    """Наиболее тяжёлый вариант. Применимо, только если шкала распознана и варианты по ней различаются."""
    scored = []
    for v in variants:
        s = severity(_field_value(v, field) if field else v.get("value"))
        if not s:
            return None
        scored.append((s, v))
    scored.sort(key=lambda kv: -kv[0])
    if scored[0][0] == scored[-1][0]:
        return None
    v = scored[0][1]
    val = _field_value(v, field) if field else v.get("value")
    return val, (f"выбран более тяжёлый вариант «{val}» (ветви: {', '.join(_authors(v))}) — "
                 "расхождение в статусе разрешается в сторону проблемы, а не благополучия")


def by_edge(variants: list[dict], field: str = "", prefer: str = "max") -> tuple | None:
    """Числовой спор по краю: перерасход и трудозатраты берём по большему, иначе занизим проблему."""
    nums = []
    for v in variants:
        n = _num(_field_value(v, field) if field else v.get("value"))
        if n is None:
            return None
        nums.append((n, v))
    if len({n for n, _ in nums}) < 2:
        return None
    nums.sort(key=lambda kv: kv[0], reverse=(prefer != "min"))
    n, v = nums[0]
    other = ", ".join(str(x) for x, _ in nums[1:])
    return (_field_value(v, field) if field else v.get("value")), \
        f"числа разошлись ({n} против {other}) — взято {'большее' if prefer != 'min' else 'меньшее'} значение"


def by_provenance(variants: list[dict], field: str = "") -> tuple | None:
    """Вариант с первоисточником весит больше: утверждение со ссылкой проверяемо, без ссылки — нет."""
    scored = sorted(((_provenance_weight(v), v) for v in variants), key=lambda kv: -kv[0])
    if len(scored) < 2 or scored[0][0] == 0 or scored[0][0] == scored[1][0]:
        return None
    v = scored[0][1]
    val = _field_value(v, field) if field else v.get("value")
    return val, (f"у варианта ветвей {', '.join(_authors(v))} есть ссылка на первоисточник "
                 f"(признаков: {scored[0][0]}), у остальных — нет")


_RULES = {"majority": by_majority, "severity": by_severity, "edge": by_edge, "provenance": by_provenance}
# Порядок по умолчанию: сначала согласие большинства, затем объяснимые правила, и только потом человек.
DEFAULT_ORDER = ("majority", "severity", "edge", "provenance")


def needs_human(field: str) -> bool:
    """Поля, где машинное решение недопустимо: срок, деньги, предлагаемое мероприятие."""
    f = str(field or "").lower()
    return any(h in f for h in _HUMAN_FIELDS)


# ── Разрешение ───────────────────────────────────────────────────────────────────────────────

def resolve(conflict: dict, *, order=DEFAULT_ORDER, prefer: str = "max") -> dict:
    """Разрешить одно расхождение правилами. Не получилось — решение за человеком, и это видно.

    Спор ведётся о конкретном поле, если доска его указала: тогда сравниваются значения этого поля, а
    не записи целиком — иначе правило тяжести не находит шкалы и всё уходит человеку.
    """
    variants = [v for v in (conflict.get("variants") or []) if isinstance(v, dict)]
    fields = [str(f) for f in (conflict.get("fields") or [])]
    field = fields[0] if len(fields) == 1 else ""
    base = {"key": conflict.get("key"), "item": conflict.get("item") or "", "field": field,
            "fields": fields, "variants": [{"value": v.get("value"), "authors": _authors(v)} for v in variants]}
    if len(variants) < 2:
        return dict(base, by="", chosen=None, reason="спора нет: вариант один", needs_human=False)
    if len(fields) > 1:
        return dict(base, by="human", chosen=None, needs_human=True,
                    reason="ветви разошлись сразу в нескольких полях (" + ", ".join(fields) +
                           ") — машинное правило тут выберет наугад, нужен человек")
    if field and needs_human(field):
        return dict(base, by="human", chosen=None, needs_human=True,
                    reason=f"поле «{field}» меняет смысл результата (срок, деньги, предлагаемое "
                           "мероприятие) — решение за человеком")
    for name in order:
        fn = _RULES.get(name)
        if not fn:
            continue
        got = fn(variants, field, prefer) if name == "edge" else fn(variants, field)
        if got:
            val, why = got
            return dict(base, by="rule:" + name, chosen=val, reason=why, needs_human=False)
    return dict(base, by="human", chosen=None, needs_human=True,
                reason="ни одно правило не различает варианты — решение за человеком")


def resolve_all(conflicts: list[dict], *, order=DEFAULT_ORDER, prefer: str = "max") -> list[dict]:
    return [resolve(c, order=order, prefer=prefer) for c in (conflicts or []) if isinstance(c, dict)]


async def resolve_by_skill(conflict: dict, ask, *, skill: str = "арбитр") -> dict:
    """Спросить навык-арбитр. Ответ не принимается на веру: выбор должен совпасть с одним из вариантов.

    Иначе модель «примиряет» варианты новым значением, которого ни одна ветвь не утверждала, — это
    выдумка, и в результат она попасть не должна.
    """
    variants = [v for v in (conflict.get("variants") or []) if isinstance(v, dict)]
    base = resolve(conflict)
    if len(variants) < 2 or not ask:
        return base
    field = base.get("field") or ""
    task = ("Ветви разошлись" + (f" в поле «{field}»" if field else "") +
            (f" по записи «{conflict.get('item')}»" if conflict.get("item") else "") +
            ". Выбери НОМЕР варианта, который подтверждён данными, и объясни одной фразой. "
            "Новых значений не придумывай.\n" +
            "\n".join(f"{i + 1}. от {', '.join(_authors(v))}: " +
                      json.dumps(_field_value(v, field) if field else v.get("value"),
                                 ensure_ascii=False, default=str)[:600]
                      for i, v in enumerate(variants)))
    try:
        ans = await ask(task)
    except Exception as e:  # noqa: BLE001 — недоступный арбитр не должен ронять прогон
        return dict(base, by="human", chosen=None, needs_human=True,
                    reason=f"навык-арбитр недоступен ({str(e)[:80]}) — решение за человеком")
    text = str((ans or {}).get("text") if isinstance(ans, dict) else ans or "")
    pick, why = _parse_pick(text, len(variants))
    if pick is None:
        return dict(base, by="human", chosen=None, needs_human=True,
                    reason="арбитр не указал вариант однозначно — решение за человеком")
    v = variants[pick]
    val = _field_value(v, field) if field else v.get("value")
    return dict(base, by="skill:" + skill, chosen=val, needs_human=False,
                reason=(why or "выбор навыка-арбитра") + f" (вариант {pick + 1}: {', '.join(_authors(v))})")


def _parse_pick(text: str, n: int) -> tuple[int | None, str]:
    """Номер варианта из ответа арбитра. Два номера в ответе — это не выбор."""
    import re
    t = str(text or "")
    try:
        o = json.loads(t[t.find("{"):t.rfind("}") + 1])
        for k in ("вариант", "выбор", "choice", "pick"):
            if str(o.get(k) or "").strip().isdigit():
                i = int(str(o[k]).strip()) - 1
                if 0 <= i < n:
                    return i, str(o.get("обоснование") or o.get("reason") or "")
    except Exception:  # noqa: BLE001 — ответ мог прийти прозой, разбираем ниже
        pass
    nums = {int(m) for m in re.findall(r"\b([1-9])\b", t[:300]) if 1 <= int(m) <= n}
    if len(nums) == 1:
        i = nums.pop() - 1
        return i, t.strip()[:200]
    return None, ""


# ── Применение решений ───────────────────────────────────────────────────────────────────────

def apply_to_rows(rows: list[dict], decisions: list[dict]) -> int:
    """Проставить решения в сведённые записи: значение поля и отметка, кем и почему.

    Нерешённое расхождение остаётся видимым: запись помечается «ждёт человека», а не подчищается.
    """
    if not rows or not decisions:
        return 0
    n = 0
    for d in decisions:
        item, field = str(d.get("item") or ""), str(d.get("field") or "")
        for r in rows:
            if not isinstance(r, dict):
                continue
            if item and item not in _row_ids(r):
                continue
            if d.get("needs_human") or d.get("chosen") is None:
                r.setdefault("_арбитраж", {})[field or d.get("key") or "—"] = {
                    "решение": "ждёт человека", "почему": d.get("reason")}
            else:
                if field:
                    r[field] = d["chosen"]
                r.setdefault("_арбитраж", {})[field or d.get("key") or "—"] = {
                    "решение": d["chosen"], "кем": d.get("by"), "почему": d.get("reason")}
            n += 1
    return n


def _row_ids(row: dict) -> str:
    """Все опознаваемые части записи одной строкой: решение ищет свою запись по идентификатору."""
    from . import blackboard as bb
    own = bb._record_id(row)
    extra = " ".join(str(v) for k, v in row.items()
                     if not isinstance(v, (dict, list)) and v not in (None, "", [], {}))
    return own + " " + extra


def report(decisions: list[dict]) -> dict:
    """Итог арбитража для метрик прогона и сводки группы."""
    rules = sum(1 for d in decisions if str(d.get("by") or "").startswith("rule:"))
    skills = sum(1 for d in decisions if str(d.get("by") or "").startswith("skill:"))
    human = [d for d in decisions if d.get("needs_human")]
    return {"schema": "abop.arbitration/1.0", "total": len(decisions),
            "by_rule": rules, "by_skill": skills, "needs_human": len(human),
            "open": [{"key": d.get("key"), "item": d.get("item"), "field": d.get("field"),
                      "reason": d.get("reason")} for d in human[:20]],
            "decisions": decisions,
            "note": ("расхождений нет" if not decisions else
                     f"расхождений {len(decisions)}: правилом {rules}" +
                     (f", навыком-арбитром {skills}" if skills else "") +
                     (f", ждут человека {len(human)}" if human else ""))}
