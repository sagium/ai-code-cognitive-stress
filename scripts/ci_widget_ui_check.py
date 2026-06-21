#!/usr/bin/env python3
"""CI: render the desktop-widget card in a headless browser and assert it works.

The widget UI *is* the HTML+SVG that `aicogstress --emit-html-card` prints
(ai_code_cognitive_stress/output/widget_card.py) — the Übersicht JSX host and preview.html both
inject that output verbatim. So loading preview.html in headless Chromium
exercises the real widget markup, no Mac required: we assert it renders without
console/page errors, that the structural elements are present and visible, then
save a screenshot artifact for a human to eyeball.

Run from the repo root (CI does this for you):

    python scripts/generate_demo_report.py --dayview-card desktop/ubersicht/card.html
    python scripts/ci_widget_ui_check.py

`card.html` must sit next to preview.html — preview.html fetch()es it, which is
why we serve the directory over HTTP rather than opening a file:// URL.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
WIDGET_DIR = REPO / "desktop" / "ubersicht"
SCREENSHOT = REPO / "widget-card.png"

# Structural elements the card always emits (ai_code_cognitive_stress/output/widget_card.py).
# `.cogstress` is the root the JSX host keys on; the rest are the visible parts
# of the daily view. A bare `.cogstress` with no children would pass a naive
# "did it render" check but be a broken card — so we assert the real surface.
REQUIRED_SELECTORS = {
    ".cogstress": 1,   # card root
    ".score": 1,       # composite score
    ".spark": 1,       # sparkline
    ".chart": 1,       # per-hour concurrency chart
    ".tile": 3,        # the three axis tiles (>= 3)
}


def _serve(directory: Path) -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )
    # Port 0 → the OS picks a free port, so concurrent CI jobs never collide.
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    card = WIDGET_DIR / "card.html"
    if not card.is_file():
        print(
            f"FAIL: {card} not found — generate it first:\n"
            "  python scripts/generate_demo_report.py "
            "--dayview-card desktop/ubersicht/card.html",
            file=sys.stderr,
        )
        return 1

    httpd, port = _serve(WIDGET_DIR)
    errors: list[str] = []
    problems: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 900, "height": 1200})
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
                if msg.type in ("error", "warning")
                else None,
            )
            page.goto(f"http://127.0.0.1:{port}/preview.html",
                      wait_until="networkidle")
            # preview.html fetch()es and injects the card; wait for the root.
            page.wait_for_selector(".cogstress", state="visible", timeout=10_000)

            for selector, minimum in REQUIRED_SELECTORS.items():
                n = page.locator(selector).count()
                if n < minimum:
                    problems.append(
                        f"  expected >= {minimum} of '{selector}', found {n}"
                    )

            box = page.locator(".cogstress").bounding_box()
            if not box or box["width"] < 100 or box["height"] < 100:
                problems.append(f"  '.cogstress' has no real size: {box}")

            page.screenshot(path=str(SCREENSHOT), full_page=True)
            browser.close()
    finally:
        httpd.shutdown()

    if errors:
        problems += ["  browser reported:"] + [f"    {e}" for e in errors]

    if problems:
        print("FAIL: widget UI render check:\n" + "\n".join(problems),
              file=sys.stderr)
        return 1

    print(f"OK: widget card rendered, all selectors present "
          f"(screenshot: {SCREENSHOT.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
