"""Tests for the HTML renderer."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from stress_levels.aggregate import AggregateStats, DayAggregate, StreamDayActivity
from stress_levels.metrics import DayMetrics, StressProfile, WorkWindow
from stress_levels.render import (
    CALIBRATING_LABEL,
    _color_for_composite,
    _heatmap_day_cell,
    _max_consecutive_days_above,
    _status_for_composite,
    _status_for_count,
    report,
)


UTC = timezone.utc


def _utc(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Document shell

def test_report_returns_a_complete_html_document():
    profile = StressProfile()
    html = report(profile, {}, label="2026-05")
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html
    assert "</html>" in html


def test_report_includes_label_in_title():
    html = report(StressProfile(), {}, label="my-window")
    assert "my-window" in html


def test_report_escapes_html_in_label():
    html = report(StressProfile(), {}, label="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_empty_profile_does_not_crash():
    html = report(StressProfile(), {}, label="empty")
    # No year overview, no month overview, but methodology footer is always shown.
    assert "Methodology" in html


# ---------------------------------------------------------------------------
# Status / color helpers

def test_status_for_composite_uses_personal_percentiles_when_available():
    profile = StressProfile(composite_p75=60.0, composite_p90=80.0)
    assert _status_for_composite(50, profile) == "good"
    assert _status_for_composite(70, profile) == "caution"
    assert _status_for_composite(85, profile) == "high"


def test_status_for_composite_falls_back_when_calibrating():
    profile = StressProfile()  # no percentiles
    assert _status_for_composite(30, profile) == "good"
    assert _status_for_composite(60, profile) == "caution"
    assert _status_for_composite(80, profile) == "high"


def test_status_for_composite_zero_returns_no_status():
    assert _status_for_composite(0, StressProfile()) == ""


def test_status_for_count_bands():
    # thresholds=[good_max, caution_max]
    assert _status_for_count(0, [3, 6]) == "good"
    assert _status_for_count(3, [3, 6]) == "good"
    assert _status_for_count(4, [3, 6]) == "caution"
    assert _status_for_count(6, [3, 6]) == "caution"
    assert _status_for_count(7, [3, 6]) == "high"


def test_color_for_composite_zero_is_neutral():
    profile = StressProfile()
    assert _color_for_composite(0, profile) == "#efece5"


def test_color_for_composite_increasing_score_warms_palette():
    profile = StressProfile()
    low = _color_for_composite(15, profile)
    mid = _color_for_composite(55, profile)
    hi = _color_for_composite(90, profile)
    # Specific values matter less than monotonicity in this small palette;
    # just confirm they're distinct.
    assert low != mid != hi


# ---------------------------------------------------------------------------
# Heatmap cells

def test_heatmap_day_cell_no_metrics_renders_zero_class():
    cell = _heatmap_day_cell(date(2026, 5, 15), None, StressProfile())
    assert 'class="cell zero"' in cell
    assert ">15<" in cell


def test_heatmap_day_cell_with_metrics_includes_value_and_background():
    metrics = DayMetrics(day=date(2026, 5, 15), composite=68.0)
    cell = _heatmap_day_cell(date(2026, 5, 15), metrics, StressProfile())
    assert ">15<" in cell
    assert ">68<" in cell
    assert "background:" in cell


def test_heatmap_day_cell_high_score_uses_white_text():
    metrics = DayMetrics(day=date(2026, 5, 15), composite=85.0)
    cell = _heatmap_day_cell(date(2026, 5, 15), metrics, StressProfile())
    assert "#fff" in cell


# ---------------------------------------------------------------------------
# Consecutive-days helper

def test_max_consecutive_days_above_counts_runs():
    # All values on workdays. May 4-8 is Mon-Fri 2026.
    days = {
        date(2026, 5, 4): DayMetrics(day=date(2026, 5, 4), composite=30),  # Mon
        date(2026, 5, 5): DayMetrics(day=date(2026, 5, 5), composite=80),  # Tue
        date(2026, 5, 6): DayMetrics(day=date(2026, 5, 6), composite=80),  # Wed
        date(2026, 5, 7): DayMetrics(day=date(2026, 5, 7), composite=80),  # Thu
        date(2026, 5, 8): DayMetrics(day=date(2026, 5, 8), composite=30),  # Fri
        date(2026, 5, 11): DayMetrics(day=date(2026, 5, 11), composite=85),  # next Mon
    }
    profile = StressProfile(days=days)
    # Tue, Wed, Thu run = 3 workdays above 70.
    assert _max_consecutive_days_above(profile, 70) == 3


def test_max_consecutive_days_above_treats_weekends_as_skipped_not_breaking():
    """A run from Friday into the next Monday counts as 2 consecutive workdays
    above threshold — the weekend is skipped, not a break."""
    days = {
        date(2026, 5, 8): DayMetrics(day=date(2026, 5, 8), composite=80),   # Fri
        # Sat May 9 / Sun May 10 — even with weekend activity, doesn't break
        date(2026, 5, 9): DayMetrics(day=date(2026, 5, 9), composite=0,
                                      off_hours_minutes=120),
        date(2026, 5, 11): DayMetrics(day=date(2026, 5, 11), composite=80),  # Mon
    }
    profile = StressProfile(days=days)
    assert _max_consecutive_days_above(profile, 70) == 2


def test_max_consecutive_days_above_breaks_on_workday_gap():
    # Mon Tue, missing Wed, Thu Fri — run is 2 then 2, not 4
    days = {
        date(2026, 5, 4): DayMetrics(day=date(2026, 5, 4), composite=80),
        date(2026, 5, 5): DayMetrics(day=date(2026, 5, 5), composite=80),
        date(2026, 5, 7): DayMetrics(day=date(2026, 5, 7), composite=80),
        date(2026, 5, 8): DayMetrics(day=date(2026, 5, 8), composite=80),
    }
    profile = StressProfile(days=days)
    # Gap on Wed May 6 (no metrics for that day at all) breaks the run.
    assert _max_consecutive_days_above(profile, 70) == 2


# ---------------------------------------------------------------------------
# Integration: report on a realistic profile

def _next_weekdays(start: date, n: int):
    """Yield `n` consecutive weekdays starting at or after `start`."""
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _profile_with_active_days(active_days: int = 5) -> StressProfile:
    # Weekdays only — weekends are off-hours-only by design.
    weekdays = _next_weekdays(date(2026, 5, 4), active_days)  # Mon onward
    days = {}
    for i, d in enumerate(weekdays):
        days[d] = DayMetrics(
            day=d,
            codl_avg=1.5 + i * 0.3,
            codl_peak=2 + i,
            interruption_rate=2.0,
            closure_deficit=0.2 + i * 0.05,
            off_hours_minutes=30 if i % 2 == 0 else 0,
            composite=40 + i * 8,
            work_window_local=(time(9), time(18)),
        )
    return StressProfile(
        days=days,
        work_windows={i: WorkWindow(weekday=i, start=time(9), end=time(18),
                                     is_default=True) for i in range(7)},
        local_tz_name="UTC",
        composite_p50=50.0,
        composite_p75=65.0,
        composite_p90=70.0,
        personal_optimum=1.5,
    )


def test_report_renders_year_overview_when_profile_has_days():
    profile = _profile_with_active_days()
    html = report(profile, {}, label="May 2026")
    assert "year overview" in html.lower()


def test_report_renders_month_section_with_summary_kpis():
    profile = _profile_with_active_days()
    html = report(profile, {}, label="May 2026")
    assert "month overview" in html.lower()
    assert "Avg composite" in html
    assert "Peak day" in html
    assert "Days &gt; personal p75" in html
    assert "Off-hours days" in html


def test_report_renders_methodology_with_citations():
    profile = _profile_with_active_days()
    html = report(profile, {}, label="May 2026")
    assert "Methodology" in html
    assert "Cowan" in html  # at least one citation should land
    assert "Maslach Burnout Inventory" in html or "Maslach" in html


def test_report_surfaces_calibrating_label_when_no_optimum():
    profile = StressProfile(
        days={date(2026, 5, 1): DayMetrics(
            day=date(2026, 5, 1), composite=40,
            work_window_local=(time(9), time(18)),
        )},
    )
    html = report(profile, {}, label="May 2026")
    assert CALIBRATING_LABEL in html


def test_report_recommendation_fires_when_5_consecutive_high_days():
    # Five consecutive workdays (Mon-Fri 4-8 May 2026) above p75.
    days = {
        d: DayMetrics(day=d, composite=80, work_window_local=(time(9), time(18)))
        for d in _next_weekdays(date(2026, 5, 4), 5)
    }
    profile = StressProfile(
        days=days,
        composite_p75=70.0,
        composite_p90=85.0,
    )
    html = report(profile, {}, label="May 2026")
    assert "Sustained elevated load" in html


def test_report_recommendation_does_not_fire_when_below_threshold():
    days = {
        d: DayMetrics(day=d, composite=40, work_window_local=(time(9), time(18)))
        for d in _next_weekdays(date(2026, 5, 4), 5)
    }
    profile = StressProfile(
        days=days,
        composite_p75=70.0,
    )
    html = report(profile, {}, label="May 2026")
    assert "Sustained elevated load" not in html


def test_report_off_hours_recommendation_fires_when_two_days_have_off_hours():
    days = {
        date(2026, 5, 1): DayMetrics(
            day=date(2026, 5, 1), composite=40, off_hours_minutes=60,
            work_window_local=(time(9), time(18)),
        ),
        date(2026, 5, 2): DayMetrics(
            day=date(2026, 5, 2), composite=40, off_hours_minutes=120,
            work_window_local=(time(9), time(18)),
        ),
    }
    profile = StressProfile(days=days)
    html = report(profile, {}, label="May 2026")
    assert "Off-hours engagement" in html


def test_report_fan_out_recommendation_fires_when_three_days_at_high_peak():
    days = {
        date(2026, 5, d): DayMetrics(
            day=date(2026, 5, d), composite=40, codl_peak=4,
            work_window_local=(time(9), time(18)),
        )
        for d in (1, 2, 3)
    }
    profile = StressProfile(days=days)
    html = report(profile, {}, label="May 2026")
    assert "Parallel-stream fan-out" in html


# ---------------------------------------------------------------------------
# Multi-month / multi-day navigation

def _multi_month_profile():
    """Profile with 2 active days in April + 3 active days in May."""
    days = {}
    for d in (10, 22):  # April
        days[date(2026, 4, d)] = DayMetrics(
            day=date(2026, 4, d), composite=40 + d,
            codl_avg=1.2, codl_peak=2,
            work_window_local=(time(9), time(18)),
        )
    for d in (5, 12, 28):  # May
        days[date(2026, 5, d)] = DayMetrics(
            day=date(2026, 5, d), composite=50 + d,
            codl_avg=1.8, codl_peak=3,
            work_window_local=(time(9), time(18)),
        )
    return StressProfile(
        days=days,
        composite_p50=55.0,
        composite_p75=70.0,
        composite_p90=78.0,
    )


def test_report_emits_one_month_section_per_active_month():
    html = report(_multi_month_profile(), {}, label="2026")
    # Both April and May should have their own section
    assert 'id="month-2026-04"' in html
    assert 'id="month-2026-05"' in html
    # And both headings should be present
    assert "April 2026" in html
    assert "May 2026" in html


def test_report_emits_one_day_section_per_active_day():
    html = report(_multi_month_profile(), {}, label="2026")
    # Every active day in either month should anchor its own section
    for d in (date(2026, 4, 10), date(2026, 4, 22),
              date(2026, 5, 5), date(2026, 5, 12), date(2026, 5, 28)):
        assert f'id="day-{d.isoformat()}"' in html, f"missing day section for {d}"


def test_report_year_cells_link_to_month_anchors():
    html = report(_multi_month_profile(), {}, label="2026")
    # Active months should be anchor-linked from the year overview
    assert 'href="#month-2026-04"' in html
    assert 'href="#month-2026-05"' in html


def test_report_year_cells_for_empty_months_are_not_links():
    html = report(_multi_month_profile(), {}, label="2026")
    # January has no data → no link to month-2026-01 should exist anywhere
    assert 'href="#month-2026-01"' not in html


def test_report_heatmap_active_day_cells_link_to_day_anchors():
    html = report(_multi_month_profile(), {}, label="2026")
    # An active day's heatmap cell should be an anchor to its drill-down
    assert 'href="#day-2026-05-05"' in html
    assert 'href="#day-2026-04-10"' in html


def test_report_heatmap_inactive_day_cells_are_not_links():
    html = report(_multi_month_profile(), {}, label="2026")
    # A day without composite > 0 should not be hrefed
    assert 'href="#day-2026-05-01"' not in html


def test_single_active_month_still_renders_normally():
    """The pre-refactor "snapshot of latest active month" behavior should
    still work — a profile with just one active month renders that month."""
    profile = _profile_with_active_days()
    html = report(profile, {}, label="May 2026")
    assert 'id="month-2026-05"' in html
    # Every active workday in the fixture should have its own drill-down.
    for d, m in profile.days.items():
        if m.composite > 0:
            assert f'id="day-{d.isoformat()}"' in html


def test_day_sections_are_hidden_until_target_via_css():
    """Day-view drill-downs should be hidden by default; the :target CSS
    rule reveals them when their anchor is clicked."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    # The two rules together implement the show-on-click behavior.
    assert "section.day-view { display: none;" in html
    assert "section.day-view:target { display: block;" in html


