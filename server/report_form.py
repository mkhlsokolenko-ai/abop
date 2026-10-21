"""Бланк документа: раскладка вертикали живёт в БД рядом с формой, а не в коде отчёта.

Прежде каждая форма была одной и той же последовательностью плейсхолдеров — реквизиты, резюме,
навыки, находки, оговорка. Получатель кредитного заключения, ADR и делового письма читает разные
документы: у них разный порядок разделов, разные обязательные графы и разная мера ответственности.
Одинаковая последовательность блоков превращала все тринадцать вертикалей в один отчёт с другой
подписью снизу.

Образец взят у формы аудита 1С, согласованной с заказчиком: шапка с объёмом работы, затем находки
карточками с ЧЕТЫРЬМЯ ПОДПИСАННЫМИ графами (что не сходится / откуда / чем грозит / что проверить),
существенность бейджем, код находки и группа проверки. Там это собрано кодом под один навык; здесь
то же самое объявляется данными — чтобы бланк под вертикаль добавлялся правкой в БД, а не правкой
web_api.

Раскладка — список блоков. Блок знает, ОТКУДА брать значение (ссылка «путь.в.структуре» или
«навык:путь.в.структуре»), и КАК его назвать в документе. Поля берутся из структурированных ответов
навыков прогона (`result.skill_outputs[].structured`) — тех самых, что объявлены в json_schema
шаблона навыка. Выдумывать значения бланк не может: чего навык не дал, того в документе нет.

Пустой блок не рендерится вовсе: заголовок над пустотой читается как «тут ничего не нашли», хотя
данных просто не было. Исключение — таблица реквизитов: в служебном документе пустая графа значит
«не указано», и это сведение, а не оформление.
"""
from __future__ import annotations

import html as _html
import re

from . import report_store

_MAX_ROWS = 60                     # столько строк реестра показываем, остальное — счётчиком
_EMPTY = (None, "", [], {}, "не удалось определить", "—")


def esc(x) -> str:
    return _html.escape(str(x if x is not None else ""))


def _dig(obj, path: str):
    """Значение по пути «a.b.c» внутри структуры навыка. Нет такого пути — None."""
    cur = obj
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            # список на пути: собираем поле со всех элементов — так «исполнение.статус» даёт колонку
            vals = [x.get(part) for x in cur if isinstance(x, dict) and x.get(part) not in _EMPTY]
            cur = vals or None
        else:
            return None
        if cur in _EMPTY:
            return None
    return cur


def outputs(result: dict) -> list[tuple[str, dict]]:
    """Структурированные ответы навыков прогона в порядке выполнения."""
    out = []
    for so in (result or {}).get("skill_outputs") or []:
        st = so.get("structured")
        if isinstance(st, dict) and st:
            out.append((str(so.get("skill") or ""), st))
    return out


def value(result: dict, ref: str):
    """Значение по ссылке бланка. «навык:путь» — только из этого навыка, «путь» — из первого, кто дал.

    Форма обслуживает ГРУППУ навыков, а в прогоне обычно работает один из них: поэтому ссылка без
    имени навыка ищет поле у всех по очереди. Так одна раскладка годится и для `adr-writer`, и для
    `architecture-chooser` — каждый заполняет те графы, которые у него есть.
    """
    for alt in str(ref or "").split("|"):
        alt = alt.strip()
        if not alt:
            continue
        sid, path = alt.split(":", 1) if ":" in alt else ("", alt)
        for name, st in outputs(result):
            if sid and name != sid:
                continue
            v = _dig(st, path)
            if v not in _EMPTY:
                return v
    return None


def field(it: dict, name: str):
    """Графа строки реестра. Тоже допускает «поле|поле»: у разных навыков графа названа по-своему,
    а колонка в документе одна («Исполнитель» — это и `ответственный`, и `кому`)."""
    for alt in str(name or "").split("|"):
        alt = alt.strip()
        if alt and isinstance(it, dict) and it.get(alt) not in _EMPTY:
            return it.get(alt)
    return None


