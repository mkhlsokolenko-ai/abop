"""Контракт навыка: что ему нужно на входе, что он отдаёт и по какому ключу это соединяется.

Аудит 29.09.2026 вскрыл корень трёх проблем сразу: схема навыка описывает только ВЫХОД, а вход задан
прозой в инструкции. Из-за этого граф агента нечем исполнять по порядку (непонятно, что кому
передавать), совместимость шагов цепочки приходится угадывать по совпадению имён полей, а одно и то же
понятие в разных навыках названо по-разному («дебет» и «счёт_дт», «функциональная_работа» и
«функциональное»).

Здесь: формат контракта, реестр канонических имён полей и проверки. Модуль без побочных эффектов,
чтобы его одинаково использовали валидатор шаблонов, сборка агента и рантайм.

Формат в `skills/<sid>/template.json`:

    "inputs": {
      "required": [
        {"from": "data",    "entity": "roadmap_item", "fields": ["проект", "план_финиш"]},
        {"from": "skill",   "skill": "audit1c-checks", "path": "находки", "fields": ["id", "класс"]},
        {"from": "slot",    "name": "проект"},
        {"from": "context", "note": "письмо руководителя или задача трекера"}
      ],
      "optional": [ ... ]
    },
    "produces": {"path": "находки", "key": "id", "join": "находка"}

`produces.join` — имя сквозного ключа: по нему результат соединяется с результатами других навыков.
"""
from __future__ import annotations

# ── Реестр канонических имён полей ────────────────────────────────────────────────────────────
# Собран из фактических схем каталога (58 навыков, 1081 уникальное имя поля). Каноническое имя —
# то, что уже преобладает; синонимы перечислены, чтобы валидатор подсказывал замену, а планировщик
# считал такие поля совместимыми. Реестр не запрещает свои поля: он нормализует ОБЩИЕ понятия,
# по которым навыки соединяются друг с другом.
CANON: dict[str, tuple[str, ...]] = {
    "id":                  ("код", "номер", "uid", "идентификатор"),
    "название":            ("заголовок", "имя", "наименование"),
    "статус":              ("состояние",),
    "срок":                ("дедлайн", "дата_окончания", "когда_нужно"),
    "дата":                ("день",),
    "ответственный":       ("владелец", "исполнитель", "ответственное_лицо"),
    "основание":           ("почему", "обоснование", "доказательство", "подтверждение"),
    "источник":            ("откуда", "источник_данных"),
    "цитата":              ("выдержка", "фрагмент"),
    "критичность":         ("серьёзность", "существенность", "уровень_риска"),
    "приоритет":           ("ранг", "очерёдность"),
    "сумма":               ("сумма_руб", "сумма_₽", "объём_руб"),
    "период":              ("интервал",),
    "счёт_дт":             ("дебет", "дт", "счет_дт"),
    "счёт_кт":             ("кредит", "кт", "счет_кт"),
    "критерии_приёмки":    ("критерий_приёмки", "критерии_приемки"),
    "функциональная_работа": ("функциональное", "функциональная"),
    "итог":                ("вывод", "резюме_итога"),
    "комментарий":         ("примечание", "пояснение_кратко"),
    "риск":                ("угроза",),
    "метрика":             ("показатель",),
    "проект":              ("проект_код", "код_проекта"),
    "контрагент":          ("партнёр", "поставщик_или_покупатель"),
    # «Сделай задачи в трекере» и «нарежь тикеты» — одно и то же понятие. Без этой связки
    # планировщик не узнавал в задаче навык, который отдаёт тикеты.
    "задача":              ("задачи", "тикет", "тикеты", "issue", "задание"),
    "письмо":              ("письма", "email", "e-mail", "сообщение"),
    "находка":             ("находки", "расхождение", "расхождения", "нарушение"),
    "решение":             ("решения", "мероприятие", "мероприятия", "действие", "действия"),
}

# обратный индекс: синоним → каноническое имя
ALIAS: dict[str, str] = {}
for _canon, _syns in CANON.items():
    for _s in _syns:
        ALIAS[_s] = _canon

# Откуда навык берёт вход. «board» — объявленный ключ общей памяти прогона (так ветви видят
# выводы друг друга). «run» — ВЕСЬ прогон: привилегированное чтение, нужное редактору отчёта,
# который обязан видеть все разделы, чтобы сложить из них один документ. Привилегия объявлена
# в контракте, а не спрятана в коде: по контракту видно, кто читает больше остальных.
SOURCES = ("data", "skill", "slot", "context", "board", "run")


def canonical(field: str) -> str:
    """Каноническое имя поля (или само поле, если оно вне реестра)."""
    f = str(field or "").strip()
    return ALIAS.get(f, f)


def same_field(a: str, b: str) -> bool:
    """Два имени означают одно понятие (с учётом синонимов реестра)."""
    return canonical(a) == canonical(b)


# ── Валидация контракта ───────────────────────────────────────────────────────────────────────

def validate_inputs(inputs: dict | None) -> list[str]:
    """Ошибки описания входов. Пустой список — контракт корректен."""
    errs: list[str] = []
    if inputs in (None, {}, []):
        return errs
    if not isinstance(inputs, dict):
        return ["inputs должен быть объектом с ключами required и optional"]
    for bucket in ("required", "optional"):
        items = inputs.get(bucket)
        if items is None:
            continue
        if not isinstance(items, list):
            errs.append(f"inputs.{bucket} должен быть списком")
            continue
        for n, it in enumerate(items, 1):
            errs += _validate_one(it, f"inputs.{bucket}[{n}]")
    unknown = [k for k in inputs if k not in ("required", "optional")]
    if unknown:
        errs.append("лишние ключи в inputs: " + ", ".join(sorted(unknown)))
    return errs


