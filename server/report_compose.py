"""Сшивка отчёта из вертикальных бланков, когда в прогоне навыки разных вертикалей.

Форма отчёта выбиралась правилом «та, что объявлена для наименьшего числа навыков». Для одного
навыка это верно, для смеси — произвол: прогон из статус-отчёта, ADR, письма и БФТ получал бланк
ADR, а остальные три навыка уходили в общий хвост. Владелец сказал об этом прямо: «отчёт выходит на
каждый шаг, нужен механизм объединения».

Здесь первый шаг — детерминированная сшивка, без модели. Раскладка собирается механически: шапка
документа, затем раздел каждого навыка — взятый из ЕГО вертикального бланка, в порядке волн прогона,
— затем служебная часть. Никакого отбора «что важнее»: выбрасывать раздел навыка потому, что он
«не по задаче», документ записи не вправе.

Ссылки каждого раздела ПРИШПИЛИВАЮТСЯ к своему навыку (`навык:путь`). Без этого графа из бланка
исследования тянула бы поле «гипотеза» у любого навыка прогона, у которого такое поле есть, и
разделы начали бы подсматривать друг у друга.
"""
from __future__ import annotations

# Блоки-рамка: они описывают ДОКУМЕНТ, а не содержание навыка. У сшитого документа шапка, оговорка и
# подписи свои — иначе их было бы по одной на каждый навык.
_FRAME = ("head", "note", "sign")
# Врезки из контекста (графики, источник данных) ставятся один раз, в известном месте.
_CTX = ("ctx",)
# Поля блока, в которых стоит ссылка на поле навыка.
_REF_KEYS = ("src", "badge", "why")
_PAIR_KEYS = ("meta", "rows")


def pin(ref: str, sid: str) -> str:
    """Пришпилить ссылку к навыку: «вердикт» → «idea-scorer:вердикт».

    Альтернативы («критичность|ценность») пришпиливаются поштучно. Уже пришпиленную (в ней есть
    двоеточие) не трогаем: бланк мог сослаться на конкретный навык осознанно.
    """
    out = []
    for alt in str(ref or "").split("|"):
        alt = alt.strip()
        if not alt:
            continue
        out.append(alt if ":" in alt else f"{sid}:{alt}")
    return "|".join(out)


def pin_block(block: dict, sid: str) -> dict:
    """Копия блока, у которой все ссылки читают только этот навык."""
    b = dict(block)
    for k in _REF_KEYS:
        if b.get(k):
            b[k] = pin(str(b[k]), sid)
    for k in _PAIR_KEYS:
        pairs = b.get(k)
        if isinstance(pairs, list):
            b[k] = [[p[0], pin(str(p[1]), sid)] if isinstance(p, (list, tuple)) and len(p) == 2 else p
                    for p in pairs]
    return b


def section(sid: str, name: str, layout) -> list[dict]:
    """Раздел одного навыка: заголовок части и содержательные блоки его бланка.

    Из бланка берём только содержание: шапку, оговорку и подписи оставляем документу. Пустые блоки
    отсеются сами при рендере — здесь мы не знаем, что навык заполнил.
    """
    if not isinstance(layout, list):
        return []
    body = [dict(pin_block(b, sid), lean=True) for b in layout
            if isinstance(b, dict) and str(b.get("t") or "") not in (_FRAME + _CTX)]
    if not body:
        return []
    return [{"t": "part", "title": name or sid, "sub": "навык " + sid}] + body


HTML = ("<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'><title>{{title}}</title>"
        "<style>{{css}}</style></head><body>"
        "{{blank}}{{tool_usage}}{{deliveries}}{{schema_notes}}{{service}}{{footer}}</body></html>")

NOTE = ("Документ сшит из бланков навыков, которые работали в этом прогоне: раздел каждого навыка "
        "взят из его вертикальной формы, порядок — порядок выполнения. Разделы не отбирались по "
        "важности: отсутствующий раздел означает, что навык не заполнил ни одной графы.")
# Внизу — не повтор объяснения, а граница ответственности: то же, что делают оговорки вертикальных
# бланков. Повторять один и тот же абзац дважды в одном документе незачем.
FOOT_NOTE = ("Разделы приведены так, как их сформировали навыки; значения взяты из их ответов без "
             "пересказа. Решение по документу принимает человек.")


