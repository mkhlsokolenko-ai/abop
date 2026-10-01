#!/usr/bin/env python3
"""Добавить в презентацию для заказчиков кейсы, появившиеся после её сборки, и поставить маскота.

Презентация собрана вручную, со своей сеткой и палитрой. Поэтому новые слайды строятся теми же
примитивами и по тем же координатам, что и существующие кейсы: иначе вставка читается как чужая.

Запуск:  python docs/deck/add_cases.py "<исходник>" "<результат>"
         тот же вызов с --catalog добавляет только слайд каталога навыков
Исходник не затирается: рядом кладётся копия с суффиксом «.bak».
"""
from __future__ import annotations

import pathlib
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor as C
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

# ── палитра и шрифты сняты с существующих слайдов ──
BG = C(0xF4, 0xF5, 0xFA)
INK = C(0x1B, 0x21, 0x40)
ACC = C(0x4F, 0x46, 0xE5)
CARD, CARD_LINE = C(0xFF, 0xFF, 0xFF), C(0xDD, 0xE0, 0xEC)
PANEL = C(0x12, 0x17, 0x2E)
PANEL_EYE, PANEL_TXT, PANEL_MUT = C(0xA5, 0xB4, 0xFC), C(0xF2, 0xF3, 0xF8), C(0xB7, 0xBD, 0xD6)
AMBER = C(0xF5, 0xA5, 0x24)
FOOT = C(0x5B, 0x64, 0x80)
ALERT_BG, ALERT_LINE = C(0xFF, 0xE9, 0xE9), C(0xDC, 0x26, 0x26)
OK_BG, OK_LINE = C(0xE8, 0xF8, 0xEF), C(0x10, 0xB9, 0x81)

DEFAULT_SRC = pathlib.Path(r"C:\Users\Михаил Соколенко\Downloads") / "ABOP для заказчиков (final).pptx"

MONO, HEAD, BODY = "JetBrains Mono", "Rubik", "IBM Plex Sans"


