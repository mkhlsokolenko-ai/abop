# -*- coding: utf-8 -*-
"""Контекст шаблона отчёта: готовые HTML-блоки под все формы результата прогона.

Вынесено из `server/web_api.py` при расщеплении монолита (ADR-052). Это не «ещё один домен», а
общий слой: контекст собирают и карточка прогона, и предпросмотр шаблона, и выгрузка PDF. Пока он
лежал в приложении, домен шаблонов нельзя было вынести, не притащив приложение за собой.

Два правила этого слоя, которые нельзя потерять:

* **Каждый вид результата рендерится по-своему.** Раньше блок находок делал `json.dumps` по
  skill-словарям и терял расследования и структуру — кастом-шаблоны выглядели пустыми.
* **Заголовок приходит вместе с содержимым.** «Расследования от симптома» над пустотой читается
  как «ничего не нашли», хотя их и не искали.

`_report_context` СИНХРОННА и читает canonical store — звать через `asyncio.to_thread`, иначе она
блокирует событийный цикл на разборе JSONL.
"""
from __future__ import annotations

from cli import ape

from . import charts, finding_store, findings, report_store, skill_templates


def _audit_cards_html(result: dict) -> str:
    """Находки аудита карточками: существенность, код, группа проверки и четыре подписанных поля.

    Поля собираем тем же кодом, что и карточки интерфейса (findings.card_from_finding): отчёт и экран
    не должны объяснять находку по-разному.
    """
    import html as _html
    esc = lambda x: _html.escape(str(x if x is not None else ""))  # noqa: E731
    rows = []
    fnds = [f for f in (result.get("findings") or []) if isinstance(f, dict) and f.get("проверка")]
    for i, f in enumerate(fnds, 1):
        try:
            c = findings.card_from_finding(result, f, None)
        except Exception:  # noqa: BLE001 — одна кривая находка не должна ронять весь отчёт
            continue
        sev = (c.get("severity") or "").upper()
        sev_cls = "hi" if sev.startswith("ВЫС") else ("mid" if sev.startswith("СРЕД") else "low")
        ex = c.get("explain") or {}
        doc = c.get("doc") or {}
        # «Откуда»: документ с датой и контрагентом. У находок по НСИ документа нет вовсе — тогда не
        # пишем «Документ от .», а показываем то, что действительно известно.
        where = esc(c.get("doc_line") or "")
        ctr = str(doc.get("Контрагент") or "")
        # Контрагент уже входит в строку документа — второй раз его писать не нужно.
        if ctr and ctr not in (c.get("doc_line") or ""):
            where = (where + ", контрагент «" + esc(ctr) + "»") if where else ("контрагент «" + esc(ctr) + "»")
        proof = str(f.get("доказательство") or "")
        # «Чем грозит» — только риск и норма; «Что проверить» — только действие. Раньше сюда
        # сливались ещё и последствия вместе с действиями, и обе графы говорили одно и то же.
        nm = c.get("norm") or {}
        # Ссылка на норму — это «ФСБУ 5/2019» или «гл. 21 НК РФ», а не название самой проверки:
        # title нормы у нас совпадает с заголовком находки и в скобках выглядел маслом масляным.
        # Только настоящая ссылка на норму («ФСБУ 5/2019», «гл. 21 НК РФ»). Заголовок секции
        # эксперта нормой не является: в скобках он читался как выдуманное основание.
        ref = (ex.get("risk_ref") or "").strip()
        grozit = (ex.get("risk") or "").strip()
        if not grozit:
            grozit = "; ".join(str(x) for x in (ex.get("consequences") or [])[:1])
        if ref and ref.lower() not in grozit.lower():
            grozit = (grozit + f" ({ref})") if grozit else ref
        grozit = grozit or "—"
        action = (ex.get("action") or "").strip() or "; ".join(str(x) for x in (ex.get("consequences") or [])[:1]) or "—"
        code = esc(c.get("id") or f"{c.get('cls') or '?'}{i}")
        url = c.get("doc_url") or ""
        rows.append(
            "<div class='ac'>"
            f"<div class='ac-h'><span class='sev {sev_cls}'>{esc(sev or '—')}</span>"
            f"<span class='code'>{code}</span>"
            f"<span class='grp'>{esc(c.get('cls') or '')} · {esc(c.get('kind') or '')}</span></div>"
            f"<div class='ac-t'>{esc(c.get('check') or '')}</div>"
            "<table class='ac-f'>"
            f"<tr><td class='k'>Что не сходится</td><td>{esc(ex.get('what') or '—')}</td></tr>"
            f"<tr><td class='k'>Откуда</td><td>{where or '—'}"
            + (f"<div class='pf'>Доказательство: {esc(proof)}</div>" if proof else "")
            + (f"<div class='pf'><a href='{esc(url)}'>открыть документ в 1С</a></div>" if url else "")
            + "</td></tr>"
            f"<tr><td class='k'>Чем грозит</td><td>{esc(grozit)}</td></tr>"
            f"<tr><td class='k'>Что проверить</td><td>{esc(action)}</td></tr>"
            "</table></div>")
    return "".join(rows)


