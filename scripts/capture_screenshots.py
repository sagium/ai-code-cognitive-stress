"""Refresh the README screenshots in docs/screenshots/ as crisp, scalable SVGs.

The captures are *mostly-vector*: text, charts, and rules come out as real
vector glyphs/paths (sharp at any zoom), and only the glass-blur panel
backgrounds are embedded as raster patches — at an effective 2x density, so
they stay smooth well past the README's display size.

How it works (the recipe — see .claude/skills/crisp-screenshots/SKILL.md):

  1. scripts/generate_demo_report.py writes a deterministic, synthetic demo
     report (no real session data) to /tmp/demo-report.html;
  2. headless Chrome prints it to a single-page vector PDF — with the page
     laid out at ``body{zoom:2}`` in a 2x viewport, which is what forces the
     rasterized glass patches to land at 2x density while text stays vector;
  3. pdftocairo converts the PDF to SVG;
  4. the SVG root gets its intrinsic size normalized back to 1x CSS pixels,
     and the day-modal capture is cropped (via viewBox) to the drill-down.

Usage:

    uv run --with playwright --with scour python scripts/capture_screenshots.py

Dev-only dependencies (never needed at package runtime): Chrome/Chromium,
playwright (provided by uv at invocation time), and pdftocairo (poppler).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "screenshots"
DEMO_HTML = Path("/tmp/demo-report.html")  # where generate_demo_report.py writes

# Preferred drill-down day (WIDGET_DEMO_DAY in generate_demo_report.py); the
# seeded generator may leave it inactive, so the closest active day is used.
DEMO_DAY = "2026-03-11"

VIEWPORT_W = 2560   # 2x of the 1280px capture width
ZOOM = 2            # body{zoom:2} → raster patches at effective 2x density
PT_PER_PX = 0.75    # pdftocairo viewBox units are pt; Chrome prints 96dpi px


def _missing(fragment: str) -> None:
    raise SystemExit(f"#{fragment} not found in demo report")


def _build_demo_report() -> None:
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "generate_demo_report.py")],
        check=True,
    )
    if not DEMO_HTML.is_file():
        raise SystemExit(f"demo report not found at {DEMO_HTML}")


def _print_pdf(fragment: str | None) -> tuple[Path, dict | None]:
    """Print the demo report to a single-page vector PDF.

    Returns the PDF path and, when a fragment targets a day drill-down, that
    section's bounding box in (zoomed) CSS pixels for the viewBox crop.
    """
    from playwright.sync_api import sync_playwright

    # IMPORTANT: never navigate to the #fragment itself. With a :target'd
    # drill-down (or any :has(:target) state) Chrome's print path rasterizes
    # the ENTIRE page into one bitmap — no vector text. Revealing the modal
    # with inline styles instead keeps the print vector.
    pdf_path = Path(tempfile.mkstemp(suffix=".pdf")[1])
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT_W, "height": 1200})
        page.goto(DEMO_HTML.as_uri())
        page.wait_for_timeout(500)
        page.add_style_tag(content=f"body{{zoom:{ZOOM}}}")
        if fragment:
            # Reveal the drill-down in flow (it is a position:fixed overlay),
            # drop its backdrop, force its ancestor chain visible, and
            # visibility:hide everything else so the PDF paints only the
            # modal (small file). display:none would be tighter, but the
            # zoom+display:none combination re-triggers the full-page
            # rasterization fallback — visibility does not.
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.style.display = 'block';
                    el.style.position = 'static';
                    el.querySelector('.day-backdrop').style.display = 'none';
                    for (let n = el; n && n !== document.body; n = n.parentElement) {
                        n.style.visibility = 'visible';
                        n.style.display = n.style.display || 'block';
                        for (const sib of n.parentElement.children)
                            if (sib !== n) sib.style.visibility = 'hidden';
                    }
                    window.scrollTo(0, 0);
                    return true;
                }""",
                f"#{fragment}",
            ) or _missing(fragment)
        page.wait_for_timeout(300)
        height = page.evaluate("document.documentElement.scrollHeight")
        bbox = None
        if fragment:
            bbox = page.evaluate(
                """(sel) => {
                    const r = document.querySelector(sel).getBoundingClientRect();
                    return {x: r.x + window.scrollX, y: r.y + window.scrollY,
                            w: r.width, h: r.height};
                }""",
                f"#{fragment} .day-modal",
            )
        page.pdf(
            path=str(pdf_path),
            width=f"{VIEWPORT_W}px",
            height=f"{height}px",
            print_background=True,
            page_ranges="1",
        )
        browser.close()
    return pdf_path, bbox