# После даты допускается ТОЛЬКО время: хвост `.*` съедал всё остальное, и «2026-07-01 — 2026-09-30»
# превращалось в «01.07.26» — период отчёта терял вторую половину молча.
_ДАТА_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})"
                       r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def _дата(v: str) -> str:
    """ISO-дата → 23.01.26. В служебном документе дата пишется одним способом, а не так, как её
    отдала выгрузка: «2026-08-14» рядом с «14.08.2026» читается как разные источники."""
    s = str(v).strip()
    m = _ДАТА_ISO.match(s)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)[2:]}"
    if "://" in s:
        return s
    return re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b",
                  lambda g: f"{g.group(3)}.{g.group(2)}.{g.group(1)[2:]}", s)


def _деньги(v) -> str:
    """Разрядка тысяч неразрывным пробелом: 13000000 → 13 000 000.

    Только от 10 000 по модулю — иначе разрядку получили бы коды строк формы (4110) и годы,
    которые числами в этом смысле не являются.
    """
    s = str(v if v is not None else "").strip()
    if not s:
        return s
    знак = "-" if s.startswith("-") else ""
    тело = s.lstrip("+-").replace(" ", "").replace(" ", "")
    целое, точка, дробь = тело.partition(".")
    if not целое.isdigit() or len(целое) < 5:
        return s
    куски = []
    while целое:
        куски.insert(0, целое[-3:])
        целое = целое[:-3]
    return знак + " ".join(куски) + (("," + дробь) if точка and дробь else "")


def _flat(v) -> str:
    """Значение одной графой: список — через « · », объект — «ключ: значение», скаляр — как есть."""
    if isinstance(v, list):
        return " · ".join(_flat(x) for x in v if x not in _EMPTY)
    if isinstance(v, dict):
        return " · ".join(f"{report_store.label(k)}: {_flat(x)}" for k, x in v.items() if x not in _EMPTY)
    if isinstance(v, bool):
        return "да" if v else "нет"
    return _дата(v if v is not None else "")


def _rows(v) -> list[dict]:
    """Источник блока-перечня → список объектов. Строки становятся объектами с одной графой."""
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list):
        v = [v] if v not in _EMPTY else []
    out = []
    for it in v:
        out.append(it if isinstance(it, dict) else {"значение": it})
    return out


# ── блоки бланка ───────────────────────────────────────────────────────────────────────────────────

def _b_head(b: dict, result: dict, ctx: dict) -> str:
    """Шапка документа: название вида, строка происхождения, объём работы и метод.

    Объём берётся из тех же данных, что и содержимое: «рассмотрено 14 требований» и перечень из
    четырнадцати строк не могут разойтись, потому что это одно и то же поле.
    """
    meta = []
    for lbl, ref in b.get("meta") or []:
        v = value(result, ref)
        if v in _EMPTY:
            continue
        if isinstance(v, list):
            v = len(v)
        meta.append(f"<span><i>{esc(lbl)}</i>{esc(_flat(v))}</span>")
    lines = [f"<div class='line'>{esc(b['sub'])}</div>"] if b.get("sub") else []
    # Готовые строки из контекста отчёта (источник данных) — часть шапки: документ сначала говорит,
    # откуда числа, и только потом их показывает. Так сделано в согласованной форме аудита.
    for key in b.get("ctx") or []:
        line = str((ctx or {}).get(str(key)) or "")
        if line:
            lines.append(line)
    if meta:
        # Сеткой, а не строкой через точку: в строке длинные значения («12 мес., 2026-04…2027-03»)
        # рвались посреди слова, и шапка документа читалась как случайный абзац. В сетке подпись
        # стоит над значением, и реквизиты видно с одного взгляда — как в бланке.
        lines.append("<div class='hmeta'>" + "".join(meta) + "</div>")
    if b.get("note"):
        lines.append(f"<div class='meth'>{esc(b['note'])}</div>")
    # Верхняя строка листа: слева тема (откуда документ), справа его вид. Читатель понимает, что у
    # него в руках, до того как дойдёт до заголовка; в бумажной форме ту же роль играет угловой
    # штамп. Тема объявляется бланком (`brand`), вид — его коротким именем (`kind`) или заголовком.
    тема = esc(str(b.get("brand") or (ctx or {}).get("brand") or "Агентный процесс ABOP"))
    вид = esc(str(b.get("kind") or b.get("title") or ""))
    верх = (f"<div class='top'><div class='brand'>{тема}</div>"
            + (f"<div class='pill'>{вид}</div>" if вид else "") + "</div>")
    return (верх + f"<div class='ahd'><h1>{esc(b.get('title') or '')}</h1>"
            + "".join(lines) + "</div>")


