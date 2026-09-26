# -*- coding: utf-8 -*-
"""Браузерный smoke веба (CI): страница поднимается, рендерит Обзор в dev-режиме (без Keycloak → dev-admin),
навигация по хэшу работает, тема переключается, ошибок JS нет. Запуск: сервер на 127.0.0.1:8091."""
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

BASE = os.getenv("ABOP_SMOKE_URL", "http://127.0.0.1:8091/")


async def main() -> int:
    errors: list[str] = []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1366, "height": 860})
        pg.on("pageerror", lambda e: errors.append("PAGEERROR " + str(e)[:200]))
        await pg.goto(BASE, wait_until="domcontentloaded", timeout=120000)
        await pg.wait_for_timeout(6000)
        txt = await pg.evaluate("document.body.innerText")
        checks = {
            "overview": "Обзор" in txt,
            "nav": await pg.evaluate("!!document.querySelector('nav[aria-label]')"),
            "landmarks": await pg.evaluate("!!document.querySelector('[role=main]') && !!document.querySelector('[role=banner]')"),
            "theme_attr": await pg.evaluate("['light','dark'].includes(document.documentElement.getAttribute('data-theme'))"),
        }
        await pg.evaluate("location.hash='#agents'"); await pg.wait_for_timeout(1500)
        checks["agents_screen"] = "Агенты" in await pg.evaluate("document.body.innerText")
        await pg.evaluate("location.hash='#canvas'"); await pg.wait_for_timeout(2500)
        checks["lens_tabs"] = await pg.evaluate("document.querySelectorAll('[role=tablist] [role=tab]').length") >= 4
        await pg.click("button:has-text('Эксплуатирую')"); await pg.wait_for_timeout(2500)
        checks["fleet_lens"] = "панели флота" in (await pg.evaluate("document.body.innerText")).lower()
        await b.close()
    checks["no_js_errors"] = not errors
    print(json.dumps({"checks": checks, "errors": errors[:5]}, ensure_ascii=False, indent=1))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
