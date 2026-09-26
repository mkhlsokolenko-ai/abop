"""Карточки находок аудита 1С для пилота (Блок 4): доменные поля, цепочка документов с разрывом,
слой объяснения «что не сходится / откуда / чем грозит / что проверить» и метрики пилота.

Источник фактов — детерминированный движок проверок (`cli/ape.py: audit1c_run_checks/trace_chains`);
здесь ничего не «находится» заново — только раскладывается по участкам, дополняется нормой из
`demo/audit1c/norms/*.md` (написано экспертом один раз, не RAG «на лету») и последствиями
(уточнённые декларации, элиминация, договорные условия — замечания эксперта владельца 2026-09-15).
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

NORMS_DIR = Path(__file__).resolve().parent.parent / "demo" / "audit1c" / "norms"
ONE_C_WEB = (os.getenv("ABOP_1C_WEB_URL") or "http://201.51.5.24:8092/audit/").rstrip("/") + "/"

# проверка → участки, тип расхождения, файл нормы + ключ секции, шаблон цепочки, последствия
CHECK_META: dict[str, dict] = {
    "Реализация без счёта-фактуры выданного": {
        "from": "Реализация", "to": "НДС", "kind": "разрыв цепочки · НДС не предъявлен",
        "norm": ("01_nds_scheta_faktury.md", "Реализация без счёта-фактуры выданного"),
        "chain": [("Реализация", "есть"), ("Счёт-фактура выданный", "РАЗРЫВ"), ("Оплата · взаиморасчёты", "н/д")],
        "consequences": ["уточнённая декларация по НДС за период реализации (гл. 21 НК РФ)",
                         "выставить счёт-фактуру покупателю; проверить книгу продаж"],
        "contract": True},
    "Поступление без счёта-фактуры полученного": {
        "from": "Поступление", "to": "НДС (вычет)", "kind": "разрыв цепочки · вычет не подтверждён",
        "norm": ("01_nds_scheta_faktury.md", "Поступление без счёта-фактуры полученного"),
        "chain": [("Поступление", "есть"), ("Счёт-фактура полученный", "РАЗРЫВ"), ("Оплата · взаиморасчёты", "н/д")],
        "consequences": ["снять вычет или запросить счёт-фактуру у поставщика; при заявленном вычете — уточнённая декларация по НДС",
                         "проверить отражение НДС на 19 и списание в 68.02"],
        "contract": True},
    "Оплата без документа-основания": {
        "from": "Банк", "to": "Взаиморасчёты", "kind": "разрыв цепочки · платёж без основания",
        "norm": ("05_deb_kred_raschety.md", "без документа-основания"),
        "chain": [("Документ-основание", "РАЗРЫВ"), ("Платёж", "есть")],
        "consequences": ["привязать платёж к документу поставки/реализации; акт сверки с контрагентом",
                         "проверить сальдо 60/62 по контрагенту — риск нераспознанного аванса"],
        "contract": True},
    "Поступление не оплачено (кредиторка)": {
        "from": "Поступление", "to": "Взаиморасчёты", "kind": "открытая кредиторская задолженность",
        "norm": ("05_deb_kred_raschety.md", "кредитор"),
        "chain": [("Поступление", "есть"), ("Счёт-фактура полученный", "н/д"), ("Оплата поставщику", "РАЗРЫВ")],
        "consequences": ["сверить срок оплаты по договору; при просрочке — риск неустойки",
                         "проверить сальдо 60 и классификацию задолженности в отчётности"],
        "contract": True},
    "Реализация не оплачена (дебиторка)": {
        "from": "Реализация", "to": "Взаиморасчёты", "kind": "открытая дебиторская задолженность",
        "norm": ("05_deb_kred_raschety.md", "дебитор"),
        "chain": [("Реализация", "есть"), ("Счёт-фактура выданный", "н/д"), ("Оплата покупателя", "РАЗРЫВ")],
        "consequences": ["сверить срок оплаты по договору; резерв по сомнительным долгам при просрочке (ПБУ/ФСБУ)",
                         "проверить сальдо 62 и претензионную работу"],
        "contract": True},
    "Реализация без списания себестоимости": {
        "from": "Реализация", "to": "Себестоимость (90.02)", "kind": "инвариант проводок · прибыль завышена",
        "norm": ("02_fsbu5_sebestoimost.md", "Реализация без списания себестоимости"),
        "chain": [("Выручка Дт 62 Кт 90.01", "есть"), ("Себестоимость Дт 90.02 Кт 41", "РАЗРЫВ")],
        "consequences": ["уточнённая декларация по налогу на прибыль (расходы занижены → налог завышен или наоборот при корректировке)",
                         "доначислить проводку списания; проверить остатки 41/43 (ФСБУ 5/2019)"],
        "contract": False},
    "Дубль контрагента по ИНН": {
        "from": "НСИ · Контрагенты", "to": "Взаиморасчёты", "kind": "дубль справочника · задвоение расчётов",
        "norm": ("04_nsi_kontragenty.md", "Дубл"),
        "chain": [("Карточка контрагента №1", "есть"), ("Карточка контрагента №2 (тот же ИНН)", "ДУБЛЬ"), ("Сальдо 60/62 по каждой", "н/д")],
        "consequences": ["объединить карточки; перепривязать документы к одной", "сверить сальдо по обеим карточкам — риск искажения отчётности"],
        "contract": False},
    "Внутригрупповой оборот (ВГО)": {
        "from": "Организация А", "to": "Организация Б · консолидация", "kind": "ВГО без элиминации",
        "norm": ("03_vgo_konsolidatsiya.md", "без пометки ВГО"),
        "chain": [("Реализация (орг. А)", "есть"), ("Поступление (орг. Б)", "н/д"), ("Элиминация в консолидации", "РАЗРЫВ")],
        "consequences": ["пометить оборот как внутригрупповой; исключить при консолидации (IFRS 10)",
                         "сверить зеркальные документы и НДС у второй организации группы"],
        "contract": True},
    "Переходящая операция (разные периоды)": {
        "from": "Период N", "to": "Период N+1", "kind": "граница периода · cut-off",
        "norm": ("06_period_cutoff.md", "Переход"),
        "chain": [("Документ-основание (год N)", "есть"), ("Оплата (год N+1)", "есть"), ("Отражение на границе периода", "ПРОВЕРИТЬ")],
        "consequences": ["проверить период признания дохода/расхода и остатков на 31.12", "при ошибке периода — корректировка отчётности (ПБУ 22/2010)"],
        "contract": True},
}
INVEST_META = {"from": "Реализация", "kind": "расследование от симптома", "norm": ("01_nds_scheta_faktury.md", "Реализация без счёта-фактуры выданного")}

BROKEN = {"РАЗРЫВ", "ДУБЛЬ"}


@lru_cache(maxsize=64)
def norm_excerpt(file: str, key: str) -> dict:
    """Секция нормы из md эксперта: заголовок, суть, «Чем грозит», «Что проверить», ссылка на статью."""
    p = NORMS_DIR / file
    out = {"file": file, "title": file, "url": "/api/audit1c/norms/" + file, "risk": "", "action": "", "ref": "", "body": ""}
    if not p.exists():
        return out
    text = p.read_text(encoding="utf-8")
    out["title"] = (re.search(r"^#\s+(.+)$", text, re.M) or [None, file])[1].strip() if re.search(r"^#\s+(.+)$", text, re.M) else file
    sections = re.split(r"^##\s+", text, flags=re.M)
    pick = None
    for sec in sections[1:]:
        head = sec.split("\n", 1)[0]
        if key.lower() in sec.lower()[:400] or key.lower() in head.lower():
            pick = sec
            break
    if pick is None and len(sections) > 1:
        pick = sections[1]
    if pick:
        head, body = (pick.split("\n", 1) + [""])[:2]
        out["section"] = head.strip()
        m = re.search(r"\*\*Чем грозит:\*\*\s*(.+)", body)
        out["risk"] = m.group(1).strip() if m else ""
        m = re.search(r"\*\*Что проверить:\*\*\s*(.+)", body)
        out["action"] = m.group(1).strip() if m else ""
        m = (re.search(r"\((ст\.[^)]+|гл\.[^)]+|ФСБУ[^)]+|ПБУ[^)]+|IFRS[^)]+)\)", body)
             or re.search(r"\((ст\.[^)]+|гл\.[^)]+|ФСБУ[^)]+|ПБУ[^)]+|IFRS[^)]+)\)", text))
        out["ref"] = m.group(1).strip() if m else ""
        out["body"] = re.sub(r"\*\*(Чем грозит|Что проверить):\*\*.*", "", body).strip()[:600]
        if not out["risk"]:
            m = re.search(r"\*\*Чем грозит:\*\*\s*(.+)", text); out["risk"] = m.group(1).strip() if m else ""
        if not out["action"]:
            m = re.search(r"\*\*Что проверить:\*\*\s*(.+)", text); out["action"] = m.group(1).strip() if m else ""
    return out


def _amount_from(f: dict) -> float | None:
    for k in ("сумма", "amount"):
        if f.get(k) not in (None, ""):
            try:
                return float(f[k])
            except (TypeError, ValueError):
                pass
    m = re.search(r"(\d[\d\s]{2,}[.,]\d{2})\s*(?:₽|руб)", (f.get("описание") or "") + " " + (f.get("доказательство") or ""))
    if m:
        try:
            return float(m.group(1).replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    return None


def _fmt_rub(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s + " ₽"


def doc_url(doc: dict | None) -> str:
    if not doc or not doc.get("uuid") or not doc.get("тип"):
        return ""
    return ONE_C_WEB + "#e1cib/data/Документ." + str(doc["тип"]) + "?ref=" + str(doc["uuid"]).replace("-", "")


def _doc_line(doc: dict | None) -> str:
    if not doc:
        return ""
    return f"{doc.get('тип') or 'Документ'} № {doc.get('Номер') or '—'} от {doc.get('Дата') or '—'}" + \
        (f" · {doc.get('Контрагент')}" if doc.get("Контрагент") else "")


def _chain(f: dict, meta: dict) -> list[dict]:
    """Цепочка из движка (если он её положил) или шаблон проверки с известным разрывом."""
    raw = f.get("цепочка")
    if isinstance(raw, list) and raw:
        return [{"label": str(l.get("звено") or ""), "status": str(l.get("статус") or ("есть" if l.get("есть") else "РАЗРЫВ")),
                 "broken": str(l.get("статус") or "") in BROKEN or (l.get("есть") is False),
                 "doc": l.get("документ"), "doc_line": _doc_line(l.get("документ")), "url": doc_url(l.get("документ")),
                 "note": l.get("примечание") or ""} for l in raw if isinstance(l, dict)]
    doc = f.get("документ")
    out = []
    for i, (label, st) in enumerate(meta.get("chain") or []):
        out.append({"label": label, "status": st, "broken": st in BROKEN,
                    "doc": doc if (i == 0 and st == "есть") else None,
                    "doc_line": _doc_line(doc) if (i == 0 and st == "есть") else "",
                    "url": doc_url(doc) if (i == 0 and st == "есть") else "", "note": ""})
    return out


def card_from_finding(run: dict, f: dict, label: dict | None) -> dict:
    check = str(f.get("проверка") or "")
    meta = CHECK_META.get(check) or {"from": "—", "to": "—", "kind": check or "находка", "norm": None, "chain": [], "consequences": [], "contract": False}
    norm = norm_excerpt(*meta["norm"]) if meta.get("norm") else {"title": "", "url": "", "risk": "", "action": "", "ref": ""}
    rag = f.get("нормы_rag") or []
    doc = f.get("документ")
    amount = _amount_from(f)
    chain = _chain(f, meta)
    contract_hint = ("проверьте условия договора с контрагентом «" + str(doc.get("Контрагент")) + "» — сроки оплаты, отсрочка, особые условия могут объяснять расхождение") \
        if (meta.get("contract") and doc and doc.get("Контрагент")) else ""
    return {
        "id": str(f.get("id") or ""), "run_id": run.get("run_id") or run.get("id") or "", "agent_id": run.get("agent_id"),
        "created_at": run.get("created_at"), "cls": str(f.get("класс") or ""), "severity": str(f.get("серьёзность") or ""),
        "check": check, "kind": meta["kind"], "section_from": meta["from"], "section_to": meta["to"],
        "cross": meta["from"] != meta["to"],
        "amount": amount, "amount_text": _fmt_rub(amount),
        "doc": doc, "doc_line": _doc_line(doc), "doc_url": doc_url(doc),
        "chain": chain, "broken_link": next((c["label"] for c in chain if c["broken"]), ""),
        "explain": {
            "what": f.get("описание") or "",
            "where": (_doc_line(doc) + (" · " if doc else "") + str(f.get("доказательство") or "")).strip(" ·"),
            "risk": (rag[0] if rag else "") or norm.get("risk") or "",
            "risk_ref": norm.get("ref") or "",
            "consequences": list(meta.get("consequences") or []) + ([contract_hint] if contract_hint else []),
            "action": norm.get("action") or "",
        },
        "norm": {"title": norm.get("title") or "", "section": norm.get("section") or "", "url": norm.get("url") or "", "file": norm.get("file") or ""},
        "label": label,
    }


def card_from_investigation(run: dict, iv: dict, label: dict | None) -> dict:
    chain_raw = iv.get("цепочка") or []
    broken = next((l for l in chain_raw if isinstance(l, dict) and (str(l.get("статус") or "") in BROKEN or l.get("есть") is False)), None)
    to = (broken or {}).get("звено") or "Взаиморасчёты · НДС"
    rec = iv.get("сверка") or {}
    start = iv.get("старт") or {}
    bl = str((broken or {}).get("звено") or "")
    norm = norm_excerpt("05_deb_kred_raschety.md", "дебитор") if "Оплата" in bl else norm_excerpt(*INVEST_META["norm"])
    rag = iv.get("нормы_rag") or []
    amount = rec.get("разница_₽") if rec.get("разница_₽") not in (None, 0, 0.0) else rec.get("реализация_₽")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    f = {"проверка": "Расследование от симптома", "цепочка": chain_raw, "документ": start}
    chain = _chain(f, {"chain": []})
    return {
        "id": str(iv.get("id") or ""), "run_id": run.get("run_id") or run.get("id") or "", "agent_id": run.get("agent_id"),
        "created_at": run.get("created_at"), "cls": "B", "severity": str(iv.get("серьёзность") or "высокая"),
        "check": "Расследование от симптома", "kind": str(iv.get("симптом") or INVEST_META["kind"]),
        "section_from": INVEST_META["from"], "section_to": str(to).split(" (")[0], "cross": True,
        "amount": amount, "amount_text": _fmt_rub(amount),
        "doc": start, "doc_line": _doc_line(start), "doc_url": doc_url(start),
        "chain": chain, "broken_link": (broken or {}).get("звено") or "",
        "explain": {
            "what": str(iv.get("симптом") or "") + (". " + str(iv.get("описание")) if iv.get("описание") else ""),
            "where": " → ".join(f"{l.get('звено')}: {l.get('статус')}" + (f" ({_doc_line(l.get('документ'))})" if l.get("документ") else "") for l in chain_raw if isinstance(l, dict)),
            "risk": (rag[0] if rag else "") or norm.get("risk") or "",
            "risk_ref": norm.get("ref") or "",
            "consequences": ["сверка сумм по звеньям: реализация " + _fmt_rub(rec.get("реализация_₽")) + " · оплачено " + _fmt_rub(rec.get("оплачено_₽")) + " · НДС " + _fmt_rub(rec.get("ндс_₽")),
                             "устранить разрыв в звене «" + ((broken or {}).get("звено") or "—") + "», затем повторить сверку"],
            "action": norm.get("action") or "",
        },
        "norm": {"title": norm.get("title") or "", "section": norm.get("section") or "", "url": norm.get("url") or "", "file": norm.get("file") or ""},
        "label": label,
    }


def cards_for_run(run: dict, labels: dict[str, dict]) -> list[dict]:
    out = []
    for f in run.get("findings") or []:
        if isinstance(f, dict) and f.get("проверка"):
            out.append(card_from_finding(run, f, labels.get(str(f.get("id") or ""))))
    for iv in run.get("investigations") or []:
        if isinstance(iv, dict) and iv.get("id"):
            out.append(card_from_investigation(run, iv, labels.get(str(iv.get("id")))))
    order = {"высокая": 0, "средняя": 1, "низкая": 2}
    out.sort(key=lambda c: (order.get(c["severity"], 3), c["cls"], c["id"]))
    return out


def pilot_metrics(cards: list[dict]) -> dict:
    """Метрики пилота (demo-1c-auditor): точность ≥80 % значимых (слепая разметка), доля межучастковых ≥50 %,
    ≥3 из 20 «не нашли бы вручную»; считаются по экспертной разметке в интерфейсе, не в таблице."""
    total = len(cards)
    labeled = [c for c in cards if c.get("label")]
    confirmed = [c for c in labeled if (c["label"] or {}).get("decision") == "confirmed"]
    rejected = [c for c in labeled if (c["label"] or {}).get("decision") == "rejected"]
    decided = len(confirmed) + len(rejected)
    manual_miss = [c for c in confirmed if (c["label"] or {}).get("manual_miss")]
    cross = [c for c in cards if c.get("cross")]
    cross_conf = [c for c in confirmed if c.get("cross")]
    precision = round(100 * len(confirmed) / decided) if decided else None
    cross_share = round(100 * len(cross_conf) / len(confirmed)) if confirmed else (round(100 * len(cross) / total) if total else None)
    return {"total": total, "labeled": len(labeled), "confirmed": len(confirmed), "rejected": len(rejected),
            "unsure": len(labeled) - decided, "precision": precision, "precision_ok": (precision is not None and precision >= 80),
            "cross_share": cross_share, "cross_ok": (cross_share is not None and cross_share >= 50),
            "manual_miss": len(manual_miss), "manual_miss_ok": len(manual_miss) >= 3,
            "thresholds": {"precision": 80, "cross_share": 50, "manual_miss": 3},
            "by_class": {k: sum(1 for c in cards if c["cls"] == k) for k in ("A", "B", "C", "D")}}