def _b_stamp(b: dict, result: dict, ctx: dict) -> str:
    """Угловой штамп письма: кому, от кого, тема, дата. Без него письмо не письмо, а абзац текста."""
    rows = []
    for lbl, ref in b.get("rows") or []:
        v = value(result, ref)
        if v in _EMPTY:
            continue
        rows.append(f"<tr><th>{esc(lbl)}</th><td>{esc(_flat(v))}</td></tr>")
    return f"<table class='stamp'>{''.join(rows)}</table>" if rows else ""


def _b_attrs(b: dict, result: dict, ctx: dict) -> str:
    """Таблица реквизитов документа. Пустая графа остаётся со словом «не указано»: в служебном
    документе отсутствие значения — это сведение, а не повод спрятать строку."""
    # `lean` ставит сшивка: в бланке вертикали пустая графа означает «не указано» и это сведение,
    # а в разделе сшитого документа графы чужих навыков той же формы просто не имеют смысла —
    # пять строк «не указано» читаются как потерянные данные.
    lean = bool(b.get("lean"))
    rows, filled = [], 0
    for lbl, ref in b.get("rows") or []:
        v = value(result, ref)
        if v not in _EMPTY:
            filled += 1
        elif lean:
            continue
        # Деньги в реквизитах разряжаются так же, как в таблицах: «7231800» рядом с «7 231 800»
        # в одном документе читается как два разных числа из разных источников.
        текст = _flat(v)
        if report_store.is_number(текст):
            текст = _деньги(текст)
        rows.append(f"<tr><th>{esc(lbl)}</th><td>{esc(текст) or 'не указано'}</td></tr>")
    if not filled:
        return ""
    return (_h2(b) + f"<div class='table-wrap'><table class='at'>{''.join(rows)}</table></div>")


def _b_verdict(b: dict, result: dict, ctx: dict) -> str:
    """Решение документа: то, за чем его открывают. Бейдж, значение, основание и условия рядом."""
    val = value(result, b.get("src") or "")
    badge = value(result, b.get("badge") or "")
    why = value(result, b.get("why") or "")
    meta = []
    for lbl, ref in b.get("meta") or []:
        v = value(result, ref)
        if v not in _EMPTY:
            meta.append(f"<span><i>{esc(lbl)}</i>{esc(_flat(v))}</span>")
    if val in _EMPTY and not meta:
        return ""
    # Бейдж, слово в слово повторяющий значение, ничего не добавляет: у многих навыков вердикт
    # один и попадает и в бейдж, и в значение.
    if badge not in _EMPTY and _flat(badge).strip().lower() == _flat(val).strip().lower():
        badge = None
    _bc = str(_flat(badge) or "").lower()
    cls = "no" if any(p in _bc for p in ("отказ", "reject", "не прин", "red", "стоп")) else (
        "warn" if any(p in _bc for p in ("замеч", "amber", "услов", "доработ", "hitl")) else "ok")
    return (_h2(b)
            + f"<div class='vb {cls}'>"
            # «нет» бейджем без подписи читается как отказ, хотя это ответ на вопрос «успеваем?».
            + (("<div class='vb-b'>"
                + (f"<i>{esc(b['badge_label'])}</i>" if b.get("badge_label") else "")
                + esc(_flat(badge)) + "</div>") if badge not in _EMPTY else "")
            + (f"<div class='vb-v'>{esc(_flat(val))}</div>" if val not in _EMPTY else "")
            + (f"<div class='vb-w'>{esc(_flat(why))}</div>" if why not in _EMPTY else "")
            + (f"<div class='vb-m'>{''.join(meta)}</div>" if meta else "")
            + "</div>")


