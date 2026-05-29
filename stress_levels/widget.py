"""Live, always-on-top desktop widget showing TODAY's full daily view, in tkinter.

Optional feature (`--widget`). tkinter is the stdlib GUI toolkit, so this stays
within the project's zero-third-party-dependency rule. tkinter is imported
lazily inside `run_widget` so importing this module (and the rest of the tool,
and the test suite) never requires a display or the tk libraries.

The widget renders the SAME daily view as the HTML report and the KDE Plasma
widget — composite, work window, the per-hour concurrency chart, and the three
axis tiles with zone range bars + baseline/optimum/you markers + methodology —
all from the shared `dayview` model, so the three surfaces can't drift.

Architecture (the thread-safety bit matters): tkinter is single-threaded, so a
daemon worker thread does the (~150–600 ms) recompute and pushes results onto a
queue — it never touches a tk object. The main thread drains the queue via
`root.after(...)` and repaints only when the day view actually changed.

The pure data layer (`compute_today_dayview`) and the model it returns
(`dayview.DayView`) carry no tk dependency and are unit-tested headlessly.
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta, timezone, tzinfo

from . import dayview
from .aggregate import get_day_aggregates
from .dayview import AxisTile, DayView
from .metrics import DayMetrics, build_profile
from .scales import PALETTE

MIN_REFRESH_SECONDS = 10


# ---------------------------------------------------------------------------
# Pure data layer (no tkinter)

def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def compute_today_dayview(
    baseline_days: int = 30,
    sources=None,
    projects_dir=None,
    cache_dir=None,
    now: datetime | None = None,
) -> DayView:
    """Recompute today's full daily view. Reads only today's session files live
    (past days come from the on-disk cache). `now` is injectable for tests."""
    tz = _local_tz()
    today = (now or datetime.now(tz)).astimezone(tz).date()
    since = today - timedelta(days=baseline_days)
    aggregates, _ = get_day_aggregates(
        since, today,
        projects_dir=projects_dir, cache_dir=cache_dir,
        sources=sources, local_tz=tz, now=now,
    )
    profile = build_profile(aggregates, baseline_days=baseline_days, local_tz=tz)
    metrics = profile.days.get(today) or DayMetrics(day=today)
    return dayview.build_dayview(metrics, aggregates.get(today), profile, tz)


# ---------------------------------------------------------------------------
# Colour helpers (blend toward the panel colour to mirror the report's SVG
# opacities, since tkinter fills have no alpha channel).

def _blend(fg: str, alpha: float, bg: str = "#ffffff") -> str:
    fr, fgc, fb = int(fg[1:3], 16), int(fg[3:5], 16), int(fg[5:7], 16)
    br, bgc, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    r = round(fr * alpha + br * (1 - alpha))
    g = round(fgc * alpha + bgc * (1 - alpha))
    b = round(fb * alpha + bb * (1 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# tkinter shell

def run_widget(  # pragma: no cover — tkinter event loop; needs a display
    baseline_days: int = 30,
    sources=None,
    refresh_seconds: int = 60,
) -> int:
    """Launch the always-on-top live widget. Blocks until the window closes.
    Returns 0 on clean exit, 1 if tkinter is unavailable."""
    try:
        import tkinter as tk
    except Exception:  # pragma: no cover — environment without tk
        import sys
        print(
            "stress-levels: the --widget feature needs tkinter, which isn't "
            "available.\n  On Debian/Ubuntu: sudo apt install python3-tk\n"
            "  On macOS/Windows it ships with python.org builds; uvx-provisioned "
            "Python includes it.",
            file=sys.stderr,
        )
        return 1

    refresh = max(MIN_REFRESH_SECONDS, int(refresh_seconds))
    q: "queue.Queue[DayView | Exception]" = queue.Queue(maxsize=2)
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                q.put(compute_today_dayview(baseline_days, sources))
            except Exception as exc:  # surface, don't die
                q.put(exc)
            stop.wait(refresh)

    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover — headless / no $DISPLAY
        import sys
        print(
            f"stress-levels: could not open a display for the widget ({exc}). "
            "Run it from a desktop session (not headless/SSH without X).",
            file=sys.stderr,
        )
        return 1
    root.title("Stress · today")
    root.attributes("-topmost", True)
    root.geometry("660x780")
    root.configure(bg=PALETTE["bg"])

    inner, set_status = _build_content(tk, root)
    state: dict = {"last": None}

    def drain() -> None:
        try:
            item = q.get_nowait()
        except queue.Empty:
            pass
        else:
            if isinstance(item, Exception):
                set_status(f"error: {item}", PALETTE["bad"])
            elif item != state["last"]:
                state["last"] = item
                set_status("", PALETTE["bg"])
                _paint_dayview(tk, inner, item)
                _fit_window_height(root, inner)
        if not stop.is_set():
            root.after(250, drain)

    def on_close() -> None:
        stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    threading.Thread(target=worker, daemon=True).start()
    root.after(50, drain)
    root.mainloop()
    stop.set()
    return 0


def _build_content(tk, root):  # pragma: no cover — tk rendering
    """A plain content frame (no scrollbar — the window is sized to fit the
    whole daily view). Returns the frame to paint into, plus a set_status(text,
    colour) callback for a small error line at the top."""
    status = tk.Label(
        root, text="", bg=PALETTE["bg"], fg=PALETTE["bad"],
        font=("TkDefaultFont", 9), anchor="w",
    )
    inner = tk.Frame(root, bg=PALETTE["bg"])
    inner.pack(fill="both", expand=True)

    def set_status(text: str, colour: str) -> None:
        # Only occupy a row when there's an error, so it doesn't add a top gap.
        if text:
            status.config(text=text, fg=colour)
            status.pack(fill="x", padx=14, pady=(8, 0), before=inner)
        else:
            status.pack_forget()

    return inner, set_status


def _fit_window_height(root, inner) -> None:  # pragma: no cover — tk rendering
    """Size the window to fit the whole daily view (capped to the screen) so
    every section is visible without scrolling."""
    inner.update_idletasks()
    need = inner.winfo_reqheight() + 24  # small status row + window chrome
    avail = root.winfo_screenheight() - 96
    root.geometry(f"660x{max(420, min(need, avail))}")


def _paint_dayview(tk, inner, dv: DayView) -> None:  # pragma: no cover — tk rendering
    """Clear and rebuild the daily view. Called only when the data changes."""
    for child in inner.winfo_children():
        child.destroy()

    pad = {"padx": 14}

    # Header: composite / 100 on the left, the one-word advice on the right,
    # vertically centred against the big number, with the intraday score
    # progression sparkline between them. No date.
    header = tk.Frame(inner, bg=PALETTE["bg"])
    header.pack(fill="x", **pad, pady=(12, 8))
    comp = tk.Frame(header, bg=PALETTE["bg"])
    comp.pack(side="left")
    tk.Label(
        comp, text=dv.composite_label, bg=PALETTE["bg"], fg=dv.composite_color,
        font=("TkDefaultFont", 34, "bold"),
    ).pack(side="left")
    tk.Label(
        comp, text="/ 100", bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
        font=("TkDefaultFont", 12), anchor="sw",
    ).pack(side="left", padx=(6, 0), pady=(0, 8))
    tk.Label(
        header, text=dv.advice, bg=PALETTE["bg"], fg=dv.composite_color,
        font=("TkDefaultFont", 16, "bold"),
    ).pack(side="right")
    if len(dv.score_progression) >= 2:
        _draw_sparkline(tk, header, dv).pack(side="left", expand=True)

    _draw_chart(tk, inner, dv)

    for tile in dv.axes:
        _draw_tile(tk, inner, tile)

    tk.Label(
        inner, text="local-only · updates live", bg=PALETTE["bg"],
        fg=PALETTE["ink_faint"], font=("TkDefaultFont", 8), anchor="w",
    ).pack(fill="x", **pad, pady=(8, 12))


def _draw_chart(tk, parent, dv: DayView) -> None:  # pragma: no cover — tk rendering
    """Per-hour concurrent-session bar chart, mirroring the report's day chart:
    work-window shade, integer y-ticks, value labels, 3-hourly x-labels. Redraws
    to the canvas's actual width so it fills the window and never truncates."""
    H = 200
    c = tk.Canvas(parent, height=H, bg=PALETTE["panel"],
                  highlightthickness=1, highlightbackground=PALETTE["rule"])
    c.pack(fill="x", padx=14, pady=(4, 16))
    max_count = max(dv.peak_concurrent, 1)
    bar_fill = _blend(PALETTE["warn"], 0.85, PALETTE["panel"])
    shade = _blend(PALETTE["good"], 0.12, PALETTE["panel"])

    def redraw(W: int) -> None:
        c.delete("all")
        m_top, m_right, m_bottom, m_left = 38, 12, 26, 30
        plot_w = W - m_left - m_right
        plot_h = H - m_top - m_bottom
        if plot_w <= 0:
            return
        bar_w = plot_w / 24

        c.create_text(10, 15, text="Concurrent agent sessions per hour",
                      anchor="w", fill=PALETTE["ink"], font=("TkDefaultFont", 10, "bold"))

        if dv.work_window and dv.work_window.end_hour > dv.work_window.start_hour:
            c.create_rectangle(m_left + dv.work_window.start_hour * bar_w, m_top,
                               m_left + dv.work_window.end_hour * bar_w, m_top + plot_h,
                               fill=shade, width=0)

        for i in range(max_count + 1):
            y = m_top + plot_h - (i / max_count) * plot_h
            c.create_line(m_left, y, m_left + plot_w, y, fill=PALETTE["rule"])
            c.create_text(m_left - 5, y, text=str(i), anchor="e",
                          fill=PALETTE["ink_faint"], font=("TkDefaultFont", 8))

        for h, count in enumerate(dv.hours):
            if count <= 0:
                continue
            bar_px = (count / max_count) * plot_h
            x = m_left + h * bar_w + bar_w * 0.08
            y = m_top + plot_h - bar_px
            c.create_rectangle(x, y, x + bar_w * 0.84, m_top + plot_h, fill=bar_fill, width=0)
            c.create_text(x + bar_w * 0.42, y - 6, text=str(count),
                          fill=PALETTE["ink"], font=("TkDefaultFont", 8, "bold"))

        c.create_line(m_left, m_top + plot_h, m_left + plot_w, m_top + plot_h,
                      fill=PALETTE["ink_soft"])
        for h in range(0, 25, 3):
            c.create_text(m_left + h * bar_w, m_top + plot_h + 12, text=f"{h:02d}",
                          fill=PALETTE["ink_faint"], font=("TkDefaultFont", 8))

    c.bind("<Configure>", lambda e: redraw(e.width))


