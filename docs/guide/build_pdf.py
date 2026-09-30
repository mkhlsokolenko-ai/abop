"""Сборка PDF из руководств: тот же HTML, печатная вёрстка, колонтитул со страницами.

Запуск:  python docs/guide/build_pdf.py
Требует playwright с установленным chromium (pip install playwright && playwright install chromium).
PDF нужен для поставки: один файл, который открывается везде и не теряет картинки при пересылке.
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
DOCS = [("RUKOVODSTVO_POLZOVATELYA.html", "ABOP · Руководство пользователя"),
        ("RUKOVODSTVO_ADMINISTRATORA.html", "ABOP · Руководство администратора")]

HEADER = """<div style="font:9px 'Segoe UI',system-ui,sans-serif;color:#767C86;width:100%;
 padding:0 16mm;display:flex;justify-content:space-between;">
 <span>{title}</span><span>версия 1.1 · 30.09.2026</span></div>"""
FOOTER = """<div style="font:9px 'Segoe UI',system-ui,sans-serif;color:#767C86;width:100%;
 padding:0 16mm;text-align:right;"><span class="pageNumber"></span> / <span class="totalPages"></span></div>"""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нужен playwright: pip install playwright && playwright install chromium")
        return 2
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for name, title in DOCS:
            src = HERE / name
            if not src.exists():
                print("нет файла:", name)
                continue
            page = b.new_page()
            page.goto(src.as_uri(), wait_until="networkidle")
            page.wait_for_timeout(1500)
            out = src.with_suffix(".pdf")
            page.pdf(path=str(out), format="A4", print_background=True,
                     display_header_footer=True,
                     header_template=HEADER.format(title=title), footer_template=FOOTER,
                     margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"})
            page.close()
            print(f"{out.name}: {out.stat().st_size // 1024} КБ")
        b.close()
    return 0


sys.exit(main())