def _b_kpi(b: dict, result: dict, ctx: dict) -> str:
    """Показатели плашками: эти цифры переносят в документ выше по иерархии, им нужен размер."""
    cells = []
    for lbl, ref in b.get("rows") or []:
        v = value(result, ref)
        if v in _EMPTY:
            continue
        if isinstance(v, list):
            v = len(v)
        показ = _деньги(_flat(v)) if report_store.is_number(v) else _flat(v)
        cells.append(f"<div class='kpi'><span class='k'>{esc(lbl)}</span><span class='v'>{esc(показ)}</span></div>")
    return (_h2(b) + f"<div class='kpis'>{''.join(cells)}</div>") if cells else ""


def _b_prose(b: dict, result: dict, ctx: dict) -> str:
    """Раздел связным текстом: цель, контекст, обоснование. Список строк — абзацами.

    Блок может нести текст сам (`text`), а не только ссылку в результат (`src`). Это нужно навыкам,
    которые отвечают прозой: ссылаться у них не на что — схемы они не объявляли, — и без этого их
    работа в документ не попадала вовсе.
    """
    if not b.get("src") and str(b.get("text") or "").strip():
        parts = str(b["text"]).split("\n\n")
        body = "".join("<p>" + esc(x).replace("\n", "<br>") + "</p>" for x in parts if x.strip())
        return (_h2(b) + f"<div class='prose'>{body}</div>") if body else ""
    v = value(result, b.get("src") or "")
    if v in _EMPTY:
        return ""
    parts = v if isinstance(v, list) else str(v).split("\n\n") if isinstance(v, str) else [v]
    body = "".join("<p>" + esc(_flat(x)).replace("\n", "<br>") + "</p>" for x in parts if x not in _EMPTY)
    return (_h2(b) + f"<div class='prose'>{body}</div>") if body else ""


def _b_list(b: dict, result: dict, ctx: dict) -> str:
    """Перечень: план, допущения, границы. Объекты сводятся к одной графе или к «ключ: значение»."""
    items = _rows(value(result, b.get("src") or ""))
    fld = b.get("field") or ""
    li = []
    for it in items[:_MAX_ROWS]:
        txt = _flat(field(it, fld)) if fld else _flat(it.get("значение") if set(it) == {"значение"} else it)
        if txt:
            li.append(f"<li>{esc(txt)}</li>")
    return (_h2(b) + f"<ul class='lst'>{''.join(li)}</ul>") if li else ""


def _b_cards(b: dict, result: dict, ctx: dict) -> str:
    """Карточки с подписанными графами — форма, согласованная с заказчиком на аудите 1С.

    Каждый пункт отвечает на одни и те же вопросы в одном и том же порядке: тогда документ можно
    читать по диагонали и сравнивать пункты между собой. Строкой «класс B · проверка» решение
    принять нельзя — ровно это и было причиной переделки формы аудита.
    """
    items = _rows(value(result, b.get("src") or ""))
    flt = b.get("filter") or []
    out = []
    for i, it in enumerate(items[:_MAX_ROWS], 1):
        if len(flt) == 2 and str(_flat(field(it, flt[0]))).lower() not in (str(flt[1]).lower(), "да", "true"):
            continue
        head = _flat(field(it, b.get("head") or "")) or _flat(it.get("значение")) or f"Пункт {i}"
        badge = _flat(field(it, b.get("badge") or ""))
        code = _flat(field(it, b.get("code") or ""))
        _bl = badge.lower()
        sev = "hi" if any(p in _bl for p in ("выс", "критич", "блок", "red", "не прин", "отказ")) else (
            "mid" if any(p in _bl for p in ("сред", "замеч", "amber", "услов")) else "low")
        graphs = []
        for lbl, fld in b.get("graphs") or []:
            v = field(it, fld)
            if v in _EMPTY:
                continue
            graphs.append(f"<tr><td class='k'>{esc(lbl)}</td><td>{esc(_flat(v))}</td></tr>")
        if not graphs and not badge:
            # нечем заполнить подписанные графы — карточка выродилась бы в заголовок
            graphs.append(f"<tr><td class='k'>Содержание</td><td>{esc(_flat(it))}</td></tr>")
        # Бейдж без подписи читается как тревога: «НЕТ» у стоп-фактора выглядел срабатыванием,
        # хотя означает обратное. Там, где значение само себя не объясняет, бланк называет графу.
        _bl = b.get("badge_label")
        _badge_html = (f"<span class='sev {sev}'><i>{esc(_bl)}</i>{esc(badge)}</span>" if (badge and _bl)
                       else (f"<span class='sev {sev}'>{esc(badge.upper())}</span>" if badge else ""))
        out.append("<div class='ac'><div class='ac-h'>"
                   + _badge_html
                   + (f"<span class='code'>{esc(code)}</span>" if code else "")
                   + (f"<span class='grp'>{esc(b.get('group') or '')}</span>" if b.get("group") else "")
                   + f"</div><div class='ac-t'>{esc(head)}</div>"
                   + f"<table class='ac-f'>{''.join(graphs)}</table></div>")
    return (_h2(b) + "".join(out)) if out else ""