def _pdf_to_svg(pdf_path: Path, out: Path, bbox: dict | None) -> None:
    subprocess.run(["pdftocairo", "-svg", str(pdf_path), str(out)], check=True)
    text = out.read_text(encoding="utf-8")

    # The width/height carry a "pt" unit on older poppler and none on newer
    # (the numbers are pt either way: px × PT_PER_PX); accept both.
    m = re.search(
        r'<svg([^>]*?)width="([\d.]+)(?:pt)?" height="([\d.]+)(?:pt)?" '
        r'viewBox="0 0 ([\d.]+) ([\d.]+)"', text
    )
    if not m:
        raise SystemExit(f"unexpected SVG root in {out}")
    vb_w, vb_h = float(m.group(4)), float(m.group(5))

    if bbox:  # crop to the drill-down (bbox is in zoomed CSS px → pt)
        pad = 16 * ZOOM * PT_PER_PX
        x = max(0.0, bbox["x"] * PT_PER_PX - pad)
        y = max(0.0, bbox["y"] * PT_PER_PX - pad)
        w = min(vb_w - x, bbox["w"] * PT_PER_PX + 2 * pad)
        h = min(vb_h - y, bbox["h"] * PT_PER_PX + 2 * pad)
    else:
        x, y, w, h = 0.0, 0.0, vb_w, vb_h

    # Intrinsic size back to 1x CSS pixels (undo the 2x zoom and the pt unit).
    css_w = round(w / PT_PER_PX / ZOOM, 2)
    css_h = round(h / PT_PER_PX / ZOOM, 2)
    root = (
        f'<svg{m.group(1)}width="{css_w}" height="{css_h}" '
        f'viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}"'
    )
    out.write_text(_minify(text.replace(m.group(0), root, 1)), encoding="utf-8")
    print(f"wrote {out} ({css_w:g}x{css_h:g} css px, viewBox {w:.0f}x{h:.0f}pt, "
          f"{out.stat().st_size // 1024} KB)")


def _minify(svg: str) -> str:
    """Scour the verbose pdftocairo output (about half the size); lossless
    apart from path-coordinate precision, which stays ample at 5 digits."""
    try:
        from scour import scour
    except ImportError:
        print("  (scour not available — emitting unminified SVG; "
              "run via: uv run --with playwright --with scour ...)")
        return svg
    options = scour.sanitizeOptions(None)
    options.digits = 5
    options.remove_metadata = True
    options.strip_comments = True
    options.strip_xml_space_attribute = True
    options.shorten_ids = True
    return scour.scourString(svg, options)


def capture(name: str, fragment: str | None) -> None:
    pdf_path, bbox = _print_pdf(fragment)
    try:
        _pdf_to_svg(pdf_path, OUT_DIR / name, bbox)
    finally:
        pdf_path.unlink(missing_ok=True)


def _pick_demo_day() -> str:
    """The preferred demo day, or the closest active day the generator made."""
    from datetime import date

    days = sorted(set(re.findall(r'id="day-(\d{4}-\d{2}-\d{2})"',
                                 DEMO_HTML.read_text(encoding="utf-8"))))
    if not days:
        raise SystemExit("no day drill-downs in the demo report")
    if DEMO_DAY in days:
        return DEMO_DAY
    want = date.fromisoformat(DEMO_DAY)
    return min(days, key=lambda d: abs(date.fromisoformat(d) - want))


def main() -> None:
    _build_demo_report()
    capture("report-overview.svg", None)
    capture("day-modal.svg", f"day-{_pick_demo_day()}")


if __name__ == "__main__":
    main()
