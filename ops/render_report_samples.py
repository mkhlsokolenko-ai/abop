# -*- coding: utf-8 -*-
"""Образцы ВСЕХ форм отчёта: HTML и картинки — чтобы вид правили глазами, а не на ощупь.

Отчёт нельзя оценить по CSS: пока документ не собран целиком, не видно ни иерархии, ни того, как
блоки стоят друг под другом, ни пустых граф. Скрипт собирает каждую из форм на данных-образцах,
построенных по `json_schema` навыков этой формы (`report_form.example_from_schema`) — то есть ровно
на тех полях, которыми форма реально располагает, без выдуманных значений.

Данные стенда не нужны: образец показывает устройство документа, а не результат прогона.

    python ops/render_report_samples.py              # HTML в build/report_samples/
    python ops/render_report_samples.py --png        # ещё и PNG через playwright (если установлен)
    python ops/render_report_samples.py --only credit,audit1c,letter

Картинки нужны для разбора вида: в PDF гарнитуры подменяются (в рендерере есть только Noto Sans,
Liberation Sans/Serif, DejaVu Sans/Mono), поэтому смотреть надо то, что соберётся на той же
гарнитуре, что и PDF.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ))

ВЫХОД = КОРЕНЬ / "build" / "report_samples"

# Прогон-образец для форм без раскладки (общий отчёт, аудит, расследование, дайджест): у них разделы
# свёрстаны плейсхолдерами, а не раскладкой, поэтому контекст нужно наполнить руками. Значения
# синтетические, но формы — настоящие: так же выглядит результат прогона на стенде.
ОБРАЗЕЦ_ПРОГОНА = {
    "run_id": "run-obrazec-0001",
    "started_by": "образец",
    "created_at": dt.datetime.now().isoformat()[:16],
    "verdict": {"ok": False, "autonomy_used": "A1"},
    "waves": [["skill-1"], ["skill-2"]],
    "findings_summary": {"by_class": {"A": 2, "B": 3, "C": 5, "D": 1}},
    "findings": [
        {"класс": "A", "проверка": "НДС в декларации не сходится с оборотами по счёту 68.02",
         "описание": "Расхождение 412 870,40 ₽ за III квартал: в декларации 3 980 140,00 ₽, "
                     "по проводкам 4 393 010,40 ₽.",
         "нормы_rag": ["НК РФ ст. 171 — условия принятия НДС к вычету"],
         "серьёзность": "высокая", "документ": "Декларация НДС от 25.10.2026",
         "сумма": 412870.40, "участок_от": "реализация", "участок_до": "налоги"},
        {"класс": "B", "проверка": "Реализация РТ-0002 без счёта-фактуры",
         "описание": "Отгрузка проведена 14.08.2026, счёт-фактура не выставлен.",
         "нормы_rag": ["НК РФ ст. 169 — обязанность выставить счёт-фактуру"],
         "серьёзность": "средняя", "документ": "Реализация РТ-0002 от 14.08.2026"},
        {"класс": "C", "проверка": "Контрагент без ИНН в справочнике",
         "описание": "ООО «Северный ветер» заведён без ИНН — сверка с ЕГРЮЛ невозможна.",
         "серьёзность": "низкая"},
    ],
    "investigations": [
        {"id": "INV-1", "симптом": "Себестоимость партии выросла на 18% без роста закупочных цен",
         "серьёзность": "высокая",
         "цепочка": [{"звено": "поступление", "есть": True, "статус": "✓ найдено"},
                     {"звено": "перемещение", "есть": True, "статус": "✓ найдено"},
                     {"звено": "списание", "есть": False, "статус": "✗ нет документа"}],
         "сверка": {"разница_₽": 284500.0},
         "нормы_rag": ["ФСБУ 5/2019 — оценка запасов при списании"]},
    ],
    "skill_outputs": [],
}


def контекст(agent: dict, run: dict) -> dict:
    from server import web_api
    return web_api._report_context(agent, run)


def образец_прогона(for_skills: list[str]) -> dict:
    """Прогон-образец по схемам навыков формы — то, что видит превью шаблона в вебе."""
    from server import web_api
    if not for_skills:
        return dict(ОБРАЗЕЦ_ПРОГОНА)
    run = web_api._example_run(for_skills)
    # Служебные поля шапки и подвала в образце по схемам отсутствуют — дорисовываем, иначе
    # реквизиты и подвал окажутся пустыми, и документ нельзя оценить целиком.
    run.update({"created_at": ОБРАЗЕЦ_ПРОГОНА["created_at"],
                "verdict": {"ok": True, "autonomy_used": "A1"},
                "waves": [[s] for s in for_skills]})
    return run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", action="store_true", help="снять картинки через playwright")
    ap.add_argument("--only", default="", help="только эти формы, через запятую")
    ap.add_argument("--width", type=int, default=900, help="ширина кадра, px")
    a = ap.parse_args()

    from server import report_store

    формы = report_store.load_files()
    нужны = {x.strip() for x in a.only.split(",") if x.strip()}
    ВЫХОД.mkdir(parents=True, exist_ok=True)
    собрано = []
    for tid, spec in sorted(формы.items()):
        if нужны and tid not in нужны:
            continue
        run = образец_прогона(spec.get("for_skills") or [])
        agent = {"name": spec.get("name") or tid, "id": "obrazec"}
        ctx = контекст(agent, run)
        ctx["_result"] = run
        html = report_store.render(spec, ctx)
        p = ВЫХОД / f"{tid}.html"
        p.write_text(html, encoding="utf-8")
        собрано.append((tid, p, len(html)))
        print(f"  {tid:<12} {len(html):>7} знаков · блоков раскладки {len(spec.get('layout') or [])}")

    print(f"собрано форм: {len(собрано)} → {ВЫХОД}")
    if not a.png:
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        print("playwright недоступен, картинок не будет:", type(e).__name__, e)
        return 0
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": a.width, "height": 1400}, device_scale_factor=2)
        for tid, p, _ in собрано:
            pg.goto(p.as_uri())
            pg.wait_for_timeout(120)
            out = ВЫХОД / f"{tid}.png"
            pg.screenshot(path=str(out), full_page=True)
            print(f"  снято: {out.name} ({os.path.getsize(out) // 1024} КБ)")
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
