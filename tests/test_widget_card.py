"""Tests for the widget card renderer (widget_card.py) — the single HTML
renderer behind both desktop widgets (`aicogstress --emit-html-card`)."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from stress_levels.aggregate import DayAggregate, StreamDayActivity
from stress_levels.dayview import build_dayview
from stress_levels.metrics import DayMetrics, StressProfile
from stress_levels.widget_card import CARD_WIDTH, render_card, render_error_card


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
    assert "Concurrent agent sessions per hour" in html
    for name in ("CODL", "Interruption Index", "Closure Deficit"):
        assert name in html
    assert dv.day_label in html
    # "you" markers on every scored axis.
    assert len(re.findall(r"you \d+\.\d\d", html)) == 3
    # Month stamp in the footer.
    assert ">2026-05<" in html


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
