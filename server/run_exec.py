# -*- coding: utf-8 -*-
"""Исполнение прогона: от графа агента до доставленного результата.

Самый крупный слой, вынесенный из монолита (ADR-052, docs/RASKLADKA_SERVERA.md). Это не домен
маршрутов: ручек здесь нет вовсе. Это то, что домены ЗОВУТ — прогоны, агенты, триггеры, цепочки,
повтор задания из DLQ и пробный шаг. Пока он лежал в `web_api`, ни один из этих доменов не мог
переехать: каждый тянул бы приложение за собой.

Что здесь собрано, по порядку работы прогона:

* **границы данных** — `_gate_agent_data`, `_refresh_source_data`, `_data_fingerprint`: во что
  агенту можно смотреть, свежо ли это и каким снимком данных подписан прогон;
* **исполнение** — `execute_agent_run`: волны, бюджет, доска, кэш частей, HITL-гейты;
* **документ** — `_template_for_run`, `_build_report_html` и помощники раскладки: один
  визуальный язык отчёта (ADR-040);
* **доставка** — `_deliver_out_nodes`, `_send_channel`: наружу только через OUT-узел и только
  после подтверждения человека (ADR-014).

Зависимости наружу: сторы, ядро `ape`, контекст отчёта (`report_context`) и посадки карты
(`routes.process._seat_ops` — прогон записывает участки, которые подтвердил). `web_api` отсюда не
импортируется: это и был смысл выноса.
"""
from __future__ import annotations

import asyncio as _asyncio
import os
import time
import time as _time

from cli import ape

from . import access, admin_store, audit_store, blackboard, clients, dataplane_store
from . import delivery as delivery_mod
from . import doclink, hitl_store, identity_store, langfuse_trace
from . import observability as obs
from . import report_compose, report_store, run_cache_store, run_parts_store, run_queue
from . import run_store, runner, schema_store, skill_store, skill_templates, skill_tools
from . import slava, systems_store
from .config import settings
from .report_context import _labels_ctx, _report_context
from .routes.process import _seat_ops


async def _gate_agent_data(agent: dict, fam_key: str, actor: str) -> tuple[set, list]:
    """ABAC на пути ДАННЫХ: сущности агента, чьи наполняющие рецепты привязаны к системам (system_id)
    вне scope семьи, — закрываем. Возвращает (blocked_entities, data_denied[]). Отказы → аудит."""
    ents = set()
    for n in (agent.get("graph") or {}).get("nodes") or []:
        if n.get("kind") == "skill":
            sid = n.get("skill") or n.get("title")
            for ds in (ape.skill_datasources_resolved(sid) or []):
                if ds.get("entity"):
                    ents.add(ds["entity"])
    ent_sys: dict = {}
    for r in await dataplane_store.recipes_all():
        e, sid = r.get("entity"), r.get("system_id")
        if e and sid:
            ent_sys.setdefault(e, set()).add(sid)
    blocked, denied = set(), []
    for e in ents:
        sids = ent_sys.get(e) or set()
        if not sids:
            continue  # сущность не привязана к системе реестра (локально/публично) — не гейтим
        allowed_any = False
        for sid in sids:
            ok, _r = access.can_reach_system(fam_key, await systems_store.get(sid) or {})
            allowed_any = allowed_any or ok
        if not allowed_any:
            blocked.add(e)
            denied.append({"entity": e, "systems": sorted(sids)})
            await access.audit_denial(actor, fam_key, ",".join(sorted(sids)), "data:" + e, "система сущности вне scope")
    return blocked, denied


def _agent_knowledge_fn(agent: dict, actor: str):
    """Собрать knowledge_fn для прогона: RAG-запрос к корпусу семьи (sLAVA) с ABAC-гейтом. Корпус —
    из knowledge-source узла графа (node.corpus{collection|family,top_k}); нет узла ⇒ None (без RAG)."""
    corpus_col = corpus_fam = None
    top_k = 4
    fam = agent.get("family")
    for n in (agent.get("graph") or {}).get("nodes") or []:
        if n.get("kind") in ("source", "doc"):
            c = n.get("corpus") or {}
            if c.get("collection") or c.get("family"):
                corpus_fam = c.get("family")
                corpus_col = c.get("collection") or slava.fam_collection(c.get("family"))
                top_k = int(c.get("top_k") or 4)
                break
    # фолбэк: если source-узла с corpus нет в сохранённом графе — берём корпус СЕМЬИ агента (slava_fam_<family>)
    if not corpus_col and fam and fam != "*":
        corpus_col = slava.fam_collection(fam)
        corpus_fam = fam
    if not corpus_col:
        return None
    fam_key = access.scope_key(family=fam)
    # ABAC: корпус привязан к СЕМЬЕ и семья агента её не достигает → знание закрыто (аналитик ≠ архитектура)
    if corpus_fam and fam != "*" and not access.can_reach_family(fam, corpus_fam):
        async def _denied(sid, entities):
            await access.audit_denial(actor, fam_key, corpus_col, "knowledge", "семья без доступа к корпусу")
            return []
        return _denied

    async def _kfn(sid, entities, hint: str = ""):
        # запрос из методики навыка (домен-релевантный) + сущности — иначе sLAVA отсекает по релевантности
        q = ((hint or "")[:400] + " " + " ".join(entities)).strip() or f"нормы для {sid}"
        try:
            res = await slava.query(corpus_col, q, top_k=top_k, tenant="abop")
            return [s.get("text", "") for s in (res.get("sources") or []) if s.get("text")]
        except Exception:  # noqa: BLE001 — корпус недоступен → без RAG, прогон не падает
            return []
    return _kfn


async def _pick_template_id(result: dict) -> str:
    """Какая форма оформит этот результат.

    Сначала спрашиваем сами формы: та, что объявила себя для навыков прогона, знает предмет лучше
    общей. Если такой нет — выбираем по форме результата, как и раньше. Привязка живёт рядом с
    формой в базе, поэтому добавить бланк под вертикаль можно, не трогая код.
    """
    skills = [str(o.get("skill") or "") for o in (result.get("skill_outputs") or []) if o.get("skill")]
    try:
        by_skill = await report_store.template_for_skills(skills)
    except Exception:  # noqa: BLE001 — подбор по навыку усиление, а не условие работы отчёта
        by_skill = ""
    return by_skill or _auto_template_id(result)


def _run_skills(result: dict) -> list[str]:
    """Навыки прогона в порядке выполнения, без редактора: он задаёт вид, а не содержание."""
    out: list[str] = []
    for o in (result or {}).get("skill_outputs") or []:
        sid = str(o.get("skill") or "")
        if sid and sid != report_compose.EDITOR_SKILL and o.get("structured") and sid not in out:
            out.append(sid)
    return out


def _with_prose(form: dict | None, result: dict, names: dict | None = None) -> dict | None:
    """Дописать в форму разделы навыков, ответивших прозой.

    Бланк рисует только объявленные поля, поэтому навык без схемы в него не попадал — ни в выбранный
    бланк вертикали, ни в сшитый. Прогон на шесть навыков давал документ, в котором нет ни одного из
    них. Их текст — тоже работа, и место ему в документе, перед оговоркой и подписями.
    """
    if not form or not isinstance(form.get("layout"), list):
        return form
    extra = report_compose.prose_sections(result, names or {})
    if not extra:
        return form
    lay = list(form.get("layout") or [])
    tail = [b for b in lay if isinstance(b, dict) and b.get("t") in ("note", "sign", "svc")]
    head = [b for b in lay if b not in tail]
    out = dict(form)
    out["layout"] = head + extra + tail
    return out


async def _skill_names() -> dict:
    try:
        return {sid: (t.get("name") or sid) for sid, t in skill_templates.load_all().items()}
    except Exception:  # noqa: BLE001 — названия украшают, а не определяют документ
        return {}