def _requisites_html(agent: dict, result: dict, esc) -> str:
    """Шапка-реквизиты служебного документа: кто, когда, кем запущен, какими навыками, какой прогон.

    Без этого отчёт нельзя ни подшить, ни оспорить: получатель не знает, откуда взялись цифры.
    """
    import datetime as _dtm
    when = str(result.get("created_at") or "")[:16].replace("T", " ") or \
        _dtm.datetime.now().strftime("%Y-%m-%d %H:%M")
    skills = ", ".join(str(o.get("skill") or "") for o in (result.get("skill_outputs") or []) if o.get("skill"))
    cells = [("сформирован", when),
             ("запустил", result.get("started_by") or "—")]
    if skills:
        cells.append(("навыки", skills))
    rid = str(result.get("run_id") or result.get("id") or "")
    if rid:
        cells.append(("прогон", rid))
    row = "".join(f"<th>{esc(k)}</th><td>{esc(v)}</td>" for k, v in cells)
    return f"<section class='req'><h1>{esc(agent.get('name') or 'Отчёт ABOP')}</h1><table><tr>{row}</tr></table></section>"


def _sources_html(result: dict, esc) -> str:
    """Источник данных документа: какие сущности читал прогон, сколько записей и когда загружены.

    В согласованной с заказчиком форме аудита эта строка стоит в шапке: «Источник: снапшот
    1С:Предприятие 8.3 (закрытый период, read-only)». Без неё документ нечем подтвердить — числа
    есть, а откуда они, получатель не знает. Строка собирается из провенанса тех сущностей, которые
    навыки прогона действительно читали, а не из названия кейса.
    """
    import datetime as _dtm
    ents: list[str] = []
    for f in (result.get("findings") or []):
        for e in ((f or {}).get("entities") or []) if isinstance(f, dict) else []:
            if e and e not in ents:
                ents.append(str(e))
    # Документы по ссылке — такой же источник, как выгрузка: показываем их ВМЕСТЕ с сущностями, а не
    # вместо. Прогон может стоять и на том, и на другом, и получателю документа нужно видеть оба.
    docs = [d for d in ((result.get("run_metrics") or {}).get("documents") or []) if d.get("ok")]
    doc_bits = [f"<b>{esc(str(d.get('url'))[:120])}</b> ({esc(d.get('kind') or 'документ')}, "
                f"знаков {esc(d.get('chars') or 0)})" for d in docs[:6]]
    if not ents:
        # Молчание здесь — худший из вариантов: документ без строки источника выглядит так же
        # убедительно, как документ по выгрузке из 1С, хотя стоит за ним только текст задачи и общие
        # знания модели. Говорим прямо и называем, чего не хватило, — тогда читатель знает цену
        # выводам, а автор знает, что приложить в следующий раз.
        if doc_bits:
            return "<div class='line'>Источник данных: " + " · ".join(doc_bits) + "</div>"
        km = [str(x) for x in ((result.get("run_metrics") or {}).get("knowledge_mode") or [])]
        if km:
            return ("<div class='line'>Источник данных: <b>данные и документы по задаче "
                    "не предоставлены</b> — выводы навыков " + esc(", ".join(km[:6]))
                    + " опираются на приложенный материал и общие знания модели. Чтобы вывод стал "
                      "обоснованным, приложите выгрузку, регламент или ссылку на страницу вики.</div>")
        return ""
    bits = []
    for e in ents[:6]:
        try:
            p = ape.entity_provenance(e)
        except Exception:  # noqa: BLE001 — провенанс недоступен: назовём хотя бы сущность
            p = {"entity": e, "records": 0}
        # Провенанс отдаёт источники вида «http:http://host/path» (вид адаптера + адрес). Вторая
        # приставка в документе выглядит ошибкой, поэтому показываем адрес как есть.
        src = ", ".join(sorted(str(x).split(":", 1)[1] if str(x).startswith(("http:http", "https:http"))
                               else str(x) for x in (p.get("sources") or {}))) \
            or ", ".join(sorted((p.get("recipes") or {}))) or "—"
        when = ""
        if p.get("fetched_at"):
            when = " · загружено " + _dtm.datetime.fromtimestamp(float(p["fetched_at"])).strftime("%d.%m.%Y %H:%M")
        bits.append(f"<b>{esc(e)}</b> ({esc(src)}, записей {esc(p.get('records') or 0)}{esc(when)})")
    return "<div class='line'>Источник данных: " + " · ".join(bits + doc_bits) + "</div>"