def _ячейка(запись: dict, поле: str, число: bool) -> str:
    """Значение графы реестра. Ссылка на документ — компактной ссылкой, а не полным URL.

    Полный адрес в графе печатался целиком и, не разрываясь по словам, уносил таблицу за край
    листа. В документе важна не строка адреса, а возможность открыть первоисточник, поэтому
    подписью служит имя документа из той же записи (или слово «открыть»), а адрес остаётся в
    атрибуте — ссылка в PDF работает.
    """
    v = _flat(field(запись, поле))
    текст = str(v or "").strip()
    if текст.startswith(("http://", "https://")):
        подпись = str(field(запись, "документ") or field(запись, "id") or "открыть").strip()
        return f"<a href='{esc(текст)}'>{esc(подпись or 'открыть')}</a>"
    return esc(_деньги(v) if число else v)


def _b_register(b: dict, result: dict, ctx: dict) -> str:
    """Реестр: строки с заданными колонками в заданном порядке. Колонки объявлены бланком, а не
    угаданы по данным — в служебном документе состав графы и её название часть формы."""
    items = _rows(value(result, b.get("src") or ""))
    cols = b.get("cols") or []
    if not items or not cols:
        return ""
    # Колонка, пустая во ВСЕХ строках, из документа убирается. Форма обслуживает группу навыков, и
    # у работавшего навыка таких граф нет вовсе: пустой столбец «Решение приёмки» читается как
    # «приёмка не проведена», хотя приёмки в этом документе и не было.
    cols = [(lbl, f) for lbl, f in cols
            if any(str(field(it, f) or "").strip() != "" for it in items)] or cols[:1]
    num = set()
    for lbl, fld in cols:
        vals = [field(it, fld) for it in items if str(field(it, fld) or "").strip() != ""]
        if vals and all(report_store.is_number(x) for x in vals):
            num.add(fld)
    # Графа, у которой ВСЕ значения короткие (код строки, счёт, дата), прижимается по содержимому
    # и не переносится. Раньше это делалось по позиции — «первая графа» — и ломало реестры, где
    # первой идёт норма или причина: запрет переноса раздвигал таблицу за край листа.
    def _короткая(f: str) -> bool:
        vals = [str(_flat(field(it, f)) or "") for it in items]
        vals = [v for v in vals if v.strip()]
        return bool(vals) and max(len(v) for v in vals) <= 12 and len(str(dict(cols).get(f, ""))) <= 16
    узкие = {f for _l, f in cols if _короткая(f)}

    def _cls(f: str) -> str:
        кл = [c for c in (("n" if f in num else ""), ("k" if f in узкие else "")) if c]
        return (" class='" + " ".join(кл) + "'") if кл else ""

    head = "".join(f"<th{_cls(f)}>{esc(lbl)}</th>" for lbl, f in cols)
    # Бланк может сказать, в каком поле лежит ВИД строки: итоговые и сальдовые строки формы
    # выделяются заливкой — их ищут глазами первыми, и в бумажной форме так же.
    поле_вида = str(b.get("rowclass") or "")
    body = []
    for it in items[:_MAX_ROWS]:
        tds = "".join(f"<td{_cls(f)}>{_ячейка(it, f, f in num)}</td>" for _l, f in cols)
        вид = str(field(it, поле_вида) or "").strip().lower() if поле_вида else ""
        кл = {"подытог": " class='subtotal'", "итог": " class='subtotal'",
              "сальдо": " class='balance'"}.get(вид, "")
        body.append(f"<tr{кл}>{tds}</tr>")
    more = f" · показаны первые {_MAX_ROWS} из {len(items)}" if len(items) > _MAX_ROWS else ""
    # `wide` объявляет бланк; с шести граф форма становится широкой сама — иначе мелкий кегль и
    # фиксированная раскладка объявлены в CSS, но до разметки не доезжают.
    широкая = bool(b.get("wide")) or len(cols) >= 6
    кл_таблицы = "tbl wide" if широкая else "tbl"
    # `thead` повторяет шапку на каждой странице печати; контейнер даёт рамку со скруглением и
    # прокрутку широкой таблицы на экране, не раздвигая лист.
    таблица = (f"<table class='{кл_таблицы}'><thead><tr>{head}</tr></thead>"
               f"<tbody>{''.join(body)}</tbody></table>")
    return (_h2(b) + f"<div class='table-wrap'>{таблица}</div>"
            + f"<div class='cnt'>строк: {len(items)}{more}"
            + (f" · {esc(b['note'])}" if b.get("note") else "") + "</div>")