def test_day_sections_include_close_link_back_to_their_month():
    html = report(_profile_with_active_days(), {}, label="May 2026")
    # Every active day section should contain a close link pointing at its
    # parent month, so clicking it removes the :target without scrolling.
    assert 'href="#month-2026-05"' in html
    assert "close-day" in html


def test_day_sections_use_modal_overlay_with_backdrop():
    """Day drill-downs render as overlays — there's a backdrop layer that
    closes the modal on click, plus a .day-modal content card."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    assert 'class="day-backdrop"' in html
    assert 'class="day-modal"' in html
    # Backdrop click also returns to the month anchor.
    assert html.count('href="#month-2026-05"') >= 2  # close button + backdrop per day section


def test_day_section_is_aria_dialog():
    """The overlay section is annotated for assistive tech."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html


def test_axis_tiles_carry_plain_english_descriptions():
    """Each axis tile should lead with a one-sentence explanation in plain
    English, not the technique. Technique sits behind a <details> toggle."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    assert 'class="tile-meaning"' in html
    # Each metric has a recognisable plain-English phrase
    assert "working-memory limit" in html or "working memory" in html.lower()
    assert "attention-pulling" in html or "Mark (2008)" in html
    assert "juggling" in html or "more than one Claude session" in html


def test_axis_tiles_have_status_banner():
    """A status banner (good / moderate / caution / high) sits in the tile
    header so the reader can scan where they stand at a glance."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    assert 'class="tile-status status-' in html