def _validate_one(it, where: str) -> list[str]:
    if not isinstance(it, dict):
        return [f"{where}: вход описывается объектом"]
    src = str(it.get("from") or "").strip()
    if src not in SOURCES:
        return [f"{where}: from должен быть одним из {', '.join(SOURCES)}"]
    errs: list[str] = []
    if src == "data" and not str(it.get("entity") or "").strip():
        errs.append(f"{where}: для входа из данных нужна entity")
    if src == "skill" and not str(it.get("skill") or "").strip():
        errs.append(f"{where}: для входа из навыка нужен skill")
    if src == "slot" and not str(it.get("name") or "").strip():
        errs.append(f"{where}: для входа из слота нужно name")
    flds = it.get("fields")
    if flds is not None and (not isinstance(flds, list) or any(not isinstance(f, str) for f in flds)):
        errs.append(f"{where}: fields — список строк")
    return errs


def produces_list(produces) -> list[dict]:
    """Выходы навыка списком. Навык может отдавать несколько списков: сверка плана и факта даёт и
    «исполнение» по пунктам, и «решения» для руководителя, и следующий шаг берёт разные из них."""
    if not produces:
        return []
    if isinstance(produces, dict):
        return [produces]
    return [p for p in produces if isinstance(p, dict)]


def validate_produces(produces) -> list[str]:
    """Ошибки описания выхода: путь к списку и ключ соединения. Допустим объект или список объектов."""
    if produces in (None, {}, []):
        return []
    if not isinstance(produces, (dict, list)):
        return ["produces должен быть объектом или списком объектов"]
    errs: list[str] = []
    for n, p in enumerate(produces_list(produces), 1):
        where = "produces" if isinstance(produces, dict) else f"produces[{n}]"
        for k in ("path", "key", "join"):
            v = p.get(k)
            if v is not None and not isinstance(v, str):
                errs.append(f"{where}.{k} — строка")
        if p.get("key") and not p.get("path"):
            errs.append(f"{where}.key задан без path: непонятно, у какого списка ключ")
        unknown = [k for k in p if k not in ("path", "key", "join")]
        if unknown:
            errs.append(f"лишние ключи в {where}: " + ", ".join(sorted(unknown)))
    return errs


def validate_contract(template: dict) -> list[str]:
    """Полная проверка контракта шаблона: входы, выход и согласованность с json_schema."""
    t = template or {}
    errs = validate_inputs(t.get("inputs")) + validate_produces(t.get("produces"))
    sch = (t.get("json_schema") or {}).get("properties") or {}
    for p in produces_list(t.get("produces")):
        path = str(p.get("path") or "").strip()
        if path and sch and path not in sch:
            errs.append(f"produces.path «{path}» не найден в свойствах json_schema")
    # вход из слота обязан существовать среди объявленных слотов
    slot_names = {str(s.get("name") or "") for s in (t.get("slots") or []) if isinstance(s, dict)}
    for bucket in ("required", "optional"):
        for it in ((t.get("inputs") or {}).get(bucket) or []):
            if isinstance(it, dict) and it.get("from") == "slot" and str(it.get("name")) not in slot_names:
                errs.append(f"вход из слота «{it.get('name')}» не объявлен в slots")
    return errs


def field_warnings(template: dict) -> list[str]:
    """Подсказки по именам полей: где стоит использовать каноническое имя из реестра.
    Это предупреждения, а не ошибки: свои поля у навыка допустимы, реестр нормализует общие понятия."""
    out: list[str] = []
    seen: set[str] = set()

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        for k, v in (node.get("properties") or {}).items():
            c = ALIAS.get(k)
            if c and k not in seen:
                seen.add(k)
                out.append(f"поле «{k}» — синоним канонического «{c}»; для соединения шагов лучше «{c}»")
            if isinstance(v, dict):
                walk(v)
                walk(v.get("items") or {})

    walk((template or {}).get("json_schema") or {})
    return out


# ── Покрытие входов при сборке агента (этап 2 плана) ──────────────────────────────────────────

def coverage(skill_inputs: dict | None, *, entities: set[str], slots: set[str],
             upstream: dict[str, dict]) -> list[str]:
    """Чего не хватает навыку для запуска. Пустой список — вход покрыт.

    entities — сущности, доступные агенту; slots — заполненные слоты;
    upstream — {skill_id: produces} навыков, идущих раньше по графу.
    """
    miss: list[str] = []
    for it in ((skill_inputs or {}).get("required") or []):
        if not isinstance(it, dict):
            continue
        src = it.get("from")
        if src == "data":
            ent = str(it.get("entity") or "")
            if ent and ent not in entities:
                miss.append(f"нет данных сущности «{ent}»")
        elif src == "slot":
            nm = str(it.get("name") or "")
            if nm and nm not in slots:
                miss.append(f"не задан предмет «{nm}»")
        elif src == "skill":
            sid = str(it.get("skill") or "")
            if sid and sid not in upstream:
                miss.append(f"нет результата навыка «{sid}» до этого шага")
            else:
                outs = produces_list(upstream.get(sid))
                if not outs:
                    miss.append(f"навык «{sid}» не объявил, какой список отдаёт")
                elif it.get("path") and not any(o.get("path") == it["path"] for o in outs):
                    miss.append(f"навык «{sid}» не отдаёт список «{it['path']}»")
    return miss
