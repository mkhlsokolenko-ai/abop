#!/usr/bin/env python3
"""Векторная версия схемы архитектуры: ABOP_Architecture.html → ABOP_Architecture.svg.

Зачем генератор, а не нарисованный руками SVG: источник правды должен остаться один. Схема живёт в
HTML, её правят по месту; векторная версия собирается из неё же, поэтому не расходится. Вставлять в
презентацию, печатать и масштабировать удобнее вектором, а не снимком экрана.

Запуск:  python docs/arch_svg.py          (пишет docs/ABOP_Architecture.svg)
"""
from __future__ import annotations

import html as _html
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "ABOP_Architecture.html"
OUT = ROOT / "ABOP_Architecture.svg"

# Палитра повторяет тёмную тему схемы: вектор должен узнаваться как та же картинка.
BG, PANEL, LINE = "#080b12", "#0d131d", "#1b2637"
INK, INK2, INK3 = "#e7eef8", "#9db1c6", "#5c6f82"
ACCENT, OK, WARN = "#3b82f6", "#22c55e", "#f59e0b"
MONO = "'JetBrains Mono','Cascadia Mono',Consolas,monospace"
UI = "'Segoe UI',Inter,system-ui,sans-serif"

PAD, GAP, COL_GAP = 14, 10, 18
W_SIDE, W_MAIN = 250, 470


def esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def wrap(text: str, width: int, size: float) -> list[str]:
    """Перенос по словам под ширину в пикселях. Ширина символа оценивается по кеглю: точные метрики
    потребовали бы шрифтовых таблиц, а расхождение в пару процентов на схеме незаметно."""
    per = max(1, int(width / (size * 0.56)))
    out, line = [], ""
    for word in text.split():
        probe = (line + " " + word).strip()
        if len(probe) <= per:
            line = probe
        else:
            if line:
                out.append(line)
            line = word
    if line:
        out.append(line)
    return out


class Card:
    """Карточка схемы: заголовок, боксы (имя + описание), чипы и свободные строки."""

    def __init__(self, title: str):
        self.title = title
        self.boxes: list[tuple[str, str, bool]] = []
        self.chips: list[str] = []
        self.lines: list[str] = []

    def height(self, w: int) -> int:
        h = PAD + (18 * len(wrap(self.title, w - 2 * PAD, 9.5)) if self.title else 0)
        # Высота обязана совпадать с тем, на сколько двигается курсор в draw(): расхождение в
        # несколько пикселей на бокс складывается и обрезает последние строки карточки.
        for name, desc, _ in self.boxes:
            h += self._box_h(name, desc, w) + 8
        if self.chips:
            h += 22 + 20 * self._chip_rows(w)
        for ln in self.lines:
            h += 13 * len(wrap(ln, w - 2 * PAD, 10))
        return h + PAD

    def _box_h(self, name: str, desc: str, w: int) -> int:
        """Высота бокса ровно та, на которую потом сдвинется курсор: иначе рамка накрывает соседа."""
        return 6 + 14 * len(wrap(name, w - 2 * PAD - 12, 11.5)) + 12 * len(wrap(desc, w - 2 * PAD - 12, 10))

    def _chip_rows(self, w: int) -> int:
        avail, x, rows = w - 2 * PAD, 0, 1
        for c in self.chips:
            cw = len(c) * 6.0 + 14
            if x + cw > avail:
                rows += 1
                x = 0
            x += cw + 5
        return rows

    def draw(self, x: int, y: int, w: int) -> list[str]:
        h = self.height(w)
        s = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{PANEL}" stroke="{LINE}"/>']
        cy = y + PAD + 4
        if self.title:
            # Длинный заголовок переносим: иначе он уезжает за край карточки и обрезается.
            for part in wrap(self.title.upper(), w - 2 * PAD, 9.5):
                s.append(f'<text x="{x + PAD}" y="{cy + 6}" font-family="{MONO}" font-size="9.5" '
                         f'letter-spacing="1.1" fill="{INK3}">{esc(part)}</text>')
                cy += 18
        for name, desc, hl in self.boxes:
            nm = wrap(name, w - 2 * PAD - 12, 11.5)
            ds = wrap(desc, w - 2 * PAD - 12, 10)
            bh = self._box_h(name, desc, w)
            s.append(f'<rect x="{x + PAD - 6}" y="{cy - 2}" width="{w - 2 * PAD + 12}" height="{bh}" '
                     f'rx="8" fill="{"#111a2b" if hl else "#0a0f18"}" stroke="{ACCENT if hl else LINE}" '
                     f'stroke-opacity="{0.55 if hl else 1}"/>')
            for ln in nm:
                cy += 14
                s.append(f'<text x="{x + PAD}" y="{cy}" font-family="{UI}" font-size="11.5" '
                         f'font-weight="600" fill="{INK}">{esc(ln)}</text>')
            for ln in ds:
                cy += 12
                s.append(f'<text x="{x + PAD}" y="{cy}" font-family="{UI}" font-size="10" '
                         f'fill="{INK2}">{esc(ln)}</text>')
            cy += 14
        if self.chips:
            cx, cy = x + PAD, cy + 8
            for c in self.chips:
                cw = len(c) * 6.0 + 14
                if cx + cw > x + w - PAD:
                    cx, cy = x + PAD, cy + 20
                s.append(f'<rect x="{cx}" y="{cy - 11}" width="{cw:.0f}" height="17" rx="6" '
                         f'fill="#0f1725" stroke="{LINE}"/>')
                s.append(f'<text x="{cx + 7}" y="{cy + 1}" font-family="{MONO}" font-size="9" '
                         f'fill="{INK2}">{esc(c)}</text>')
                cx += cw + 5
            cy += 14
        for ln in self.lines:
            for part in wrap(ln, w - 2 * PAD, 10):
                cy += 12
                s.append(f'<text x="{x + PAD}" y="{cy}" font-family="{UI}" font-size="10" '
                         f'fill="{INK3}">{esc(part)}</text>')
        return s


