"""Оркестратор: одно решение «кого звать и что собрать» — и запись, почему именно так.

До этого цепочка решений была размазана: достаточность описания спрашивал один код, подбор агента —
другой, сборку из навыков — третий, форму отчёта — четвёртый. Каждый канал (чат десктопа, веб, крон,
шина) сшивал эту последовательность сам, и они расходились. Сегодняшний случай ровно об этом:
карточка в чате держала план, собранный до правки подбора, и человек запустил устаревший.

Здесь решение принимается ОДИН раз и целиком, чистой функцией: на входе факты (каталог навыков, что
есть в данных, подбор по агентам, известные раскладки), на выходе — что делать и почему. Никаких
обращений к базе: факты собирает вызывающая сторона, поэтому решение воспроизводимо и проверяемо
тестом, а не только живым прогоном.

«Почему» — не украшение. За два дня подбор дважды выбрал не тот навык (статус-отчёт вместо оценки
идеи, разбор почты вместо нарезки тикетов), и оба раза это заметили только потому, что было видно,
кто отработал. Незаметный оркестратор выдал бы гладкий документ не по задаче — и ему бы поверили.
Поэтому каждое решение несёт трассу: что выбрано, с каким счётом, что отброшено и по какой причине.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import planner

# Нижняя планка подбора готового агента: ниже этого совпадение — шум, агента даже не рассматриваем.
# Сама по себе планка исполнителя НЕ выбирает: счёт подбора агента и счёт подбора навыка считаются
# по-разному и в одной шкале не сравниваются. Проверено на живом запросе: «проверь идею сервиса…»
# давало агента «Финаналитик» со счётом выше порога, хотя собрать нужно было оценку идеи.
AGENT_SURE = 0.32
# Ниже этого агента даже не упоминаем: совпадение на уровне шума.
AGENT_FLOOR = 0.25
# Навыков в плане больше одного — документ собирает редактор отчёта.
EDITOR_FROM = 2
# Какую долю плана готовый агент должен уметь, чтобы звать его, а не собирать заново. Строгое
# «умеет ВСЁ» давало дубликаты: на «проведи аудит данных 1С» агент аудита покрывал два шага из трёх
# и признавался «делающим другое» — человек получал предложение собрать второго такого же агента.
# Требование к главному шагу остаётся жёстким: агент, не умеющий ГЛАВНОГО, не исполнитель.
AGENT_COVER = 0.6
# Ниже этого совпадения выбор навыка — догадка, а не решение. Догадку честнее превратить в вопрос:
# на проде «проверь идею ниже — идея сервиса…» дало цепочку аудита 1С со счётом 0.2 — уверенно
# неверный ответ, который стоил бы реального прогона. Порог кандидатности (0.08) тут не годится: он
# отвечает на вопрос «рассматривать ли вообще», а не «можно ли на этом строить план».
SKILL_SURE = 0.30
# Счёт подбора — величина относительная: на длинной формулировке своими словами даже точный навык
# набирает 0.2, потому что слов в запросе много, а совпадают не все. Поэтому планка не единственный
# признак решения: лидер, который ОТОРВАЛСЯ от следующего кандидата, — это выбор, а не догадка.
# Проверено на живом запросе «проанализируй идею сервиса…»: idea-scorer 0.215 против 0.143 у
# следующего — по абсолютной планке это был бы вопрос, хотя навык найден верно.
SKILL_LEAD = 1.4      # во сколько раз лидер должен опережать следующего
SKILL_FLOOR = 0.15    # ниже этого отрыв уже ни о чём не говорит


_SMALL_TALK = ("привет", "здравств", "добрый день", "доброе утро", "добрый вечер", "как дела",
               "спасибо", "благодарю", "пока", "до свидания", "ты кто", "кто ты", "что умеешь",
               "как ты", "ок", "окей", "ясно", "понятно")


def _is_small_talk(t: str) -> bool:
    """Реплика без работы: приветствие, благодарность, вопрос о самом помощнике.

    Проверяем только КОРОТКИЕ фразы: «спасибо, а теперь сверь проводки с первичкой» — это задача,
    и отвечать на неё болтовнёй нельзя.
    """
    low = str(t or "").strip().lower()
    if len(low) > 60:
        return False
    return any(low.startswith(w) or low == w for w in _SMALL_TALK)


@dataclass
class Decision:
    """Что делать с задачей и почему. `kind`: ask | agent | build | chat."""
    kind: str
    task: str = ""
    questions: list = field(default_factory=list)      # ask: чего не хватает в описании
    agent_id: str = ""                                 # agent: кого запускать
    agent_name: str = ""
    alternatives: list = field(default_factory=list)   # agent: кто ещё похож
    skills: list = field(default_factory=list)         # build: из чего собрать исполнителя
    # Вторая возможность, которую человек вправе выбрать вместо предложенной. Решение одно, но
    # выбор остаётся за человеком: раньше «запустить готового» и «собрать из навыков» жили в разных
    # карточках, подбирались разными механизмами и не знали друг о друге.
    alt_skills: list = field(default_factory=list)     # agent: чем это же можно собрать
    alt_agent: dict = field(default_factory=dict)      # build: кто из готовых был близок
    gaps: list = field(default_factory=list)           # agent: чего он из плана НЕ делает
    editor: str = ""                                   # build: "added" | "cached" | ""
    form: str = ""                                     # ожидаемая форма документа
    why: list = field(default_factory=list)            # трасса решения, человеческими словами
    facts: dict = field(default_factory=dict)          # числа решения: счёт, подсказки, порог

    def as_dict(self) -> dict:
        return {"kind": self.kind, "task": self.task, "questions": self.questions,
                "agent_id": self.agent_id, "agent_name": self.agent_name,
                "alternatives": self.alternatives, "skills": self.skills, "editor": self.editor,
                "form": self.form, "why": self.why, "facts": self.facts,
                "alt_skills": self.alt_skills, "alt_agent": self.alt_agent, "gaps": self.gaps}


def decide(task: str, *, catalog: dict, entities: set, slots: set, matches: list,
           plan: dict | None = None, cached_layout: bool = False, hints: dict | None = None,
           max_steps: int = 4) -> Decision:
    """Решение по задаче. Факты на входе, никаких походов в базу.

    `matches` — подбор по существующим агентам (как его отдаёт подбор агентов: id, name, score).
    `plan` — готовый план из навыков, если вызывающая сторона его уже считала; иначе посчитаем здесь.
    `cached_layout` — для набора навыков плана раскладка отчёта уже известна (редактор не нужен).
    """
    t = str(task or "").strip()
    d = Decision(kind="chat", task=t)
    if not t:
        d.why.append("задача пустая — нечего решать")
        return d
    # Приветствие — не работа: спрашивать в ответ «что нужно сделать?» грубо и бессмысленно,
    # человек просто поздоровался. Отвечаем в чате, как и на любую реплику без работы.
    if _is_small_talk(t):
        d.why.append("это реплика, а не задача — отвечаем в чате")
        return d

    top = (matches or [{}])[0] if matches else {}
    score = float(top.get("score") or 0)
    # Первый по похожести — не обязательно тот, кто УМЕЕТ. «Черновик ответа в БФТ» выигрывал
    # «Мой менеджер-агент» (0.85, умеет bft-draft и письмо клиенту), тогда как «Аналитик БФТ» (0.73)
    # закрывал задачу целиком. Похожесть ранжирует кандидатов, решает — умение.
    d.facts["подбор_агента"] = round(score, 3)
    d.facts["планка_агента"] = AGENT_SURE

    # План считаем ВСЕГДА, даже когда готовый агент выглядит подходящим: решение «звать готового или
    # собирать» принимается сравнением сопоставимого — что умеет агент против того, что нужно задаче.
    # Подбор детерминированный, модель тут не участвует, так что лишнего вызова это не стоит.
    p = plan if isinstance(plan, dict) else planner.plan(
        t, catalog, entities=set(entities or ()), slots=set(slots or ()),
        max_steps=max_steps, hints=hints or {})
    steps = [str(s.get("skill") or "") for s in (p.get("steps") or []) if s.get("skill")]

    # 1. Готовый агент — если он прошёл планку И УМЕЕТ то, что нужно задаче: его навыки покрывают
    #    план. Сравниваем объявленное с объявленным, а не два счёта из разных шкал. Пустой план с
    #    подходящим агентом — тоже его случай: собирать всё равно нечего.
    # Покрытие считаем долей, а не строгим вложением: план дробит работу мельче, чем её объявляет
    # агент (у аудита 1С три навыка против двух объявленных), и строгое правило объявляло умеющего
    # агента «делающим другое».
    need = [x for x in steps if x]

    def _fit(m: dict) -> tuple[float, list, bool]:
        """Доля плана, которую агент умеет, что именно умеет и закрыт ли ГЛАВНЫЙ шаг."""
        have = {str(x) for x in (m.get("skills") or [])}
        got = [x for x in need if x in have]
        r = (len(got) / len(need)) if need else 1.0
        return r, got, ((not need) or (need[0] in have))

    # Среди прошедших планку берём того, кто покрывает план лучше всех; при равенстве — похожего.
    fits = []
    for m in (matches or []):
        sc = float(m.get("score") or 0)
        if sc < AGENT_SURE:
            continue
        r, got, lead_ok = _fit(m)
        if (bool(need) and lead_ok and r >= AGENT_COVER) or (not steps):
            fits.append((r, sc, m, got))
    fits.sort(key=lambda x: (-x[0], -x[1]))
    if fits:
        ratio, score, top, covered = fits[0][0], fits[0][1], fits[0][2], fits[0][3]
        cover = {str(x) for x in (top.get("skills") or [])}
        covers = True
    else:
        cover = {str(x) for x in (top.get("skills") or [])}
        ratio, covered, _lead = _fit(top)
        covers = False
    if need:
        d.facts["покрытие_агента"] = round(ratio, 2)
    if score >= AGENT_SURE and (covers or not steps):
        d.kind = "agent"
        d.agent_id = str(top.get("id") or "")
        d.agent_name = str(top.get("name") or d.agent_id)
        d.alternatives = [{"id": m.get("id"), "name": m.get("name"), "score": round(float(m.get("score") or 0), 3)}
                          for m in (matches or [])[1:3] if float(m.get("score") or 0) >= AGENT_FLOOR]
        d.facts["покрывает_план"] = sorted(covered)
        d.gaps = sorted(set(need) - cover)
        # Вторая возможность: ровно те же навыки, собранные в нового исполнителя. Человек видит
        # обе и выбирает сам — это и есть «один механизм подбора» вместо двух несвязанных.
        d.alt_skills = list(need)
        d.why.append(f"готовый агент «{d.agent_name}» умеет то, что нужно задаче"
                     + (f" ({', '.join(covered)})" if covered else "")
                     + f"; совпадение {score:.2f}")
        if d.gaps:
            d.why.append("из плана он не делает: " + ", ".join(d.gaps)
                         + " — можно добавить шаги или собрать исполнителя из навыков")
        if d.alternatives:
            d.why.append("рядом были: " + ", ".join(f"{a['name']} {a['score']:.2f}" for a in d.alternatives))
        return d

    if score >= AGENT_SURE and steps and not covers:
        # Агент не подошёл — но человек вправе о нём знать и выбрать его сам.
        d.alt_agent = {"id": str(top.get("id") or ""), "name": str(top.get("name") or top.get("id") or ""),
                       "score": round(score, 3), "covers": sorted(covered)}
        # Самый коварный случай: агент похож по словам, но делает не то. Раньше он выигрывал по
        # порогу, и человек получал гладкий документ не по своей задаче.
        d.why.append(f"готовый агент «{top.get('name') or top.get('id')}» похож ({score:.2f}), но делает другое: "
                     f"задаче нужны {', '.join(steps)}, а он умеет "
                     + (", ".join(sorted(cover)) if cover else "другое"))
    elif score > 0:
        d.why.append(f"готовый агент не выбран: лучшее совпадение {score:.2f} < планки {AGENT_SURE}")
    else:
        d.why.append("похожих агентов нет")

    if p.get("need_more"):
        d.kind = "ask"
        d.questions = list(((p.get("sufficiency") or {}).get("вопросы")) or [])
        d.why.append("описания не хватает, чтобы выбрать исполнителя: " + str(p.get("note") or ""))
        return d

    if not steps:
        d.why.append("исполнителя под такую задачу в каталоге нет: " + str(p.get("note") or ""))
        d.facts["не_хватает"] = list(p.get("missing") or [])[:4]
        return d

    # Слабое совпадение лидера — не решение, а догадка: спрашиваем, вместо того чтобы собрать
    # красивую цепочку не по задаче. Те же слова, что и у агента: не угадываем исполнителя.
    lead = max((float(st.get("score") or 0) for st in (p.get("steps") or [])), default=0.0)
    runner_up = float((p.get("runner_up") or {}).get("score") or 0)
    # Отрыв считается только когда второй кандидат ИЗВЕСТЕН: без него «ноль у следующего» — это
    # отсутствие данных, а не отрыв. Иначе любой слабый лидер выглядел бы уверенным.
    clear = lead >= SKILL_FLOOR and runner_up > 0 and lead >= runner_up * SKILL_LEAD
    d.facts["совпадение_навыка"] = round(lead, 3)
    d.facts["планка_навыка"] = SKILL_SURE
    if runner_up:
        d.facts["следующий_кандидат"] = round(runner_up, 3)
    if lead and lead < SKILL_SURE and not clear:
        d.kind = "ask"
        d.questions = ["Что именно сделать с этим? По описанию подходит слишком много навыков — "
                       "назовите действие или предмет работы.",
                       "Ближе всего: " + ", ".join(
                           f"{st.get('title') or st.get('skill')}" for st in (p.get("steps") or [])[:3])]
        d.facts["догадка"] = [str(st.get("skill")) for st in (p.get("steps") or [])][:4]
        d.why.append(f"лучшее совпадение навыка {lead:.2f} < планки {SKILL_SURE}"
                     + (f" и отрыв от следующего ({runner_up:.2f}) мал" if runner_up else "")
                     + ": это догадка, а не выбор — спрашиваем вместо сборки")
        d.why.append("на чём остановился подбор: " + " → ".join(steps))
        return d

    d.kind = "build"
    d.skills = steps
    d.form = str(p.get("report_template") or "")
    d.facts["шагов"] = len(steps)
    d.facts["не_хватает"] = list(p.get("missing") or [])[:4]
    d.why.append("собираем исполнителя из навыков: " + " → ".join(steps))
    if lead < SKILL_SURE and clear:
        d.why.append(f"совпадение {lead:.2f} ниже планки, но лидер оторвался от следующего "
                     f"({runner_up:.2f}) — это выбор, а не догадка")
    for s in (p.get("steps") or []):
        if s.get("why"):
            d.why.append(f"  {s.get('skill')}: {s.get('why')}")

    # 3. Редактор отчёта: при нескольких навыках документ собирает он, но только если раскладки для
    #    этого набора ещё нет — вызов модели нужен на новое сочетание, а не на каждый запуск.
    if len(steps) >= EDITOR_FROM:
        d.editor = "cached" if cached_layout else "added"
        d.why.append("раскладка отчёта для этого набора уже известна — редактор не нужен"
                     if cached_layout else
                     "навыков больше одного: добавляем редактор отчёта, он сложит разделы в один документ")
    return d


def explain(d: Decision) -> str:
    """Одна строка для человека: что сделано. Подробности — в `why`, их показывают по запросу.

    Нативность на входе не означает непрозрачности на выходе: человек не выбирает навыки, но должен
    иметь возможность проверить выбор.
    """
    if d.kind == "ask":
        return "Нужно уточнить задачу: " + "; ".join(d.questions[:2])
    if d.kind == "agent":
        return f"Запускаю готового агента «{d.agent_name}»"
    if d.kind == "build":
        ed = {"added": " + редактор отчёта", "cached": ""}.get(d.editor, "")
        return f"Собираю исполнителя из навыков ({len(d.skills)}){ed}: " + " → ".join(d.skills)
    return "Отвечаю в чате: отдельный исполнитель не нужен"