def _draw_sparkline(tk, parent, dv: DayView):  # pragma: no cover — tk rendering
    """A small line of the day's cumulative composite (0–100), drawn as a
    severity gradient (each segment tinted by that level's zone colour) — sits
    between the score and the advice in the header."""
    series = dv.score_progression
    W, H, p = 160, 42, 5
    c = tk.Canvas(parent, width=W, height=H, bg=PALETTE["bg"], highlightthickness=0)
    n = len(series)
    if n < 2:
        return c

    def sx(i: int) -> float:
        return p + (i / (n - 1)) * (W - 2 * p)

    def sy(v: float) -> float:
        return H - p - (max(0.0, min(100.0, v)) / 100.0) * (H - 2 * p)

    c.create_line(p, H - p, W - p, H - p, fill=PALETTE["rule"])  # baseline
    # Per-segment colour → the line shifts green→amber→red as the score climbs.
    for i in range(n - 1):
        c.create_line(sx(i), sy(series[i].value), sx(i + 1), sy(series[i + 1].value),
                      fill=series[i + 1].color, width=2)
    last = series[-1]
    lx, ly = sx(n - 1), sy(last.value)
    c.create_oval(lx - 2.5, ly - 2.5, lx + 2.5, ly + 2.5, fill=last.color, width=0)
    return c


