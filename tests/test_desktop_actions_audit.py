"""Обход интерфейса: у действия — знак с подсказкой, у предложения — выход.

03.10 владелец попросил проверить остальные разделы: где не хватает отмены и где кнопки всё ещё
носят фразы вроде «Запустить агента». Две проверки держат результат обхода.

Знак без подсказки — ребус: нажимать его человек будет наугад. Поэтому каждая кнопка-знак обязана
иметь `title` (подсказка под курсором), а подпись для чтения с экрана — `aria-label`.

Карточка, предлагающая запуск или задающая вопрос, обязана иметь выход. Без него у человека два
пути: согласиться или уйти со страницы — и это та же дыра, что была на сборке цепочки.
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "desktop" / "ui"
FILES = sorted(UI.rglob("*.js"))
CHAT = (UI / "modules" / "chat" / "panel.js").read_text(encoding="utf-8")

# Фразы, которые незачем печатать на кнопке: действие объясняет знак и подсказка.
RUN_PHRASES = ("Запустить агента", "Запустить цепочку", "Запустить граф", "Запустить роли",
               "Запустить ▸", "▶ Запустить")


def test_у_каждого_знака_есть_подсказка():
    bad = []
    for p in FILES:
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r'<button[^>]*class="ico[^"]*"[^>]*>', src):
            if "title=" not in m.group(0):
                bad.append(f"{p.relative_to(UI)}:{src[:m.start()].count(chr(10)) + 1}")
    assert not bad, "знак без подсказки под курсором: " + ", ".join(bad)


def test_кнопки_не_носят_фразу_запуска():
    bad = []
    for p in FILES:
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r'<button[^>]*>([^<]{1,60})', src):
            label = m.group(1)
            if any(ph in label for ph in RUN_PHRASES):
                bad.append(f"{p.relative_to(UI)}: «{label.strip()[:40]}»")
    assert not bad, "на кнопке осталась фраза запуска: " + "; ".join(bad)


def test_подсказки_у_знаков_запуска_объясняют_действие():
    """Знак ▶ без слов должен объясняться подсказкой — иначе непонятно, что именно запустится."""
    for m in re.finditer(r'<button[^>]*class="ico go[^"]*"[^>]*>', CHAT + (UI / "modules" / "graphlens" / "panel.js").read_text(encoding="utf-8")):
        tag = m.group(0)
        t = re.search(r'title="([^"]*)"', tag)
        assert t and "апуст" in t.group(1), f"знак запуска без внятной подсказки: {tag[:90]}"


def test_у_карточек_предложения_есть_выход():
    """Запуск, цепочка, сборка, предмет работы, уточнение задачи — у каждой карточки своя отмена."""
    # Ищем именно разметку карточки: то же слово встречается и в вызове note(), где выхода не ждут.
    for kicker in ('dcard-kicker">задача для агента',
                   'dcard-kicker">цепочка из ${cs.steps.length} шагов',
                   # Пометка у сборки условная: исправленная человеком цепочка подписана иначе.
                   'dcard-kicker">${a.manual ?',
                   'dcard-kicker">предмет работы',
                   'ape-label">нужно уточнить задачу'):
        i = CHAT.index(kicker)
        assert "cardCloseHTML" in CHAT[i:i + 240], f"у карточки «{kicker[-26:]}» нет выхода"


def test_отмена_понимает_все_карточки():
    i = CHAT.index("async function cancelCard")
    body = CHAT[i:i + 900]
    for key in ("chain_suggest", "decision", "assemble", "slot_ask", "clarify"):
        assert key in body, f"отмена не знает про карточку {key} — текст задачи потеряется"


def test_диалог_всегда_закрывается():
    """Общий диалог: кнопка отмены, закрытие по фону и Esc — проверяем в одном месте, в ядре."""
    app = (UI / "core" / "app.js").read_text(encoding="utf-8")
    i = app.index("export function modal(")
    body = app[i:i + 1400]
    assert 'id="mCancel"' in body, "у диалога нет кнопки отмены"
    assert "closeOnBackdrop" in body, "диалог не закрывается по фону"
    assert "Отмена" in body and "Закрыть" in body, "подпись отмены зависит от автора окна"