def _tb(slide, x, y, w, h, text, *, font=BODY, size=12, color=INK, bold=False, space=None):
    """Текстовый блок без заливки: вся типографика слайдов сделана именно так."""
    sh = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = sh.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, line in enumerate(text.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        r = p.add_run()
        r.text = line
        r.font.name, r.font.size, r.font.bold = font, Pt(size), bold
        r.font.color.rgb = color
        if space:
            p.space_after = Pt(space)
        p.line_spacing = 1.25
    return sh


def _rect(slide, x, y, w, h, fill, line=None, radius=0.06):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line
        sh.line.width = Pt(1)
    sh.shadow.inherit = False
    try:
        sh.adjustments[0] = radius
    except Exception:  # noqa: BLE001 — у фигуры может не быть регулировки скругления
        pass
    sh.text_frame.text = ""
    return sh


def new_slide(prs, eyebrow: str, title: str, lead: str):
    """Каркас кейсового слайда: надзаголовок, заголовок, вводный абзац, подвал."""
    s = prs.slides.add_slide(prs.slide_layouts[0])
    for ph in list(s.placeholders):
        ph._element.getparent().remove(ph._element)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = BG
    _tb(s, 0.89, 0.89, 11.90, 0.26, eyebrow, font=MONO, size=12, color=ACC)
    _tb(s, 0.89, 1.22, 11.90, 1.00, title, font=HEAD, size=31, color=INK, bold=True)
    _tb(s, 0.89, 2.43, 7.12, 0.95, lead, size=15, color=INK)
    _tb(s, 0.89, 6.82, 5.72, 0.26, "ABOP · кейсы", font=MONO, size=12, color=FOOT)
    # Номер проставляется позже, когда слайды встанут на свои места (см. renumber).
    num = _tb(s, 11.00, 6.82, 1.44, 0.26, "0", font=MONO, size=12, color=FOOT)
    num.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT
    return s


def step(s, x, y, w, h, label, text, *, fill=CARD, line=CARD_LINE, label_color=ACC):
    """Карточка-шаг: подпись моноширинным и текст под ней."""
    _rect(s, x, y, w, h, fill, line)
    _tb(s, x + 0.20, y + 0.20, w - 0.40, 0.26, label, font=MONO, size=11, color=label_color)
    _tb(s, x + 0.20, y + 0.52, w - 0.40, h - 0.72, text, size=12, color=INK)


def panel(s, eyebrow: str, blocks: list[tuple[str, object]], *, x=8.28, y=2.43, w=4.17, h=3.96):
    """Тёмная панель справа: «что это даёт» — та же, что на существующих кейсах."""
    _rect(s, x, y, w, h, PANEL, None, radius=0.05)
    _tb(s, x + 0.25, y + 0.25, w - 0.50, 0.25, eyebrow, font=MONO, size=11.5, color=PANEL_EYE)
    cy = y + 0.58
    per_line, line_h = 36, 0.215   # знаков в строке и высота строки при 12.3 pt на 3.67"
    for text, color in blocks:
        lines = max(1, -(-len(text) // per_line))
        h_txt = lines * line_h
        _tb(s, x + 0.25, cy, w - 0.50, h_txt, text, size=12.3, color=color)
        cy += h_txt + 0.20          # воздух между абзацами
    return cy


def arrow(s, x, y, w=0.39, h=0.19):
    """Стрелка между шагами — тонкий треугольник в акцентном цвете."""
    sh = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = C(0xC7, 0xD2, 0xFE)
    sh.line.fill.background()
    sh.shadow.inherit = False


def put_mascot(prs, png: pathlib.Path) -> bool:
    """Поставить маскота в центр колец титульного слайда."""
    if not png.exists():
        return False
    s = prs.slides[0]
    # Внутреннюю залитую форму убираем: именно её место занимает маскот.
    for sh in list(s.shapes):
        try:
            filled = sh.fill.type == 1 and str(sh.fill.fore_color.rgb) == '4F46E5'
        except Exception:  # noqa: BLE001 — у фигуры может не быть заливки
            filled = False
        if filled and abs(sh.width - Inches(0.92)) < Inches(0.1):
            sh._element.getparent().remove(sh._element)
    w = 1.62
    h = w * 560 / 520          # пропорции рисунка
    cx, cy = 10.25, 3.08       # центр колец титула
    s.shapes.add_picture(str(png), Inches(cx - w / 2), Inches(cy - h / 2), Inches(w), Inches(h))
    return True


def link(s, x1, y1, x2, y2):
    """Связь в майндмепе: тонкая линия от центра к семье."""
    c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = C(0xC7, 0xD2, 0xFE)
    c.line.width = Pt(1.25)
    return c


def family(s, x, y, w, h, name, skills):
    """Узел семьи: название отдела и сколько в нём навыков."""
    _rect(s, x, y, w, h, CARD, CARD_LINE)
    _tb(s, x + 0.14, y + 0.05, w - 0.28, 0.22, name, size=11.5, color=INK, bold=True)
    _tb(s, x + 0.14, y + 0.25, w - 0.28, 0.18, f"{skills} навыков", font=MONO, size=9, color=ACC)


def slide_catalog(prs):
    """Из чего собираются агенты: каталог навыков и сколько агентов он даёт."""
    s = new_slide(
        prs, "КЕЙСЫ · КАТАЛОГ",
        "Из чего собираются агенты: 59 навыков в десяти семьях",
        "Навык — методика одного шага: что сделать и что считать результатом. Семья — отдел, "
        "внутри которого навыки сочетаются; один навык может числиться в нескольких семьях.")

    cx, cy = 4.46, 4.75                      # центр майндмепа
    _rect(s, cx - 0.78, cy - 0.33, 1.56, 0.66, PANEL, None, radius=0.22)
    _tb(s, cx - 0.64, cy - 0.23, 1.28, 0.22, "59 навыков", size=12, color=PANEL_TXT, bold=True)
    _tb(s, cx - 0.64, cy + 0.02, 1.28, 0.20, "10 семей · 32 роли", font=MONO, size=9, color=PANEL_EYE)

    left = [("Аналитика", 17), ("Менеджмент", 15), ("Финансы", 11), ("Аудит", 9), ("Инженерия", 9)]
    right = [("Архитектура", 8), ("Ресёрч", 6), ("Кредитование", 3), ("Критик", 3), ("Решения", 3)]
    w, h, gap = 1.74, 0.44, 0.09
    top = cy - (len(left) * (h + gap) - gap) / 2
    for i, (name, n) in enumerate(left):
        y = top + i * (h + gap)
        family(s, 0.89, y, w, h, name, n)
        link(s, 0.89 + w, y + h / 2, cx - 0.78, cy)
    for i, (name, n) in enumerate(right):
        y = top + i * (h + gap)
        family(s, 6.27, y, w, h, name, n)
        link(s, cx + 0.78, cy, 6.27, y + h / 2)

    _tb(s, 0.89, 6.14, 7.12, 0.56,
        "Сценарии демо и их семьи: аудит 1С и расследование причин — «Аудит» · контроль проектов, "
        "дайджест и БФТ — «Менеджмент» · закрытие периода и сверка регистров — «Финансы» · "
        "оценка объекта и обзор рынка — «Аналитика» и «Ресёрч».",
        size=10.5, color=FOOT)

    panel(s, "СКОЛЬКО АГЕНТОВ ИЗ ЭТОГО СОБИРАЕТСЯ", [
        ("32 роли — агент в один клик: выбрали семью и роль.", PANEL_TXT),
        ("420 пар и 1 547 троек навыков — если набирать состав внутри семьи самому.", PANEL_TXT),
        ("30 стыков навык-навык объявлены контрактом: такие цепочки среда строит сама "
         "и проверяет до прогона.", AMBER),
        ("167 246 сочетаний — комбинаторный потолок. Мы его не обещаем: предел не в числе "
         "наборов, а в ваших данных.", PANEL_MUT),
    ])
    return s


def _last_case_index(prs) -> int:
    """Индекс последнего кейсового слайда: новый слайд каталога встаёт сразу за ним."""
    last = -1
    for i, sl in enumerate(prs.slides):
        for sh in sl.shapes:
            if sh.has_text_frame and sh.text_frame.text.strip().startswith("КЕЙСЫ"):
                last = i
                break
    return last


def build_catalog_only(path: pathlib.Path, out: pathlib.Path | None = None) -> None:
    """Добавить в уже собранную презентацию только слайд каталога навыков."""
    out = out or path
    prs = Presentation(str(path))
    at = _last_case_index(prs) + 1
    slide_catalog(prs)
    lst = prs.slides._sldIdLst
    el = list(lst)[-1]              # только что добавленный слайд лежит в конце
    lst.remove(el)
    lst.insert(at, el)
    renumber(prs)
    prs.save(str(out))
    print(f"слайд каталога встал на позицию {at + 1} · слайдов: {len(lst)}")


def build(path: pathlib.Path, out: pathlib.Path | None = None) -> None:
    """Читаем исходник, пишем результат.

    По умолчанию в тот же файл, но открытый в PowerPoint файл Windows перезаписать не даёт —
    тогда передают отдельный путь вывода вторым аргументом.
    """
    out = out or path
    prs = Presentation(str(path))
    made = []

    # ── 1. Один агент — много проектов ────────────────────────────────────────────────────
    s = new_slide(
        prs, "КЕЙСЫ · ПРОЕКТЫ",
        "Один агент на сорок проектов, а не сорок копий",
        "Первое возражение зала: «у меня сорок проектов и десятки подрядчиков — ваш агент всё "
        "перепутает». Агент один. Предмет работы он уточняет, а не угадывает.")
    step(s, 0.89, 3.55, 2.20, 1.55, "фраза", "«Сверь дорожную карту с отчётами подрядчиков»")
    arrow(s, 3.20, 4.23)
    step(s, 3.72, 3.55, 2.20, 1.55, "уточнение", "«Какой проект?» — выбор из ваших проектов: заказчик, дата ввода")
    arrow(s, 6.03, 4.23)
    step(s, 6.55, 3.55, 1.45, 1.55, "выборка", "Данные сужены до одного проекта")
    _tb(s, 0.89, 5.35, 7.12, 0.60,
        "Вопрос задаётся только когда ответ меняет результат: если проект назван в запросе, "
        "среда подставит его сама и покажет строкой «понял так» с кнопкой поменять.",
        size=11.5, color=C(0x5B, 0x64, 0x80))
    panel(s, "ЧТО ЭТО МЕНЯЕТ", [
        ("Предмет сужает саму выборку данных, а не только текст запроса.", PANEL_TXT),
        ("Агент физически не видит чужие проекты — перепутать их нечем.", PANEL_TXT),
        ("Пункты с одинаковыми номерами из разных карт не смешиваются.", AMBER),
        ("Проверено на стенде: запрос «склад» разрешается в нужный проект, чужие записи в вывод "
         "не попадают.", PANEL_MUT),
    ])
    made.append(s)

    # ── 2. Веер по предметам ──────────────────────────────────────────────────────────────
    s = new_slide(
        prs, "КЕЙСЫ · МАСШТАБ",
        "Все проекты за один запуск: группа и общая сводка",
        "Следующий вопрос после первого: «хорошо, один проект вы показали — а сорок?» "
        "Один агент, одна кнопка, по заданию на каждый предмет.")
    step(s, 0.89, 3.55, 2.20, 1.55, "группа", "Одна группа заданий на все выбранные предметы")
    arrow(s, 3.20, 4.23)
    step(s, 3.72, 3.55, 2.20, 1.55, "ветви", "Каждая ветвь видит только свой проект")
    arrow(s, 6.03, 4.23)
    step(s, 6.55, 3.55, 1.45, 1.55, "сводка", "Итог по общему ключу")
    _rect(s, 0.89, 5.35, 7.12, 0.75, OK_BG, OK_LINE)
    _tb(s, 1.09, 5.52, 6.72, 0.45,
        "Проверено на стенде: три проекта — 24 записи сведены по ключу «проект + пункт»; "
        "37 пунктов, 8 просрочено, перерасход 465 часов — в одной таблице.",
        size=11.5, color=INK)
    panel(s, "ЧТО ПОЛУЧАЕТ РУКОВОДИТЕЛЬ", [
        ("Картина по всем проектам сразу, а не отчёт по каждому.", PANEL_TXT),
        ("Ключ сведения берётся из контрактов данных, а не угадывается: пункт пятый одного "
         "проекта не склеится с пунктом пятым другого.", PANEL_TXT),
        ("Упавшая ветвь не рушит сводку — она названа отдельно.", AMBER),
        ("Остановить можно всю группу разом, одной кнопкой.", PANEL_MUT),
    ])
    made.append(s)

    # ── 3. Арбитраж расхождений ───────────────────────────────────────────────────────────
    s = new_slide(
        prs, "КЕЙСЫ · ДОВЕРИЕ",
        "Два агента сказали разное: спор найден и разрешён",
        "Главный вопрос к мультиагентным системам. Подрядчик пишет «выполнено», приёмка по тому же "
        "пункту — «в работе». Молча выбрать любой ответ хуже, чем не отвечать.")
    step(s, 0.89, 3.55, 2.20, 1.30, "ветвь 1", "Сверка с отчётом подрядчика: «выполнено»")
    step(s, 0.89, 4.95, 2.20, 1.15, "ветвь 2", "Приёмка заказчика: «в работе»", fill=ALERT_BG, line=ALERT_LINE)
    arrow(s, 3.20, 4.60)
    step(s, 3.72, 3.80, 2.20, 1.55, "правило", "Тяжесть статуса: «в работе» весомее «выполнено»")
    arrow(s, 6.03, 4.60)
    step(s, 6.55, 3.80, 1.45, 1.55, "решение", "Выбрано и объяснено", fill=OK_BG, line=OK_LINE)
    panel(s, "КАК РАЗРЕШАЕТСЯ СПОР", [
        ("Спором считается только общее поле с разными значениями на одной и той же записи. "
         "Дополняющие друг друга ответы — не спор.", PANEL_TXT),
        ("Порядок: большинство → тяжесть → край → происхождение, затем арбитр, затем человек.", PANEL_TXT),
        ("Срок, деньги и мероприятие уходят человеку всегда.", AMBER),
        ("В записи остаются обе версии и название правила: видно не только что выбрано, "
         "но и что отвергнуто.", PANEL_MUT),
    ])
    made.append(s)

    # ── 4. Задача словами → цепочка ───────────────────────────────────────────────────────
    s = new_slide(
        prs, "КЕЙСЫ · СБОРКА",
        "Задача словами — цепочка собирается по контрактам",
        "Агента не обязательно собирать руками. Среда разбирает задачу на этапы и строит цепочку "
        "из навыков, которые реально выполнятся на ваших данных.")
    step(s, 0.89, 3.55, 1.72, 1.55, "задача", "Описание бизнес-словами")
    arrow(s, 2.72, 4.23)
    step(s, 3.24, 3.55, 1.72, 1.55, "план", "Этапы и навыки по контрактам")
    arrow(s, 5.07, 4.23)
    step(s, 5.59, 3.55, 1.72, 1.55, "проверка", "Чем кормить, что отдаёт, хватает ли данных")

    _rect(s, 0.89, 5.35, 7.12, 0.75, CARD, CARD_LINE)
    _tb(s, 1.09, 5.52, 6.72, 0.45,
        "Короткий запрос не угадывается: среда называет причину и задаёт три вопроса — "
        "что сделать, по чему работаем, что должно получиться.",
        size=11.5, color=INK)
    panel(s, "ПОЧЕМУ НЕ «УГАДАТЬ»", [
        ("Под «сделай отчёт» подходит десяток навыков из шестидесяти. Уверенный выбор из них — "
         "обман, за который платят дважды: деньгами и доверием.", PANEL_TXT),
        ("Навык, которому нечем работать, в план не попадает.", PANEL_TXT),
        ("Пустой план — это ответ, а не ошибка.", AMBER),
        ("Из плана агенты собираются одной кнопкой и остаются на канве.", PANEL_MUT),
    ])
    made.append(s)

    # ── 5. Каталог навыков и счёт агентов ──────────────────────────────────────────────────
    made.append(slide_catalog(prs))

    # ── расставить новые кейсы сразу за существующими и перенумеровать подвал ──
    xml_slides = prs.slides._sldIdLst
    ids = list(xml_slides)
    for n, s in enumerate(made):
        el = ids[len(ids) - len(made) + n]
        xml_slides.remove(el)
        xml_slides.insert(16 + n, el)        # после 16-го слайда (кейсы управления)
    mascot = pathlib.Path(__file__).resolve().parent / 'mascot_hand.png'
    if put_mascot(prs, mascot):
        print('маскот на титуле:', mascot.name)
    renumber(prs)
    prs.save(str(out))
    print(f"добавлено кейсов: {len(made)} · слайдов стало: {len(prs.slides.__iter__.__self__._sldIdLst)}")


def renumber(prs) -> None:
    """Номер страницы — отдельный текстовый блок на каждом слайде; после вставки их надо сдвинуть."""
    for i, s in enumerate(prs.slides, 1):
        for sh in s.shapes:
            if not sh.has_text_frame:
                continue
            t = sh.text_frame.text.strip()
            if t.isdigit() and sh.left > Inches(10.5):
                for p in sh.text_frame.paragraphs:
                    for r in p.runs:
                        r.text = str(i) if r.text.strip().isdigit() else r.text


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "--catalog"]
    src = pathlib.Path(argv[0]) if argv else DEFAULT_SRC
    dst = pathlib.Path(argv[1]) if len(argv) > 1 else src
    (build_catalog_only if "--catalog" in sys.argv else build)(src, dst)