async def _template_for_run(result: dict, forced: str = "") -> dict | None:
    """Форма этого прогона: выбранная или СШИТАЯ из бланков его навыков.

    Пока в прогоне навыки одной вертикали, правильно взять её бланк — он под эти результаты и
    согласован. Как только навыков из разных вертикалей больше одного, прежнее правило («форма,
    объявленная для наименьшего числа навыков») давало произвол: прогон из статус-отчёта, ADR, письма
    и БФТ получал бланк ADR, а остальные три навыка — общий хвост. Тогда документ сшивается: раздел
    каждого навыка из его бланка, в порядке выполнения.

    `forced` — форма, названная человеком или OUT-узлом: её не переигрываем.
    """
    if forced:
        return _with_prose(await report_store.get(forced) or await report_store.get("default"),
                           result, await _skill_names())
    # Редактор отчёта в прогоне — его раскладка главнее механической сшивки: он смотрел и на
    # результаты, и на задачу. Но только если раскладка прошла проверку; негодную отбрасываем и
    # говорим об этом в замечаниях, а не молча собираем документ наугад.
    lay, why = report_compose.editor_layout(result)
    base0 = await report_store.get("default")
    if lay:
        # Пригодную раскладку кладём в кэш: следующий прогон того же набора навыков обойдётся без
        # вызова модели. Сбой записи отчёт не ломает — он уже собран.
        try:
            _cid = report_compose.cache_id(_run_skills(result))
            if _cid and not await report_store.get(_cid):
                await report_store.save(_cid, {"name": "Авто-раскладка: " + ", ".join(_run_skills(result)),
                                               "html": report_compose.HTML, "css": (base0 or {}).get("css") or "",
                                               "layout": lay, "for_skills": []}, editor="report-editor")
        except Exception:  # noqa: BLE001
            pass
        return _with_prose(report_compose.with_editor(lay, title="Отчёт по задаче",
                                                      css=(base0 or {}).get("css") or ""),
                           result, await _skill_names())
    if why:
        result.setdefault("report_notes", []).append("раскладка редактора отброшена: " + why)
    # Кэш: раскладку для этого набора навыков редактор уже собирал — берём её, модель не нужна.
    _cid = report_compose.cache_id(_run_skills(result))
    if _cid:
        cached = await report_store.get(_cid)
        if cached and cached.get("layout"):
            # Кэш — это раскладка, собранная для ТАКОГО ЖЕ набора навыков, но на другом прогоне.
            # Поля в ответах могли быть другими, поэтому проверяем её так же, как свежую: иначе
            # документ соберётся из пустых граф и свалится в запасной путь.
            _why = report_compose.unresolved(cached["layout"], result)
            if _why:
                result.setdefault("report_notes", []).append("раскладка из кэша не подошла: " + _why)
            else:
                return _with_prose(report_compose.with_editor(cached["layout"], title="Отчёт по задаче",
                                                              css=(base0 or {}).get("css") or ""),
                                   result, await _skill_names())
    sids = _run_skills(result)
    forms: dict[str, str] = {}
    for sid in sids:
        try:
            forms[sid] = await report_store.template_for_skills([sid])
        except Exception:  # noqa: BLE001 — реестр недоступен: решит форма результата
            forms[sid] = ""
    distinct = {f for f in forms.values() if f}
    if len(distinct) < 2:
        return _with_prose(await report_store.get(await _pick_template_id(result))
                           or await report_store.get("default"), result, await _skill_names())
    names = {}
    try:
        names = {sid: (t.get("name") or sid) for sid, t in skill_templates.load_all().items()}
    except Exception:  # noqa: BLE001
        names = {}
    sections = []
    for sid in sids:
        tpl = await report_store.get(forms.get(sid) or "") if forms.get(sid) else None
        if tpl and tpl.get("layout"):
            sections.append((sid, names.get(sid) or sid, tpl["layout"]))
    if len(sections) < 2:      # сшивать нечего: у бланков нет раскладки (старые формы по виду)
        return _with_prose(await report_store.get(await _pick_template_id(result))
                           or await report_store.get("default"), result, await _skill_names())
    base = await report_store.get("default")
    return _with_prose(report_compose.compose(sections, title="Отчёт по задаче",
                                             css=(base or {}).get("css") or ""), result, names)


def _auto_template_id(result: dict) -> str:
    """Кейс-шаблон отчёта по форме результата (когда OUT-узел не задал report_template_id явно):
    расследования → invest; аудит-находки A/B/C/D → audit1c; structured-вывод навыка → digest; иначе default."""
    if result.get("investigations"):
        return "invest"
    if (result.get("findings_summary") or {}).get("by_class"):
        return "audit1c"
    for f in result.get("findings") or []:
        if isinstance(f, dict) and f.get("skill") and f.get("structured"):
            return "digest"
    return "default"


def _is_number(x) -> bool:
    """Значение — число? Нужно только для выключки вправо: так колонки цифр читаются столбиком."""
    if isinstance(x, bool):
        return False
    if isinstance(x, (int, float)):
        return True
    t = str(x or "").strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def _label(key: str) -> str:
    """Имя поля человеку: «часы_план_факт» → «часы план факт»."""
    return str(key or "").replace("_", " ").strip()


def _kpi_html(d: dict, esc) -> str:
    """Ключевые показатели строкой плашек: их переносят в сводку выше по иерархии."""
    cells = "".join(f"<div class='kpi'><span class='k'>{esc(_label(k))}</span>"
                    f"<span class='v'>{esc(v)}</span></div>" for k, v in d.items())
    return f"<div class='kpis'>{cells}</div>"


def _structured_html(node, esc, depth: int = 0) -> str:
    """Структурный результат навыка — в HTML по форме данных.

    Список однородных записей становится таблицей: так видно колонку «отклонение» целиком, а не
    по одной строке на абзац. Словарь из одних чисел — строка показателей, прочий словарь — две
    колонки. Глубже трёх уровней не идём: дальше это уже не отчёт, а дамп, и его место в журнале.
    """
    if node is None or node == "" or node == [] or node == {}:
        return ""
    if isinstance(node, (str, int, float, bool)):
        return f"<p>{esc(node)}</p>"
    if isinstance(node, list):
        rows = [x for x in node if isinstance(x, dict)]
        if rows and len(rows) == len(node) and depth < 3:
            cols: list = []
            for r in rows:
                for k in r:
                    if k not in cols and not isinstance(r.get(k), (dict, list)):
                        cols.append(k)
            if cols:
                num = {c for c in cols
                       if all(_is_number(r.get(c)) for r in rows if str(r.get(c, "")).strip() != "")
                       and any(str(r.get(c, "")).strip() != "" for r in rows)}
                head = "".join(f"<th{' class=num' if c in num else ''}>{esc(_label(c))}</th>" for c in cols)
                body = ""
                for r in rows[:50]:
                    body += "<tr>" + "".join(
                        f"<td{' class=num' if c in num else ''}>{esc(r.get(c, ''))}</td>" for c in cols) + "</tr>"
                    deep = {k: v for k, v in r.items() if isinstance(v, (dict, list)) and v}
                    for k, v in deep.items():
                        body += (f"<tr><td colspan='{len(cols)}' class='sub'><b>{esc(_label(k))}:</b> "
                                 + _structured_html(v, esc, depth + 2) + "</td></tr>")
                more = f" · показаны первые 50 из {len(rows)}" if len(rows) > 50 else ""
                return (f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
                        f"<p class='cnt'>строк: {len(rows)}{more}</p>")
        return "<ul>" + "".join(
            f"<li>{_structured_html(x, esc, depth + 1) if not isinstance(x, (str, int, float, bool)) else esc(x)}</li>"
            for x in node[:50]) + "</ul>"
    if isinstance(node, dict):
        out = []
        plain = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
        if plain:
            nums = [k for k, v in plain.items() if _is_number(v)]
            # Сводка почти всегда лежит разделом внутри результата, а не на верхнем уровне,
            # поэтому плашки разрешены и на первом вложении. Глубже — уже подробности, им место
            # в обычной таблице.
            if depth <= 1 and len(plain) >= 3 and len(nums) >= len(plain) - 1:
                out.append(_kpi_html(plain, esc))     # сводка числами — крупно, строкой
            else:
                out.append("<table class='kv'>" + "".join(
                    f"<tr><th>{esc(_label(k))}</th><td{' class=num' if _is_number(v) else ''}>"
                    f"{esc(v)}</td></tr>" for k, v in plain.items()) + "</table>")
        for k, v in node.items():
            if isinstance(v, (dict, list)) and v:
                tag = "h4" if depth else "h3"
                out.append(f"<{tag}>{esc(_label(k))}</{tag}>" + _structured_html(v, esc, depth + 1))
        return "".join(out)
    return f"<p>{esc(node)}</p>"


_REPORT_CSS = (
    "@page{size:A4;margin:18mm 16mm}"
    "*{box-sizing:border-box}"
    "body{font-family:'Segoe UI',Arial,sans-serif;max-width:900px;margin:0 auto;padding:28px 24px;"
    "color:#15181f;line-height:1.5;font-size:14px;background:#fff}"
    ".req{border:1px solid #c9ced9;border-bottom-width:2px;margin-bottom:22px}"
    ".req h1{margin:0;padding:14px 16px 10px;font-size:19px;font-weight:700;letter-spacing:-.2px}"
    ".req table{margin:0;border:0;border-top:1px solid #e3e6ee;font-size:12.5px}"
    ".req td,.req th{border:0;border-right:1px solid #e3e6ee;padding:7px 16px;vertical-align:top}"
    ".req th{background:#f6f7fa;font-weight:600;color:#5a6274;width:1%;white-space:nowrap;"
    "text-transform:uppercase;font-size:10.5px;letter-spacing:.6px}"
    ".req tr td:last-child,.req tr th:last-child{border-right:0}"
    ".summary{border-left:3px solid #2f4f8f;background:#f6f8fc;padding:12px 16px;margin:0 0 22px;"
    "font-size:15px;line-height:1.55}"
    ".summary b{display:block;font-size:10.5px;letter-spacing:.6px;text-transform:uppercase;"
    "color:#5a6274;margin-bottom:4px;font-weight:600}"
    "h2{font-size:15px;margin:26px 0 8px;padding-bottom:5px;border-bottom:1px solid #c9ced9;font-weight:700}"
    "h3{font-size:13.5px;margin:16px 0 6px;font-weight:600}"
    "h4{font-size:12.5px;margin:12px 0 4px;color:#4a5160;font-weight:600}"
    "p{margin:6px 0}li{margin:5px 0}i{color:#2f6f4f}"
    ".kpis{display:flex;flex-wrap:wrap;gap:1px;background:#c9ced9;border:1px solid #c9ced9;margin:10px 0 16px}"
    ".kpi{flex:1 1 110px;background:#fff;padding:9px 12px}"
    ".kpi .k{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:#5a6274}"
    ".kpi .v{display:block;font-size:19px;font-weight:700;margin-top:2px}"
    "table{border-collapse:collapse;width:100%;margin:8px 0 4px;font-size:12.5px}"
    "th,td{border:1px solid #d6dae3;padding:6px 9px;text-align:left;vertical-align:top}"
    "thead th{background:#f6f7fa;font-weight:600;white-space:nowrap}"
    "tbody tr:nth-child(even) td{background:#fbfcfe}"
    ".num{text-align:right;font-variant-numeric:tabular-nums}"
    "table.kv th{width:32%;background:#fbfcfe;font-weight:500;color:#4a5160}"
    "td.sub{background:#fbfcfe;font-size:12px}"
    ".cnt{font-size:11px;color:#79808f;margin:0 0 14px}"
    ".foot{margin-top:28px;border-top:1px solid #c9ced9;padding-top:10px;font-size:11.5px;color:#79808f}"
    ".foot span{margin-right:16px}"
    "@media print{body{padding:0}tr{page-break-inside:avoid}h2{page-break-after:avoid}}"
)


