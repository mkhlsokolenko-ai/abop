"""Динамическая сборка цепочки по контрактам навыков (этап 8).

Чем отличается от подбора по словам. Подбор ищет агента, похожего на фразу: у него есть словарь слов
и сравнение по смыслу. Он отвечает на вопрос «кто похож», но не на вопрос «а сможет ли этот кто-то
выполниться». Навык может требовать сущность, которой в среде нет, или выход другого навыка, который
никто не поставит, — и это выясняется посреди прогона, когда деньги уже потрачены.

Планировщик решает по ОБЪЯВЛЕННЫМ контрактам. Он берёт задачу, находит навыки-кандидаты, проверяет
покрытие входов по фактическому состоянию среды (какие сущности наполнены, какие предметы известны,
что отдают предыдущие шаги) и достраивает недостающие звенья теми навыками, которые их производят.
Цепочка получается исполнимой по построению, а если исполнимой цепочки нет — планировщик говорит,
чего именно не хватает, вместо того чтобы собрать красивую и упасть на середине.
"""
from __future__ import annotations

import re

from . import skill_contract as sc

# Сколько звеньев планировщик достраивает, разрешая зависимости. Глубже пяти цепочка перестаёт быть
# понятной человеку, а выигрыш сомнителен: это уже не план, а угадывание.
MAX_DEPTH = 5
# Сколько кандидатов рассматриваем на шаг. Больше — дольше и без пользы: разница в хвосте рейтинга
# уже случайная.
MAX_CANDIDATES = 6

_STOP = {"и", "в", "на", "по", "с", "для", "из", "что", "как", "мне", "нам", "где", "за", "от", "до",
         "это", "все", "всех", "весь", "надо", "нужно", "сделай", "сделать", "покажи", "посмотри"}


# Грубое усечение окончаний. Без него «разбери почту» не находит навык, в методике которого
# написано «почта» и «письма»: для совпадения нужна одна и та же словоформа, а люди её не угадывают.
# Полноценная морфология тут не нужна и стоила бы отдельной зависимости.
# Длина общего начала, при которой считаем слова одним понятием. Четыре буквы — компромисс:
# «сверь»/«сверка», «почту»/«почта», «решениям»/«решений» сходятся, а «дорожный»/«договорный» нет.
STEM_LEN = 4


def _stem(w: str) -> str:
    """Начало слова вместо попытки отрезать окончание.

    Правильно срезать русское окончание без словаря нельзя: «сверь» и «сверка» дают разные корни при
    любом наборе правил. Сравнение по общему началу решает ту же задачу честнее и предсказуемее.
    """
    return w[:STEM_LEN] if len(w) > STEM_LEN else w


def _raw_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яёa-z0-9-]{3,}", str(text or "").lower()) if w not in _STOP}


def _words(text: str) -> set[str]:
    """Слова задачи без служебных, с усечёнными окончаниями: по ним считаем близость к методике."""
    return {_stem(w) for w in _raw_words(text)}


def _concepts(text: str) -> set[str]:
    """Понятия текста: сначала синоним сводим к канону, и только потом усекаем окончание.

    Порядок важен. «тикеты» — синоним «задачи»; если усечь раньше, в реестре понятий не найдётся ни
    «тикет», ни «задач», и связь потеряется.
    """
    return {_stem(sc.canonical(w)) for w in _raw_words(text)}


# Люди пишут многосоставные задачи одной фразой: «сверь план с фактом И сделай задачи в трекере».
# Одним мешком слов такую фразу не разобрать — лидер забирает всё, а второй этап теряется.
_SPLIT = re.compile(r"\s*(?:,\s*(?:а\s+)?(?:затем|потом|после\s+чего)|;|\.\s+|и\s+(?=[а-яё]{3,}\s)|а\s+также|затем|потом|после\s+чего)\s*", re.I)


def clauses(task: str) -> list[str]:
    """Этапы задачи. Слишком короткие куски не считаем этапом: «и» внутри перечисления не делит работу."""
    parts = [p.strip(" ,;.") for p in _SPLIT.split(str(task or "")) if p and len(p.strip()) > 12]
    return parts or ([str(task or "").strip()] if str(task or "").strip() else [])


def _skill_text(sid: str, meta: dict) -> str:
    """Всё, чем навык себя описывает: название, назначение, методика, имена полей результата."""
    parts = [sid, str(meta.get("title") or ""), str(meta.get("short") or ""), str(meta.get("body") or "")]
    for it in sc.produces_list(meta.get("produces")):
        parts.append(str(it.get("path") or ""))
        parts.append(str(it.get("key") or ""))
    return " ".join(parts)


