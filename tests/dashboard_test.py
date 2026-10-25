#!/usr/bin/env python3
"""
Klicktest für das Dashboard (läuft im täglichen GitHub-Lauf vor dem Veröffentlichen).

Öffnet site/index.html in einem echten Browser (Desktop und Handy), klickt alle
Schalter (Kabine, Vergleichszeitraum, Kalender, Sortierung), öffnet Reisen und
den Wunschreise-Dialog. Jeder JavaScript-Fehler oder ein Schalter, der nicht
reagiert, lässt den Test scheitern -> das alte Dashboard bleibt online.

Nutzung: python tests/dashboard_test.py [site/index.html]
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PAGE = Path(sys.argv[1] if len(sys.argv) > 1 else "site/index.html").resolve()


async def run() -> list[str]:
    problems: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for viewport, scheme in (({"width": 1280, "height": 900}, "dark"), ({"width": 390, "height": 844}, "light")):
            page = await browser.new_page(viewport=viewport, color_scheme=scheme)
            page.on("pageerror", lambda e: problems.append(f"JavaScript-Fehler: {e}"))
            await page.route("**/fonts.*/**", lambda r: r.abort())
            await page.goto(PAGE.as_uri())
            await page.wait_for_timeout(500)
            w = viewport["width"]

            async def pressed(group: str) -> str | None:
                return await page.locator(f"#{group} [aria-pressed=true]").get_attribute("data-v")

            if (await page.locator(".trip").count()) == 0:
                problems.append(f"[{w}] Reiseliste ist leer")
            for cab in ("outside", "balcony", "any"):
                await page.click(f"#cabinSeg button[data-v={cab}]")
                await page.wait_for_timeout(150)
                if await pressed("cabinSeg") != cab:
                    problems.append(f"[{w}] Kabinenschalter '{cab}' reagiert nicht")
                for cmp in ("1", "7", "30"):
                    await page.click(f"#cmpSeg button[data-v='{cmp}']")
                    await page.wait_for_timeout(100)
                    if await pressed("cmpSeg") != cmp:
                        problems.append(f"[{w}] Vergleichsschalter '{cmp}' reagiert nicht")
                if await page.locator(".trip").count():
                    await page.locator(".trip .trip-main").first.click()
                    await page.wait_for_timeout(150)
                    if await page.locator(".detail").count() == 0:
                        problems.append(f"[{w}] Reise lässt sich nicht öffnen ({cab})")
            for v in ("Mein Schiff", "AIDA", ""):
                await page.click(f"#calBrand button[data-v='{v}']")
            for v in ("night", "total"):
                await page.click(f"#calMetric button[data-v={v}]")
            for v in ("night", "date", "drop", "price"):
                await page.select_option("#fSort", v)
            await page.click("[data-wish-edit]")
            await page.click("#wishCancel")
            if await page.locator("#flagToggle").count():
                await page.click("#flagToggle")
            await page.close()
        await browser.close()
    return problems


def main() -> int:
    problems = asyncio.run(run())
    for msg in dict.fromkeys(problems):
        print(f"::error::Dashboard-Test: {msg}")
    if problems:
        print("Dashboard-Test fehlgeschlagen – das bisherige Dashboard bleibt online.")
        return 1
    print("Dashboard-Test bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