def _build_report_html(agent: dict, result: dict) -> str:
    """Аварийный отчёт БЕЗ шаблона: собирается кодом, когда в базе форм нет вовсе.

    Нормальный путь один — форма из базы (report_store): её правят в одном месте, и письмо из узла
    вывода, «Показать отчёт» в приложении и PDF рисуются ею же. Эта функция остаётся только на
    случай пустой базы при первом старте, поэтому её вид намеренно скромен: реквизиты, резюме,
    разделы, подвал — ровно то, без чего документ не документ.
    """
    import datetime as _dt
    import html as _html
    esc = lambda x: _html.escape(str(x if x is not None else ""))  # noqa: E731
    name = esc(agent.get("name") or "Агент ABOP")
    v = result.get("verdict") or {}
    fnds = result.get("findings") or []
    struct = [f for f in fnds if isinstance(f, dict) and f.get("проверка")]
    invs = result.get("investigations") or []
    outs = [o for o in (result.get("skill_outputs") or []) if isinstance(o, dict)]
    rich = [o for o in outs if isinstance(o.get("structured"), dict) and o["structured"]]
    llm = [f for f in fnds if isinstance(f, dict) and f.get("skill") and f.get("text")]

    # ── шапка-реквизиты ──
    rid = str(result.get("run_id") or result.get("id") or "")
    when = str(result.get("created_at") or "")[:16].replace("T", " ") or         _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    skills_line = ", ".join(str(o.get("skill") or "") for o in outs) or "—"
    head = [f"<section class='req'><h1>Отчёт агента «{name}»</h1><table><tr>",
            f"<th>сформирован</th><td>{esc(when)}</td>",
            f"<th>запустил</th><td>{esc(result.get('started_by') or '—')}</td>",
            f"<th>навыки</th><td>{esc(skills_line)}</td></tr></table></section>"]

    # ── резюме: ради него документ и открывают ──
    lead = ""
    for o in rich:
        for key in ("итог", "вывод", "резюме"):
            val = (o.get("structured") or {}).get(key)
            if isinstance(val, str) and val.strip():
                lead = val.strip()
                break
        if lead:
            break
    if lead:
        head.append(f"<section class='summary'><b>Резюме</b>{esc(lead)}</section>")

    parts: list = []
    n = 0

    def section(title: str) -> None:
        nonlocal n
        n += 1
        parts.append(f"<h2>{n}. {esc(title)}</h2>")

    fs = result.get("findings_summary")
    if fs:
        section(f"Находки аудита: {fs.get('total')}")
        bc = fs.get("by_class") or {}
        parts.append(_kpi_html({f"класс {k}": bc.get(k, 0) for k in ("A", "B", "C", "D")}, esc))
    if struct:
        if not fs:
            section("Находки")
        parts.append("<ul>")
        for f in struct[:30]:
            norm = (f.get("нормы_rag") or [""])[0]
            parts.append(f"<li><b>[{esc(f.get('класс'))}] {esc(f.get('проверка'))}</b> — {esc(f.get('описание'))}"
                         + (f"<br><i>§ {esc(norm[:200])}</i>" if norm else "") + "</li>")
        parts.append("</ul>")
    if invs:
        section(f"Расследования от симптома: {len(invs)}")
        parts.append("<ul>")
        for iv in invs[:30]:
            chain = " → ".join(f"{esc(l.get('звено'))}: {esc(l.get('статус'))}" for l in (iv.get("цепочка") or []))
            rec = iv.get("сверка") or {}
            norm = (iv.get("нормы_rag") or [""])[0]
            parts.append(f"<li><b>{esc(iv.get('id'))} [{esc(iv.get('серьёзность'))}]</b> — {esc(iv.get('симптом'))}"
                         f"<br>{chain}<br>расхождение Δ {esc(rec.get('разница_₽'))} ₽"
                         + (f"<br><i>§ {esc(norm[:200])}</i>" if norm else "") + "</li>")
        parts.append("</ul>")
    if rich and not struct:
        for o in rich[:20]:
            st = {k: val for k, val in (o.get("structured") or {}).items()
                  if k not in ("итог", "вывод", "резюме")}
            # Заголовок раздела — как навык называется людям: «roadmap-fact» в служебном
            # документе выглядит кодом, а не разделом.
            sid = str(o.get("skill") or "")
            meta = ape.SKILLS.get(sid) or ()
            section(str(meta[0]) if meta else (sid or "Результат навыка"))
            parts.append(_structured_html(st, esc) or "<p>нет данных</p>")
    elif llm and not struct:
        for f in llm[:20]:
            section(str(f.get("skill") or "Результат навыка"))
            parts.append(f"<pre style='white-space:pre-wrap'>{esc((f.get('text') or '')[:4000])}</pre>")

    foot = ("<div class='foot'>"
            f"<span>Сформировано ABOP</span>"
            f"<span>вердикт: {'пройден' if v.get('ok') else 'есть замечания'}</span>"
            f"<span>автономия: {esc(v.get('autonomy_used') or '—')}</span>"
            f"<span>этапов: {len(result.get('waves') or [])}</span>"
            + (f"<span>прогон: {esc(rid)}</span>" if rid else "") + "</div>")
    return ("<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            f"<title>Отчёт агента «{name}»</title><style>{_REPORT_CSS}</style></head>"
            f"<body>{''.join(head)}{''.join(parts)}{foot}</body></html>")


def _html_to_text(h: str) -> str:
    """HTML отчёта - читаемый текст письма. Стили и скрипты вырезаются ВМЕСТЕ с содержимым: иначе в тело
    письма уезжал весь CSS отчёта (body{font-family:...}) и получатель видел служебную разметку."""
    import html as _h
    import re as _re
    t = _re.sub(r"(?is)<(style|script|head|svg)[^>]*>.*?</\1>", " ", h or "")
    t = _re.sub(r"(?is)<!--.*?-->", " ", t)
    t = _re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = _re.sub(r"(?i)</(li|p|h1|h2|h3|div|tr)>", "\n", t)
    t = _re.sub(r"(?i)<li[^>]*>", "- ", t)
    t = _re.sub(r"(?i)</t[dh]>", " | ", t)
    t = _re.sub(r"(?i)</(table|ul|ol|h4|section|figure)>", "\n", t)
    t = _re.sub(r"(?i)</(span|strong|b|em|dt|dd|small|code)>", " ", t)
    t = _re.sub(r"<[^>]+>", "", t)
    t = _h.unescape(t)
    t = "\n".join(_re.sub(r"[ \t]{2,}", " ", ln).strip(" |") for ln in t.splitlines())
    return _re.sub(r"\n{3,}", "\n\n", t).strip()


def _out_nodes(agent: dict) -> list:
    """OUT-узлы графа с настроенным каналом доставки (kind:'out', out{channel,...})."""
    return [n for n in (agent.get("graph") or {}).get("nodes") or []
            if n.get("kind") == "out" and (n.get("out") or {}).get("channel")]


# Канал доставки → система реестра (для ABAC-гейта на действии). Локальные каналы (pdf/file) → None.
_CHANNEL_SYSTEM = {"redmine": "redmine", "bookstack": "bookstack", "email": "mailpit",
                   "yandex": "mailpit", "yougile": "yougile", "twenty": "twenty", "nocodb": "nocodb"}


async def _send_channel(cfg: dict, agent_name: str, html_report: str, real: bool) -> str:
    """Отправка отчёта в канал OUT-узла (почта/BookStack/YouGile/Яндекс/PDF). real=False → dry_run
    (превью). Переиспользуется прогоном и подтверждением HITL (approve → real=True)."""
    import asyncio
    import re as _re
    channel = cfg.get("channel")
    title = cfg.get("subject") or (agent_name or "Отчёт ABOP")
    loop = asyncio.get_event_loop()
    rf = "true" if real else "false"
    try:
        if channel == "bookstack":
            return await loop.run_in_executor(None, ape._t_bookstack_publish,
                {"title": title, "html": html_report, "book_id": cfg.get("book_id") or 1, "run": rf})
        if channel in ("email", "yandex"):
            att = ""
            if (cfg.get("format") or "pdf") == "pdf":
                pr = await loop.run_in_executor(None, ape._t_pdf_render, {"html": html_report, "name": "report"})
                m = _re.search(r"PDF готов:\s*(\S+)", pr or "")
                att = m.group(1) if m else ""
            body_txt = "Отчёт агента ABOP во вложении." if att else _html_to_text(html_report)[:4000]
            fn = ape._t_yandex_email if channel == "yandex" else ape._t_email_send
            args = {"to": cfg.get("to") or "audit@demo.local", "subject": title,
                    "body": body_txt, "attachment": att, "run": rf}
            if not att:
                args["html"] = html_report      # письмо-отчёт вёрсткой, текст - запасной вариант
            return await loop.run_in_executor(None, fn, args)
        if channel == "yougile":
            return await loop.run_in_executor(None, ape._t_yougile_task,
                {"title": title, "description": _html_to_text(html_report)[:6000],
                 "column_id": cfg.get("column_id") or cfg.get("to") or "", "run": rf})
        if channel == "redmine":
            return await loop.run_in_executor(None, ape._t_redmine_create_issue,
                {"subject": title, "description": _html_to_text(html_report)[:6000],
                 "project": cfg.get("project") or cfg.get("to") or "", "run": rf})
        if channel in ("pdf", "file"):
            return await loop.run_in_executor(None, ape._t_pdf_render,
                {"html": html_report, "name": cfg.get("to") or "report"})
        return f"неизвестный канал: {channel}"
    except Exception as ex:  # noqa: BLE001
        return f"ошибка доставки: {type(ex).__name__}: {ex}"