def _service_html(agent: dict, result: dict, esc) -> str:
    """Служебная отметка бланка: кем и когда сформирован, какими навыками, какой прогон.

    У формы-бланка шапку задаёт сам документ (вид, объём, метод) — реквизиты с собственным
    заголовком наверху давали вторую «шапку» и спорили с ней. Внизу это служебная отметка: по ней
    документ подшивают и к ней возвращаются, когда цифры нужно оспорить.
    """
    import datetime as _dtm
    when = str(result.get("created_at") or "")[:16].replace("T", " ") or         _dtm.datetime.now().strftime("%Y-%m-%d %H:%M")
    # Служебная отметка — такая же дата документа, как и все прочие: в одном виде (15.10.26 15:58).
    # Иначе внизу стоит «2026-10-15», а в графах «14.08.26», и документ выглядит склеенным.
    from .report_form import _дата
    дата_ч, _, время = when.partition(" ")
    when = (_дата(дата_ч) + (" " + время if время else "")).strip()
    skills = ", ".join(str(o.get("skill") or "") for o in (result.get("skill_outputs") or []) if o.get("skill"))
    cells = [("документ", str(agent.get("name") or "Отчёт ABOP")), ("сформирован", when),
             ("запустил", result.get("started_by") or "—")]
    if skills:
        cells.append(("навыки", skills))
    rid = str(result.get("run_id") or result.get("id") or "")
    if rid:
        cells.append(("прогон", rid))
    row = "".join(f"<th>{esc(k)}</th><td>{esc(v)}</td>" for k, v in cells)
    return f"<table class='svc'><tr>{row}</tr></table>"


def _report_footer_html(result: dict, esc) -> str:
    """Подвал: чем этот отчёт проверить — вердикт, автономия, число этапов, идентификатор прогона."""
    v = result.get("verdict") or {}
    bits = ["Сформировано ABOP",
            "вердикт: " + ("пройден" if v.get("ok") else "есть замечания"),
            "автономия: " + str(v.get("autonomy_used") or "—"),
            "этапов: " + str(len(result.get("waves") or []))]
    rid = str(result.get("run_id") or result.get("id") or "")
    if rid:
        bits.append("прогон: " + rid)
    return "<div class='foot'>" + "".join(f"<span>{esc(b)}</span>" for b in bits) + "</div>"


