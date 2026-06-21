"""Tests for the subjective grader in the widget card.

Checks:
- grade_prompt / grade_value fields set correctly by build_dayview
- rendered card contains chips when prompted, confirmation when graded,
  and nothing when outside the window or in a period view
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from ai_code_cognitive_stress.output.dayview import (
    DayView, WorkWindow, build_dayview, dayview_to_dict,
)
from ai_code_cognitive_stress.output.widget_card import (
    _grader, render_card, render_card_tabbed,
)
from ai_code_cognitive_stress.pipeline.aggregate import DayAggregate, StreamDayActivity
from ai_code_cognitive_stress.pipeline.metrics import DayMetrics, StressProfile
from ai_code_cognitive_stress.pipeline.subjective import write_grade


def _profile(**over) -> StressProfile:
    base = dict(
        days={}, work_windows={}, local_tz_name="UTC", baseline_window_days=30,
        personal_optimum=2.0, composite_p50=20.0, composite_p75=30.0,
        composite_p90=50.0,
    )
    base.update(over)
    return StressProfile(**base)


def _agg(d: date) -> DayAggregate:
    def ts(h):
        return datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc)

    return DayAggregate(
        day=d,
        streams=(
            StreamDayActivity(
                stream_id="s1", project="p", first_ts=ts(9), last_ts=ts(16),
                user_msg_count=10, assistant_msg_count=20,
                tool_use_count=10, tool_result_count=10, tool_error_count=0,
                user_msg_timestamps=(ts(9),),
            ),
        ),
        peak_concurrent_streams=1,
    )


def _metrics(d: date, composite: float = 35.0) -> DayMetrics:
    return DayMetrics(
        day=d, codl_avg=1.5, codl_peak=2,
        interruption_rate=2.0, closure_deficit=0.1, composite=composite,
        work_window_local=(time(9, 0), time(17, 0)),
    )


# ---------------------------------------------------------------------------
# grade_prompt gating in build_dayview

def test_grade_prompt_true_in_final_work_hour_ungraded(tmp_path):
    """grade_prompt=True: now=16:30 (final hour of 09:00–17:00), ungraded."""
    d = date(2026, 6, 17)
    now = datetime(2026, 6, 17, 16, 30, tzinfo=timezone.utc)
    dv = build_dayview(
        _metrics(d), _agg(d), _profile(), timezone.utc, now=now,
        archive_dir=tmp_path,
    )
    assert dv.grade_prompt is True
    assert dv.grade_value is None


def test_grade_prompt_false_too_early(tmp_path):
    """grade_prompt=False: now=10:00, still far from the final work hour."""
    d = date(2026, 6, 17)
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    dv = build_dayview(
        _metrics(d), _agg(d), _profile(), timezone.utc, now=now,
        archive_dir=tmp_path,
    )
    assert dv.grade_prompt is False


def test_grade_prompt_true_after_window_until_midnight(tmp_path):
    """grade_prompt stays True from the final work hour through end of day.

    Work window 09:00–17:00; the prompt opens at 16:00 (end_hour − 1) and now
    persists past 17:00 until the calendar-day rollover."""
    d = date(2026, 6, 17)
    for hh, mm in [(16, 30), (17, 30), (21, 0), (23, 59)]:
        now = datetime(2026, 6, 17, hh, mm, tzinfo=timezone.utc)
        dv = build_dayview(
            _metrics(d), _agg(d), _profile(), timezone.utc, now=now,
            archive_dir=tmp_path,
        )
        assert dv.grade_prompt is True, f"expected prompt at {hh:02d}:{mm:02d}"


def test_grade_prompt_false_after_midnight_rollover(tmp_path):
    """A view whose day != now's local date carries no grader, so the prompt
    does not bleed into the small hours of the next day."""
    d = date(2026, 6, 17)
    now = datetime(2026, 6, 18, 0, 30, tzinfo=timezone.utc)  # next calendar day
    dv = build_dayview(
        _metrics(d), _agg(d), _profile(), timezone.utc, now=now,
        archive_dir=tmp_path,
    )
    assert dv.grade_prompt is False


def test_grade_prompt_true_when_already_graded_for_regrade(tmp_path):
    """In-window, the grader stays shown after a grade exists so the user can
    change their pick; grade_value carries the current choice."""
    d = date(2026, 6, 17)
    write_grade(tmp_path, d, 0)
    now = datetime(2026, 6, 17, 16, 30, tzinfo=timezone.utc)
    dv = build_dayview(
        _metrics(d), _agg(d), _profile(), timezone.utc, now=now,
        archive_dir=tmp_path,
    )
    assert dv.grade_prompt is True
    assert dv.grade_value == 0


def test_grade_value_reflects_stored_grade(tmp_path):
    d = date(2026, 6, 17)
    write_grade(tmp_path, d, 2)
    now = datetime(2026, 6, 17, 16, 30, tzinfo=timezone.utc)
    dv = build_dayview(
        _metrics(d), _agg(d), _profile(), timezone.utc, now=now,
        archive_dir=tmp_path,
    )
    assert dv.grade_value == 2


def test_grade_prompt_false_without_activity(tmp_path):
    """No chips when the day has no activity, even in the final hour."""
    d = date(2026, 6, 17)
    now = datetime(2026, 6, 17, 16, 30, tzinfo=timezone.utc)
    m = DayMetrics(
        day=d, composite=0.0,
        work_window_local=(time(9, 0), time(17, 0)),
    )
    dv = build_dayview(m, None, _profile(), timezone.utc, now=now, archive_dir=tmp_path)
    assert dv.grade_prompt is False


def test_grade_prompt_false_without_archive_dir():
    """No grade signal when archive_dir is not passed (non-production path)."""
    d = date(2026, 6, 17)
    now = datetime(2026, 6, 17, 16, 30, tzinfo=timezone.utc)
    dv = build_dayview(_metrics(d), _agg(d), _profile(), timezone.utc, now=now)
    assert dv.grade_prompt is False
    assert dv.grade_value is None


# ---------------------------------------------------------------------------
# _grader() fragment

def _day_view_with_grader(**dv_fields) -> DayView:
    """Build a minimal today DayView with the given grade_* overrides."""
    base = dict(
        day=date(2026, 6, 17),
        day_label="Tuesday 17 June 2026",
        has_activity=True,
        composite=35.0,
        composite_label="35",
        composite_status="caution",
        composite_color="#d99058",
        advice="Heating up",
        work_window=WorkWindow(start="09:00", end="17:00", start_hour=9.0, end_hour=17.0),
        work_window_label="work window: 09:00 – 17:00",
        hours=[0] * 24,
        hour_colors=["rgba(0,0,0,0)"] * 24,
        peak_concurrent=0,
        score_progression=[],
        axes=[],
        off_hours_minutes=0,
        off_hours_nag="",
        axes_frozen=False,
        grade_prompt=False,
        grade_value=None,
    )
    base.update(dv_fields)
    return DayView(**base)


def test_grader_shows_chips_when_prompted():
    dv = _day_view_with_grader(grade_prompt=True)
    html = _grader(dv)
    assert 'data-grade="0"' in html
    assert 'data-grade="1"' in html
    assert 'data-grade="2"' in html
    assert f'data-day="{dv.day.isoformat()}"' in html


def test_grader_marks_selected_chip_when_graded():
    dv = _day_view_with_grader(grade_prompt=True, grade_value=1)
    html = _grader(dv)
    # All three chips stay visible (so the user can change their mind)...
    assert html.count('data-grade=') == 3
    # ...and the recorded grade's chip is marked selected.
    assert 'class="grade-chip sel" data-grade="1"' in html
    # caption acknowledges the logged category
    assert "logged" in html.lower()


def test_grader_empty_when_neither():
    dv = _day_view_with_grader(grade_prompt=False, grade_value=None)
    assert _grader(dv) == ""


def test_grader_caption_names_logged_category_only():
    dv = _day_view_with_grader(grade_prompt=True, grade_value=2)
    html = _grader(dv)
    # The logged category is named (caption + selected chip)...
    assert "cooked" in html.lower()
    # ...and the tool's own assessment is NOT shown.
    assert "we had it as" not in html.lower()


# ---------------------------------------------------------------------------
# render_card: full card emits chips iff grade_prompt, not in period views

def test_render_card_emits_chips_when_prompted():
    dv = _day_view_with_grader(grade_prompt=True)
    html = render_card(dv)
    assert 'data-grade="0"' in html
    assert 'data-grade="1"' in html
    assert 'data-grade="2"' in html


def test_render_card_no_chips_when_not_prompted():
    dv = _day_view_with_grader(grade_prompt=False, grade_value=None)
    html = render_card(dv)
    # Check for the actual chip elements (not just the CSS class name which is
    # always in the stylesheet).
    assert 'data-grade="0"' not in html
    assert 'data-grade="1"' not in html
    assert 'data-grade="2"' not in html


def test_render_card_no_grader_when_out_of_window():
    """Out of the window (grade_prompt False) the grader is absent even if a
    grade exists — the day is locked: no chips and no grader block."""
    dv = _day_view_with_grader(grade_prompt=False, grade_value=0)
    html = render_card(dv)
    assert 'data-grade=' not in html
    assert 'class="grader"' not in html


def test_render_card_tabbed_chips_only_in_today_view():
    """Period views (week/month) must never show the grader."""
    from ai_code_cognitive_stress.output.dayview import DailyPoint, TimeframeView
    today_dv = _day_view_with_grader(grade_prompt=True)
    period_dv = _day_view_with_grader(grade_prompt=False, grade_value=None)
    # Provide a non-empty daily so the week view goes through _period_chart,
    # which means _grader is NOT called for it (the 'not period_chart' gate).
    week_daily = (DailyPoint(day=date(2026, 6, 10), composite=0.0, color="#aaa"),)
    views = [
        TimeframeView(key="today", tab_label="Today", view=today_dv),
        TimeframeView(key="week", tab_label="Week", view=period_dv, daily=week_daily),
        TimeframeView(key="month", tab_label="Month", view=period_dv),
    ]
    html = render_card_tabbed(views)
    # Chips exist (from the today view)
    assert 'data-grade="0"' in html
    # Count: only 3 chip elements (one per grade in today view only).
    assert html.count('data-grade=') == 3


# ---------------------------------------------------------------------------
# dayview_to_dict: new fields are serialised

def test_dayview_to_dict_includes_grade_fields():
    dv = _day_view_with_grader(grade_prompt=True, grade_value=None)
    d = dayview_to_dict(dv)
    assert "grade_prompt" in d
    assert "grade_value" in d
    assert d["grade_prompt"] is True
    assert d["grade_value"] is None


def test_dayview_to_dict_serialises_grade_value():
    dv = _day_view_with_grader(grade_value=2, grade_prompt=False)
    d = dayview_to_dict(dv)
    assert d["grade_value"] == 2
    assert d["grade_prompt"] is False


# ---------------------------------------------------------------------------
# _TAB_SCRIPT: grade handler wired in the tabbed card script

def test_tab_script_contains_grade_chip_handler():
    dv = _day_view_with_grader(grade_prompt=True)
    from ai_code_cognitive_stress.output.dayview import TimeframeView
    from ai_code_cognitive_stress.output.widget_card import render_card_tabbed
    views = [
        TimeframeView(key="today", tab_label="Today", view=dv),
    ]
    html = render_card_tabbed(views)
    # The _TAB_SCRIPT should contain the grade bridge pattern.
    assert "cogstress:rate:" in html
    # The today view's chips should be present.
    assert 'data-grade="0"' in html
    # Instant chip-highlight feedback must live in the shared script (not only
    # a host's injected handler), so the click registers before the --rate
    # round-trip — the cogwired guard means only one handler ever wires.
    assert "classList.toggle('sel'" in html