def test_axis_tiles_render_a_range_bar_per_metric():
    """Each tile carries an SVG range bar showing zones + user marker."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    # Three tiles → three range bars
    assert html.count('class="range-bar"') >= 3
    # The user marker is consistently labelled with the value alongside
    assert "you " in html


def test_peak_day_stat_card_is_clickable_to_its_day_section():
    """Clicking the Peak day KPI card should jump to that day's drill-down."""
    profile = _profile_with_active_days()
    html = report(profile, {}, label="May 2026")
    # Peak day is the last active workday at composite 40 + 4*8 = 72
    peak_day = max(
        (d for d, m in profile.days.items() if m.composite > 0),
        key=lambda d: profile.days[d].composite,
    )
    assert f'href="#day-{peak_day.isoformat()}"' in html
    # And the peak card is rendered as an anchor
    assert '<a class="stat-card' in html


def test_codl_range_bar_emits_zone_boundary_ticks():
    """Zone boundary numbers (1.5, 3, 4 for CODL) appear under the bar."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    # Look inside an SVG-text node for the boundary numbers
    assert ">1.5<" in html
    assert ">3<" in html
    assert ">4<" in html


def test_axis_tile_details_are_collapsed_behind_a_disclosure():
    """Technique / basis / caveat live under a <details> summary so the
    tile remains scannable at first glance."""
    html = report(_profile_with_active_days(), {}, label="May 2026")
    assert "<details" in html
    assert "How this is computed" in html


def test_codl_tile_high_value_triggers_high_status():
    """A high CODL value should land in the 'high' status band."""
    day = date(2026, 5, 15)
    metrics = DayMetrics(
        day=day, codl_avg=4.8, codl_peak=6,
        interruption_rate=1.0, closure_deficit=0.1,
        work_window_local=(time(9), time(18)),
    )
    profile = StressProfile(days={day: metrics})
    html = report(profile, {}, label="May 2026")
    assert "status-high" in html


def test_low_codl_lands_in_good_status():
    day = date(2026, 5, 15)
    metrics = DayMetrics(
        day=day, codl_avg=0.8, codl_peak=1,
        interruption_rate=1.0, closure_deficit=0.05,
        work_window_local=(time(9), time(18)),
    )
    profile = StressProfile(days={day: metrics})
    html = report(profile, {}, label="May 2026")
    assert "status-good" in html


# ---------------------------------------------------------------------------
# One-month-at-a-time visibility

def test_month_sections_hidden_by_default_via_css():
    html = report(_multi_month_profile(), {}, label="2026")
    assert "section.month-view { display: none;" in html
    assert "section.month-view:target { display: block;" in html


def test_default_month_class_applied_only_to_one_month_section():
    html = report(_multi_month_profile(), {}, label="2026")
    # Exactly one month section should carry the month-default class
    assert html.count('class="month-view month-default"') == 1


def test_default_month_class_is_assigned_to_latest_when_today_absent():
    """When the data window doesn't include today's month, the latest active
    month becomes the default visible one."""
    # _multi_month_profile() has April + May 2026. We're 'today=Jan 2030'.
    from stress_levels.render import _render_all_months
    profile = _multi_month_profile()
    html = _render_all_months(profile, {}, now=date(2030, 1, 15))
    # The latest active month is May 2026 (it sorts after April).
    assert 'id="month-2026-05" class="month-view month-default"' in html
    assert 'id="month-2026-04" class="month-view"' in html


def test_default_month_class_uses_today_when_data_includes_it():
    from stress_levels.render import _render_all_months
    profile = _multi_month_profile()
    # Today = April 2026 → April should be the default
    html = _render_all_months(profile, {}, now=date(2026, 4, 1))
    assert 'id="month-2026-04" class="month-view month-default"' in html
    assert 'class="month-view month-default"' not in (
        html.replace('id="month-2026-04" class="month-view month-default"', "")
    )