def _report_context(agent: dict, result: dict) -> dict:
    """СИНХРОННАЯ сборка: читает canonical store (строка источника данных) — вызывать через
    `asyncio.to_thread`, иначе блокирует событийный цикл на разборе JSONL.

    Контекст для шаблона отчёта (report_store.render): готовые HTML-блоки под ВСЕ формы результата
    (аудит-находки A/B/C/D, расследования-цепочки, structured-вывод навыков вроде «Дайджест задач»).
    Плейсхолдеры: title, agent, date, verdict, findings_total, investigations_total,
    by_class (HTML), findings (HTML), investigations (HTML), skills (HTML), deliveries (HTML).
    Раньше блок findings делал json.dumps по skill-словарям и терял расследования/структуру →
    кастом-шаблоны выглядели «пустыми». Теперь каждый вид рендерится по-человечески."""
    import html as _html
    import datetime as _dtm
    esc = lambda x: _html.escape(str(x if x is not None else ""))  # noqa: E731
    fnds = result.get("findings") or []

    # 1) Находки: структурные (аудит) — с бейджем класса и нормой; скилл-словари уходят в блок skills.
    struct = [f for f in fnds if isinstance(f, dict) and (f.get("проверка") or f.get("наблюдение"))]
    frows = []
    for f in struct[:60]:
        cls = f.get("класс") or f.get("class") or ""
        txt = f.get("проверка") or f.get("наблюдение") or ""
        desc = f.get("описание") or ""
        norm = (f.get("нормы_rag") or [""])[0]
        # Класс уходит в CSS-класс бейджа: лестница существенности A→D красится одними и теми же
        # токенами, что карточки находок и столбцы графика. Без этого бейджи серые, и читатель
        # вынужден вчитываться в букву вместо того, чтобы увидеть критичное.
        _c = str(cls).strip().upper() if str(cls).strip().upper() in ("A", "B", "C", "D") else ""
        frows.append("<div class='fnd'>" + (f"<span class='cls {_c}'>{esc(cls)}</span>" if cls else "")
                     + f"<b>{esc(str(txt)[:300])}</b>" + (f" — {esc(str(desc)[:400])}" if desc else "")
                     + (f"<span class='norm'>§ {esc(str(norm)[:220])}</span>" if norm else "") + "</div>")
    findings_html = "".join(frows)

    # 2) Расследования от симптома: цепочка звеньев + расхождение ₽ + норма.
    inv_rows = []
    for iv in (result.get("investigations") or [])[:40]:
        if not isinstance(iv, dict):
            continue
        chain = " → ".join(f"{esc(l.get('звено'))}: {esc(l.get('статус') or ('✓' if l.get('есть') else '✗'))}"
                           for l in (iv.get("цепочка") or []))
        delta = (iv.get("сверка") or {}).get("разница_₽")
        norm = (iv.get("нормы_rag") or [""])[0]
        inv_rows.append("<div class='inv'>"
                        f"<span class='sev'>{esc(iv.get('серьёзность'))}</span> "
                        f"<span class='sym'>{esc(iv.get('id'))} — {esc(iv.get('симптом'))}</span>"
                        + (f"<div class='chain'>{chain}</div>" if chain else "")
                        + (f"<span class='delta'>расхождение Δ {esc(delta)} ₽</span>" if delta is not None else "")
                        + (f"<span class='norm'>§ {esc(str(norm)[:220])}</span>" if norm else "") + "</div>")
    # Заголовок приходит вместе с содержимым: расследований может не быть, и «Расследования от
    # симптома» над пустотой читается как «ничего не нашли», хотя их и не искали.
    investigations_html = ("<h2>Расследования от симптома</h2>" + "".join(inv_rows)) if inv_rows else ""

    # 3) Вывод навыков (LLM): структурный список (задачи/пункты) рендерим по-человечески, иначе — текст.
    sk_rows = []
    for f in fnds:
        if not (isinstance(f, dict) and f.get("skill") and (f.get("text") or f.get("structured"))):
            continue
        st = f.get("structured")
        items = None
        if isinstance(st, dict):
            for v in st.values():                        # первый список в структуре = задачи/находки/пункты
                if isinstance(v, list) and v:
                    items = v
                    break
        elif isinstance(st, list):
            items = st
        if items:
            body = "".join("<div class='task'>" + esc(
                " · ".join(f"{k}: {vv}" for k, vv in it.items()) if isinstance(it, dict) else str(it)
            )[:400] + "</div>" for it in items[:40])
        else:
            body = f"<pre>{esc((f.get('text') or '')[:2500])}</pre>"
        sk_rows.append(f"<h2>{esc(f.get('skill'))}</h2><div class='sk'>{body}</div>")
    skills_html = "".join(sk_rows)
    # 3b) Структурированные ответы навыков по шаблонам извлечения (result.skill_outputs) — таблицы/списки;
    #     общий блок {{skills}} и адресные {{skill_<sid>}} (дефисы → подчёркивания) для кейс-шаблонов.
    per_skill: dict[str, str] = {}
    so_rows = []
    _names = {}
    try:
        _names = {sid: t.get("name") for sid, t in skill_templates.load_all().items()}
    except Exception:  # noqa: BLE001
        _names = {}
    for so in result.get("skill_outputs") or []:
        sid = str(so.get("skill") or "")
        st = so.get("structured")
        if not sid or not isinstance(st, dict):
            continue
        block = report_store.struct_html(st)
        if not block:
            continue
        per_skill["skill_" + sid.replace("-", "_")] = block
        so_rows.append(f"<h2>{esc(_names.get(sid) or sid)}</h2><div class='sk'>{block}</div>")
    if so_rows:
        skills_html = "".join(so_rows)

    # 3c) Врезка «Главное»: резюме навыка (для главбуха/заказчика) выносится наверх отчёта. Раньше оно
    #     лежало внутри карточки навыка и терялось между находками — отчёт читался как перечень строк.
    # приоритет: адресное резюме («для главбуха») → вывод/главное → общий итог. Технические навыки
    # (загрузка снапшота, построение графа) во врезку не идут: их «итог» — про механику, а не про суть.
    _SUM_KEYS = ("резюме", "вывод", "главное", "рекомендаци", "итог")
    _TECH_SKILLS = ("extract", "graph-build", "checks", "load", "snapshot")
    ranked = []
    for so in result.get("skill_outputs") or []:
        st = so.get("structured")
        sid = str(so.get("skill") or "")
        if not isinstance(st, dict) or any(p in sid for p in _TECH_SKILLS):
            continue
        best = None
        for k, vv in st.items():
            kl = str(k).lower()
            if not (isinstance(vv, str) and len(vv.strip()) > 40):
                continue
            for rank, p in enumerate(_SUM_KEYS):
                if p in kl:
                    # внутри навыка выбираем ЛУЧШЕЕ поле, а не первое по порядку: у пояснений аудита
                    # «итог» идёт раньше «резюме_для_главбуха», и врезка получалась про порядок работ
                    r = rank - (5 if "для_" in kl else 0)
                    if best is None or r < best[0]:
                        best = (r, esc(vv.strip()))
                    break
        if best:
            ranked.append((best[0], len(ranked), best[1]))
    summary_bits = [x[2] for x in sorted(ranked)]
    summary_html = ("<div class='lead'>" + "</div><div class='lead'>".join(summary_bits[:3]) + "</div>") if summary_bits else ""

    # 3d) Недобор схемы — в отчёт, а не в лог: получатель должен видеть, что навык не заполнил поле,
    #     иначе пустая секция выглядит как «нарушений нет».
    miss_rows = []
    for so in result.get("skill_outputs") or []:
        ms = [m for m in (so.get("schema_miss") or []) if not str(m).startswith("_")]
        cut = "_обрезан_по_лимиту" in (so.get("schema_miss") or [])
        if ms or cut:
            miss_rows.append("<div class='dl'>" + esc(so.get("skill"))
                             + (": не заполнено — " + esc(", ".join(ms)) if ms else "")
                             + (" · ответ обрезан по лимиту токенов" if cut else "") + "</div>")
    # Отброшенная раскладка редактора — тоже замечание к отчёту: читатель должен знать, что документ
    # собран механической сшивкой, потому что предложенный порядок не прошёл проверку.
    for _n in (result.get("report_notes") or []):
        miss_rows.append("<div class='dl'>" + esc(_n) + "</div>")
    schema_notes_html = ("<h2>Замечания к сбору данных</h2>" + "".join(miss_rows)) if miss_rows else ""

    # by_class бейджи (аудит)
    bc = (result.get("findings_summary") or {}).get("by_class") or {}
    by_class_html = ""
    if bc:
        by_class_html = "<div class='badges'>" + "".join(
            f"<span class='b {k}'>{k}: {esc(bc.get(k, 0))}</span>" for k in ("A", "B", "C", "D")) + "</div>"

    v = result.get("verdict") or {}
    verdict = (("✓ пройден" if v.get("ok") else "⚠ есть замечания")
               + (f" · автономия {esc(v.get('autonomy_used'))}" if v.get("autonomy_used") else "")
               + (f" · волн {len(result.get('waves') or [])}" if result.get("waves") else ""))
    dls = "".join(f"<div class='dl'>{esc(d.get('channel'))} → {esc(d.get('to') or '—')} · {esc(d.get('mode'))}</div>"
                  for d in (result.get("delivery") or []))
    # Область проверки: без неё «найдено 10 расхождений» повисает в воздухе — десять из скольких?
    _sc = result.get("audit_scope") or {}
    scope_html = ""
    if _sc:
        scope_html = ("Проверено документов: <b>" + esc(_sc.get("документов")) + "</b> · "
                      "справочных элементов: <b>" + esc(_sc.get("справочных_элементов")) + "</b> · "
                      "связей «основание»: <b>" + esc(_sc.get("связей_основание")) + "</b>")
    _bc = (result.get("findings_summary") or {}).get("by_class") or {}
    found_html = ""
    if result.get("findings_summary"):
        _parts = " · ".join(f"{k}: {v}" for k, v in _bc.items() if v)
        found_html = ("Найдено расхождений: <b>" + esc((result.get("findings_summary") or {}).get("total"))
                      + "</b>" + (f" ({esc(_parts)})" if _parts else ""))

    # Пояснения навыков собираем В ОДИН блок с заголовками: отдельные плейсхолдеры оставляли в
    # отчёте висячие заголовки над пустотой, если навык в агента не входил.
    _EXPL = [("skill_audit1c_explain", "Пояснения по находкам"),
             ("skill_audit1c_rank", "Ранжирование по существенности"),
             ("skill_audit1c_root_cause", "Первопричины"),
             ("skill_audit1c_match_weak", "Слабые сопоставления")]
    _expl = "".join(f"<h2>{t}</h2>{per_skill[k]}" for k, t in _EXPL if (per_skill.get(k) or "").strip())

    # Счётчики плашками — только непустые. Два огромных нуля «Находок 0 · Расследований 0» наверху
    # отчёта говорят читателю ровно противоположное тому, что было в прогоне: будто работа шла и
    # ничего не дала. Если считать нечего, блока в документе нет.
    _f_total = (result.get("findings_summary") or {}).get("total") or len(struct)
    _i_total = ((result.get("investigations_summary") or {}).get("total")
                or len(result.get("investigations") or []))
    _cnt_cells = [(lbl, n) for lbl, n in (("Находок", _f_total), ("Расследований", _i_total)) if n]
    counters_html = ("<div class='sum'>" + "".join(
        f"<div class='card'><h3>{esc(lbl)}</h3><div class='num'>{esc(n)}</div></div>"
        for lbl, n in _cnt_cells) + "</div>") if _cnt_cells else ""

    return {"counters": counters_html,
            "summary": summary_html, "schema_notes": schema_notes_html,
            "audit_cards": _audit_cards_html(result), "audit_explain": _expl,
            "audit_title": "Отчёт аудита данных 1С",
            "audit_scope": scope_html, "audit_found": found_html,
            "title": esc(agent.get("name") or "Отчёт агента ABOP"), "agent": esc(agent.get("name") or ""),
            "date": _dtm.datetime.now().strftime("%d.%m.%Y %H:%M"), "verdict": verdict,
            "findings_total": (result.get("findings_summary") or {}).get("total") or len(struct),
            "investigations_total": (result.get("investigations_summary") or {}).get("total") or len(result.get("investigations") or []),
            "by_class": by_class_html,
            "charts": charts.charts_html(result),
            "findings": findings_html,
            "investigations": investigations_html,
            "tool_usage": (f"<h2>Что шаги делали сами</h2>" + report_store.struct_html(result["tool_usage"]))
                          if result.get("tool_usage") else "",
            "requisites": _requisites_html(agent, result, esc),
            "service": _service_html(agent, result, esc),
            "sources": _sources_html(result, esc),
            # Бланк вертикали собирается по раскладке из той же записи БД, что и HTML формы: отдаём
            # ему результат целиком. Ключ служебный — render() снимает его перед подстановкой.
            "_result": result,
            "footer": _report_footer_html(result, esc),
            "skills": skills_html,
            # Заголовок приходит вместе с содержимым: раздел «Доставка» над прочерком — тот же
            # висячий заголовок, что мы убрали из дайджеста.
            "deliveries": ("<h2>Доставка</h2>" + dls) if dls else "",
            **per_skill}


