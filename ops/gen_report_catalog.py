# -*- coding: utf-8 -*-
"""Пересборка раздела «Каталог шаблонов навыков» в docs/REPORT_TEMPLATES_CATALOG.md.

Каталог описывает, что каждый навык отдаёт (поля `json_schema` — это и есть материал вёрстки), чем
питается и какой формой отчёта оформляется. Руками его вести нельзя: 55 шаблонов, и после правки
любого из них описание расходится с кодом молча — дизайнер проектирует под поля, которых уже нет.

Разделы 1–8 документа (разбор слоёв, диагноз, задание на проектирование) написаны руками и
сохраняются: скрипт переписывает только раздел 9 ниже маркера.

Запуск:  python ops/gen_report_catalog.py
"""
import glob
import io
import json
import os
import re

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ДОК = os.path.join(КОРЕНЬ, "docs", "REPORT_TEMPLATES_CATALOG.md")
МАРКЕР = "## 9. Каталог шаблонов навыков"


def читать(p):
    return json.load(io.open(p, encoding="utf-8"))


ФОРМЫ = читать(os.path.join(КОРЕНЬ, "reports", "index.json"))
ШАБЛОНЫ = {}
for p in glob.glob(os.path.join(КОРЕНЬ, "skills", "*", "template.json")):
    sid = os.path.basename(os.path.dirname(p))
    try:
        ШАБЛОНЫ[sid] = читать(p)
    except Exception as e:  # noqa: BLE001
        ШАБЛОНЫ[sid] = {"_ошибка": str(e)}

# навык → форма
ФОРМА_НАВЫКА = {}
for fid, f in ФОРМЫ.items():
    for sid in (f.get("for_skills") or []):
        ФОРМА_НАВЫКА[sid] = fid
# Старые формы по ВИДУ результата: привязки `for_skills` у них нет, форма выбирается по тому, что
# в результате (`_auto_template_id`), а разделы свёрстаны плейсхолдерами в HTML, не раскладкой.
ПО_ВИДУ = {"audit1c": "audit1c-", "invest": "invest1c-"}
for fid, prefix in ПО_ВИДУ.items():
    for sid in ШАБЛОНЫ:
        if sid.startswith(prefix):
            ФОРМА_НАВЫКА.setdefault(sid, fid)

ТИП = {"string": "строка", "array": "список", "object": "объект", "number": "число",
       "integer": "целое", "boolean": "да/нет"}


def тип(sch):
    t = sch.get("type")
    if t == "array":
        it = sch.get("items") or {}
        внутр = it.get("type")
        if внутр == "object":
            return "список объектов"
        return f"список {ТИП.get(внутр, внутр or '?')}"
    if sch.get("enum"):
        return "из набора"
    return ТИП.get(t, str(t))


def кратко(s, n=110):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def графы(sch):
    """Для списка объектов — перечень граф элемента; для объекта — перечень полей."""
    if sch.get("type") == "array":
        it = sch.get("items") or {}
        props = it.get("properties") or {}
    elif sch.get("type") == "object":
        props = sch.get("properties") or {}
    else:
        props = {}
    out = []
    for k, v in props.items():
        e = f"`{k}`"
        if v.get("enum"):
            e += " (" + "/".join(str(x) for x in v["enum"][:5]) + ")"
        out.append(e)
    return ", ".join(out)


def входы(tpl):
    inp = tpl.get("inputs") or {}
    строки = []
    for вид, ключ in (("обязательно", "required"), ("необязательно", "optional")):
        for it in (inp.get(ключ) or []):
            f = it.get("from")
            if f == "data":
                поля = ", ".join(it.get("fields") or []) or "все поля"
                строки.append(f"{вид}: данные `{it.get('entity')}` ({поля})")
            elif f == "skill":
                поля = ", ".join(it.get("fields") or [])
                строки.append(f"{вид}: от навыка `{it.get('skill')}` → `{it.get('path')}`"
                              + (f" ({поля})" if поля else ""))
            elif f == "context":
                строки.append(f"{вид}: текст задачи" + (f" — {кратко(it.get('note'), 70)}"
                                                        if it.get("note") else ""))
            else:
                строки.append(f"{вид}: {json.dumps(it, ensure_ascii=False)[:80]}")
    return строки or ["не объявлены"]


def отдаёт(tpl):
    out = []
    pr = tpl.get("produces")
    pr = pr if isinstance(pr, list) else ([pr] if isinstance(pr, dict) else [])
    for it in pr:
        s = f"`{it.get('path')}`"
        if it.get("key"):
            s += f" (ключ `{it['key']}`"
            s += f", связь «{it['join']}»)" if it.get("join") else ")"
        elif it.get("join"):
            s += f" (связь «{it['join']}»)"
        out.append(s)
    return ", ".join(out) or "—"


