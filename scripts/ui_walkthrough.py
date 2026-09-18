"""Drive the map app the way a reviewer would, at desktop and phone sizes, with a mocked GPS fix.

    pip install playwright && python -m playwright install chromium
    python -m lakeaudit serve                      # in another terminal
    python scripts/ui_walkthrough.py               # screenshots -> docs/screenshots/

Flow per viewport: home -> click a lake -> Use my location (mocked 17.4935, 78.4043, +-35 m)
-> manual point at accuracy 40 (refusal) and 5 (verdict) -> toggle layers.
Exits non-zero on any console error, uncaught exception, failed check, touch target < 44 px,
or horizontal page scroll."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

GEO = {"latitude": 17.4935, "longitude": 78.4043, "accuracy": 35}
EDGE = ("17.430068", "78.486859")      # Hussain Sagar east shore, 20 m outside the extent (REVIEW_DEMO.md)
VIEWPORTS = {
    "desktop": {"viewport": {"width": 1400, "height": 900}},
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
}
# every visible control a finger has to hit; Leaflet's attribution links are text, not controls
TARGETS = "button:visible, summary:visible, label.opt:visible, input[type=range]:visible, .field input:visible, .leaflet-control-zoom a:visible"


def walk(p, base: str, out: Path, name: str, opts: dict) -> list[str]:
    browser = p.chromium.launch()
    ctx = browser.new_context(geolocation=GEO, permissions=["geolocation"], **opts)
    page = ctx.new_page()
    problems: list[str] = []
    page.on("console", lambda m: m.type == "error" and problems.append(f"console error: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    n = 0

    def check(cond: bool, what: str):
        if not cond:
            problems.append(f"{name}: {what}")

    def shot(tag: str):
        nonlocal n
        page.wait_for_timeout(1600)                     # flyTo animation + tiles
        n += 1
        path = out / f"{name}-{n:02d}-{tag}.png"
        page.screenshot(path=str(path))
        small = page.eval_on_selector_all(TARGETS, """els => els.map(e => {const r = e.getBoundingClientRect();
            return {h: r.height, w: r.width, t: (e.id || e.textContent || e.className || e.tagName).toString().trim().slice(0, 40)};})
            .filter(b => b.w > 0 && (b.h < 43.5))""")
        for b in small:
            problems.append(f"{name}/{tag}: touch target {b['h']:.0f}px tall: {b['t']!r}")
        overflow = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        check(overflow <= 0, f"{tag}: horizontal page scroll of {overflow}px")
        print(f"  {path}")

    def labels_inside(tag: str, names: list[str] | None = None):
        boxes = page.eval_on_selector_all(".leaflet-tooltip", """els => els
            .filter(e => getComputedStyle(e).display !== 'none')
            .map(e => { const r = e.getBoundingClientRect(); return {t: e.textContent, l: r.left, r: r.right, w: innerWidth}; })""")
        for b in boxes:
            if names is None or any(nm in b["t"] for nm in names):
                check(b["l"] >= 0 and b["r"] <= b["w"], f"{tag}: map label clipped at the viewport edge: {b['t']!r}")

    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector('body[data-ready="1"]', timeout=60000)
    check(page.locator("#lakeList .item").count() >= 1, "home: no lakes listed")
    check("lake" in page.inner_text("#brandMeta"), "home: brand badge has no lake count")
    shot("home")
    labels_inside("home")

    first = page.locator("#lakeList .item").first
    lake_name = first.locator(".nm").inner_text()
    first.click()
    page.wait_for_selector("#vLake:not([hidden])")
    check(page.locator("#lakeBody .wit li").count() == 3, "lake: witnesses list is not 3 rows")
    check("not yet extracted from the HMDA notification sheet" in page.inner_text("#lakeBody"), "lake: official FTL reason missing")
    check(page.locator("#lakeBody .bars .bar").count() >= 1, "lake: no year bars")
    shot("lake")

    page.click("#vLake [data-back]")
    page.click("#btnLocate")
    page.wait_for_selector("#resBody .card.dark", timeout=20000)
    body = page.inner_text("#resBody")
    check("lakes nearby" in body, "located: no 'N lakes nearby' title")
    check("±35 m" in body, "located: mocked accuracy not shown")
    check(page.locator("#resBody .card.verdict, #resBody .card.refusal").count() == 1, "located: no verdict or refusal card")
    check(page.locator("#resBody .card.dark + .card").count() == 1, "located: 'How sure are we' card is not first")
    shot("located")
    nearest = page.evaluate("lakeaudit.state.byId[lakeaudit.state.lookup.lake_id].name")
    labels_inside("located", ["You", nearest])

    page.click("#vResult [data-back]")
    if page.get_attribute("#manual", "open") is None:
        page.click("#manual summary")
    page.fill("#inLat", EDGE[0])
    page.fill("#inLon", EDGE[1])
    page.fill("#inAcc", "40")
    page.click("#btnCheck")
    page.wait_for_selector("#resBody .card.refusal", timeout=20000)
    check("±40 m" in page.inner_text("#resBody .card.refusal"), "manual 40 m: refusal reason missing")
    check(page.locator('[data-res="retry"]').count() == 1 and page.locator('[data-res="place"]').count() == 1, "manual 40 m: refusal buttons missing")
    shot("manual-40m-refusal")

    page.click("#vResult [data-back]")
    page.fill("#inAcc", "5")
    page.click("#btnCheck")
    page.wait_for_selector("#resBody .card.verdict", timeout=20000)
    check(page.locator("#resBody .card.verdict.z-in_buffer").count() == 1, "manual 5 m: expected in_buffer verdict")
    shot("manual-5m-verdict")

    if name == "phone":
        page.click("#layersToggle")
        page.wait_for_selector("#layersBody", state="visible")
    check(page.is_disabled("#lyFtl"), "layers: Notified FTL checkbox is not disabled")
    page.check('input[name="base"][value="satellite"]')
    page.uncheck("#lyStruct")
    page.check("#lySeed")
    page.locator("#yr").fill("2020")
    check(page.inner_text("#yrOut") == "2020", "layers: year slider did not update")
    shot("layers")

    print(f"  [{name}] first lake clicked: {lake_name}")
    browser.close()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "docs" / "screenshots"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    problems = []
    with sync_playwright() as p:
        for name, opts in VIEWPORTS.items():
            print(f"{name} {opts['viewport']['width']}x{opts['viewport']['height']}")
            problems += walk(p, a.base, out, name, opts)
    for x in problems:
        print("PROBLEM", x)
    print("OK — no console errors, all checks passed" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
