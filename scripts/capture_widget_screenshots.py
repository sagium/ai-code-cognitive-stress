#!/usr/bin/env python3
"""Refresh the desktop-widget screenshots in docs/screenshots/.

The widget card is inherently a glass/blur surface over a wallpaper, so unlike
the report screenshots (which are mostly-vector SVGs — see
scripts/capture_screenshots.py and .claude/skills/crisp-screenshots/SKILL.md)
these are raster PNGs captured at 2x device density — the one case the
crisp-screenshots skill admits a raster for.

The card markup itself comes from the real renderer
(ai_code_cognitive_stress/output/widget_card.py) via scripts/generate_demo_report.py, and it
is rendered through desktop/ubersicht/preview.html — the same dusk-wallpaper
preview surface a human uses — so the screenshots are faithful to the widget,
with no duplicated rendering to drift.

What it writes (all under docs/screenshots/):
  - widget-{today,week,month,year}.png  the full card, one PNG per tab
  - widget-mini.png                     the small (compact) card, Today tab

Usage (from the repo root):

    uv run --with playwright python scripts/capture_widget_screenshots.py

Dev-only dependencies (never package runtime deps): Chromium (provided by
playwright, itself provided by uv at invocation time).
"""

from __future__ import annotations

import functools
import http.server
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WIDGET_DIR = REPO / "desktop" / "ubersicht"
OUT_DIR = REPO / "docs" / "screenshots"
GEN = REPO / "scripts" / "generate_demo_report.py"

# The card is a fixed 384px wide (widget_card.CARD_WIDTH). A 34px wallpaper
# margin each side and a 2x device scale give (384 + 68) * 2 = 904px PNGs —
# matching the committed widget screenshots. Vertical margin is symmetric.
DEVICE_SCALE = 2
MARGIN_X = 34
MARGIN_Y = 34

# The full card's four tabs, each captured as its own PNG.
TABS = ["today", "week", "month", "year"]


def _serve(directory: Path) -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )
    # Port 0 → the OS picks a free port (concurrent runs never collide).
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _gen_card(out: Path, *, compact: bool) -> None:
    cmd = [sys.executable, str(GEN), "--dayview-card-tabbed", str(out)]
    if compact:
        cmd.append("--compact")
    subprocess.run(cmd, check=True)


def _capture(page, port: int, out: Path, *, tab: str) -> None:
    """Load preview.html, activate ``tab``, and clip-screenshot the card."""
    page.goto(f"http://127.0.0.1:{port}/preview.html", wait_until="networkidle")
    page.wait_for_selector(".cogstress", state="visible", timeout=10_000)
    # preview.html wires the tab toggle with a delegated click handler (the same
    # one the Übersicht host uses), so clicking the tab is the faithful path.
    page.locator(f'.tab[data-view="{tab}"]').click()
    page.wait_for_timeout(150)  # let the view swap settle
    box = page.locator(".cogstress").bounding_box()
    if not box:
        raise SystemExit(f"could not measure the card for {out.name}")
    clip = {
        "x": box["x"] - MARGIN_X,
        "y": box["y"] - MARGIN_Y,
        "width": box["width"] + 2 * MARGIN_X,
        "height": box["height"] + 2 * MARGIN_Y,
    }
    page.screenshot(path=str(out), clip=clip)
    _optimize(out)
    w = round(clip["width"] * DEVICE_SCALE)
    h = round(clip["height"] * DEVICE_SCALE)
    print(f"wrote {out} ({w}x{h}px @ {DEVICE_SCALE}x, "
          f"{out.stat().st_size // 1024} KB)")


def _optimize(png: Path) -> None:
    """Losslessly shrink the PNG with optipng if it's on PATH (the pixels are
    unchanged — just smaller on disk). A no-op when optipng isn't installed."""
    if shutil.which("optipng"):
        subprocess.run(["optipng", "-quiet", "-o5", "-strip", "all", str(png)],
                       check=True)


def main() -> None:
    from playwright.sync_api import sync_playwright

    card = WIDGET_DIR / "card.html"  # the path preview.html fetch()es
    httpd, port = _serve(WIDGET_DIR)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": 600, "height": 1400},
                device_scale_factor=DEVICE_SCALE,
            )

            _gen_card(card, compact=False)
            for tab in TABS:
                _capture(page, port, OUT_DIR / f"widget-{tab}.png", tab=tab)

            _gen_card(card, compact=True)
            _capture(page, port, OUT_DIR / "widget-mini.png", tab="today")

            browser.close()
    finally:
        httpd.shutdown()
        # Leave card.html as the full card (its committed/CI-expected state).
        _gen_card(card, compact=False)


if __name__ == "__main__":
    main()