async def _labels_ctx(run: dict) -> dict:
    """Экспертная разметка находок (слепая, из интерфейса пилота) → плейсхолдеры отчёта:
    {{разметка_сводка}} (метрики пилота против порогов) и {{разметка}} (находки со статусом).
    Пусто → плейсхолдеры пустые, секция отчёта не рендерится."""
    import html as _h
    rid = run.get("run_id") or run.get("id") or ""
    if not rid:
        return {"разметка": "", "разметка_сводка": ""}
    try:
        labels = (await finding_store.labels_for_runs([rid])).get(rid, {})
        cards = findings.cards_for_run(run, labels)
    except Exception:  # noqa: BLE001 — разметка опциональна, отчёт не валим
        return {"разметка": "", "разметка_сводка": ""}
    marked = [c for c in cards if c.get("label")]
    if not marked:
        return {"разметка": "", "разметка_сводка": ""}
    m = findings.pilot_metrics(cards)
    esc = lambda x: _h.escape(str(x if x is not None else ""))  # noqa: E731
    th = m.get("thresholds") or {}
    chip = lambda ok: "#0a7c66" if ok else "#b45309"  # noqa: E731
    summary = ("<h2>Экспертная разметка (слепая проверка)</h2>"
               "<div class='kv'><b>Размечено:</b> " + esc(m["labeled"]) + " из " + esc(m["total"])
               + " · подтверждено " + esc(m["confirmed"]) + " · отклонено " + esc(m["rejected"])
               + (" · без решения " + esc(m["unsure"]) if m.get("unsure") else "") + "</div>"
               + "<div class='kv'><b>Точность значимых:</b> <span style='color:" + chip(m.get("precision_ok"))
               + "'>" + (esc(m["precision"]) + " %" if m.get("precision") is not None else "—")
               + "</span> (порог " + esc(th.get("precision")) + " %)</div>"
               + "<div class='kv'><b>Межучастковые расхождения:</b> <span style='color:" + chip(m.get("cross_ok"))
               + "'>" + (esc(m["cross_share"]) + " %" if m.get("cross_share") is not None else "—")
               + "</span> (порог " + esc(th.get("cross_share")) + " %)</div>"
               + "<div class='kv'><b>«Вручную бы не нашли»:</b> <span style='color:" + chip(m.get("manual_miss_ok"))
               + "'>" + esc(m["manual_miss"]) + "</span> (порог " + esc(th.get("manual_miss")) + ")</div>")
    _ru = {"confirmed": "подтверждена экспертом", "rejected": "отклонена экспертом", "unsure": "под вопросом"}
    rows = []
    for c in marked[:60]:
        lb = c.get("label") or {}
        dec = _ru.get(str(lb.get("decision") or ""), str(lb.get("decision") or ""))
        _c = str(c.get("cls") or "").strip().upper()
        _c = _c if _c in ("A", "B", "C", "D") else ""
        rows.append("<div class='fnd'>" + (f"<span class='cls {_c}'>{esc(c.get('cls'))}</span>" if c.get("cls") else "")
                    + f"<b>{esc(str(c.get('check') or '')[:200])}</b>"
                    + (f" — {esc(c.get('amount_text'))}" if c.get("amount_text") else "")
                    + f"<span class='norm'>{esc(dec)}"
                    + (" · вручную бы не нашли" if lb.get("manual_miss") else "")
                    + (f" · {esc(str(lb.get('comment'))[:200])}" if lb.get("comment") else "") + "</span></div>")
    return {"разметка": "".join(rows), "разметка_сводка": summary}
