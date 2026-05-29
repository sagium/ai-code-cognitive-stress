"""Tests for the widget's pure data layer (no tkinter / no display).

The tkinter rendering in run_widget is exercised manually; the headless tests
cover compute_today_dayview and that the module imports without tk/a display.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from stress_levels.dayview import DayView
from stress_levels.widget import compute_today_dayview


def test_compute_today_dayview_empty_projects_is_no_activity(tmp_path):
    import stress_levels.widget as widget_mod

    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    expected_day = now.astimezone(widget_mod._local_tz()).date()

    projects = tmp_path / "projects"
    projects.mkdir()
    dv = compute_today_dayview(
        baseline_days=30, projects_dir=projects,
        cache_dir=tmp_path / "cache", now=now,
    )
    assert isinstance(dv, DayView)
    assert dv.day == expected_day
    assert dv.has_activity is False
    assert [a.name for a in dv.axes] == ["CODL", "Interruption Index", "Closure Deficit"]


def test_compute_today_dayview_reads_today_session(tmp_path):
    """A session with activity 'today' yields a day view for the right day."""
    import stress_levels.widget as widget_mod

    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    tz = widget_mod._local_tz()
    today = now.astimezone(tz).date()

    proj = tmp_path / "projects" / "-home-test"
    proj.mkdir(parents=True)
    midday_local = datetime(today.year, today.month, today.day, 12, 0, tzinfo=tz)
    recs = [
        {"type": "user", "sessionId": "s1",
         "timestamp": midday_local.astimezone(timezone.utc).isoformat(),
         "cwd": "/x", "gitBranch": "main",
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "sessionId": "s1",
         "timestamp": midday_local.astimezone(timezone.utc).isoformat(),
         "cwd": "/x", "gitBranch": "main",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}},
    ]
    (proj / "s1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8",
    )
    dv = compute_today_dayview(
        baseline_days=30, projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache", now=now,
    )
    assert dv.day == today
    assert len(dv.axes) == 3
    assert len(dv.hours) == 24


def test_widget_module_imports_without_tk():
    # Importing the module (done at top) must not require tkinter or a display.
    import stress_levels.widget as widget_mod
    assert hasattr(widget_mod, "run_widget")
    assert hasattr(widget_mod, "compute_today_dayview")


# ---------------------------------------------------------------------------
# _blend — pure colour helper (no tk)

def test_blend_full_alpha_returns_foreground():
    from stress_levels.widget import _blend
    assert _blend("#ff0000", 1.0) == "#ff0000"


def test_blend_zero_alpha_returns_background():
    from stress_levels.widget import _blend
    assert _blend("#ff0000", 0.0, "#ffffff") == "#ffffff"


def test_blend_midpoint_mixes_channels():
    from stress_levels.widget import _blend
    # 50% red over white → full red, half-lifted green/blue.
    assert _blend("#ff0000", 0.5, "#ffffff") == "#ff8080"
