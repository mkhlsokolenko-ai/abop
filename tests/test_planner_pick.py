# -*- coding: utf-8 -*-
"""Подбор навыка: подсказка усиливает, но не выбирает; названный адресат решает спор.

04.10 владелец дал «проанализируй идею сервиса…» и получил цепочку из статус-отчёта, ADR, письма и
БФТ — навыков агента, лишь отдалённо похожего на задачу. Причина: подсказка от подбора агентов
БРАЛАСЬ ВМЕСТО собственного счёта навыка (`max`), поэтому один похожий агент диктовал план целиком,
всеми своими навыками.

Второй случай старый: «нарежь задачи в трекере» уходило в разбор почты. Оба навыка отдают задачи и
совпадают с задачей одними и теми же словами; перевес в 0.06 давала однострочная подпись, где у
разбора почты стоит слово «задачи», а у нарезки тикетов — «тикеты». Решать такой спор подписью
нельзя: человек назвал АДРЕСАТА — трекер, и выбирать нужно по тому, кто в трекер кладёт.
"""
from __future__ import annotations

from server import planner

CATALOG = {
    "to-tickets": {
        "title": "To Tickets", "short": "план → тикеты",
        "body": "из решений и плана сделай задачи трекера с исполнителем и сроком",
        "inputs": {"required": [{"from": "context"}]},
        "produces": {"path": "тикеты", "key": "id", "join": "задача"},
    },
    "mail-triage": {
        "title": "Разбор почты", "short": "письма → задачи",
        "body": "разбери почту: задачи, сроки, трекер, что требует ответа",
        "inputs": {"required": [{"from": "context"}]},
        "produces": [{"path": "задачи", "key": "задача", "join": "задача"}],
    },
    "user-stories": {
        "title": "Истории", "short": "исследование → истории",
        "body": "пользовательские истории и сценарии использования по ролям",
        "inputs": {"required": [{"from": "context"}]},
        "produces": [{"path": "истории", "key": "id"}],
        "delivery": {"system": "bookstack", "type": "page.publish"},
    },
    "c4-diagram": {
        "title": "Схема C4", "short": "система → схема",
        "body": "диаграмма контейнеров и связей как код",
        "inputs": {"required": [{"from": "context"}]},
        "produces": [{"path": "элементы", "key": "id"}],
    },
}


def _plan(task, **kw):
    p = planner.plan(task, CATALOG, entities=set(), slots=set(), max_steps=3, **kw)
    return [s.get("skill") for s in (p.get("steps") or [])]


def test_названный_трекер_решает_спор_задач():
    """«задачи в трекере» — к тому, кто кладёт в трекер тикеты, а не к разбору почты."""
    assert _plan("нарежь задачи в трекере")[0] == "to-tickets"


def test_источник_не_путается_с_адресатом():
    """«разбери почту» — почта тут источник: адресата нет, и подарка навыкам-письмам тоже."""
    assert planner._named_targets("разбери почту и собери задачи") == set()
    assert planner._named_targets("отправь сводку на почту") == {"mailpit", "yandex"}
    assert planner._named_targets("выложи в вики") == {"bookstack"}
    assert planner._named_targets("нарежь задачи в трекере") == {"redmine"}


def test_нацеленность_берётся_из_контракта():
    """Либо объявленная доставка, либо то, что навык отдаёт (тикеты — слово трекера)."""
    assert planner._aims_at(CATALOG["user-stories"], {"bookstack"}) is True   # объявленная доставка
    assert planner._aims_at(CATALOG["to-tickets"], {"redmine"}) is True       # отдаёт тикеты
    assert planner._aims_at(CATALOG["mail-triage"], {"redmine"}) is False     # отдаёт задачи, не тикеты
    assert planner._aims_at(CATALOG["to-tickets"], set()) is False            # адресат не назван


def test_подсказка_не_создаёт_кандидата():
    """Похожий агент пользуется навыком — это не причина включить навык, который не о задаче."""
    task = "нарежь задачи в трекере"
    assert "c4-diagram" not in _plan(task, hints={"c4-diagram": 1.0})


# Два одинаковых по сути навыка: сами по себе они равны, и порядок решает идентификатор. Это и есть
# место, где подсказка полезна — спор равных.
TIE = {
    "aaa-reader": {"title": "Первый", "short": "письма → задачи",
                   "body": "разбери письма: задачи, сроки, ответственные",
                   "inputs": {"required": [{"from": "context"}]},
                   "produces": [{"path": "задачи", "key": "задача"}]},
    "zzz-reader": {"title": "Второй", "short": "письма → задачи",
                   "body": "разбери письма: задачи, сроки, ответственные",
                   "inputs": {"required": [{"from": "context"}]},
                   "produces": [{"path": "задачи", "key": "задача"}]},
}


def test_подсказка_решает_спор_равных():
    """Навыки равны по счёту — тогда подсказка «похожий агент пользуется этим» и нужна."""
    task = "разбери письма: задачи, сроки и ответственные"
    plain = planner.plan(task, TIE, entities=set(), slots=set(), max_steps=1)
    assert [s["skill"] for s in plain["steps"]] == ["aaa-reader"], "ничья решается не идентификатором"
    hinted = planner.plan(task, TIE, entities=set(), slots=set(), max_steps=1,
                          hints={"zzz-reader": 1.0})
    assert [s["skill"] for s in hinted["steps"]] == ["zzz-reader"], "подсказка ни на что не влияет"


def test_названный_адресат_сильнее_подсказки():
    """Слова человека весят больше статистики по соседним агентам."""
    assert _plan("нарежь задачи в трекере", hints={"mail-triage": 1.0})[0] == "to-tickets"


def test_в_исходниках_нет_управляющих_символов():
    """Защита от капкана, который дважды сработал в этой сессии: «\b» в патче превращался в
    символ backspace, и правило молча перестаёт работать — в регулярке это не видно глазом."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    bad = []
    for p in list((root / "server").rglob("*.py")) + list((root / "cli").rglob("*.py")) + list((root / "tests").rglob("*.py")):
        txt = p.read_text(encoding="utf-8")
        for ch in ("\x08", "\x0c", "\x07", "\x1b", "\x00"):
            if ch in txt:
                bad.append(f"{p.relative_to(root)}: символ {hex(ord(ch))}")
    assert not bad, "управляющий символ в исходнике: " + "; ".join(bad)


def test_подсказка_слабее_названного_адресата_по_построению():
    """Неравенство между весами — часть правила, а не случайность подбора чисел."""
    assert planner.HINT_CAP < planner.TARGET_BONUS,         "подсказка от похожего агента стала сильнее слов человека про адресата"
    assert 0 < planner.HINT_WEIGHT <= 1 and planner.MIN_CANDIDATE > 0