def _b_pre(b: dict, result: dict, ctx: dict) -> str:
    """Исходник как есть: код схемы, фрагмент OpenAPI, текст письма. Правят его в репозитории."""
    v = value(result, b.get("src") or "")
    if v in _EMPTY:
        return ""
    return _h2(b) + f"<pre class='code'>{esc(_flat(v) if not isinstance(v, str) else v)}</pre>"


def _b_sign(b: dict, result: dict, ctx: dict) -> str:
    """Подписи: кто подготовил, кто проверил, кто утверждает. Документ без подписей не вводится в
    оборот — а решение принимает человек, даже когда документ собрал агент."""
    cells = "".join(f"<div class='sg'><div class='ln'></div><span>{esc(r)}</span></div>"
                    for r in (b.get("rows") or []))
    return (_h2(b) + f"<div class='sign'>{cells}</div>") if cells else ""


def _b_part(b: dict, result: dict, ctx: dict) -> str:
    """Заголовок части сшитого документа: чей это раздел.

    В документе из нескольких навыков читатель должен видеть, кто что сказал: без этого разделы
    сливаются в один поток, и спорить с конкретным выводом не получится — непонятно, чей он.
    """
    t = esc(b.get("title") or "")
    sub = esc(b.get("sub") or "")
    if not t:
        return ""
    return (f"<div class='part'><h2>{t}</h2>" + (f"<span>{sub}</span>" if sub else "") + "</div>")


def _b_note(b: dict, result: dict, ctx: dict) -> str:
    """Оговорка вида документа: чего он НЕ заменяет.

    Оформляется выноской — бежевая подложка и линия слева, без тревожных цветов: это не ошибка, а
    условие, которое нельзя пропустить. Мелкий серый текст внизу листа читатель пропускал.
    """
    return f"<div class='callout'>{esc(b.get('text') or '')}</div>" if b.get("text") else ""


def _b_raw(b: dict, result: dict, ctx: dict) -> str:
    """Готовый блок из контекста отчёта (графики, пояснения навыков) — встраивается по месту."""
    return ""      # подставляется в render() из ctx: сюда попадать не должен