async def _deliver_out_nodes(agent: dict, result: dict, actor: str, deliver_filter: str = "") -> None:
    """Проброс OUT-узла в доставку. Три режима на узел:
    - hitl=true → создаём ЗАЯВКУ в очередь HITL (pending), наружу НЕ шлём (подтвердит человек);
    - run=true, без hitl → РЕАЛЬНАЯ отправка;
    - иначе → dry_run (превью). (ADR-014: наружу — под подтверждением/явным флагом.)
    deliver_filter (дерево решений чата, «куда результат»): '' — все каналы; 'chat' — НИ одного
    (результат только в чат); имя канала (redmine/email/bookstack) — только этот канал."""
    nodes = _out_nodes(agent)
    if deliver_filter == "chat":
        result["delivery"] = []       # пользователь выбрал «только в чат» — наружу ничего
        return
    if deliver_filter:                # выбран конкретный канал — фильтруем OUT-узлы
        nodes = [n for n in nodes if (n.get("out") or {}).get("channel") == deliver_filter]
    if not nodes:
        return
    html_report = _build_report_html(agent, result)
    fam = agent.get("family") or ""
    run_id = result.get("run_id") or (result.get("verdict") or {}).get("run_id") or ""
    # Шаблон отчёта из БД (Schema-driven: вид задаётся шаблоном, не кодом). Если OUT-узел ссылается на
    # report_template_id — рендерим по нему; иначе — прежний захардкоженный HTML. Контент готовит ABOP.
    async def _report_for(cfg: dict) -> str:
        # Явно заданный шаблон приоритетен; иначе авто-выбор по форме результата (кейс-шаблон), чтобы
        # демо-агенты давали красивый отчёт без правки графа. Фолбэк — прежний детерминированный HTML.
        tpl = await _template_for_run(result, forced=str((cfg or {}).get("report_template_id") or ""))
        if not tpl:
            return html_report
        _ctx = await _asyncio.to_thread(_report_context, agent, result)
        return report_store.render(tpl, {**_ctx, **(await _labels_ctx(result))})
    # Сквозной ID: агент действует «от имени» пользователя — подмешиваем его аккаунты в системах
    # (Redmine assignee, почта). Так задача назначается на него, письмо адресно. Best-effort.
    idsys = {}
    try:
        idsys = (await identity_store.bundle(actor)).get("systems") or {}
    except Exception:  # noqa: BLE001
        idsys = {}
    def _enrich(cfg: dict, channel: str) -> dict:
        c = dict(cfg)
        if channel == "redmine":
            rm = idsys.get("redmine") or {}
            attrs = rm.get("attrs") or {}
            if attrs.get("assignee_id") and not c.get("assigned_to"):
                c["assigned_to"] = attrs.get("assignee_id")
            if attrs.get("project") and not c.get("project"):
                c["project"] = attrs.get("project")
        elif channel in ("email", "yandex"):
            em = idsys.get("email") or {}
            if em.get("external_id") and not c.get("to"):
                c["to"] = em.get("external_id")   # по умолчанию — себе (свой ящик)
        return c
    fam_key = access.scope_key(family=fam)
    deliveries = []
    for n in nodes:
        cfg = _enrich(n.get("out") or {}, (n.get("out") or {}).get("channel"))
        channel = cfg.get("channel")
        # RBAC агентов на ШЛЮЗЕ (действие): каждый outbound tool-call к системе гейтится по scope семьи
        # (deny-by-default) — «аналитик-агент не пишет в системы архитектуры». Локальные каналы (pdf/file)
        # без системы. Complement к read-гейту (_gate_agent_data). Отказ → пропуск узла + аудит.
        sys_id = _CHANNEL_SYSTEM.get(channel)
        if sys_id:
            sysrec = await systems_store.get(sys_id)
            if sysrec:
                ok, reason = access.can_reach_system(fam_key, sysrec)
                if not ok:
                    await access.audit_denial(actor, fam_key, sys_id, "deliver:" + str(channel), reason)
                    deliveries.append({"node": n.get("id"), "title": n.get("title"), "channel": channel,
                                       "to": cfg.get("to"), "mode": "denied",
                                       "result": f"⛔ доступ к системе «{sys_id}» закрыт для семьи (ABAC): {reason}"})
                    continue
        node_html = await _report_for(cfg)   # отчёт по шаблону узла (или дефолтный)
        if cfg.get("hitl"):
            # заявка в очередь HITL — оператор подтвердит, тогда отправим реально
            item = await hitl_store.create(
                run_id=str(run_id), agent_id=agent.get("id") or "", family=fam,
                node=n.get("id") or "", title=n.get("title") or channel,
                channel=channel or "", to_addr=str(cfg.get("to") or cfg.get("book_id") or ""),
                payload={"cfg": cfg, "html": node_html, "agent_name": agent.get("name"),
                         "job_id": run_queue.CURRENT_JOB.get()},
                requested_by=actor)
            deliveries.append({"node": n.get("id"), "title": n.get("title"), "channel": channel,
                               "to": cfg.get("to"), "format": cfg.get("format"),
                               "mode": "awaiting_hitl", "hitl_id": item["id"],
                               "result": f"ожидает подтверждения оператора (заявка {item['id']})"})
            await audit_store.record(actor, "agent.deliver", agent.get("id"),
                                     {"channel": channel, "mode": "awaiting_hitl", "hitl_id": item["id"]},
                                     severity="info")
            continue
        real = str(cfg.get("run")).lower() == "true"
        out = await _send_channel(cfg, agent.get("name"), node_html, real)
        deliveries.append({"node": n.get("id"), "title": n.get("title"), "channel": channel,
                           "to": cfg.get("to"), "format": cfg.get("format"),
                           "mode": "real" if real else "dry_run", "result": str(out)[:400]})
        await audit_store.record(actor, "agent.deliver", agent.get("id"),
                                 {"channel": channel, "to": cfg.get("to"),
                                  "mode": "real" if real else "dry_run"}, severity="info")
    result["delivery"] = deliveries


async def _deliver_templates(agent: dict, result: dict, actor: str, skill_schemas: dict, deliver_filter: str = "") -> None:
    """Результат навыка → В СИСТЕМУ: секция delivery шаблона собирает команды коннектора из structured-ответа,
    каждая становится HITL-заявкой с превью того, что уйдёт (ABOP — платформа запуска, результат живёт в системе).
    После «да» оператора команда публикуется в шину; ответ коннектора (command.done/failed) возвращается в заявку."""
    if deliver_filter == "chat" or not skill_schemas:
        return
    fam = agent.get("family") or ""
    fam_key = access.scope_key(family=fam)
    run_id = result.get("run_id") or (result.get("verdict") or {}).get("run_id") or ""
    deliveries = list(result.get("delivery") or [])
    made = 0
    for so in result.get("skill_outputs") or []:
        sid = so.get("skill") or ""
        spec = (skill_schemas.get(sid) or {}).get("delivery")
        if not spec:
            continue
        if deliver_filter and deliver_filter != spec.get("system"):
            continue
        cmds, skipped = delivery_mod.split_skipped(delivery_mod.build_commands(spec, so.get("structured") or {}, skill=sid))
        for sk in skipped[:5]:   # видно в карточке прогона: почему команда не собралась (навыку нечего отправлять)
            deliveries.append({"node": "tpl:" + sid, "title": f"{spec['system']}/{spec['type']} — не собрано", "channel": spec["system"],
                               "to": spec["system"] + "/" + spec["type"], "mode": "skipped",
                               "result": "навык не дал данных для команды: " + str(sk.get("reason") or "")})
        if not cmds:
            continue
        sysrec = await systems_store.get(spec["system"])
        ok, reason = access.can_reach_system(fam_key, sysrec) if sysrec else (False, "системы нет в реестре")
        if not ok:
            await access.audit_denial(actor, fam_key, spec["system"], "deliver:" + str(spec.get("type")), reason)
            deliveries.append({"node": "tpl:" + sid, "title": f"{spec['system']}/{spec['type']} × {len(cmds)}", "channel": spec["system"],
                               "to": spec["system"], "mode": "denied", "result": f"⛔ доступ к «{spec['system']}» закрыт (ABAC): {reason}"})
            continue
        for cmd in cmds[:20]:
            item = await hitl_store.create(
                run_id=str(run_id), agent_id=agent.get("id") or "", family=fam, node="tpl:" + sid, title=cmd["title"],
                channel="command", to_addr=cmd["system"] + "/" + cmd["type"],
                payload={"kind": "command", "system": cmd["system"], "type": cmd["type"], "payload": cmd["payload"],
                         "actor": actor, "trace_id": obs.current_trace_id(), "agent_name": agent.get("name"),
                         "source": cmd.get("source"), "html": delivery_mod.preview_html(cmd),
                         "job_id": run_queue.CURRENT_JOB.get()},
                requested_by=actor)
            deliveries.append({"node": "tpl:" + sid, "title": cmd["title"], "channel": cmd["system"], "to": cmd["system"] + "/" + cmd["type"],
                               "format": "command", "mode": "awaiting_hitl", "hitl_id": item["id"],
                               "subject": str((cmd["payload"] or {}).get("subject") or (cmd["payload"] or {}).get("title") or "")[:200],
                               "result": f"ожидает подтверждения оператора (заявка {item['id']})"})
            made += 1
        await audit_store.record(actor, "agent.deliver", agent.get("id"),
                                 {"channel": spec["system"], "type": spec["type"], "mode": "awaiting_hitl", "skill": sid, "count": len(cmds)}, severity="info")
    if made or len(deliveries) != len(result.get("delivery") or []):
        result["delivery"] = deliveries
        obs.inc("abop_delivery_commands_total", float(made), system="templates")


