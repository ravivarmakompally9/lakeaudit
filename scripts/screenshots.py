"""Screenshots of the web app on real outputs, desktop and phone, for the review deck.
All checks live in tests/test_ui.py; this only takes pictures.

    python -m lakeaudit serve            # in another terminal
    python scripts/screenshots.py        # -> docs/screenshots/
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
GPS = {"latitude": 17.4935, "longitude": 78.4043, "accuracy": 35}     # Kukatpally, ~1.5 km from Nalla Cheruvu
EDGE = ("17.430068", "78.486859")                                     # Hussain Sagar east shore, 20 m outside
SIZES = {"desktop": {"viewport": {"width": 1400, "height": 900}},
         "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}}


def settle(page):
    page.wait_for_timeout(1500)
    page.wait_for_function("() => [...document.querySelectorAll('.leaflet-tile-container img')]"
                           ".every(i => i.classList.contains('leaflet-tile-loaded'))", timeout=15000)
    page.wait_for_timeout(300)


def check(page, acc):
    page.fill("#inLat", EDGE[0])
    page.fill("#inLon", EDGE[1])
    page.fill("#inAcc", acc)
    page.click("#btnCheck")
    page.wait_for_selector("#answer")


OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, size in SIZES.items():
        ctx = browser.new_context(**size, geolocation=GPS, permissions=["geolocation"])
        page = ctx.new_page()
        page.goto(BASE)
        page.wait_for_selector('body[data-ready="1"]')
        shots = [("1-home", lambda: None),
                 ("2-lake", lambda: page.locator("#lakeList .lake", has_text="Hussain Sagar").click()),
                 ("3-my-location", lambda: (page.click("#vLake [data-back]"), page.click("#btnLocate"), page.wait_for_selector("#answer"))),
                 ("4-cannot-answer", lambda: (page.click("#vResult [data-back]"), page.click("#manual summary"), check(page, "40"))),
                 ("5-answer", lambda: (page.click("#vResult [data-back]"), check(page, "5")))]
        for tag, step in shots:
            step()
            settle(page)
            page.screenshot(path=str(OUT / f"{name}-{tag}.png"))
            print(OUT / f"{name}-{tag}.png")
        ctx.close()
    browser.close()