def compose(sections: list[tuple[str, str, list]], *, title: str, css: str = "",
            pdf_options: dict | None = None) -> dict:
    """Форма под этот прогон: шапка, разделы навыков, графики, оговорка, подписи.

    Возвращает запись формы того же вида, что лежит в БД, — её не сохраняют: она собрана под один
    прогон. Поэтому и `id` у неё служебный: по нему видно, что форма сшитая, а не выбранная.
    """
    body: list[dict] = []
    used: list[str] = []
    for sid, name, layout in sections:
        part = section(sid, name, layout)
        if part:
            body += part
            used.append(sid)
    # Шапка не обещает больше, чем в документе есть: число разделов известно только после сборки.
    # Графы «meta» тут не нужны — они читают поля навыков, а это свойство самого документа.
    blocks: list[dict] = [{
        "t": "head", "title": title or "Отчёт по задаче",
        "sub": "Сшитый документ · навыков " + str(len(used)) + " · агентный процесс ABOP",
        "ctx": ["sources"],
        "note": NOTE,
    }]
    blocks += body
    blocks.append({"t": "ctx", "key": "charts"})
    blocks.append({"t": "note", "text": FOOT_NOTE})
    blocks.append({"t": "sign", "rows": ["Подготовил (агентный процесс ABOP)", "Проверил"]})
    return {"id": "composed", "name": "Сшитый отчёт по навыкам прогона", "html": HTML, "css": css,
            "layout": blocks, "builtin": True,
            "pdf_options": pdf_options or {"format": "A4", "orientation": "portrait"},
            "for_skills": list(used)}


# ── раскладка от навыка-редактора ────────────────────────────────────────────────────────────────

EDITOR_SKILL = "report-editor"


def editor_layout(result: dict):
    """Раскладка, объявленная навыком-редактором, если она пригодна к рендеру.

    Проверяем, а не доверяем: модель могла придумать тип блока, вернуть строку вместо списка или
    забыть пришпилить ссылку к навыку. Негодную раскладку отбрасываем целиком — честный механический
    порядок по волнам лучше документа, собранного наугад. Отброс не прячем: вызывающая сторона
    получает причину и пишет её в замечания к отчёту.

    Возвращает (раскладка | None, причина отказа).
    """
    from . import report_form
    out = None
    for o in (result or {}).get("skill_outputs") or []:
        if str(o.get("skill") or "") == EDITOR_SKILL and isinstance(o.get("structured"), dict):
            out = o["structured"]
    if not out:
        return None, ""
    lay = out.get("раскладка")
    if not isinstance(lay, list) or not lay:
        return None, "редактор не вернул раскладку"
    known = set(report_form.BLOCK_NAMES)
    bad = sorted({str(b.get("t") or "?") for b in lay if not isinstance(b, dict) or str(b.get("t") or "") not in known})
    if bad:
        return None, "неизвестные блоки раскладки: " + ", ".join(bad)
    # Ссылки должны быть пришпилены к навыку: иначе графа возьмёт одноимённое поле у соседа.
    loose = [r for r in report_form.used_refs(lay) if ":" not in r.split("|")[0]]
    if loose:
        return None, "ссылки без имени навыка: " + ", ".join(sorted(set(loose))[:4])
    sids = {str(o.get("skill") or "") for o in (result or {}).get("skill_outputs") or []}
    alien = sorted({r.split(":", 1)[0] for r in report_form.used_refs(lay)
                    if ":" in r.split("|")[0]} - sids)
    if alien:
        return None, "ссылки на навыки вне прогона: " + ", ".join(alien[:4])
    return lay, ""


def with_editor(lay: list, *, title: str, css: str = "", pdf_options: dict | None = None) -> dict:
    """Форма прогона по раскладке редактора: его порядок, но рамка документа наша.

    Шапку, оговорку и подписи ставим сами: это свойства документа, а не выбор модели. Если редактор
    прислал свои — они останутся внутри, но рамка будет в любом случае.
    """
    blocks = [{"t": "head", "title": title or "Отчёт по задаче",
               "sub": "Документ собран редактором отчёта · агентный процесс ABOP",
               "ctx": ["sources"], "note": EDITOR_NOTE}]
    blocks += [b for b in lay if isinstance(b, dict)]
    blocks.append({"t": "ctx", "key": "charts"})
    blocks.append({"t": "note", "text": FOOT_NOTE})
    blocks.append({"t": "sign", "rows": ["Подготовил (агентный процесс ABOP)", "Проверил"]})
    return {"id": "edited", "name": "Отчёт по раскладке редактора", "html": HTML, "css": css,
            "layout": blocks, "builtin": True,
            "pdf_options": pdf_options or {"format": "A4", "orientation": "portrait"},
            "for_skills": []}


EDITOR_NOTE = ("Порядок разделов объявил навык-редактор, глядя на все результаты прогона и на задачу. "
               "Значения в графах — из ответов навыков, дословно: редактор выбирает вид документа, "
               "а не его содержание.")