def _findings_context_text(findings: list, investigations: list) -> str:
    """Компактный текст детерминированных находок/расследований для grounded-объяснения навыками
    (LLM объясняет РЕАЛЬНЫЕ находки кода, а не ищет заново на сэмпле-дайджесте)."""
    lines = []
    for f in (findings or [])[:30]:
        if not isinstance(f, dict):
            continue
        doc = f.get("документ") or {}
        num = doc.get("Номер") if isinstance(doc, dict) else None
        lines.append(f"[{f.get('класс')}] {f.get('проверка')} ({f.get('серьёзность')}) — {f.get('описание')}"
                     + (f" · док {num}" if num else ""))
    for iv in (investigations or [])[:20]:
        if isinstance(iv, dict):
            lines.append(f"[расследование {iv.get('id')}] {iv.get('симптом')}")
    return "\n".join(lines)


def _build_run_trace(agent: dict, result: dict) -> list:
    """Аудит-трасса прогона: по каждому навыку (в порядке волн) — что он читал (сущности+происхождение),
    какая модель отработала, сколько токенов/времени. Отвечает «как рассуждал / откуда данные».
    Provenance сущностей кэшируется в пределах прогона (одна сущность у многих навыков)."""
    findings = {f.get("skill"): f for f in (result.get("findings") or []) if isinstance(f, dict) and f.get("skill")}
    prov_cache: dict = {}

    def _prov(ent: str) -> dict:
        if ent not in prov_cache:
            try:
                prov_cache[ent] = ape.entity_provenance(ent)
            except Exception:  # noqa: BLE001
                prov_cache[ent] = {"entity": ent, "records": 0}
        return prov_cache[ent]

    steps = []
    for n in (agent.get("graph") or {}).get("nodes") or []:
        if n.get("kind") != "skill":
            continue
        sid = n.get("skill") or n.get("id")
        f = findings.get(sid) or {}
        ents = f.get("entities") or [ds.get("entity") for ds in (ape.skill_datasources_resolved(sid) or []) if ds.get("entity")]
        sources = [_prov(e) for e in ents if e]
        steps.append({
            "skill": sid,
            "reads_entities": ents,
            "data_sources": sources,                       # откуда данные: рецепт/источник/объём/свежесть
            "model": f.get("model") or None,               # какая модель рассуждала
            "input_tokens": f.get("input_tokens"), "output_tokens": f.get("output_tokens"),
            "ms": f.get("ms"),
            "error": f.get("error"),
            "output_kind": "structured" if f.get("structured") else ("text" if f.get("text") else None),
        })
    return steps


def _collect_soft_errors(result: dict) -> list:
    """Собирает МЯГКИЕ (не фатальные) ошибки прогона в один список — чтобы опциональные шаги
    (находки/расследования/доставка/нормы) не отваливались МОЛЧА. Каждая логируется с trace_id;
    выводится в результат (soft_errors) и в модалку прогона. Прогон при этом не падает."""
    soft = []
    for key, stage in (("findings_error", "находки"), ("investigations_error", "расследования"),
                       ("delivery_error", "доставка"), ("manifest_error", "манифест доступа")):
        if result.get(key):
            soft.append({"stage": stage, "error": str(result.get(key))[:300]})
    for d in result.get("delivery") or []:
        r = str(d.get("result") or "")
        if "ошибка" in r.lower():
            soft.append({"stage": "доставка·" + (d.get("channel") or "?"), "error": r[:300]})
    if result.get("norms_error"):
        soft.append({"stage": "нормы (RAG)", "error": str(result["norms_error"])[:300]})
    for f in result.get("findings") or []:
        if isinstance(f, dict) and f.get("error"):
            soft.append({"stage": "навык·" + str(f.get("skill") or "?"), "error": str(f["error"])[:300]})
    for s in soft:
        obs.log_event("warn", "run.soft_error", stage=s["stage"], error=s["error"])
    return soft


_ENT_SIG_CACHE: dict = {}   # entity -> (сигнатура, ts) — короткий TTL, чтобы не читать jsonl на каждый прогон


_ENT_SIG_TTL = float(os.getenv("ABOP_ENT_SIG_TTL", "5"))


def _entity_sig(e: str) -> str:
    """Сигнатура версии сущности (счёт + max fetched_at) с коротким TTL-кэшем — горячий cached-путь
    не должен перечитывать весь jsonl на каждый прогон (при всплеске юзеров данные те же)."""
    now = _time.time()
    hit = _ENT_SIG_CACHE.get(e)
    if hit and now - hit[1] < _ENT_SIG_TTL:
        return hit[0]
    try:
        recs = ape.data_query(e, limit=100000)
        mx = max((float((r.get("provenance") or {}).get("fetched_at", 0) or 0) for r in recs), default=0)
        sig = f"{e}:{len(recs)}:{mx}"
    except Exception:  # noqa: BLE001
        sig = f"{e}:err"
    _ENT_SIG_CACHE[e] = (sig, now)
    return sig