def доставка(tpl):
    d = tpl.get("delivery")
    specs = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
    return ", ".join(f"`{x.get('system')}` → `{x.get('type')}`" for x in specs if x) or "—"


def блок_навыка(sid, tpl):
    L = []
    имя = tpl.get("name") or sid
    L.append(f"#### `{sid}` — {имя}")
    L.append("")
    fid = ФОРМА_НАВЫКА.get(sid)
    L.append(f"* **Форма отчёта:** {'`' + fid + '`' if fid else '`default` (своей формы нет)'}")
    L.append(f"* **Входы:** " + "; ".join(входы(tpl)))
    L.append(f"* **Отдаёт в цепочку:** {отдаёт(tpl)}")
    if tpl.get("max_tokens"):
        L.append(f"* **Потолок ответа:** {tpl['max_tokens']} токенов")
    if доставка(tpl) != "—":
        L.append(f"* **Доставка наружу:** {доставка(tpl)}")
    if tpl.get("slots"):
        L.append(f"* **Предмет работы (slots):** {json.dumps(tpl['slots'], ensure_ascii=False)[:160]}")
    L.append("")
    sch = tpl.get("json_schema") or {}
    props = sch.get("properties") or {}
    req = set(sch.get("required") or [])
    if props:
        L.append("| поле результата | тип | обяз. | что внутри |")
        L.append("|---|---|---|---|")
        for k, v in props.items():
            g = графы(v)
            что = кратко(v.get("description") or "")
            if g:
                что = (что + " · графы: " + g) if что else "графы: " + g
            L.append(f"| `{k}` | {тип(v)} | {'да' if k in req else '—'} | {кратко(что, 220)} |")
        L.append("")
    инстр = кратко(tpl.get("instruction"), 400)
    if инстр:
        L.append(f"> **Инструкция модели:** {инстр}")
        L.append("")
    return L


L = []
без_формы = sorted(sid for sid in ШАБЛОНЫ if sid not in ФОРМА_НАВЫКА)
L.append(f"Шаблонов навыков: **{len(ШАБЛОНЫ)}**. Форм отчёта: **{len(ФОРМЫ)}**, из них с раскладкой: "
         f"**{sum(1 for f in ФОРМЫ.values() if f.get('layout'))}**. "
         f"Навыков без своей формы (оформляются `default`): **{len(без_формы)}**.")
L.append("")

for fid, f in ФОРМЫ.items():
    навыки = sorted(s for s, v in ФОРМА_НАВЫКА.items() if v == fid)
    if not навыки:
        continue
    lay = f.get("layout") or []
    L.append(f"### Форма `{fid}` — {f.get('name')}")
    L.append("")
    if lay:
        L.append(f"**Раскладка ({len(lay)} блоков):** " +
                 " → ".join(f"`{b.get('t')}`" + (f"[{кратко(b.get('title'), 24)}]" if b.get("title") else "")
                            for b in lay))
    else:
        L.append("**Раскладки нет** — это форма старого типа: разделы захардкожены плейсхолдерами "
                 "в `reports/" + fid + ".html`, порядок менять можно только правкой HTML. "
                 "Выбирается по виду результата, а не по составу навыков.")
    L.append("")
    for sid in навыки:
        L += блок_навыка(sid, ШАБЛОНЫ[sid])
    L.append("")

if без_формы:
    L.append("### Навыки без своей формы — оформляются `default`")
    L.append("")
    L.append("Их результат попадает в стандартный отчёт секциями «навык → текст/таблица». Именно здесь "
             "отчёт выглядит хуже всего: порядок разделов случайный, графы без подписей, "
             "смысловые блоки не отделены друг от друга.")
    L.append("")
    for sid in без_формы:
        L += блок_навыка(sid, ШАБЛОНЫ[sid])

# Разделы 1–8 написаны руками — их не трогаем: переписываем ровно раздел 9.
старое = io.open(ДОК, encoding="utf-8").read() if os.path.exists(ДОК) else МАРКЕР + "\n"
i = старое.find(МАРКЕР)
if i < 0:
    raise SystemExit("в " + ДОК + " нет маркера «" + МАРКЕР + "» — каталог дописывать некуда")
новое = старое[: i + len(МАРКЕР)] + "\n\n" + "\n".join(L) + "\n"
io.open(ДОК, "w", encoding="utf-8", newline="\n").write(новое)
print("каталог обновлён:", ДОК, "·", len(L), "строк раздела 9")
if без_формы:
    print("навыки без своей формы:", ", ".join(без_формы))
