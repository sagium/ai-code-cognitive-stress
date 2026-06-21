"""Tests for the widget card renderer (widget_card.py) — the single HTML
renderer behind both desktop widgets (`aicogstress --emit-html-card`)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from ai_code_cognitive_stress.pipeline.aggregate import DayAggregate, StreamDayActivity
from ai_code_cognitive_stress.output.dayview import (
    TimeframeView, build_dayview, build_period_view, build_year_view,
)
from ai_code_cognitive_stress.pipeline.metrics import DayMetrics, StressProfile
from ai_code_cognitive_stress.output.widget_card import (
    CARD_WIDTH, render_card, render_card_tabbed, render_error_card,
)


def _profile(**over) -> StressProfile:
    base = dict(
        days={}, work_windows={}, local_tz_name="UTC", baseline_window_days=30,
        personal_optimum=2.0, composite_p50=20.0, composite_p75=30.0,
        composite_p90=50.0,
    )
    base.update(over)
    return StressProfile(**base)


def _active_dayview():
    d = date(2026, 5, 29)

    def ts(h, mnt=0):
        return datetime(d.year, d.month, d.day, h, mnt, tzinfo=timezone.utc)

    agg = DayAggregate(
        day=d,
        streams=(
            StreamDayActivity(
                stream_id="s1", project="p", first_ts=ts(9), last_ts=ts(16),
                user_msg_count=20, assistant_msg_count=40,
                tool_use_count=40, tool_result_count=40, tool_error_count=3,
                user_msg_timestamps=tuple(ts(9 + i) for i in range(7)),
            ),
            StreamDayActivity(
                stream_id="s2", project="p", first_ts=ts(10), last_ts=ts(14),
                user_msg_count=10, assistant_msg_count=20,
                tool_use_count=20, tool_result_count=20, tool_error_count=0,
                user_msg_timestamps=tuple(ts(10 + i) for i in range(4)),
            ),
        ),
        peak_concurrent_streams=2,
    )
    m = DayMetrics(
        day=d, codl_avg=1.8, codl_peak=2, interruption_rate=2.5,
        closure_deficit=0.2, composite=35.0,
        work_window_local=(
            datetime(2026, 5, 29, 9).time(), datetime(2026, 5, 29, 17).time(),
        ),
    )
    return build_dayview(m, agg, _profile(), timezone.utc)


# --- the card -----------------------------------------------------------------


def test_render_card_is_one_self_contained_fragment():
    html = render_card(_active_dayview())
    assert html.startswith("<style>")
    assert html.count('class="cogstress"') == 1
    # Self-contained: no scripts, no external references.
    assert "<script" not in html
    assert "http://" not in html and "https://" not in html
    assert f"width: {CARD_WIDTH}px" in html


def test_render_card_headline_data_attributes():
    dv = _active_dayview()
    html = render_card(dv)
    assert f'data-composite-label="{dv.composite_label}"' in html
    assert f'data-composite-color="{dv.composite_color}"' in html
    assert 'data-has-activity="true"' in html


def test_render_card_active_day_content():
    dv = _active_dayview()
    html = render_card(dv)
    assert dv.composite_label in html
    assert "Max concurrent agent sessions per hour" in html
    for name in ("CODL", "Interruption Index", "Closure Deficit"):
        assert name in html
    assert dv.day_label in html
    # "you" markers on every scored axis.
    assert len(re.findall(r"you \d+\.\d\d", html)) == 3
    # Month stamp in the footer.
    assert ">2026-05<" in html


def test_render_card_compact_hides_tiles_via_data_attr_keeps_chart():
    dv = _active_dayview()
    html = render_card(dv, compact=True)
    # Composite headline stays — score, advice, day label, footer month stamp.
    assert dv.composite_label in html
    assert dv.advice in html
    assert dv.day_label in html
    assert ">2026-05<" in html
    # The concurrency chart always stays.
    assert 'class="chart"' in html
    assert "Max concurrent agent sessions per hour" in html
    # Compact state lives on data-compact (NOT the class — hosts validate the
    # card with the literal substring class="cogstress").
    assert 'data-compact="true"' in html
    assert 'class="cogstress"' in html
    # Tiles are NOT dropped — they stay in the DOM (hidden by CSS) so the in-card
    # toggle can reveal them with no re-render. The hide rule is present.
    assert 'class="tile"' in html
    assert 'class="axis-tiles' in html
    assert '[data-compact="true"] .axis-tiles { display: none; }' in html
    # Still one self-contained fragment.
    assert html.startswith("<style>")
    assert html.count('class="cogstress"') == 1


def test_render_card_full_has_data_compact_false():
    html = render_card(_active_dayview(), compact=False)
    assert 'data-compact="false"' in html
    assert 'class="tile"' in html


def test_render_card_tabbed_compact_hides_tiles_via_data_attr_each_tab():
    dv = _active_dayview()
    views = [
        TimeframeView(key="today", tab_label="Today", view=dv),
        TimeframeView(key="today2", tab_label="Today2", view=dv),
    ]
    html = render_card_tabbed(views, compact=True)
    assert 'data-compact="true"' in html
    assert 'class="tile"' in html              # present, hidden by CSS
    assert 'class="chart"' in html
    assert dv.composite_label in html


def test_render_card_no_activity_day():
    dv = build_dayview(
        DayMetrics(day=date(2026, 5, 29)), None, _profile(), timezone.utc,
    )
    html = render_card(dv)
    assert 'data-has-activity="false"' in html
    assert "—" in html                       # blank composite
    # Closure has no data on a day with no activity: the scale stays, the
    # marker doesn't — a 0-position "you" would read as a perfect score.
    assert "not measured this day" in html
    assert "Concurrent agent sessions" not in html  # chart hidden
    # CODL / interruption still score 0.00 (same model as the report/JSON).
    assert len(re.findall(r"you \d+\.\d\d", html)) == 2


def test_render_card_nag_banner_only_when_present():
    dv = _active_dayview()
    assert ('class="nag"' in render_card(dv)) == bool(dv.off_hours_nag)
    quiet = build_dayview(
        DayMetrics(day=date(2026, 5, 29)), None, _profile(), timezone.utc,
    )
    assert 'class="nag"' not in render_card(quiet)


def test_render_card_sparkline_needs_two_points():
    quiet = build_dayview(
        DayMetrics(day=date(2026, 5, 29)), None, _profile(), timezone.utc,
    )
    assert '<div class="spark"></div>' in render_card(quiet)
    active = render_card(_active_dayview())
    assert 'class="spark"><svg' in active


# --- the error card -------------------------------------------------------------


def test_render_error_card_escapes_message():
    html = render_error_card('<script>alert("x")</script> & more')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; more" in html
    assert 'class="cogstress"' in html
    assert 'data-has-activity="false"' in html


# --- period views + tabbed card ----------------------------------------------

def _multi_day_profile(today):
    """A profile with four active days inside the last week (composites
    20/40/60/30; one day has no closure data)."""
    specs = [
        (today - timedelta(days=1), 1.0, 2.0, 0.1, 20.0, 2),
        (today - timedelta(days=2), 2.0, 3.0, 0.3, 40.0, 3),
        (today - timedelta(days=4), 3.0, 1.0, 0.5, 60.0, 4),
        (today - timedelta(days=6), 2.0, 2.0, None, 30.0, 2),
    ]
    days = {
        d: DayMetrics(day=d, codl_avg=codl, codl_peak=peak,
                      interruption_rate=intr, closure_deficit=clos, composite=comp)
        for d, codl, intr, clos, comp, peak in specs
    }
    return _profile(days=days)


def test_build_period_view_averages_active_days():
    today = date(2026, 5, 29)
    view, daily = build_period_view(_multi_day_profile(today), 7, "Last 7 days", today)
    assert view.has_activity
    # composite = mean over the 4 active days = (20+40+60+30)/4 = 37.5 -> "38"
    assert abs(view.composite - 37.5) < 1e-6
    assert view.composite_label == "38"
    assert view.day_label == "Last 7 days"
    assert "4 active days" in view.work_window_label
    # one point per calendar day in the window; no per-hour data on a period
    assert len(daily) == 7
    assert view.hours == []
    # axis tiles built from the period mean: codl avg = (1+2+3+2)/4 = 2.0
    codl = next(a for a in view.axes if a.key == "codl")
    assert abs(codl.value - 2.0) < 1e-6


def test_build_period_view_no_activity():
    today = date(2026, 5, 29)
    view, daily = build_period_view(_profile(), 30, "Last 30 days", today)
    assert not view.has_activity
    assert view.composite_label == "—"
    assert len(daily) == 30
    assert all(p.composite == 0.0 for p in daily)


def test_build_year_view_monthly_bars():
    today = date(2026, 6, 15)
    days = {
        date(2026, 6, 1): DayMetrics(
            day=date(2026, 6, 1), codl_avg=1.0, codl_peak=2,
            interruption_rate=2.0, closure_deficit=0.2, composite=20.0),
        date(2026, 6, 10): DayMetrics(
            day=date(2026, 6, 10), codl_avg=3.0, codl_peak=4,
            interruption_rate=4.0, closure_deficit=0.4, composite=40.0),
        date(2026, 5, 20): DayMetrics(
            day=date(2026, 5, 20), codl_avg=2.0, codl_peak=3,
            interruption_rate=3.0, closure_deficit=0.6, composite=60.0),
    }
    prof = _profile(days=days)
    view, monthly = build_year_view(prof, prof, "Last 12 months", today)
    assert len(monthly) == 12
    # last bar = June 2026 = mean(20, 40) = 30; previous = May 2026 = 60
    assert (monthly[-1].year, monthly[-1].month) == (2026, 6)
    assert abs(monthly[-1].composite - 30.0) < 1e-6
    assert (monthly[-2].year, monthly[-2].month) == (2026, 5)
    assert abs(monthly[-2].composite - 60.0) < 1e-6
    # headline = mean over all three active days = (20+40+60)/3 = 40
    assert abs(view.composite - 40.0) < 1e-6
    assert view.has_activity
    # months with no activity render a zero-height bar
    assert any(p.composite == 0.0 for p in monthly)


def test_render_card_tabbed_has_resize_toggle_and_signal():
    """The tabbed card carries the toggle (both glyphs) and the script wires the
    client-side flip + cogstress:compact persist signal."""
    views = [TimeframeView(key="today", tab_label="Today", view=_active_dayview())]
    html = render_card_tabbed(views, compact=False)
    assert 'class="resize-toggle"' in html
    assert 'class="icon-collapse"' in html and 'class="icon-expand"' in html
    assert "cogstress:compact:" in html              # persist signal branch
    assert "setAttribute('data-compact'" in html     # instant client-side flip


def test_render_card_tabbed_structure():
    today = date(2026, 5, 29)
    prof = _multi_day_profile(today)
    week, week_daily = build_period_view(prof, 7, "Last 7 days", today)
    month, month_daily = build_period_view(prof, 30, "Last 30 days", today)
    year, year_monthly = build_year_view(prof, prof, "Last 12 months", today)
    views = [
        TimeframeView(key="today", tab_label="Today", view=_active_dayview()),
        TimeframeView(key="week", tab_label="Week", view=week, daily=week_daily),
        TimeframeView(key="month", tab_label="Month", view=month, daily=month_daily),
        TimeframeView(key="year", tab_label="Year", view=year, monthly=year_monthly),
    ]
    html = render_card_tabbed(views)
    assert html.count('class="cogstress"') == 1
    # four tab buttons, today active; four panels, only today visible
    assert html.count('<button class="tab') == 4
    assert 'class="tab active" data-view="today"' in html
    assert html.count('class="view hidden"') == 3
    # the tab switcher + height bridge live only in the tabbed card
    assert "<script" in html
    assert "cogstress:h:" in html
    assert "<script" not in render_card(_active_dayview())
    # period/year body shows the composite-by-day chart; headline mirrors today
    assert "Composite stress by day" in html
    assert f'data-composite-label="{views[0].view.composite_label}"' in html


# --- active_view persistence (tab selection) ---------------------------------


def _four_views(today=None):
    """Build a standard four-tab view list for testing active_view."""
    today = today or date(2026, 5, 29)
    prof = _multi_day_profile(today)
    week, week_daily = build_period_view(prof, 7, "Last 7 days", today)
    month, month_daily = build_period_view(prof, 30, "Last 30 days", today)
    year, year_monthly = build_year_view(prof, prof, "Last 12 months", today)
    return [
        TimeframeView(key="today", tab_label="Today", view=_active_dayview()),
        TimeframeView(key="week", tab_label="Week", view=week, daily=week_daily),
        TimeframeView(key="month", tab_label="Month", view=month, daily=month_daily),
        TimeframeView(key="year", tab_label="Year", view=year, monthly=year_monthly),
    ]


def test_render_card_tabbed_active_view_week():
    """active_view='week' makes the week tab active and week view visible; today hidden."""
    views = _four_views()
    html = render_card_tabbed(views, active_view="week")
    # The week tab must be .active; the today tab must not.
    assert 'class="tab active" data-view="week"' in html
    assert 'class="tab active" data-view="today"' not in html
    # Week view visible (no 'hidden' class); today view hidden.
    assert 'class="view" data-view="week"' in html
    assert 'class="view hidden" data-view="today"' in html
    # Three non-active views are hidden (week is the one visible).
    assert html.count('class="view hidden"') == 3


def test_render_card_tabbed_active_view_month():
    views = _four_views()
    html = render_card_tabbed(views, active_view="month")
    assert 'class="tab active" data-view="month"' in html
    assert 'class="view" data-view="month"' in html
    assert 'class="view hidden" data-view="today"' in html
    assert html.count('class="view hidden"') == 3


def test_render_card_tabbed_active_view_year():
    views = _four_views()
    html = render_card_tabbed(views, active_view="year")
    assert 'class="tab active" data-view="year"' in html
    assert 'class="view" data-view="year"' in html
    assert html.count('class="view hidden"') == 3


def test_render_card_tabbed_active_view_unknown_falls_back_to_first():
    """An active_view key not in the views list falls back to the first tab."""
    views = _four_views()
    html = render_card_tabbed(views, active_view="quarterly")
    assert 'class="tab active" data-view="today"' in html
    assert 'class="view" data-view="today"' in html
    assert html.count('class="view hidden"') == 3


def test_render_card_tabbed_default_active_view_is_today():
    """The default (no active_view) keeps the today tab active — no regression."""
    views = _four_views()
    html = render_card_tabbed(views)
    assert 'class="tab active" data-view="today"' in html
    assert html.count('class="view hidden"') == 3


def test_render_card_tabbed_script_has_cogwired_guard():
    """The inline script includes the idempotency guard so double-wiring is prevented."""
    views = [TimeframeView(key="today", tab_label="Today", view=_active_dayview())]
    html = render_card_tabbed(views)
    # The guard string must appear in the embedded script.
    assert "dataset.cogwired" in html


def test_render_card_tabbed_script_has_view_bridge():
    """The inline script emits 'cogstress:view:' so hosts can persist the chosen tab."""
    views = [TimeframeView(key="today", tab_label="Today", view=_active_dayview())]
    html = render_card_tabbed(views)
    assert "cogstress:view:" in html