def _card_spans(src: str) -> list[tuple[int, int]]:
    """Границы каждой карточки с учётом вложенности: наивный поиск «до следующей карточки» путает
    внешнюю карточку с внутренней и перемешивает их содержимое."""
    spans, i = [], 0
    open_tag = '<div class="card">'
    while True:
        a = src.find(open_tag, i)
        if a < 0:
            return spans
        depth, j = 1, a + len(open_tag)
        while depth and j < len(src):
            nxt_open = src.find("<div", j)
            nxt_close = src.find("</div>", j)
            if nxt_close < 0:
                break
            if 0 <= nxt_open < nxt_close:
                depth += 1
                j = nxt_open + 4
            else:
                depth -= 1
                j = nxt_close + 6
        spans.append((a, j))
        i = a + len(open_tag)


def _own(src: str, span: tuple[int, int], spans: list[tuple[int, int]]) -> str:
    """Содержимое карточки без того, что принадлежит вложенным карточкам."""
    a, b = span
    inner = [(x, y) for (x, y) in spans if x > a and y <= b]
    # оставляем только карточки первого уровня вложенности
    top = [s0 for s0 in inner if not any(x < s0[0] and s0[1] <= y for (x, y) in inner if (x, y) != s0)]
    out, cur = [], a
    for x, y in sorted(top):
        out.append(src[cur:x])
        cur = y
    out.append(src[cur:b])
    return "".join(out)


def _sections(src: str) -> list[tuple[str, str]]:
    """Разделы схемы: заголовок и всё до следующего заголовка того же вида.

    Часть разделов свёрстана без обёртки `.card` (колонка десктопа, нижняя полоса рабочих систем),
    поэтому единицей берём именно ЗАГОЛОВОК: он есть у каждого смыслового блока.
    """
    # Нижняя полоса (GPU-бокс, шина, системы) озаглавлена иначе — через `.hd > .t`: заголовок там
    # цветной, с подписью справа. Без него содержимое этих блоков прилипало к соседней секции.
    pat = r'<div class="(?:sec|st)">(.*?)</div>|<div class="hd[^"]*"><span class="t">(.*?)</span>'
    marks = [(m.start(), strip_tags(m.group(1) or m.group(2)))
             for m in re.finditer(pat, src, re.S)]
    out = []
    for n, (pos, title) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(src)
        out.append((title, src[pos:end]))
    return out