# Цвета зон — по подписи из данных. Код не решает, какая зона страшнее: он только раскрашивает
# то, что назвала организация. Незнакомая подпись останется нейтральной, а не исчезнет.
_ЗОНЫ_ЦВЕТ = {"красная": ("#fee2e2", "#991b1b"), "жёлтая": ("#fef3c7", "#92400e"),
               "желтая": ("#fef3c7", "#92400e"), "зелёная": ("#dcfce7", "#166534"),
               "зеленая": ("#dcfce7", "#166534")}


def _b_heatmap(b: dict, result: dict, ctx: dict) -> str:
    """Тепловая карта рисков: строки — влияние, столбцы — вероятность, в клетке — сколько и что.

    Решётку рисует код, но ни одной подписи не придумывает: полосы и зоны приходят в данных
    (`вероятность`, `влияние`, `ячейки` с полем `зона`). У другой организации полос может быть
    четыре или шесть — карта соберётся так же.
    """
    карта = value(result, b.get("src") or "")
    if not isinstance(карта, dict):
        return ""
    столбцы = [str(с) for с in (карта.get("вероятность") or []) if str(с).strip()]
    строки = [str(с) for с in (карта.get("влияние") or []) if str(с).strip()]
    ячейки = карта.get("ячейки") or []
    если_пусто = str(карта.get("почему_пусто") or "")
    if not (столбцы and строки):
        # Пустая карта — не повод промолчать: сказать, почему её нет, важнее, чем не показать.
        return (_h2(b) + f"<div class='dcard-note'>{esc(если_пусто)}</div>") if если_пусто else ""
    по_клетке = {}
    for я in ячейки:
        if isinstance(я, dict):
            по_клетке[(str(я.get("строка") or ""), str(я.get("столбец") or ""))] = я
    head = "".join(f"<th>{esc(с)}</th>" for с in столбцы)
    тело = []
    # Влияние сверху вниз от тяжёлого к легкому: красный угол читается первым, как у всех матриц.
    for стр in reversed(строки):
        клетки = []
        for стб in столбцы:
            я = по_клетке.get((стр, стб)) or {}
            сколько = я.get("сколько") or 0
            фон, цвет = _ЗОНЫ_ЦВЕТ.get(str(я.get("зона") or "").strip().lower(),
                                        ("transparent", "var(--ink-2)"))
            коды = esc(str(я.get("коды") or ""))
            внутри = (f"<b style='font-size:15px'>{сколько}</b>"
                      f"<div style='font-size:9.5px;line-height:1.25;margin-top:2px'>{коды}</div>"
                      if сколько else "<span style='color:var(--ink-3)'>—</span>")
            клетки.append(f"<td style='background:{фон};color:{цвет};vertical-align:top;"
                          f"padding:6px 7px'>{внутри}</td>")
        тело.append(f"<tr><th style='text-align:left;white-space:nowrap'>{esc(стр)}</th>"
                    + "".join(клетки) + "</tr>")
    не_оценено = карта.get("не_оценено") or 0
    подпись = (f"<div class='dcard-note'>Не оценено правилами: {не_оценено} — у этих рисков нет "
               "вероятности и влияния в данных, и в карту они не попали.</div>"
               if не_оценено else "")
    return (_h2(b) + "<table class='reg'><thead><tr><th>влияние ↓ / вероятность →</th>"
            + head + "</tr></thead><tbody>" + "".join(тело) + "</tbody></table>" + подпись)


def _h2(b: dict) -> str:
    t = b.get("title")
    return f"<h2>{esc(t)}</h2>" if t else ""


# Рамка документа: эти блоки не несут данных прогона и сами по себе содержанием не являются.
_FRAME = ("head", "note", "sign", "part")

_BLOCKS = {"part": _b_part, "head": _b_head, "stamp": _b_stamp, "attrs": _b_attrs, "verdict": _b_verdict,
           "kpi": _b_kpi, "prose": _b_prose, "list": _b_list, "cards": _b_cards,
           "register": _b_register, "pre": _b_pre, "sign": _b_sign, "note": _b_note,
           "heatmap": _b_heatmap}