def _data_fingerprint(agent: dict) -> str:
    """Отпечаток ВЕРСИИ данных, которые читает агент (счёт + max fetched_at по сущностям навыков).
    Меняется при обновлении Data Plane → инвалидирует кэш результатов автоматически."""
    import hashlib
    ents = set()
    for n in (agent.get("graph") or {}).get("nodes") or []:
        sid = n.get("skill")
        if not sid:
            continue
        for ds in (ape.skill_datasources_resolved(sid) or []):
            e = ds.get("entity")
            if e:
                ents.add(e)
    parts = [_entity_sig(e) for e in sorted(ents)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _run_cache_key(agent: dict) -> str:
    """Ключ кэша прогона: агент(версия) + отпечаток данных + конфиг LLM (влияет на вывод)."""
    import hashlib
    cfg = f"mt={runner._LIM.get('max_tokens')};st={int(runner._STRUCTURED)};m={settings.local_llm_model or ''}"
    return f"{agent.get('id')}::{_data_fingerprint(agent)}::{hashlib.sha256(cfg.encode()).hexdigest()[:8]}"


_EPHEMERAL_ADAPTERS = ("mailpit", "http", "vector")   # сетевые источники — свежесть важна, гоним перед прогоном


async def _refresh_source_data(agent: dict) -> None:
    """Авто-прогон рецептов ЭФЕМЕРНЫХ источников (почта/REST/knowledge) перед прогоном агента — чтобы
    canonical store был свежим без ручного data_run. Тяжёлые БД/файл-рецепты (audit1c: postgres/csv,
    наполняются бэкфиллом) НЕ трогаем. Best-effort: недоступный источник не валит прогон (работаем на store)."""
    ents = {n.get("entity") for n in (agent.get("graph") or {}).get("nodes") or []
            if n.get("kind") in ("source", "doc") and n.get("entity")}
    if not ents:
        return
    try:
        cards = ape.data_recipes_cards()
    except Exception:  # noqa: BLE001
        return
    import asyncio
    loop = asyncio.get_event_loop()
    for c in cards:
        if c.get("entity") in ents and c.get("adapter") in _EPHEMERAL_ADAPTERS:
            try:
                await loop.run_in_executor(None, ape.data_run, c["id"])
            except Exception as ex:  # noqa: BLE001
                obs.log_event("warn", "run.source_refresh_failed", recipe=c.get("id"), error=str(ex)[:200])


# Поля, по которым навык сам называет главное. Тот же список использует врезка «Главное» в отчёте:
# если навык объявил резюме, оно и есть ответ, а не наши догадки по его структуре.
_SUMMARY_KEYS = ("итог", "вывод", "резюме", "рекомендаци", "вердикт", "главное")


def run_summary(result: dict, limit: int = 4) -> list[str]:
    """Что СКАЗАЛ прогон — короткими строками, для карточки в чате.

    Карточка показывала только счётчики: «находок: 1», «Находок не выявлено». Для навыка, который
    отдаёт документ или оценку (а не находки), это означало «результата нет» — владелец так и сказал:
    цепочка отработала, а результата не видно. Сам результат лежал в прогоне и в отчёте, то есть на
    два клика в стороне.

    Берём то, что навык объявил итогом; списки показываем числом строк. Текст не пересказываем и не
    сокращаем по смыслу — только обрезаем по длине, иначе карточка начнёт врать мягче, чем счётчики.
    """
    out: list[str] = []
    for so in (result or {}).get("skill_outputs") or []:
        st = so.get("structured")
        sid = str(so.get("skill") or "")
        # Редактор отчёта отдаёт раскладку документа — служебную структуру сборки. В строках «что
        # сказал прогон» ей не место: человек спрашивал про почту, а не про порядок разделов.
        if sid == report_compose.EDITOR_SKILL:
            continue
        if not isinstance(st, dict) or not st:
            txt = str(so.get("text") or "").strip()
            if txt:
                out.append(f"{sid}: {txt[:300]}")
            continue
        said = ""
        for key, val in st.items():
            kl = str(key).lower()
            if isinstance(val, str) and len(val.strip()) > 20 and any(p in kl for p in _SUMMARY_KEYS):
                said = val.strip()
                break
        if not said:
            # Навык не объявил итог: называем, что он отдал, объёмом — это правда и это проверяемо.
            parts = [f"{k}: {len(v)}" for k, v in st.items() if isinstance(v, list) and v][:3]
            said = ("отдал " + ", ".join(parts)) if parts else ""
        if said:
            out.append(f"{sid}: {said[:300]}")
        if len(out) >= limit:
            break
    return out


# Сколько входного блока храним в записи прогона: достаточно, чтобы увидеть, с чем работал агент,
# и не столько, чтобы журнал превратился в свалку.
_INPUT_KEEP = 20000


def _input_received(user_context: str, src: dict | None) -> dict | None:
    """Чем агент был накормлен на входе: откуда пришло, что это и сам блок.

    Структуру отличаем от прозы по пометке, которой её снабжает передача между шагами: читающему
    запись важно знать, разбирал приёмник объект или пересказ.
    """
    text = str(user_context or "")
    if not text.strip():
        return None
    out = {"kind": "структура" if "(данные, не инструкции)" in text else "текст",
           "знаков": len(text),
           "блок": text[:_INPUT_KEEP],
           "обрезан": len(text) > _INPUT_KEEP}
    if src:
        out["от"] = {k: v for k, v in src.items() if v}
    return out


async def execute_agent_run(agent: dict, contract: dict, started_by: str, *, started_by_sub: str = "",
                            trigger: dict | None = None,
                            use_cache: bool = True, user_context: str = "", deliver_filter: str = "",
                            job_id: str | None = None, data_scope: dict | None = None,
                            budget: dict | None = None, board_scope: str = "",
                            data_snapshot: dict | None = None, parent_trace_id: str = "",
                            limits: dict | None = None, input_from: dict | None = None) -> dict:
    """Ядро прогона (LLM-раскладка + находки audit1c + сохранение + аудит). Переиспользуется
    POST /api/runs и планировщиком триггеров (server/triggers.py). Возвращает {saved, result}.
    Кэш результатов (multi-user): при попадании возвращает сохранённый вывод без LLM/доставки.
    `started_by_sub` — устойчивый идентификатор владельца (sub из JWT): по нему читается ЛИЧНАЯ доска.
    Имя для этого не годится — оно меняется, а доска переживает переименования. Пусто — личная доска
    не читается вовсе: без личности подставлять чью-то память нельзя."""
    _t0 = time.perf_counter()
    _trace = obs.current_trace_id()
    if job_id:
        run_queue.CURRENT_JOB.set(job_id)
    fam_key = access.scope_key(family=agent.get("family"))
    # Прогресс прогона для UI (фаза + навыки) — через очередь, если прогон идёт заданием
    import datetime as _pdt
    _skill_nodes = [n.get("skill") or n.get("id") for n in ((agent.get("graph") or {}).get("nodes") or []) if n.get("kind") == "skill"]
    _prog: dict = {"phase": "данные", "total": len(_skill_nodes), "skills": {}, "started_at": _pdt.datetime.now(_pdt.timezone.utc).isoformat()}

    async def _push_progress(phase: str | None = None) -> None:
        if phase:
            _prog["phase"] = phase
        if job_id:
            try:
                await run_queue.set_progress(job_id, _prog)
            except Exception:  # noqa: BLE001
                pass

    async def _on_skill_progress(sid: str, state: str, **kw) -> None:
        cur = _prog["skills"].get(sid) or {}
        _prog["skills"][sid] = {**cur, "state": state, **{k: v for k, v in kw.items() if v is not None}}
        await _push_progress()
    await _push_progress("данные")
    # Какие операции карты закрывает этот прогон. Берём их у посадок агента и только когда запуск
    # пришёл от триггера: ручной запуск с холста идёт без операций, и это честный признак «вне
    # карты» — для черновиков так и должно быть (ADR-044).
    _ops_прогона = await _seat_ops(agent) if trigger else []
    blocked, data_denied = await _gate_agent_data(agent, fam_key, started_by)  # ABAC на данных
    # КЭШ результатов (multi-user): тот же агент+данные+конфиг → отдаём сохранённый вывод без LLM/доставки.
    _cache_key = None
    if use_cache and os.getenv("ABOP_RUN_CACHE", "1") != "0":
        try:
            _cache_key = _run_cache_key(agent)
            _cached = await run_cache_store.get(_cache_key)
        except Exception:  # noqa: BLE001 — кэш опционален
            _cache_key, _cached = None, None
        if _cached:
            result = dict(_cached)
            result["cached"] = True
            result["trace_id"] = _trace
            result["started_by"] = started_by
            _rm = dict(result.get("run_metrics") or {})
            _cost = dict(_rm.get("cost") or {})
            _cost["cached"], _cost["rub"] = True, 0.0  # повтор из кэша — токены не тратились
            _rm["cost"] = _cost
            result["run_metrics"] = _rm
            result.pop("delivery", None)  # доставку НЕ повторяем из кэша (побочные эффекты)
            if trigger:
                result["trigger"] = {"id": trigger.get("id"), "type": (trigger.get("trig") or {}).get("type"),
                                     "title": trigger.get("title")}
            # Что прогон сказал — строками: карточка в чате и журнал показывают результат, а не
            # только счётчики находок.
            result["summary_lines"] = run_summary(result)

            result["op_ids"] = _ops_прогона
            saved = await run_store.save(result)
            _dt = time.perf_counter() - _t0
            obs.inc("abop_run_cache_total", hit="true")
            obs.observe("abop_run_seconds", _dt, family=agent.get("family") or "-")
            obs.log_event("info", "run.cache_hit", run_id=saved["id"], agent=agent.get("id"),
                          ms=round(_dt * 1000, 1))
            await audit_store.record(started_by, "agent.run", saved["id"],
                                     {"agent_id": agent.get("id"), "cached": True,
                                      "findings": (result.get("findings_summary") or {}).get("total")},
                                     severity="info")
            return {"saved": saved, "result": result}
    # Свежесть эфемерных источников: перед прогоном сами гоним рецепты почты/REST, чтобы агент читал
    # актуальные письма/вложения без ручного data_run (владелец 2026-09-24: «надо гонять рецепт»). Best-effort.
    await _refresh_source_data(agent)
    await _push_progress("проверки данных")
    # Детерминированные находки/расследования считаем ДО прогона (истина, считает КОД) — чтобы навыки в LLM
    # их ОБЪЯСНЯЛИ (grounded), а не искали заново на сэмпле-дайджесте (иначе LLM ложно пишет «расхождений нет»).
    _skills = [n.get("skill") for n in (agent.get("graph") or {}).get("nodes", [])]
    _det_findings, _det_findings_err, _audit_scope = [], None, None
    if "audit1c-checks" in _skills:
        try:
            def _checks_with_scope():
                g = ape.audit1c_build_graph()
                # Объём проверки берём из того же графа, по которому считались находки: иначе шапка
                # отчёта и его содержимое могли бы разойтись.
                scope = {"документов": len(g.get("docs") or []),
                         "справочных_элементов": len(g.get("refs") or []),
                         "связей_основание": len(g.get("edges") or []),
                         "по_типам": {k: len(v) for k, v in (g.get("by_type") or {}).items()},
                         "организаций": len(g.get("org_inns") or []),
                         "инн_с_дублями": sum(1 for v in (g.get("ctr_by_inn") or {}).values() if len(v) > 1)}
                return ape.audit1c_run_checks(g), scope
            _det_findings, _audit_scope = await _asyncio.to_thread(_checks_with_scope)
        except Exception as ex:  # noqa: BLE001
            _det_findings_err = f"{type(ex).__name__}: {ex}"
    _det_invs, _det_invs_err = [], None
    if "invest1c-trace" in _skills:
        try:
            _det_invs = await _asyncio.to_thread(ape.audit1c_trace_chains)
        except Exception as ex:  # noqa: BLE001
            _det_invs_err = f"{type(ex).__name__}: {ex}"
    _ctx = _findings_context_text(_det_findings, _det_invs) or None
    # Schema-driven: карта навык→кастомная JSON Schema (если навык ссылается на schema_template_id).
    # ЛЛМ раскладывает данные строго по схеме из БД. Пусто → рантайм использует дефолтную схему находок.
    _skill_schemas: dict = {}
    try:
        _ov = await skill_store.all()
        for _sid in set(s for s in _skills if s):
            # явная привязка из UI приоритетна, но «findings» (общий дефолт) не перебивает подробный шаблон навыка
            _explicit = ((_ov.get(_sid) or {}).get("patch") or {}).get("schema_template_id") or ""
            _stid = _explicit if (_explicit and _explicit != "findings") else _sid
            _tpl = await schema_store.get(_stid) or (await schema_store.get(_explicit) if _explicit else None)
            if _tpl and (_tpl.get("json_schema") or {}).get("properties"):
                _instr = skill_templates.strip_markers(_tpl.get("instruction") or "")
                _skill_schemas[_sid] = {"response_format": schema_store.response_format(_tpl),
                                        "instruction": _instr + "\n\nПОЛЯ СХЕМЫ (что класть):\n" + skill_templates.describe_for_prompt(_tpl),
                                        "max_tokens": skill_templates.max_tokens_of(_tpl),
                                        # шаги инструментов навыка: пусто — берётся значение среды
                                        "tool_steps": _tpl.get("tool_steps"),
                                        "delivery": _tpl.get("delivery") or None, "template_id": _tpl.get("id"),
                                        # контракт: вход из предыдущих навыков и объявленный выход —
                                        # по ним рантайм передаёт результат по волнам графа
                                        "inputs": _tpl.get("inputs") or {}, "produces": _tpl.get("produces") or {},
                                        # шаблон извлечения конкретнее дефолта формата из кода (письмо/БФТ помечены
                                        # «документ»), но НЕ перебивает явный выбор «рассуждения» в UI навыка
                                        "force_struct": ((_ov.get(_sid) or {}).get("patch") or {}).get("output") != "freeform"}
        obs.log_event("info", "skill_schemas.bound", count=len(_skill_schemas), skills=sorted(_skill_schemas))
    except Exception as _ex:  # noqa: BLE001 — схемы опциональны, не валим прогон, но НЕ молча
        obs.log_event("warning", "skill_schemas.bind_failed", error=f"{type(_ex).__name__}: {str(_ex)[:200]}")
        _skill_schemas = {}
    await _push_progress("навыки")
    # Лимиты прогона: настройка среды, поверх неё — разовое переопределение при запуске. Без слияния
    # здесь настройка из интерфейса не действовала бы ни на один прогон, кроме ручного вызова.
    _saved_limits = (await admin_store.all()).get("runLimits") or {}
    _limits = dict(_saved_limits if isinstance(_saved_limits, dict) else {})
    _limits.update({k: v for k, v in (limits or {}).items() if v not in (None, "")})
    # Общая память ветвей одного запроса. Область — группа заданий: ветвь видит выводы соседей, которые
    # успели записаться раньше. Без области доска своя и живёт только этот прогон, как было до этого.
    _board = await blackboard.board_of(board_scope) if board_scope else blackboard.Board(_trace)
    # Навык-арбитр зовём только там, где правило не различает варианты (см. arbiter.DEFAULT_ORDER).
    async def _arbiter_ask(task: str) -> dict:
        return await clients.chat(messages=[
            {"role": "system", "content": "Ты арбитр расхождений между ветвями прогона ABOP. Выбери НОМЕР "
             "варианта, подтверждённого данными, и объясни одной фразой. Новых значений не придумывай. "
             "Ответ строго JSON: {\"вариант\": N, \"обоснование\": \"...\"}."},
            {"role": "user", "content": task}], max_tokens=300)

    # Что агент получил на вход от предыдущего шага — часть провенанса, наравне со снимком
    # данных: иначе «на основании чего он так решил» отвечается только логом, который
    # живёт недолго.
    _recv = _input_received(user_context, input_from)
    # Долгие области доски — личная память человека и память отдела. Права решаются ЗДЕСЬ: рантайм не
    # знает ни JWT, ни семей, и выдумывать их ему нельзя. Читаем только то, что навыки объявили: если
    # ни один не просил долгую доску, в базу не ходим вовсе.
    _long_boards: dict = {}
    try:
        _wants = set()
        for _n in ((agent.get("graph") or {}).get("nodes") or []):
            if _n.get("kind") != "skill":
                continue
            _tpl = (_skill_schemas or {}).get(_n.get("skill")) or {}
            _wants |= set(blackboard.board_keys_scoped(_tpl.get("inputs")).keys())
        _wants.discard("run")
        if "user" in _wants and started_by_sub:
            _long_boards["user"] = await blackboard.load(blackboard.user_scope(started_by_sub))
        if "family" in _wants and agent.get("family"):
            _long_boards["family"] = await blackboard.load(blackboard.family_scope(str(agent.get("family"))))
    except Exception as _ex:  # noqa: BLE001 — доска усиление, а не условие работы прогона
        obs.log_event("warning", "board.long.read_failed", error=str(_ex)[:200])

    # Документы по ссылке: то, на чём агент должен стоять вместо общих знаний. Ссылку человек даёт
    # прямо в задаче («оцени идею по регламенту http://…»), читает её СЕРВЕР — у рантайма нет сети, и
    # давать её ему незачем. Что прочитано, попадает в прогон помеченным блоком вместе с
    # происхождением, а что не прочитано — честной строкой: молча проглоченная ссылка хуже отказа,
    # человек будет думать, что документ учтён.
    _docs: list = []
    if user_context:
        _urls = doclink.links_in(user_context)
        if _urls:
            try:
                _allow = doclink.trusted_hosts(await systems_store.all())
            except Exception:  # noqa: BLE001 — реестр недоступен: читаем только публичные адреса
                _allow = doclink.trusted_hosts([])
            for _u in _urls:
                _d = await _asyncio.to_thread(doclink.fetch, _u, _allow)
                _docs.append(_d)
                obs.log_event("info" if _d.get("ok") else "warning", "run.doclink",
                              url=_u[:120], ok=bool(_d.get("ok")), chars=_d.get("chars") or 0,
                              error=str(_d.get("error") or "")[:120])
            _block = doclink.block(_docs)
            if _block:
                user_context = _block + user_context
            _bad = [d for d in _docs if not d.get("ok")]
            if _bad:
                user_context = (user_context + "\n\n=== ССЫЛКИ, КОТОРЫЕ ПРОЧИТАТЬ НЕ УДАЛОСЬ ===\n"
                                + "\n".join(f"{d.get('url')} — {d.get('error')}" for d in _bad)
                                + "\nНе ссылайся на их содержимое и скажи, что документ не прочитан.\n")

    # Готовые части прогона: что навык уже отдал, при повторной попытке не считаем заново. Длинная
    # цепочка не укладывалась в таймаут попытки (600 с), и работа терялась ЦЕЛИКОМ — вместе с уже
    # отработавшими навыками и оплаченными токенами. Кэш живёт ровно столько, сколько идёт прогон,
    # и гасится после сведения отчёта: это не долгая память (для неё доска), а страховка попытки.
    _parts = None
    if job_id:
        _rev = run_parts_store.rev_of(agent)
        try:
            _done = await run_parts_store.load(job_id, rev=_rev)
        except Exception as _ex:  # noqa: BLE001 — кэш ускоряет, но не является условием работы
            obs.log_event("warning", "run.parts.read_failed", error=str(_ex)[:200])
            _done = {}
        if _done:
            obs.log_event("info", "run.parts.resume", job_id=job_id, parts=len(_done))

        async def _save_part(skill: str, res: dict) -> None:
            await run_parts_store.save(job_id, skill, res, rev=_rev)

        _parts = {"done": _done, "save": _save_part}

    result = await runner.run_live(agent, contract, ape.skill_safety,
                                   data_query=ape.data_query,
                                   skill_sources=ape.skill_datasources_resolved,
                                   load_body=ape.load_skill_body,
                                   chat_fn=clients.chat, blocked_entities=blocked, data_scope=data_scope,
                                   budget=budget,
                                   knowledge_fn=_agent_knowledge_fn(agent, started_by),
                                   skill_schemas=_skill_schemas,
                                   findings_context=_ctx, user_context=user_context,
                                   should_cancel=(lambda: run_queue.cancel_requested(job_id)) if job_id else None,
                                   tool_loop=skill_tools.tool_loop, actor=started_by,
                                   trace_id=(obs.current_trace_id() if hasattr(obs, "current_trace_id") else "") or "",
                                   on_progress=_on_skill_progress,
                                   board=_board, data_snapshot=data_snapshot, arbiter_ask=_arbiter_ask,
                                   limits=_limits or None, long_boards=_long_boards or None,
                                   parts=_parts)
    if _recv:
        result["input_received"] = _recv
    if _docs:
        result.setdefault("run_metrics", {})["documents"] = [
            {"url": d.get("url"), "ok": bool(d.get("ok")), "kind": d.get("kind") or "",
             "chars": int(d.get("chars") or 0), "error": str(d.get("error") or "")[:160]}
            for d in _docs]
    # Отчёт сведён — части больше ничего не значат. Инвалидация сразу, а не по сроку: иначе кэш
    # притворяется памятью и однажды подсунет вчерашний результат на свежий запуск.
    if job_id:
        try:
            await run_parts_store.drop(job_id)
        except Exception as _ex:  # noqa: BLE001
            obs.log_event("warning", "run.parts.drop_failed", error=str(_ex)[:200])
    # Выводы ветви выкладываем в общую область, чтобы следующие ветви и сводка их увидели. Сбой записи
    # прогон не валит: доска — усиление, а не условие работы.
    if board_scope:
        try:
            await blackboard.save(board_scope, result.get("board_entries") or [])
        except Exception as _bex:  # noqa: BLE001
            obs.log_event("warning", "board.save_failed", scope=board_scope,
                          error=f"{type(_bex).__name__}: {str(_bex)[:200]}")
    await _push_progress("доставка и отчёт")
    # Структурированные ответы навыков (по шаблонам) — отдельно: ниже findings подменяются детерминированными
    # Выходы навыков: берём КАЖДЫЙ, а не только отдавший строгий JSON. Прежде условие было
    # «structured — словарь», и навык, пишущий прозой (разбор идеи, адвокат дьявола, ревью
    # спецификации) выпадал из выходов целиком: прогон на шесть навыков оставлял в отчёте ноль
    # разделов, и документ выглядел пустым при 85 тысячах потраченных токенов. Текст кладём
    # обрезанным: он нужен отчёту как материал раздела, а не как второй экземпляр прогона.
    result["skill_outputs"] = [
        {"skill": f.get("skill"), "structured": f.get("structured") if isinstance(f.get("structured"), dict) else None,
         "text": str(f.get("text") or "")[:6000],
         "sources": f.get("sources") or {},
         "model": f.get("model"), "template_id": f.get("template_id") or "",
         "sources_mode": f.get("sources_mode") or "", "schema_miss": f.get("schema_miss") or []}
        for f in (result.get("findings") or [])
        if isinstance(f, dict) and f.get("skill")
        and (isinstance(f.get("structured"), dict) or str(f.get("text") or "").strip())]
    # Петля прогон→канва: прикрепляем детерминированные находки (истина, не LLM).
    if "audit1c-checks" in _skills:
        if _det_findings_err:
            result["findings"] = []
            result["findings_error"] = _det_findings_err
        else:
            result["findings"] = _det_findings
            result["findings_summary"] = {
                "total": len(_det_findings),
                "by_class": {c: sum(1 for f in _det_findings if f.get("класс") == c) for c in ("A", "B", "C", "D")},
            }
            if _audit_scope:
                result["audit_scope"] = _audit_scope
    # Демо-сценарий №2 «расследование от симптома»: цепочки реализация→взаиморасчёты→НДС.
    if "invest1c-trace" in _skills:
        if _det_invs_err:
            result["investigations"] = []
            result["investigations_error"] = _det_invs_err
        else:
            _invs = _det_invs
            result["investigations"] = _invs
            result["investigations_summary"] = {
                "total": len(_invs),
                "broken": sum(1 for i in _invs
                              if any(not l.get("есть") for l in i.get("цепочка", []))),
                "by_sev": {s: sum(1 for i in _invs if i.get("серьёзность") == s)
                           for s in ("высокая", "средняя")},
            }
    # Обогащение находок НОРМАМИ из корпуса семьи (sLAVA): запрос ПО ТЕКСТУ находки (специфичный →
    # sLAVA-retrieval срабатывает, в отличие от generic per-skill). Так объяснение получает реальную норму.
    kfn = _agent_knowledge_fn(agent, started_by)
    if kfn and isinstance(result.get("findings"), list):
        enriched = 0
        for f in result["findings"][:10]:
            if not isinstance(f, dict):
                continue
            q = " ".join(str(f.get(k) or "") for k in ("проверка", "описание")).strip()
            if not q:
                continue
            try:
                norms = await kfn("audit1c-explain", [], q)
            except Exception:  # noqa: BLE001
                norms = []
            if norms:
                f["нормы_rag"] = norms[:2]
                enriched += 1
        # обогащаем нормами и цепочки-расследования (по тексту симптома + проверки)
        for inv in (result.get("investigations") or [])[:10]:
            if not isinstance(inv, dict):
                continue
            q = " ".join(str(inv.get(k) or "") for k in ("симптом", "проверка")).strip()
            if not q:
                continue
            try:
                norms = await kfn("invest1c-verdict", [], q)
            except Exception:  # noqa: BLE001
                norms = []
            if norms:
                inv["нормы_rag"] = norms[:2]
                enriched += 1
        result["norms_enriched"] = enriched
    # Идентификатор прогона выдаём ДО доставки: заявка на подтверждение рождается здесь, и без
    # ссылки на прогон её потом не с чем связать — ни в очереди, ни в журнале.
    if not result.get("run_id"):
        result["run_id"] = await run_store.new_id(agent.get("id") or "")
    result["id"] = result["run_id"]
    # Проброс OUT-узла в реальную доставку (почта/BookStack/PDF) — dry_run по умолчанию.
    try:
        await _deliver_out_nodes(agent, result, started_by, deliver_filter=deliver_filter)
    except Exception as ex:  # noqa: BLE001 — доставка опциональна, прогон не падает
        result["delivery_error"] = f"{type(ex).__name__}: {ex}"
    try:
        await _deliver_templates(agent, result, started_by, _skill_schemas, deliver_filter=deliver_filter)
    except Exception as ex:  # noqa: BLE001
        result["delivery_error"] = (result.get("delivery_error") or "") + f" templates: {type(ex).__name__}: {ex}"
    result["started_by"] = started_by
    # Least-privilege манифест агента (ABAC): какие системы реестра доступны его семье, Qdrant-тенант,
    # и какие сущности закрыты на пути данных (система вне scope).
    try:
        result["access"] = await access.manifest(fam_key)
        if data_denied:
            result["access"]["data_denied"] = data_denied
    except Exception as ex:  # noqa: BLE001 — манифест опционален, но НЕ молча: фиксируем
        result["manifest_error"] = f"{type(ex).__name__}: {ex}"
    if trigger:  # прогон запущен триггером — фиксируем происхождение (наблюдаемость цепочек)
        result["trigger"] = {"id": trigger.get("id"), "type": (trigger.get("trig") or {}).get("type"),
                             "title": trigger.get("title")}
    # Observability: сквозной trace_id + тайминг + мягкие ошибки прогона (НЕ падаем молча — см. ниже).
    result["trace_id"] = _trace
    _dur = time.perf_counter() - _t0
    result.setdefault("run_metrics", {}).setdefault("timings", {})["total_ms"] = round(_dur * 1000, 1)
    # АУДИТ-ТРАССА: как модель рассуждала и ОТКУДА взяла данные — по каждому навыку: сущности →
    # происхождение (рецепт/источник/сколько записей/свежесть) + модель/токены/тайминг. Для аудитора.
    try:
        result["trace"] = _build_run_trace(agent, result)
    except Exception as ex:  # noqa: BLE001 — трасса опциональна
        result["trace_error"] = f"{type(ex).__name__}: {ex}"
    _soft = _collect_soft_errors(result)  # опциональные шаги, что отвалились (доставка/находки/нормы/…)
    if _soft:
        result["soft_errors"] = _soft
    # Доска прогона лежит в своём хранилище, а не в теле прогона: значения выводов повторяют
    # skill_outputs, и хранить их дважды — это просто вдвое больший прогон в базе.
    if parent_trace_id:
        # Ветвь веера — это дочерний прогон общего запроса. Без ссылки на родителя её трасса висит
        # сама по себе, и собрать картину «один запрос → двадцать ветвей» нечем.
        result.setdefault("run_metrics", {})["parent_trace_id"] = parent_trace_id
        obs.log_event("info", "run.child", parent_trace=parent_trace_id, trace=_trace,
                      agent=agent.get("id"), group=board_scope or "")
    # Что СКАЗАЛ прогон — строками для карточки в чате. Строилось это только на попадании в кэш, а
    # на обычном прогоне поле не заполнялось вовсе: человек видел «находок 7» и ни одной строки о
    # том, что именно нашли, хотя каждый навык отдал по 3–6 тысяч знаков. Найдено стресс-тестом 05.10.
    result["summary_lines"] = run_summary(result)
    _entries = result.pop("board_entries", None) or []
    result["op_ids"] = _ops_прогона
    saved = await run_store.save(result)
    if _entries:
        try:
            await blackboard.save("run-" + str(saved["id"]), _entries)
        except Exception as _bex:  # noqa: BLE001 — доска усиление, прогон из-за неё не падает
            obs.log_event("warning", "board.save_failed", run=saved["id"],
                          error=f"{type(_bex).__name__}: {str(_bex)[:200]}")
    _v = result.get("verdict") or {}
    await audit_store.record(started_by, "agent.run", saved["id"],
                             {"agent_id": agent.get("id"), "verdict_ok": bool(_v.get("ok")),
                              "autonomy_used": _v.get("autonomy_used"),
                              "findings": (result.get("findings_summary") or {}).get("total"),
                              "soft_errors": len(_soft), "trigger": (trigger or {}).get("id")},
                             severity=("warn" if (_soft or not _v.get("ok")) else "info"))
    # метрики прогона
    _fam = agent.get("family") or "-"
    obs.inc("abop_runs_total", family=_fam, ok=str(bool(_v.get("ok"))).lower())
    obs.observe("abop_run_seconds", _dur, family=_fam)
    _ft = (result.get("findings_summary") or {}).get("total")
    if _ft:
        obs.inc("abop_findings_total", val=float(_ft))
    _cost = ((result.get("run_metrics") or {}).get("cost") or {}).get("rub") or 0
    if _cost:
        obs.inc("abop_run_cost_rub_total", val=float(_cost))
    for _d in result.get("delivery") or []:
        obs.inc("abop_deliveries_total", channel=_d.get("channel") or "-", mode=_d.get("mode") or "-")
    if _soft:
        obs.inc("abop_run_soft_errors_total", val=float(len(_soft)))
    # Расхождения между ветвями — показатель качества данных, а не прогона: их рост означает, что
    # источники разъехались. Без экспорта наружу это видно только тому, кто открыл конкретный прогон.
    _arb = (result.get("run_metrics") or {}).get("arbitration") or {}
    if _arb.get("total"):
        obs.inc("abop_arbitration_total", val=float(_arb["total"]), family=_fam)
        if _arb.get("needs_human"):
            obs.inc("abop_arbitration_human_total", val=float(_arb["needs_human"]), family=_fam)
    _brd = (result.get("run_metrics") or {}).get("board") or {}
    if _brd.get("entries"):
        obs.gauge("abop_board_entries", float(_brd["entries"]), family=_fam)
    obs.log_event("warn" if _soft else "info", "agent.run.done", run_id=saved["id"],
                  agent=agent.get("id"), family=_fam, ok=bool(_v.get("ok")),
                  ms=round(_dur * 1000, 1), findings=_ft, soft_errors=(_soft or None))
    await langfuse_trace.emit_run(_trace, saved["id"], agent, result)  # LLM-трейс (best-effort, no-op без ключей)
    # Сохранить результат в кэш (multi-user): без волатильных/побочных полей (доставку не кэшируем).
    if _cache_key:
        try:
            _volatile = {"trace_id", "started_by", "cached", "delivery", "delivery_error",
                         "trigger", "soft_errors"}
            await run_cache_store.put(_cache_key, {k: v for k, v in result.items() if k not in _volatile})
            obs.inc("abop_run_cache_total", hit="false")
        except Exception as ex:  # noqa: BLE001 — кэш опционален
            obs.log_event("warn", "run.cache_put_fail", error=f"{type(ex).__name__}: {ex}")
    return {"saved": saved, "result": result}