def parse(src: str) -> tuple[str, str, list[Card]]:
    h1 = strip_tags(re.search(r"<h1>(.*?)</h1>", src, re.S).group(1))
    sub = strip_tags(re.search(r'<p class="sub">(.*?)</p>', src, re.S).group(1))
    cards: list[Card] = []
    for title, blob in _sections(src):
        card = Card(title)
        for bm in re.finditer(r'<div class="box( hl)?"[^>]*>\s*<div class="bt">(.*?)</div>\s*'
                              r'<div class="bd">(.*?)</div>', blob, re.S):
            card.boxes.append((strip_tags(bm.group(2)), strip_tags(bm.group(3)), bool(bm.group(1))))
        for cm in re.finditer(r'<div class="ct">(.*?)</div>\s*<div class="cd">(.*?)</div>', blob, re.S):
            card.boxes.append((strip_tags(cm.group(1)), strip_tags(cm.group(2)), False))
        for cm in re.finditer(r'<span class="chip[^"]*">(.*?)</span>', blob, re.S):
            card.chips.append(strip_tags(cm.group(1)))
        for cm in re.finditer(r'<div class="tag">(.*?)</div>', blob, re.S):
            card.chips.append(strip_tags(cm.group(1)))
        for lm in re.finditer(r'<div class="leg-row">(.*?)</div>', blob, re.S):
            card.lines.append(strip_tags(lm.group(1)))
        for dm in re.finditer(r'<div class="dom">(.*?)</div>', blob, re.S):
            card.chips += [strip_tags(x) for x in dm.group(1).split("<br>") if strip_tags(x)]
        for fm in re.finditer(r'<div class="flow">\s*<span class="num">(\d+)</span>\s*'
                              r'<span class="ft">(.*?)</span>', blob, re.S):
            card.lines.append(f"{fm.group(1)}. {strip_tags(fm.group(2))}")
        if card.boxes or card.chips or card.lines:
            cards.append(card)
    return h1, sub, cards


def build() -> str:
    src = SRC.read_text(encoding="utf-8")
    title, sub, cards = parse(src)

    # Раскладка: карточки раздаются в три колонки по высоте — так полотно не растёт в одну сторону.
    widths = [W_SIDE, W_MAIN, W_MAIN]
    xs = [PAD * 2]
    for w in widths[:-1]:
        xs.append(xs[-1] + w + COL_GAP)
    heights = [0, 0, 0]
    placed: list[tuple[Card, int, int, int]] = []
    head = 92
    for card in cards:
        k = heights.index(min(heights))
        w = widths[k]
        y = head + heights[k] + (GAP if heights[k] else 0)
        placed.append((card, xs[k], y, w))
        heights[k] = y - head + card.height(w)

    total_w = xs[-1] + widths[-1] + PAD * 2
    total_h = head + max(heights) + PAD * 3

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{total_h}" '
           f'viewBox="0 0 {total_w} {total_h}" font-family="{UI}">',
           f'<rect width="{total_w}" height="{total_h}" fill="{BG}"/>',
           f'<text x="{PAD * 2}" y="40" font-size="25" font-weight="800" fill="{INK}">{esc(title)}</text>']
    for i, ln in enumerate(wrap(sub, total_w - PAD * 4, 11)):
        out.append(f'<text x="{PAD * 2}" y="{58 + i * 14}" font-size="11" fill="{INK2}">{esc(ln)}</text>')
    for card, x, y, w in placed:
        out += card.draw(x, y, w)
    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    svg = build()
    OUT.write_text(svg, encoding="utf-8")
    print(f"готово: {OUT.name}, {len(svg)} знаков")
    sys.exit(0)