def _index(catalog: dict) -> dict:
    """Словарь каталога с весом редкости слова.

    Простой подсчёт совпадений не работает: слово «данные» есть почти в каждой методике и тянет на
    себя любую задачу, а «счёт-фактура» встречается у одного навыка и должно решать. Вес — обратная
    частота: чем в меньшем числе навыков слово встречается, тем оно важнее.
    """
    import math
    docs = {sid: _words(_skill_text(sid, m)) for sid, m in catalog.items()}
    df: dict[str, int] = {}
    for words in docs.values():
        for w in words:
            df[w] = df.get(w, 0) + 1
    n = max(1, len(docs))
    idf = {w: math.log(1 + n / c) for w, c in df.items()}
    return {"docs": docs, "idf": idf}


def _match_score(task_words: set[str], sid: str, meta: dict, index: dict | None = None,
                 task_concepts: set[str] | None = None) -> float:
    """Насколько навык похож на задачу: доля ВЕСА слов задачи, найденного в методике навыка."""
    if not task_words:
        return 0.0
    if index:
        hay = index["docs"].get(sid) or set()
        idf = index["idf"]
    else:
        hay = _words(_skill_text(sid, meta))
        idf = {}
    if not hay:
        return 0.0
    w_of = (lambda w: idf.get(w, 1.0))
    total = sum(w_of(w) for w in task_words) or 1.0
    overlap = sum(w_of(w) for w in (task_words & hay)) / total

    # Упоминание — слабый признак: методику пишут людям, и слово «письмо» встречается у половины
    # навыков. Сильный признак — ЧТО навык отдаёт. Сравниваем через реестр понятий: «задачи» и
    # «тикеты» одно и то же, поэтому «сделай задачи» находит навык, который отдаёт тикеты.
    canon_out = set()
    for it in sc.produces_list((meta or {}).get("produces")):
        canon_out |= _concepts(str(it.get("path") or ""))
    canon_out |= _concepts(str((meta or {}).get("title") or ""))
    canon_task = task_concepts or set()
    gives = (len(canon_task & canon_out) / max(1, len(canon_task))) if canon_out and canon_task else 0.0

    # идентификатор навыка, названный прямо в задаче, — сильный сигнал
    direct = 0.35 if sid.lower() in " ".join(task_words) else 0.0
    # Название навыка — то, как его зовут люди. При равном счёте по методикам выигрывает тот, чьё имя
    # звучит в задаче: иначе ничью решал алфавитный порядок идентификатора.
    title_hit = 0.1 if (task_words & _words(str((meta or {}).get("title") or ""))) else 0.0
    # Однострочное назначение — самый выверенный текст о навыке: «план → тикеты» против «письма →
    # задачи» различает их там, где обе методики упоминают и то и другое.
    short_w = _words(str((meta or {}).get("short") or ""))
    short_hit = 0.12 * (len(task_words & short_w) / max(1, len(short_w))) if short_w else 0.0
    return 0.65 * overlap + 0.45 * gives + direct + title_hit + short_hit


def _producers(catalog: dict, want: str) -> list[str]:
    """Кто отдаёт нужный раздел результата. Сравниваем понятиями: «тикеты» и «задачи» — одно и то же."""
    target = _concepts(want) or {want}
    out = []
    for sid, meta in catalog.items():
        for it in sc.produces_list(meta.get("produces")):
            path = str(it.get("path") or "")
            if path and (_concepts(path) & target):
                out.append(sid)
                break
    return out


def _needs(meta: dict) -> dict:
    """Что навыку нужно: сущности, предметы, выходы других навыков."""
    req = (meta.get("inputs") or {}).get("required") or []
    return {
        "entities": [str(i.get("entity")) for i in req if i.get("from") == "data" and i.get("entity")],
        "slots": [str(i.get("name")) for i in req if i.get("from") == "slot" and i.get("name")],
        "skills": [str(i.get("skill")) for i in req if i.get("from") == "skill" and i.get("skill")],
        "board": [str(i.get("key")) for i in req if i.get("from") == "board" and i.get("key")],
    }


