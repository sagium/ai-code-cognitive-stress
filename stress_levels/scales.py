"""Shared scale/zone/colour definitions for the three stress axes.

Single source of truth for the visual + threshold logic used by BOTH the HTML
report (render.py) and the live desktop widget (widget.py), so the two can
never drift. UI-agnostic: no HTML, no tkinter, no metrics/render imports.

Zones: list of (upper_bound, status_class, label). A value falls in the first
zone whose upper_bound it does not exceed. status_class ∈
{"good","moderate","caution","high"} drives the colour.
"""

from __future__ import annotations

# Named palette (mirrors the report's CSS custom properties).
PALETTE = {
    "bg": "#fafaf7",
    "panel": "#ffffff",
    "ink": "#1f2024",
    "ink_soft": "#5a5d66",
    "ink_faint": "#8a8d96",
    "rule": "#e6e4dd",
    "accent": "#355070",   # optimum line
    "good": "#6c9a8b",
    "warn": "#d99058",
    "bad": "#b04a3a",
}

# Status-class → bar colour (note "moderate" sits between good and caution).
ZONE_COLORS = {
    "good": "#6c9a8b",
    "moderate": "#c5b48a",
    "caution": "#d99058",
    "high": "#b04a3a",
}

# Per-axis zone tables. Upper bound is inclusive (value <= upper → that zone).
CODL_ZONES = [
    (1.5, "good", "Focused single-stream work"),
    (3.0, "moderate", "Parallel but manageable"),
    (4.0, "caution", "Approaching working-memory limit"),
    (999.0, "high", "Above working-memory capacity"),
]
INTERRUPTION_ZONES = [
    (2.0, "good", "Few attention-pulls"),
    (5.0, "moderate", "Normal interruption rate"),
    (8.0, "caution", "Frequently fragmented"),
    (999.0, "high", "Heavily fragmented"),
]
CLOSURE_ZONES = [
    (0.20, "good", "Loops closed in one sitting"),
    (0.45, "moderate", "Some parked and resumed"),
    (0.70, "caution", "Frequent cold resumes"),
    (1.01, "high", "Heavy resume thrashing"),
]

# Range-bar maxima per axis. CODL's ceiling is the metric's normalisation
# ceiling and lives in metrics.py (imported by callers); these two are the
# render maxima for the other axes.
INTERRUPTION_RANGE_MAX = 10.0
CLOSURE_RANGE_MAX = 1.0


def zone_for(value: float, zones: list[tuple[float, str, str]]) -> tuple[str, str]:
    """Return (status_class, label) for the first zone the value fits in."""
    for upper, status_class, label in zones:
        if value <= upper:
            return status_class, label
    last = zones[-1]
    return last[1], last[2]


def zone_color(status_class: str) -> str:
    return ZONE_COLORS.get(status_class, "#999999")


def codl_count_color(count: float) -> str:
    """Bar colour for a concurrent-session count, by its CODL zone — so the
    per-hour concurrency bars (report + both widgets) shade green→amber→red with
    rising parallelism instead of a flat colour. Shared so the surfaces can't
    drift from the CODL thresholds (Cowan 2001, ~4)."""
    status_class, _ = zone_for(count, CODL_ZONES)
    return zone_color(status_class)


def composite_color(status_class: str) -> str:
    """Colour for a composite-status class (good/caution/high). Used by the
    report header, the live widget, and the daily-view model."""
    return {
        "good": PALETTE["good"], "caution": PALETTE["warn"], "high": PALETTE["bad"],
    }.get(status_class, PALETTE["ink_faint"])


def composite_advice(status_class: str) -> str:
    """A light, one-word read on what a composite level means, shown beside the
    score in the widgets. Casual heat metaphor: chill → heating up → cooked."""
    return {
        "good": "Chill", "caution": "Heating up", "high": "Cooked",
    }.get(status_class, "Idle")


def composite_status(
    score: float,
    p75: float | None,
    p90: float | None,
) -> str:
    """Status class for a 0–100 composite. Uses the user's own p75/p90 once
    calibrated; falls back to absolute bands while calibrating. Returns "" for
    a non-positive (no-activity) score."""
    if score <= 0:
        return ""
    if p75 is None or p90 is None:
        if score < 40:
            return "good"
        if score < 70:
            return "caution"
        return "high"
    if score < p75:
        return "good"
    if score < p90:
        return "caution"
    return "high"
