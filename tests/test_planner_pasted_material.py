# -*- coding: utf-8 -*-
"""Вставленный в чат текст — материал к задаче, а не перечень этапов.

05.10 владелец вставил в чат свою идею вместе с ответом ассистента (полторы тысячи знаков) и
попросил её разобрать. Планировщик разрезал текст по точкам на одиннадцать «этапов» и подобрал
исполнителя к КАЖДОМУ куску: абзац про спам в Awesome-списках дал разбор почты со счётом 0.95,
абзац про альтернативы — ADR, упоминание «дайджест в Telegram/Email» — черновик письма. Человек
получил цепочку «Status Report → Разбор почты → Weekly Update → Восстановление ветки → ADR»,
не имеющую отношения к просьбе.

Правило: работу называет УКАЗАНИЕ (первый абзац с действием), остальное — то, над чем работают.
Материал остаётся контекстом прогона, но на выбор исполнителя не влияет.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from cli import ape
from server import orchestrator, planner

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTITIES = {"doc1c", "ref1c", "email", "issue", "customer", "transaction",
            "roadmap_item", "contractor_report", "acceptance", "project"}

УКАЗАНИЕ = ("проанализируй идею - идея сервиса - надстройка над гитхаб которая агрегирует открытые "
            "репо в группы по тегам, анализирует их редми и рейтинги, постоянно изменяется, "
            "действует как продвинутый рубрикатор с ии с подборками, топами и свежими релизами")
МАТЕРИАЛ = """

ASSISTANT

Идея отличная и своевременная. GitHub превратился в огромную свалку, где найти качественный и живой
инструмент среди 300+ миллионов репозиториев нетривиально. Существующие решения (GitHub Topics,
Awesome-списки) либо страдают от спама, либо устаревают, так как поддерживаются вручную.

Ваш сервис решает боль разработчиков, тимлидов и CTO: как быстро найти лучшее решение и не
нарваться на заброшенный проект.