# ── Достаточность запроса ───────────────────────────────────────────────────────────────────
# Порог осознанный: пяти значащих слов хватает, чтобы в фразе были действие, предмет и результат.
# Ниже — угадывание. Длинную фразу (от 100 знаков) с узнаваемым словом каталога пропускаем даже при
# формально малом числе слов: там уже есть контекст.
MIN_WORDS = 5
LONG_ENOUGH = 100
# Код предмета в тексте («PRJ-2451», «РТ-0008») — сильный сигнал: человек назвал, с чем работать.
_CODE = re.compile(r"\b[A-ZА-Я]{2,}[-–]\d{2,}\b")
# Действия, по которым видно, ЧТО делать. Без действия запрос — тема, а не задача.
_ACTIONS = ("сверь", "сравн", "провер", "разбер", "собер", "собрать", "напиш", "составь", "посчит",
            "оцен", "найд", "выяв", "объясн", "подготов", "сдела", "сформир", "рассчит", "свед",
            "проанализ", "аудит", "отправ", "создай", "обнов")
# Формы результата: что должно получиться.
_OUTCOMES = ("отчёт", "отчет", "задач", "тикет", "письм", "список", "таблиц", "находк", "план",
             "сводк", "выгрузк", "справк", "презентац", "требован", "черновик")


def sufficiency(task: str, catalog: dict, *, index: dict | None = None) -> dict:
    """Хватает ли в запросе описания, чтобы подбирать исполнителя.

    Возвращает {ok, words, почему, вопросы, подсказка, пример}. Когда ok=False, подбор делать не
    нужно: правильный ответ — доспросить человека.
    """
    text = str(task or "").strip()
    words = _raw_words(text)
    n = len(words)
    idx = index or _index(catalog)
    idf = idx["idf"]
    # Узнаваемое слово каталога: встречается у немногих навыков, значит действительно что-то называет.
    rare = sorted((w for w in _words(text) if idf.get(w, 0) > 1.6), key=lambda w: -idf.get(w, 0))
    has_code = bool(_CODE.search(text))
    low = text.lower()
    has_action = any(a in low for a in _ACTIONS)
    has_outcome = any(o in low for o in _OUTCOMES)

    # Одного счётчика слов мало: «проверь проводки и счета-фактуры в 1С» — четыре значащих слова,
    # но задача названа однозначно. Поэтому короткую фразу пропускаем, если в ней есть действие И
    # что-то узнаваемое: код предмета, форма результата или редкое слово каталога.
    ok = (n >= MIN_WORDS
          or (n >= 3 and has_action and (has_code or has_outcome or bool(rare)))
          or (len(text) >= LONG_ENOUGH and bool(rare)))
    if ok:
        return {"ok": True, "words": n, "rare": rare[:5], "has_code": has_code,
                "has_action": has_action, "has_outcome": has_outcome}

    # Чего не хватает — спрашиваем ровно об этом, а не «уточните запрос».
    ask = []
    if not has_action:
        ask.append("Что нужно сделать? Например: сверить, проверить, разобрать, собрать, написать.")
    if not (has_code or any(w in low for w in ("проект", "договор", "контрагент", "период", "почт", "1с"))):
        ask.append("По чему работаем? Назовите проект, договор, контрагента или период — можно кодом.")
    if not has_outcome:
        ask.append("Что должно получиться? Отчёт, список задач, письмо, таблица находок.")
    if not ask:
        ask.append("Добавьте подробностей: одной строки мало, чтобы выбрать исполнителя уверенно.")

    return {
        "ok": False, "words": n, "rare": rare[:5], "has_code": has_code,
        "has_action": has_action, "has_outcome": has_outcome,
        "почему": (f"в запросе {n} " + ("значащее слово" if n == 1 else
                                        "значащих слова" if 2 <= n <= 4 else "значащих слов") +
                   " — этого мало: под такое описание подходит слишком много навыков"),
        "вопросы": ask,
        "подсказка": "Опишите задачу как «что сделать» + «по чему» + «что должно получиться».",
        "пример": "Сверь дорожную карту проекта PRJ-2451 с отчётами подрядчиков и покажи, где сорваны сроки и почему",
    }


