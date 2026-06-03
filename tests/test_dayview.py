"""Tests for the shared daily-view model (dayview.py) consumed by the HTML
report, the tkinter widget, and the KDE Plasma widget."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from stress_levels.aggregate import DayAggregate, StreamDayActivity
from stress_levels.dayview import (
    AXES,
    build_axis_tile,
    build_dayview,
    dayview_to_dict,
    hour_counts,
    personal_baseline,
)
from stress_levels.metrics import CODL_NORMALISATION_CEILING, DayMetrics, StressProfile


def _profile(**over) -> StressProfile:
    base = dict(
        days={}, work_windows={}, local_tz_name="UTC", baseline_window_days=30,
        personal_optimum=2.0, composite_p50=20.0, composite_p75=30.0,
        composite_p90=50.0,
    )
    base.update(over)
    return StressProfile(**base)


# --- build_dayview ----------------------------------------------------------

def test_build_dayview_active_day():
    m = DayMetrics(
        day=date(2026, 5, 29), codl_avg=2.4, codl_peak=4,
        interruption_rate=3.0, closure_deficit=0.3, composite=35.0,
    )
    dv = build_dayview(m, None, _profile(), timezone.utc)
    assert dv.has_activity is True
    assert dv.composite_label == "35"
    assert dv.composite_status == "caution"  # 30 (p75) <= 35 < 50 (p90)
    assert dv.composite_color.startswith("#")
    assert [a.name for a in dv.axes] == ["CODL", "Interruption Index", "Closure Deficit"]
    assert len(dv.hours) == 24
    assert dv.day_label == "Friday 29 May 2026"


def test_build_dayview_no_activity():
    dv = build_dayview(DayMetrics(day=date(2026, 5, 29)), None, _profile(), timezone.utc)
    assert dv.has_activity is False
    assert dv.composite_label == "—"
    assert dv.composite_status == ""
    assert dv.hours == [0] * 24


# --- axis tiles -------------------------------------------------------------

def test_codl_tile_optimum_segments_and_ticks():
    m = DayMetrics(day=date(2026, 5, 29), codl_avg=2.4, codl_peak=4, composite=10.0)
    t = build_axis_tile(AXES[0], m, _profile(personal_optimum=2.0))
    assert t.name == "CODL"
    assert t.value_label == "2.40"
    assert t.range_max == CODL_NORMALISATION_CEILING
    assert t.optimum == 2.0 and t.optimum_fraction == 0.4
    assert "peak 4 streams" in t.unit_text
    # Inner zone-boundary ticks for CODL are 1.5, 3, 4 (same as the report).
    assert [tick.label for tick in t.boundary_ticks] == ["1.5", "3", "4"]
    # Zone segments tile the full 0..1 range.
    assert t.segments[0].start == 0.0
    assert abs(t.segments[-1].end - 1.0) < 1e-9


def test_non_codl_tiles_have_no_optimum():
    m = DayMetrics(
        day=date(2026, 5, 29), interruption_rate=4.0, closure_deficit=0.5,
        composite=10.0,
    )
    t_int = build_axis_tile(AXES[1], m, _profile())
    t_clo = build_axis_tile(AXES[2], m, _profile())
    assert t_int.optimum is None and t_int.optimum_fraction is None
    assert t_clo.optimum is None
    assert "%" in t_clo.unit_text


def test_off_scale_value_clamps_and_flags():
    m = DayMetrics(day=date(2026, 5, 29), interruption_rate=25.0, composite=10.0)
    t = build_axis_tile(AXES[1], m, _profile())  # interruption range_max = 10
    assert t.off_scale is True
    assert t.fraction == 1.0


def test_closure_tile_no_data_is_not_scored():
    """A day with no closure value (closure_deficit is None — now only a
    no-activity day) renders the Closure tile as 'not scored' (—) with no value
    marker — never as a 0 that would read as perfect closure. The zone scale is
    still drawn."""
    m = DayMetrics(day=date(2026, 5, 29), codl_avg=2.0,
                   closure_deficit=None, composite=20.0)
    t = build_axis_tile(AXES[2], m, _profile())  # AXES[2] = Closure
    assert t.has_data is False
    assert t.value_label == "—"
    assert t.fraction == 0.0 and t.off_scale is False
    assert t.baseline is None and t.optimum is None
    # The reason is stated explicitly.
    assert "not scored" in t.unit_text and "no activity" in t.unit_text
    assert t.segments  # the empty scale is still drawn for context


def test_closure_tile_zero_is_scored_not_blank():
    """A real 0.0 resumption load (every loop closed in one sitting) is a SCORED
    good day, distinct from the None no-data case — it gets a value marker."""
    m = DayMetrics(day=date(2026, 5, 29), codl_avg=2.0,
                   closure_deficit=0.0, composite=20.0)
    t = build_axis_tile(AXES[2], m, _profile())
    assert t.has_data is True
    assert t.value_label == "0.00"
    assert "resume ceiling" in t.unit_text


# --- personal baseline ------------------------------------------------------

def test_personal_baseline_needs_three_samples():
    two = _profile(days={
        date(2026, 5, 1): DayMetrics(day=date(2026, 5, 1), codl_avg=1.0, composite=5.0),
        date(2026, 5, 2): DayMetrics(day=date(2026, 5, 2), codl_avg=2.0, composite=5.0),
    })
    assert personal_baseline(two, "codl_avg") is None
    three = _profile(days={
        date(2026, 5, d): DayMetrics(day=date(2026, 5, d), codl_avg=float(d), composite=5.0)
        for d in (1, 2, 3)
    })
    assert personal_baseline(three, "codl_avg") == 2.0  # median of 1,2,3


# --- hour_counts ------------------------------------------------------------

def test_hour_counts_buckets_by_local_hour():
    day = date(2026, 5, 29)
    s = StreamDayActivity(
        stream_id="s", project="p",
        first_ts=datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
        last_ts=datetime(2026, 5, 29, 11, 0, tzinfo=timezone.utc),
    )
    counts = hour_counts(day, DayAggregate(day=day, streams=(s,)), timezone.utc)
    assert len(counts) == 24
    # Sampled at :30 → 09:30 and 10:30 fall inside [09:00, 11:00]; 11:30 does not.
    assert counts[9] == 1 and counts[10] == 1
    assert counts[8] == 0 and counts[11] == 0


def test_hour_counts_no_streams():
    assert hour_counts(date(2026, 5, 29), None, timezone.utc) == [0] * 24


# --- per-hour bar colours (CODL zone of the count) --------------------------

def test_codl_count_color_tracks_zones():
    from stress_levels.scales import codl_count_color, zone_color
    # 1 concurrent → "good" green; 4 → "caution" amber; 6 → "high" red.
    assert codl_count_color(1) == zone_color("good")
    assert codl_count_color(4) == zone_color("caution")
    assert codl_count_color(6) == zone_color("high")
    # The colours actually differ as concurrency rises (not a flat fill).
    assert len({codl_count_color(n) for n in (1, 2, 4, 6)}) >= 3


def test_dayview_hour_colors_match_counts():
    from stress_levels.scales import codl_count_color
    day = date(2026, 5, 29)
    # Three streams overlapping around 10:00 (count 3) and one alone at 14:00.
    streams = tuple(
        StreamDayActivity(
            stream_id=f"s{i}", project="p",
            first_ts=datetime(2026, 5, 29, 9, 45, tzinfo=timezone.utc),
            last_ts=datetime(2026, 5, 29, 10, 45, tzinfo=timezone.utc),
        ) for i in range(3)
    ) + (
        StreamDayActivity(
            stream_id="solo", project="p",
            first_ts=datetime(2026, 5, 29, 13, 45, tzinfo=timezone.utc),
            last_ts=datetime(2026, 5, 29, 14, 45, tzinfo=timezone.utc),
        ),
    )
    m = DayMetrics(day=day, codl_avg=2.0, codl_peak=3, composite=20.0)
    dv = build_dayview(m, DayAggregate(day=day, streams=streams), _profile(), timezone.utc)
    assert len(dv.hour_colors) == 24
    assert dv.hour_colors == [codl_count_color(c) for c in dv.hours]
    # The busy hour (3 concurrent) and the solo hour (1) are coloured differently.
    assert dv.hours[10] == 3 and dv.hours[14] == 1
    assert dv.hour_colors[10] != dv.hour_colors[14]
    # Serialised for the widgets.
    d = dayview_to_dict(dv)
    assert d["hour_colors"] == dv.hour_colors


# --- serialization ----------------------------------------------------------

def test_dayview_to_dict_json_round_trip():
    m = DayMetrics(
        day=date(2026, 5, 29), codl_avg=2.4, codl_peak=4,
        interruption_rate=3.0, closure_deficit=0.3, composite=35.0,
    )
    d = dayview_to_dict(build_dayview(m, None, _profile(), timezone.utc))
    assert json.loads(json.dumps(d)) == d
    assert d["schema"] == "ai-code-cognitive-stress.dayview.v1"
    assert d["axes"][0]["color"].startswith("#")
    assert d["axes"][0]["boundary_ticks"][0]["label"] == "1.5"
    assert len(d["hours"]) == 24