1. Семантический рубрикатор: ИИ генерирует смысловые кластеры вместо стандартных тегов.
2. Генерация TL;DR из README: ИИ читает README и извлекает суть.
3. Оценка здоровья проекта: частота коммитов, скорость ответа мейнтейнеров в Issues.
4. Динамические подборки: лучшие замены платным SaaS.
5. Персонализированные ленты: пользователь получает дайджест в Telegram/Email."""

ВСТАВКА = УКАЗАНИЕ + МАТЕРИАЛ


def _catalog() -> dict:
    out = {}
    for sid in list(ape.SKILLS):
        meta = ape.SKILLS.get(sid) or ()
        p = ROOT / "skills" / sid / "template.json"
        tpl = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        out[sid] = {"title": meta[0] if meta else sid,
                    "short": meta[1] if len(meta) > 1 else "",
                    "body": (ape.load_skill_body(sid) or "")[:6000],
                    "inputs": tpl.get("inputs") or {}, "produces": tpl.get("produces") or {},
                    "delivery": tpl.get("delivery") or {}, "mode": "read"}
    return out


CATALOG = _catalog()


def test_материал_не_режется_на_этапы():
    assert len(planner.clauses(ВСТАВКА)) == 1, "вставленный текст снова разобран как перечень работ"
    assert len(planner.clauses(УКАЗАНИЕ)) == 1


def test_указание_выделяется_из_вставки():
    d = planner.directive(ВСТАВКА)
    assert d.startswith("проанализируй идею"), f"указание выделено неверно: {d[:60]}"
    assert "Awesome" not in d and "Telegram" not in d, "в указание попал чужой текст"


def test_приветствие_перед_указанием_не_мешает():
    """Человек может начать с обращения — действие ищем в первом абзаце, где оно есть."""
    t = "Привет! Есть минутка?\n\n" + УКАЗАНИЕ + МАТЕРИАЛ
    assert planner.directive(t).startswith("проанализируй идею")


def test_вставка_и_короткая_формулировка_дают_один_план():
    short = planner.plan(УКАЗАНИЕ, CATALOG, entities=ENTITIES, slots=set(), max_steps=5)
    long = planner.plan(ВСТАВКА, CATALOG, entities=ENTITIES, slots=set(), max_steps=5)
    a = [s["skill"] for s in short["steps"]]
    b = [s["skill"] for s in long["steps"]]
    assert a == b, f"материал изменил план: без него {a}, с ним {b}"
    assert a and a[0] == "idea-scorer", f"оценку идеи делает не idea-scorer, а {a}"


def test_слова_из_материала_не_тянут_навыки_в_план():
    """«Дайджест в Telegram/Email» стояло в ЧУЖОМ абзаце — черновик письма в плане не нужен."""
    p = planner.plan(ВСТАВКА, CATALOG, entities=ENTITIES, slots=set(), max_steps=5)
    got = [s["skill"] for s in p["steps"]]
    for чужой in ("email-draft", "mail-triage", "adr-writer", "status-report",
                  "weekly-update", "email-thread-reconstruct"):
        assert чужой not in got, f"в плане появился «{чужой}» из приложенного материала: {got}"


def test_многосоставная_длинная_задача_по_прежнему_делится():
    """Защита не должна съедать настоящие этапы: одна длинная фраза без вставки режется как раньше."""
    t = ("сверь дорожную карту проекта с отчётами подрядчиков за прошедший квартал по срокам, "
         "объёмам и суммам, найди расхождения и подготовь по каждому короткое объяснение со "
         "ссылкой на документ и ответственного, затем объясни, какие из расхождений влияют на "
         "сроки сдачи этапа, а какие нет, и нарежь задачи в трекере по тем расхождениям, которые "
         "подтвердились документами, чтобы ответственные увидели их у себя в работе на неделе, "
         "а также подготовь сводку по подрядчикам: кто сдал в срок, кто сдвинул сроки и на сколько "
         "дней, с суммами принятых работ и остатком по договору, чтобы на совещании было видно "
         "общую картину без хождения по документам и без ручной сверки реестров")
    assert len(t) > planner._PASTE_FROM, "пример перестал быть длинным — проверка потеряла смысл"
    assert len(planner.clauses(t)) >= 2, "длинная многосоставная задача перестала делиться на этапы"


def test_уверенный_лидер_не_превращается_в_вопрос():
    """Счёт 0.22 на длинной формулировке — не догадка, если следующий кандидат далеко позади."""
    p = planner.plan(ВСТАВКА, CATALOG, entities=ENTITIES, slots=set(), max_steps=5)
    ru = p.get("runner_up") or {}
    assert ru.get("score") is not None, "планировщик не отдаёт второго кандидата"
    d = orchestrator.decide(ВСТАВКА, catalog=CATALOG, entities=ENTITIES, slots=set(), matches=[], plan=p)
    assert d.kind == "build", f"уверенный лидер превратился в вопрос: {d.why}"
    assert d.skills == ["idea-scorer"]


@pytest.mark.parametrize("plan_data", [
    {"ok": True, "steps": [{"skill": "audit1c-extract", "score": 0.2}], "runner_up": {"skill": "x", "score": 0.19}},
])
def test_без_отрыва_слабый_лидер_остаётся_вопросом(plan_data):
    """Правило отрыва не отменяет защиту: 0.20 против 0.19 — это по-прежнему догадка."""
    d = orchestrator.decide("проверь идею ниже", catalog={}, entities=set(), slots=set(),
                            matches=[], plan=plan_data)
    assert d.kind == "ask", d.why


def test_ссылка_не_становится_этапом_работы():
    """05.10, стресс-тест: в финансовом запросе стояли ссылки на регламент и выгрузку, в адресах
    встречались «1c», «invoices», «reglament» — подбор увидел в ссылке этап «аудит 1С» со счётом
    0.49 и потащил в план чужую вертикаль целиком. Ссылка — источник, а не работа."""
    t = ("посчитай P&L и драйверы по транзакциям за квартал, прогноз бюджета по статьям, объясни "
         "дельты, собери финотчёт с KPI. Регламент: "
         "http://5.129.192.63:6875/books/1/page/reglament-rascetov-s-podriadcikami-izvlecenie "
         "Выгрузка счетов: http://5.129.192.63:9000/abop-demo/1c/invoices.json")
    assert "reglament" not in planner.without_links(t), "ссылка осталась в тексте для подбора"
    assert len(planner.clauses(t)) == 1, "ссылка снова стала отдельным этапом"
    got = [s["skill"] for s in planner.plan(t, CATALOG, entities=ENTITIES, slots=set(),
                                            max_steps=6)["steps"]]
    for чужой in ("audit1c-extract", "audit1c-graph-build", "audit1c-checks", "audit1c-root-cause"):
        assert чужой not in got, f"ссылка притащила чужую вертикаль: {got}"
    assert any(s.startswith(("budget", "finance", "variance", "dashboard", "three")) for s in got), \
        f"финансовая задача не попала в финансовые навыки: {got}"
