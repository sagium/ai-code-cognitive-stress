"""Tests for the shared daily-view model (dayview.py) consumed by the HTML
report and the KDE Plasma widget."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

from ai_code_cognitive_stress.pipeline.aggregate import DayAggregate, StreamDayActivity
from ai_code_cognitive_stress.output.dayview import (
    AXES,
    DayView,
    _local_tz,
    build_axis_tile,
    build_dayview,
    compute_today_dayview,
    dayview_to_dict,
    hour_counts,
    personal_baseline,
    score_progression,
)
from ai_code_cognitive_stress.pipeline.metrics import CODL_NORMALISATION_CEILING, DayMetrics, StressProfile


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


def test_build_dayview_shows_live_session_before_composite_rises():
    day = date(2026, 5, 29)
    ts = datetime(2026, 5, 29, 14, 22, tzinfo=timezone.utc)
    agg = DayAggregate(
        day=day,
        streams=(
            StreamDayActivity(
                stream_id="live", project="codex", first_ts=ts, last_ts=ts,
            ),
        ),
    )

    dv = build_dayview(DayMetrics(day=day), agg, _profile(), timezone.utc)

    assert dv.has_activity is True
    assert dv.composite_label == "0"
    assert dv.peak_concurrent == 1


def test_build_dayview_preserves_sub_one_live_score():
    metrics = DayMetrics(day=date(2026, 5, 29), composite=0.1)

    dv = build_dayview(metrics, None, _profile(), timezone.utc)

    assert dv.has_activity is True
    assert dv.composite_label == "1"


def _today_metrics(now):
    return DayMetrics(
        day=now.date(), codl_avg=2.0, codl_peak=3, interruption_rate=1.0,
        closure_deficit=0.2, composite=20.0,
        work_window_local=(time(9), time(17)),
    )


def test_axes_not_frozen_before_window_start():
    # 08:00, before the 09:00 window start — the day is still ahead, so the axes
    # stay LIVE (not frozen). Graying them out early would read as "final".
    now = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
    dv = build_dayview(_today_metrics(now), None, _profile(), timezone.utc, now=now)
    assert dv.axes_frozen is False


def test_axes_not_frozen_inside_window():
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    dv = build_dayview(_today_metrics(now), None, _profile(), timezone.utc, now=now)
    assert dv.axes_frozen is False


def test_axes_frozen_after_window_end():
    # 18:00, past the 17:00 window end — the day's scores are final.
    now = datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc)
    dv = build_dayview(_today_metrics(now), None, _profile(), timezone.utc, now=now)
    assert dv.axes_frozen is True


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
    # The stream spans hours 09 and 10; it ends exactly at 11:00, so it has no
    # positive presence in hour 11 (and none in hour 08).
    assert counts[9] == 1 and counts[10] == 1
    assert counts[8] == 0 and counts[11] == 0


def test_hour_counts_no_streams():
    assert hour_counts(date(2026, 5, 29), None, timezone.utc) == [0] * 24


def test_hour_counts_short_sessions_between_samples_still_appear():
    # Two brief sessions that a single mid-hour (:30) snapshot would miss: one
    # 09:40–09:42, one 11:13–11:21. Both must register in their hour.
    day = date(2026, 5, 29)
    streams = (
        StreamDayActivity(
            stream_id="a", project="p",
            first_ts=datetime(2026, 5, 29, 9, 40, tzinfo=timezone.utc),
            last_ts=datetime(2026, 5, 29, 9, 42, tzinfo=timezone.utc),
        ),
        StreamDayActivity(
            stream_id="b", project="p",
            first_ts=datetime(2026, 5, 29, 11, 13, tzinfo=timezone.utc),
            last_ts=datetime(2026, 5, 29, 11, 21, tzinfo=timezone.utc),
        ),
    )
    counts = hour_counts(day, DayAggregate(day=day, streams=streams), timezone.utc)
    assert counts[9] == 1
    assert counts[11] == 1


def test_hour_counts_sequential_sessions_in_one_hour_are_not_concurrent():
    # Two non-overlapping sessions inside the same hour read as 1 (peak
    # concurrency), not 2 — the chart counts simultaneity, not session tally.
    day = date(2026, 5, 29)
    streams = (
        StreamDayActivity(
            stream_id="a", project="p",
            first_ts=datetime(2026, 5, 29, 10, 5, tzinfo=timezone.utc),
            last_ts=datetime(2026, 5, 29, 10, 15, tzinfo=timezone.utc),
        ),
        StreamDayActivity(
            stream_id="b", project="p",
            first_ts=datetime(2026, 5, 29, 10, 40, tzinfo=timezone.utc),
            last_ts=datetime(2026, 5, 29, 10, 50, tzinfo=timezone.utc),
        ),
    )
    counts = hour_counts(day, DayAggregate(day=day, streams=streams), timezone.utc)
    assert counts[10] == 1


def test_hour_counts_single_event_session_appears():
    # A session with one event (first_ts == last_ts) is a zero-length span; it
    # must still show up in its hour rather than vanish.
    day = date(2026, 5, 29)
    s = StreamDayActivity(
        stream_id="s", project="p",
        first_ts=datetime(2026, 5, 29, 14, 22, tzinfo=timezone.utc),
        last_ts=datetime(2026, 5, 29, 14, 22, tzinfo=timezone.utc),
    )
    counts = hour_counts(day, DayAggregate(day=day, streams=(s,)), timezone.utc)
    assert counts[14] == 1


def test_hour_counts_long_gap_splits_run_so_dead_hours_are_empty():
    # Active 09:00, app closed, reopened 13:00 (a 3.5h gap), active to 13:15. The
    # gap exceeds idle_close, so the session is split into two runs and the dead
    # hours 10–12 read 0 rather than the session spanning the whole gap as open.
    day = date(2026, 5, 29)
    s = StreamDayActivity(
        stream_id="s", project="p",
        first_ts=datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
        last_ts=datetime(2026, 5, 29, 13, 15, tzinfo=timezone.utc),
        resume_gaps=((datetime(2026, 5, 29, 13, 0, tzinfo=timezone.utc), 210 * 60),),
    )
    counts = hour_counts(
        day, DayAggregate(day=day, streams=(s,)), timezone.utc,
        idle_close_minutes=180,
    )
    assert counts[9] == 1
    assert (counts[10], counts[11], counts[12]) == (0, 0, 0)
    assert counts[13] == 1


# --- per-hour bar colours (CODL zone of the count) --------------------------

def test_codl_count_color_tracks_zones():
    from ai_code_cognitive_stress.output.scales import codl_count_color, zone_color
    # 1 concurrent → "good" green; 4 → "caution" amber; 6 → "high" red.
    assert codl_count_color(1) == zone_color("good")
    assert codl_count_color(4) == zone_color("caution")
    assert codl_count_color(6) == zone_color("high")
    # The colours actually differ as concurrency rises (not a flat fill).
    assert len({codl_count_color(n) for n in (1, 2, 4, 6)}) >= 3


def test_dayview_hour_colors_match_counts():
    from ai_code_cognitive_stress.output.scales import codl_count_color
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


# --- score progression (sparkline) ------------------------------------------

def test_score_progression_does_not_leak_future_activity():
    """Each hour-end point scores only the data that existed at that instant.
    Regression guard: the afternoon's activity used to count as 'off-hours past
    the window end' at the morning points, painting the early sparkline red on
    an ordinary day."""
    day = date(2026, 6, 3)
    # One stream active 15:00–17:00, a user message every 5 minutes.
    msgs = tuple(
        datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * i)
        for i in range(25)
    )
    s = StreamDayActivity(
        stream_id="s", project="p",
        first_ts=msgs[0], last_ts=msgs[-1],
        user_msg_count=len(msgs), user_msg_timestamps=msgs,
    )
    m = DayMetrics(day=day, composite=10.0, work_window_local=(time(9), time(19)))
    points = score_progression(
        m, DayAggregate(day=day, streams=(s,)), _profile(), timezone.utc,
    )
    assert len(points) == 10  # hour-ends 10:00 … 19:00
    # Before the stream exists (hour-ends 10:00–14:00) the score-so-far is 0 —
    # the afternoon work must not bleed back as off-hours load.
    assert all(p.value == 0 for p in points[:5])
    # Once the stream is live the score-so-far is positive.
    assert points[-1].value > 0


def test_score_progression_final_point_matches_headline_with_early_grace():
    """The sparkline's final point must equal the headline composite even when
    the scored window start has dipped into the early-start grace. Regression
    guard: feeding the extended start back into per_day_metrics slid the
    off-hours cutoff and let a pre-grace message escape the off-hours toll,
    understating the final point relative to the (correct) headline."""
    from ai_code_cognitive_stress.pipeline.metrics import build_profile

    day = date(2026, 6, 3)

    def msg(h, mi):
        return datetime(2026, 6, 3, h, mi, tzinfo=timezone.utc)

    # 05:00 is pre-grace (before the 06:00 cutoff of a 09:00 default window) →
    # off-hours; 07:00 is an early-grace start → in-window credit; the rest is
    # ordinary in-window activity.
    msgs = (msg(5, 0), msg(7, 0), msg(7, 30), msg(10, 0), msg(10, 30), msg(11, 0))
    s = StreamDayActivity(
        stream_id="s", project="p", first_ts=msg(5, 0), last_ts=msg(11, 0),
        user_msg_count=len(msgs), user_msg_timestamps=msgs,
    )
    agg = DayAggregate(day=day, streams=(s,))
    profile = build_profile({day: agg}, baseline_days=30, local_tz=timezone.utc)
    m = profile.days[day]

    # The off-hours path must actually be exercised (else the test is vacuous).
    assert m.off_hours_minutes >= 6
    assert m.work_window_local[0] == time(7, 0)  # start dipped into the grace

    points = score_progression(m, agg, profile, timezone.utc)
    assert points
    assert points[-1].value == m.composite


# --- off-hours nag ----------------------------------------------------------

def test_off_hours_nag_states_when_the_minutes_happened():
    # The nag must carry the local time-of-day ranges so an earlier-in-the-day
    # accumulation can't be misread as "you're off-hours right now".
    m = DayMetrics(
        day=date(2026, 5, 29), codl_avg=2.4, composite=35.0,
        off_hours_minutes=39,
        off_hours_ranges_local=((time(11, 12), time(11, 50)),),
        work_window_local=(time(12, 0), time(20, 0)),
    )
    dv = build_dayview(m, None, _profile(), timezone.utc)
    assert "(11:12–11:50)" in dv.off_hours_nag
    assert "work window: 12:00–20:00" in dv.off_hours_nag


def test_off_hours_nag_caps_ranges_shown():
    ranges = tuple(
        (time(h, 0), time(h, 5)) for h in (1, 2, 3, 4, 5)
    )
    m = DayMetrics(
        day=date(2026, 5, 29), codl_avg=2.4, composite=35.0,
        off_hours_minutes=30, off_hours_ranges_local=ranges,
        work_window_local=(time(9, 0), time(18, 0)),
    )
    dv = build_dayview(m, None, _profile(), timezone.utc)
    assert "01:00–01:05, 02:00–02:05, 03:00–03:05 +2 more" in dv.off_hours_nag


def test_off_hours_nag_below_threshold_is_empty():
    m = DayMetrics(
        day=date(2026, 5, 29), codl_avg=2.4, composite=35.0,
        off_hours_minutes=10,
        off_hours_ranges_local=((time(7, 0), time(7, 9)),),
    )
    assert build_dayview(m, None, _profile(), timezone.utc).off_hours_nag == ""


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


# --- compute_today_dayview (live data layer for --emit-json) -----------------

def test_compute_today_dayview_empty_projects_is_no_activity(tmp_path):
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    expected_day = now.astimezone(_local_tz()).date()

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
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    tz = _local_tz()
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