def _draw_tile(tk, parent, tile: AxisTile) -> None:  # pragma: no cover — tk rendering
    """One axis tile: head (name + zone), description, range bar, value/unit,
    and a collapsible methodology block (Technique / Basis / Caveat)."""
    frame = tk.Frame(parent, bg=PALETTE["panel"], highlightthickness=1,
                     highlightbackground=PALETTE["rule"])
    frame.pack(fill="x", padx=14, pady=5)
    ipad = {"padx": 10}

    head = tk.Frame(frame, bg=PALETTE["panel"])
    head.pack(fill="x", **ipad, pady=(8, 0))
    tk.Label(head, text=tile.name, bg=PALETTE["panel"], fg=PALETTE["ink"],
             font=("TkDefaultFont", 10, "bold")).pack(side="left")
    from .scales import zone_color
    tk.Label(head, text=tile.zone_label, bg=PALETTE["panel"],
             fg=zone_color(tile.status), font=("TkDefaultFont", 9, "bold"),
             anchor="e").pack(side="right")

    tk.Label(frame, text=tile.description, bg=PALETTE["panel"],
             fg=PALETTE["ink_soft"], font=("TkDefaultFont", 8),
             wraplength=580, justify="left", anchor="w").pack(fill="x", **ipad, pady=(2, 2))

    _draw_range_bar(tk, frame, tile)

    vrow = tk.Frame(frame, bg=PALETTE["panel"])
    vrow.pack(fill="x", **ipad)
    tk.Label(vrow, text=tile.value_label, bg=PALETTE["panel"], fg=tile.color,
             font=("TkDefaultFont", 13, "bold")).pack(side="left")
    tk.Label(vrow, text=tile.unit_text, bg=PALETTE["panel"], fg=PALETTE["ink_soft"],
             font=("TkDefaultFont", 8), anchor="e").pack(side="right")

    # Collapsible methodology — mirrors the report's <details> disclosure.
    body = tk.Frame(frame, bg=PALETTE["panel"])
    btn_text = tk.StringVar(value="▸ How this is computed & what it can't tell you")
    shown = {"open": False}

    def toggle() -> None:
        shown["open"] = not shown["open"]
        if shown["open"]:
            body.pack(fill="x", **ipad, pady=(0, 8))
            btn_text.set("▾ How this is computed & what it can't tell you")
        else:
            body.pack_forget()
            btn_text.set("▸ How this is computed & what it can't tell you")

    tk.Button(frame, textvariable=btn_text, command=toggle, relief="flat",
              bg=PALETTE["panel"], fg=PALETTE["accent"], activebackground=PALETTE["panel"],
              font=("TkDefaultFont", 8), anchor="w", borderwidth=0,
              highlightthickness=0, cursor="hand2").pack(fill="x", padx=8, pady=(2, 6))

    for head_txt, body_txt in (
        ("Technique", tile.technique),
        ("Research basis", tile.basis),
        ("Caveat", tile.caveat),
    ):
        tk.Label(body, text=head_txt, bg=PALETTE["panel"], fg=PALETTE["ink"],
                 font=("TkDefaultFont", 8, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text=body_txt, bg=PALETTE["panel"], fg=PALETTE["ink_soft"],
                 font=("TkDefaultFont", 8), wraplength=580, justify="left",
                 anchor="w").pack(fill="x", pady=(0, 4))


def _draw_range_bar(tk, parent, tile: AxisTile) -> None:  # pragma: no cover — tk rendering
    """Zone-segmented bar with baseline/optimum/you markers + boundary ticks,
    each on its own row so labels never overlap (responsive width). Layout
    top→bottom: baseline label · optimum label · bar · tick numbers · you."""
    H = 96
    bar_y, bar_h = 46, 14
    baseline_label_y = 8            # row 1: baseline ("typical day")
    optimum_label_y = 26            # row 2: optimum — well clear of the baseline
    tick_y = bar_y + bar_h + 8      # zone-boundary + endpoint numbers
    you_y = bar_y + bar_h + 21      # "you VALUE" on its own row below the ticks

    c = tk.Canvas(parent, height=H, bg=PALETTE["panel"], highlightthickness=0)
    c.pack(fill="x", padx=8, pady=2)

    def redraw(W: int) -> None:
        c.delete("all")
        pad = 26
        inner = W - 2 * pad
        if inner <= 0:
            return

        def x_at(frac: float) -> float:
            return pad + max(0.0, min(1.0, frac)) * inner

        def anchor_at(x: float) -> str:
            if x < pad + 26:
                return "w"
            if x > W - pad - 26:
                return "e"
            return "center"

        for seg in tile.segments:
            c.create_rectangle(x_at(seg.start), bar_y, x_at(seg.end), bar_y + bar_h,
                               fill=_blend(seg.color, 0.6, PALETTE["panel"]), width=0)

        for tick in tile.boundary_ticks:
            c.create_text(x_at(tick.fraction), tick_y, text=tick.label,
                          fill=PALETTE["ink_faint"], font=("TkDefaultFont", 7))
        c.create_text(pad, tick_y, text="0", fill=PALETTE["ink_faint"],
                      font=("TkDefaultFont", 7))
        c.create_text(W - pad, tick_y, text=f"{tile.range_max:g}",
                      fill=PALETTE["ink_faint"], font=("TkDefaultFont", 7))

        if tile.baseline_fraction is not None:
            bx = x_at(tile.baseline_fraction)
            c.create_line(bx, baseline_label_y + 6, bx, bar_y + bar_h + 4,
                          fill=PALETTE["ink_soft"], dash=(2, 2))
            c.create_text(bx, baseline_label_y, text=tile.baseline_label, anchor=anchor_at(bx),
                          fill=PALETTE["ink_soft"], font=("TkDefaultFont", 7))

        if tile.optimum_fraction is not None:
            ox = x_at(tile.optimum_fraction)
            c.create_line(ox, optimum_label_y + 6, ox, bar_y + bar_h + 4,
                          fill=PALETTE["accent"], dash=(3, 3))
            c.create_text(ox, optimum_label_y, text=tile.optimum_label, anchor=anchor_at(ox),
                          fill=PALETTE["accent"], font=("TkDefaultFont", 7))

        ux = x_at(min(1.0, tile.fraction))
        label = f"you {tile.value:.2f}" + (" ▶" if tile.off_scale else "")
        c.create_line(ux, bar_y - 8, ux, bar_y + bar_h + 8, fill=PALETTE["ink"], width=2)
        c.create_text(ux, you_y, text=label, anchor=anchor_at(ux),
                      fill=PALETTE["ink"], font=("TkDefaultFont", 8, "bold"))

    c.bind("<Configure>", lambda e: redraw(e.width))
