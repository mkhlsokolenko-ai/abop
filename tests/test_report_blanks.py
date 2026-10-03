"""Бланк не должен ссылаться на поле, которого навык не отдаёт.

Графа документа, указывающая в несуществующее поле, навсегда остаётся пустой — и получатель читает
это как «данных нет», хотя их никто и не собирался туда класть. Проверяем раскладки по json_schema
тех навыков, для которых форма объявлена: и пути (`заявка.запрошенная_сумма`), и графы реестров и
карточек (`пункт_политики`), включая альтернативы через «|».
"""
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
IDX = json.loads((ROOT / "reports" / "index.json").read_text(encoding="utf-8"))

# Блоки, которые документ собирает сам (служебные врезки и подписи) — поля не читают.
_NO_REFS = ("note", "sign", "ctx")


def _schema(sid: str) -> dict:
    p = ROOT / "skills" / sid / "template.json"
    if not p.is_file():
        return {}
    return (json.loads(p.read_text(encoding="utf-8")) or {}).get("json_schema") or {}


def _props(node: dict) -> dict:
    return (node or {}).get("properties") or {}


def _resolve(schemas: list[dict], path: str):
    """Узел схемы по пути «a.b.c» хотя бы у одного навыка формы. Нет — None."""
    for sch in schemas:
        node = sch
        ok = True
        for part in path.split("."):
            nxt = _props(node).get(part)
            if nxt is None and (node or {}).get("type") == "array":
                nxt = _props((node or {}).get("items") or {}).get(part)
            if nxt is None:
                ok = False
                break
            node = nxt
        if ok:
            return node
    return None


def _any(schemas: list[dict], ref: str):
    """Ссылка бланка (с альтернативами «|» и привязкой «навык:путь») разрешается хотя бы одна."""
    for alt in str(ref or "").split("|"):
        alt = alt.strip()
        if not alt:
            continue
        path = alt.split(":", 1)[1] if ":" in alt else alt
        node = _resolve(schemas, path)
        if node is not None:
            return node
    return None


def _item_props(schemas: list[dict], src: str) -> dict | None:
    """Свойства элемента массива, на который смотрит блок-перечень. Не массив — None.

    Объединяем по всем навыкам формы: «исполнение» у сверки дорожной карты и у приёмки — разные
    наборы графов, и документ показывает оба, заполняя то, что дал работавший навык.
    """
    out: dict = {}
    for alt in str(src or "").split("|"):
        alt = alt.strip()
        if not alt:
            continue
        path = alt.split(":", 1)[1] if ":" in alt else alt
        for sch in schemas:
            node = _resolve([sch], path)
            if isinstance(node, dict) and node.get("type") == "array":
                out.update(_props(node.get("items") or {}))
    return out or None


FORMS = [(tid, m) for tid, m in sorted(IDX.items()) if m.get("layout")]


def test_есть_бланки_под_вертикали():
    """Бланков должно быть много: один каркас на все вертикали — это и была проблема."""
    assert len(FORMS) >= 15, f"бланков с раскладкой всего {len(FORMS)}"
    assert all(m.get("for_skills") for _t, m in FORMS), "бланк без привязки к навыкам не выберется"


@pytest.mark.parametrize("tid,meta", FORMS, ids=[t for t, _ in FORMS])
def test_раскладка_ссылается_на_существующие_поля(tid, meta):
    schemas = [s for s in (_schema(sid) for sid in meta.get("for_skills") or []) if s]
    assert schemas, f"{tid}: ни одного шаблона навыка — проверять нечего"
    bad: list[str] = []
    for b in meta["layout"]:
        kind = str(b.get("t") or "")
        if kind in _NO_REFS:
            continue
        # у блока-перечня badge/code/head — графы элемента; у блока решения badge/why — путь в корне
        root_keys = ("src",) if kind in ("cards", "register", "list") else ("src", "badge", "why")
        for key in root_keys:
            if b.get(key) and _any(schemas, b[key]) is None:
                bad.append(f"{tid}/{kind}.{key} → {b[key]}")
        for pair in (b.get("meta") or []) + (b.get("rows") or []):
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and _any(schemas, pair[1]) is None:
                bad.append(f"{tid}/{kind} «{pair[0]}» → {pair[1]}")
        # графы реестров и карточек ищутся внутри элемента массива, а не в корне схемы
        items = _item_props(schemas, b.get("src") or "")
        if items is None:
            continue
        pairs = list(b.get("cols") or []) + list(b.get("graphs") or [])
        pairs += [(k, b[k]) for k in ("head", "badge", "code") if b.get(k)]
        for pair in pairs:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                continue
            if not any(a.strip() in items for a in str(pair[1]).split("|")):
                bad.append(f"{tid}/{kind}[{b.get('src')}] «{pair[0]}» → {pair[1]}")
    assert not bad, "графы указывают в несуществующие поля:\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("tid,meta", FORMS, ids=[t for t, _ in FORMS])
def test_бланк_это_документ_а_не_каркас(tid, meta):
    """У документа должны быть шапка, оговорка о границах и содержательные разделы.

    Именно этого не хватало прежним формам: одинаковая последовательность плейсхолдеров и одна
    строка-оговорка снизу — это каркас, а не бланк вертикали.
    """
    kinds = [str(b.get("t") or "") for b in meta["layout"]]
    assert kinds[0] == "head", f"{tid}: документ начинается не с шапки"
    assert "note" in kinds, f"{tid}: нет оговорки о границах документа"
    assert len(meta["layout"]) >= 8, f"{tid}: {len(meta['layout'])} блоков — это каркас, не бланк"
    head = meta["layout"][0]
    assert head.get("note"), f"{tid}: в шапке не объявлен метод"
    # Объём работы — в шапке; у письма его роль играет угловой штамп (кому, тема, цель).
    assert head.get("meta") or "stamp" in kinds, f"{tid}: в шапке нет объёма работы"


def test_формы_не_спорят_за_навык():
    """Один навык — одна форма: иначе выбор зависит от порядка строк в базе."""
    seen: dict[str, str] = {}
    clash = []
    for tid, m in IDX.items():
        for sid in m.get("for_skills") or []:
            if sid in seen:
                clash.append(f"{sid}: {seen[sid]} и {tid}")
            seen[sid] = tid
    assert not clash, "навык закреплён за двумя формами: " + "; ".join(clash)