# Имена блоков для проверки раскладки, пришедшей из редактора: что не в списке — опечатка, и
# молча пропустить её значит отдать документ без раздела.
BLOCK_NAMES = tuple(_BLOCKS) + ("ctx",)


def render(layout, result: dict, ctx: dict | None = None) -> str:
    """Бланк документа по раскладке. Неизвестный блок пропускаем, кривой — не роняет документ."""
    if not isinstance(layout, list):
        return ""
    ctx = ctx or {}
    out = []
    filled = False                            # хоть один раздел с данными?
    for b in layout:
        if not isinstance(b, dict):
            continue
        kind = str(b.get("t") or "")
        if kind == "ctx":                     # врезка из контекста: графики, резюме, пояснения
            out.append(str(ctx.get(str(b.get("key") or "")) or ""))
            continue
        fn = _BLOCKS.get(kind)
        if not fn:
            continue
        try:
            piece = fn(b, result, ctx)
        except Exception:  # noqa: BLE001 — один кривой блок не должен ронять весь документ
            continue
        out.append(piece)
        if piece and kind not in _FRAME:
            filled = True
    # Шапка, оговорка и подписи печатаются всегда — по ним не видно, есть ли в документе содержание.
    # Ни одного раздела с данными: это не документ, а бланк с подписями, и отдавать его нельзя.
    return "".join(out) if filled else ""


def frame(layout, result: dict, ctx: dict | None = None, kinds=_FRAME) -> str:
    """Только рамка документа: шапка, оговорка, подписи — без проверки «есть ли содержание».

    `render` намеренно возвращает пустоту, когда ни один раздел не заполнен: бланк с подписями и без
    данных отдавать нельзя. Но у запасного пути содержание своё — общие разделы прогона, — и рамку
    к нему нужно приделать отдельно, иначе документ выходит без заголовка и без подписей.
    """
    ctx = ctx or {}
    out = []
    for b in layout if isinstance(layout, list) else []:
        if not isinstance(b, dict) or str(b.get("t")) not in kinds:
            continue
        fn = _BLOCKS.get(str(b.get("t")))
        if not fn:
            continue
        try:
            out.append(fn(b, result, ctx))
        except Exception:  # noqa: BLE001 — кривой блок рамки не должен ронять документ
            continue
    return "".join(out)


def used_refs(layout) -> list[str]:
    """Все ссылки на поля, которые читает раскладка. Нужно проверке: бланк не должен ссылаться на
    поле, которого нет ни в одной json_schema навыков формы — такая графа навсегда останется пустой."""
    refs: list[str] = []
    if not isinstance(layout, list):
        return refs
    for b in layout:
        if not isinstance(b, dict):
            continue
        for k in ("src", "badge", "why"):
            if b.get(k):
                refs.append(str(b[k]))
        for pair in (b.get("meta") or []) + (b.get("rows") or []):
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                refs.append(str(pair[1]))
    return refs


def example_from_schema(schema: dict, depth: int = 0):
    """Образец значений по json_schema навыка — чтобы превью бланка показывало УСТРОЙСТВО документа.

    Превью шаблона раньше рисовалось на демо-строках, которых у бланка нет в принципе: его графы
    берутся из структурированного ответа навыка. Без данных бланк честно отдавал пусто, и в UI он
    выглядел неизменившимся. Образец собирается из самой схемы: enum — первое значение, иначе
    описание поля, иначе слово «образец». Это пример вёрстки, а не данные прогона, и так и подписан.
    """
    if not isinstance(schema, dict) or depth > 4:
        return "образец"
    t = schema.get("type")
    if t == "object":
        return {k: example_from_schema(v, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        item = schema.get("items") or {}
        n = 2 if (item.get("type") == "object") else 3
        return [example_from_schema(item, depth + 1) for _ in range(n)]
    if t == "boolean":
        return True
    if t in ("integer", "number"):
        return 12
    enum = schema.get("enum") or []
    if enum:
        return str(enum[0])
    desc = str(schema.get("description") or "").strip()
    return (desc[:60] + "…") if len(desc) > 60 else (desc or "образец")