def plan(task: str, catalog: dict, *, entities: set[str], slots: set[str],
         max_steps: int = 4, hints: dict | None = None) -> dict:
    """Собрать исполнимую цепочку под задачу.

    catalog  — {skill_id: {title, short, body, inputs, produces, families, mode}}
    entities — сущности, в которых ЕСТЬ данные (пустые не считаются: навык на них не отработает)
    slots    — предметы работы, которые известны или будут уточнены у человека
    hints    — {skill_id: 0..1} подсказки от подбора по смыслу; берём максимум со словарным счётом,
               потому что совпадение по словам и совпадение по смыслу ловят разные формулировки

    Возвращает шаги в порядке исполнения, волны, чего не хватает и почему выбран каждый навык.
    """
    hints = hints or {}
    enough = sufficiency(task, catalog)
    if not enough["ok"]:
        # Не подбираем наугад: возвращаем ровно те вопросы, ответы на которые сделают запрос рабочим.
        return {"ok": False, "need_more": True, "steps": [], "waves": [], "missing": [],
                "sufficiency": enough, "note": enough["почему"]}
    stages = clauses(task)

    index = _index(catalog)

    def rank_for(text: str) -> list[tuple[float, str]]:
        tw, tc = _words(text), _concepts(text)
        r = sorted(((max(_match_score(tw, sid, m, index, tc), float(hints.get(sid) or 0)), sid)
                    for sid, m in catalog.items()), key=lambda x: (-x[0], x[1]))
        return [(v, sid) for v, sid in r if v > 0.08][:MAX_CANDIDATES]

    ranked = rank_for(task)
    if not ranked:
        return {"ok": False, "steps": [], "waves": [], "missing": ["ни один навык не похож на задачу"],
                "note": "исполнителя под такую задачу в каталоге нет"}

    chosen: list[dict] = []          # шаги в порядке добавления
    have_out: dict[str, dict] = {}   # что уже отдают выбранные навыки
    missing: list[str] = []

    def add(sid: str, why: str, depth: int = 0) -> bool:
        """Добавить навык, предварительно добавив тех, чьих выходов ему не хватает."""
        if any(st["skill"] == sid for st in chosen):
            return True
        meta = catalog.get(sid) or {}
        need = _needs(meta)
        # 1) сущности: данные либо есть, либо навык неисполним — это не повод его брать
        no_data = [e for e in need["entities"] if e not in entities]
        if no_data:
            missing.append(f"«{sid}»: нет данных сущности " + ", ".join(f"«{e}»" for e in no_data))
            return False
        # 2) предметы: неизвестный предмет не ошибка — его уточнит человек
        # 3) выходы других навыков: достраиваем поставщиков, пока не упрёмся в глубину
        for up in need["skills"]:
            if up in have_out:
                continue
            if depth >= MAX_DEPTH:
                missing.append(f"«{sid}»: цепочка глубже {MAX_DEPTH} звеньев — не разворачиваем")
                return False
            if up in catalog:
                if not add(up, f"нужен как вход для «{sid}»", depth + 1):
                    return False
            else:
                missing.append(f"«{sid}»: поставщика «{up}» нет в каталоге")
                return False
        for key in need["board"]:
            if not any(key in [str(x.get("path") or "") for x in sc.produces_list((catalog.get(st["skill"]) or {}).get("produces"))]
                       for st in chosen):
                producers = _producers(catalog, key)
                if depth < MAX_DEPTH and producers:
                    add(producers[0], f"даёт «{key}» на общую доску для «{sid}»", depth + 1)
        chosen.append({"skill": sid, "why": why,
                       "title": str(meta.get("title") or sid),
                       "slots": need["slots"],
                       "entities": need["entities"],
                       "after": [st["skill"] for st in chosen if st["skill"] in need["skills"]]})
        have_out[sid] = meta.get("produces")
        return True

    # Сначала по одному исполнителю на этап: многосоставная задача — это несколько работ, и лидер
    # общего рейтинга не должен забирать их все.
    given: set[str] = set()
    unclear: list[dict] = []
    for stage in stages:
        if len(chosen) >= max_steps:
            break
        # Правило достаточности действует и на ЭТАП, а не только на запрос целиком. «…, затем сделай
        # задачи в трекере» — два слова: задачи умеют делать несколько навыков, и выбрать из них
        # уверенно нельзя. Честнее сказать, что этап не определён, и спросить, чем гадать.
        if len(stages) > 1:
            st_enough = sufficiency(stage, catalog, index=index)
            if not st_enough["ok"]:
                unclear.append({"этап": stage, "почему": st_enough["почему"],
                                "вопросы": st_enough["вопросы"]})
                continue
        # Следующий этап продолжает предыдущий: навык, который умеет принять результат уже выбранного,
        # уместнее похожего по словам. Так «сверь план и факт, затем сделай задачи» даёт цепочку, а не
        # два независимых навыка про задачи.
        picked = {st["skill"] for st in chosen}
        ranked_stage = rank_for(stage)
        if picked:
            ranked_stage = sorted(
                ((v + (0.15 if set(_needs(catalog.get(sid) or {})["skills"]) & picked else 0.0), sid)
                 for v, sid in ranked_stage), key=lambda x: (-x[0], x[1]))
        for score, sid in ranked_stage:
            if any(st["skill"] == sid for st in chosen):
                break          # этап уже закрыт выбранным навыком
            outs = {str(it.get("path") or "") for it in sc.produces_list((catalog.get(sid) or {}).get("produces"))}
            if outs and outs <= given:
                continue       # такой же результат уже даёт выбранный навык
            if add(sid, f"этап «{stage[:48]}» (совпадение {round(score, 2)})"):
                given |= outs
                break

    # Достройка «вниз»: задача просит результат, которого никто из выбранных не отдаёт, а в каталоге
    # есть навык, который его делает ИЗ уже выбранного. «Нарежь тикеты по решениям сверки» — сверка
    # даёт решения, тикеты делает другой навык; без этого шага задача осталась бы недоделанной.
    task_concepts = _concepts(task)
    have_concepts: set[str] = set()
    for st in chosen:
        for it in sc.produces_list((catalog.get(st["skill"]) or {}).get("produces")):
            have_concepts |= _concepts(str(it.get("path") or ""))
    idf = index["idf"]
    for want in sorted(task_concepts - have_concepts):
        if len(chosen) >= max_steps + 1:
            break
        # Достраиваем только по ЗНАЧАЩЕМУ слову. Общие слова («данные», «работа») есть в половине
        # методик и тянут в план случайный навык, который ничего к результату не добавляет.
        if idf.get(want, 0) < 1.6:
            continue
        for sid in _producers(catalog, want):
            if any(st["skill"] == sid for st in chosen):
                continue
            need = _needs(catalog.get(sid) or {})
            # берём только то, что достраивается на уже выбранном: иначе это не достройка, а угадывание
            if need["skills"] and not all(any(st["skill"] == up for st in chosen) for up in need["skills"]):
                continue
            if add(sid, f"задача просит «{want}», его отдаёт этот навык"):
                for it in sc.produces_list((catalog.get(sid) or {}).get("produces")):
                    have_concepts |= _concepts(str(it.get("path") or ""))
                break

    # Добор по общей похожести — ТОЛЬКО если по этапам не выбралось ничего. Иначе план обрастает
    # похожими навыками, которые ничего не добавляют, а платим мы за каждый шаг.
    top = ranked[0][0] if ranked else 0.0
    for score, sid in (ranked if not chosen else []):
        if len(chosen) >= max_steps:
            break
        if chosen and score < 0.6 * top:
            continue
        outs = {str(it.get("path") or "") for it in sc.produces_list((catalog.get(sid) or {}).get("produces"))}
        if chosen and outs and outs <= given:
            continue
        if add(sid, f"похож на задачу (совпадение {round(score, 2)})"):
            given |= outs

    if not chosen:
        return {"ok": False, "steps": [], "waves": [], "missing": missing,
                "note": "подходящие навыки есть, но ни один не исполним в текущей среде"}

    # Волны: шаг идёт после тех, чьи выходы ему нужны.
    waves: list[list[str]] = []
    placed: set[str] = set()
    guard = 0
    while len(placed) < len(chosen) and guard < 20:
        guard += 1
        wave = [st["skill"] for st in chosen
                if st["skill"] not in placed and all(a in placed for a in st["after"])]
        if not wave:
            break
        waves.append(wave)
        placed |= set(wave)

    # Предметы, которые придётся уточнить у человека.
    ask = sorted({s for st in chosen for s in st["slots"] if s not in slots})
    return {"ok": True, "steps": chosen, "waves": waves, "missing": missing, "ask_slots": ask,
            "unclear": unclear,
            "note": (f"цепочка из {len(chosen)} навыков в {len(waves)} волнах"
                     + (f"; уточнить предмет: {', '.join(ask)}" if ask else "")
                     + (f"; этапов без исполнителя: {len(unclear)}" if unclear else "")
                     + (f"; отброшено: {len(missing)}" if missing else ""))}


def report_template(steps: list[dict], catalog: dict) -> str:
    """Шаблон отчёта под форму результата: по тому, что навыки реально отдают."""
    paths = set()
    for st in steps:
        for it in sc.produces_list((catalog.get(st.get("skill")) or {}).get("produces")):
            paths.add(str(it.get("path") or "").lower())
    if any("расследован" in p or "цепочк" in p for p in paths):
        return "invest"
    if any("находк" in p for p in paths) or any("audit1c" in str(st.get("skill")) for st in steps):
        return "audit1c"
    if any("задач" in p for p in paths):
        return "digest"
    return "default"
