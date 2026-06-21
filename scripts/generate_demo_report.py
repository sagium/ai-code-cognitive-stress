"""Generate a synthetic full-year stress profile for the README screenshots.

This bypasses the ingest pipeline entirely — no real session data is touched —
and writes a deterministic demo report at /tmp/demo-report.html that the
screenshot-capture step uses to refresh docs/screenshots/.

With `--dayview-json PATH` it instead writes one representative day as
dayview.v1 JSON — the same payload `aicogstress --emit-json` produces — for
refreshing the desktop-widget screenshots without exposing real activity.

Seeded random so the same shape comes back every run. Tweak the constants
near the top of the file if you want a different visual.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# Ensure the package on PYTHONPATH when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_code_cognitive_stress.pipeline.aggregate import DayAggregate, StreamDayActivity  # noqa: E402
from ai_code_cognitive_stress.core.markdown_min import to_html as md_to_html  # noqa: E402
from ai_code_cognitive_stress.pipeline.metrics import (  # noqa: E402
    DayMetrics,
    RESUME_FULL_DECAY_MINUTES,
    RESUMPTION_DAILY_CEILING,
    StressProfile,
    WorkWindow,
    _composite_score,
    _percentile,
    derive_personal_optimum,
)
from ai_code_cognitive_stress.output.render import report  # noqa: E402

YEAR = 2026
SEED = 7  # deterministic shape

# Tweakables for the synthetic shape — kept honest within the metric scales.
ACTIVE_WORKDAY_PROB = 0.82           # most workdays the demo user is active
WEEKEND_OFFHOURS_PROB = 0.06         # rare weekend incursions
PEAK_WEEKS_PER_YEAR = 4              # spikes scattered across the year

# Realistic distributions for a moderate-load knowledge worker who uses
# coding agents every day but not always intensely.
CODL_AVG_MEAN = 1.4
CODL_AVG_SIGMA = 0.35
INTERRUPTION_RATE_SHAPE = 2.0        # gamma shape — k
INTERRUPTION_RATE_SCALE = 1.6        # gamma scale — theta; mean ~3.2
CLOSURE_DEFICIT_ALPHA = 1.7          # beta α — skews toward 0
CLOSURE_DEFICIT_BETA = 4.0           # beta β
OFFHOURS_PROB = 0.18                 # weekday extension past the window
OFFHOURS_RANGE = (15, 95)            # minutes
WEEKEND_OFFHOURS_RANGE = (60, 240)


def _peak_dates(year: int) -> set[date]:
    """Pick a few scattered Mondays/Thursdays to host visible spikes — gives
    the heatmap and sparkline an interesting shape without looking artificial."""
    rng = random.Random(SEED + 999)
    candidates = []
    d = date(year, 1, 1)
    while d <= date(year, 12, 31):
        if d.weekday() in (0, 3):  # Mon or Thu
            candidates.append(d)
        d += timedelta(days=1)
    return set(rng.sample(candidates, k=PEAK_WEEKS_PER_YEAR))


def _gen_day_metrics(
    d: date, peak_dates: set[date], rng: random.Random,
) -> DayMetrics | None:
    """Synthesise one day's metrics. Returns None for days with no activity."""
    weekday = d.weekday()

    # Weekends: small chance of off-hours-only activity.
    if weekday >= 5:
        if rng.random() < WEEKEND_OFFHOURS_PROB:
            mins = rng.randint(*WEEKEND_OFFHOURS_RANGE)
            return DayMetrics(day=d, off_hours_minutes=mins, work_window_local=None)
        return None

    # Workdays: skip the inactive ones (PTO, holidays, sick days).
    if rng.random() > ACTIVE_WORKDAY_PROB:
        return None

    is_peak = d in peak_dates

    # Composite-driving factors.
    codl_avg = max(0.2, rng.lognormvariate(0, CODL_AVG_SIGMA) * CODL_AVG_MEAN)
    codl_avg = min(codl_avg, 3.6)
    codl_peak = max(1, int(round(codl_avg + rng.uniform(0.4, 1.6))))

    interruption_rate = rng.gammavariate(
        INTERRUPTION_RATE_SHAPE, INTERRUPTION_RATE_SCALE,
    )
    interruption_rate = min(interruption_rate, 9.0)

    closure_deficit = rng.betavariate(
        CLOSURE_DEFICIT_ALPHA, CLOSURE_DEFICIT_BETA,
    )

    # Peak days: push all three axes up.
    if is_peak:
        codl_avg = max(codl_avg, rng.uniform(2.2, 3.0))
        codl_peak = max(codl_peak, rng.randint(4, 5))
        interruption_rate = max(interruption_rate, rng.uniform(5.0, 7.5))
        closure_deficit = max(closure_deficit, rng.uniform(0.55, 0.75))

    off_hours_minutes = 0
    if rng.random() < OFFHOURS_PROB:
        off_hours_minutes = rng.randint(*OFFHOURS_RANGE)

    composite = _composite_score(codl_avg, interruption_rate, closure_deficit)

    return DayMetrics(
        day=d,
        codl_avg=round(codl_avg, 3),
        codl_peak=codl_peak,
        interruption_rate=round(interruption_rate, 3),
        closure_deficit=round(closure_deficit, 3),
        off_hours_minutes=off_hours_minutes,
        composite=round(composite, 1),
        work_window_local=(time(9, 0), time(18, 0)),
    )


def _build_aggregate(d: date, m: DayMetrics, rng: random.Random) -> DayAggregate:
    """Synthesise a DayAggregate consistent with a DayMetrics so the modal's
    per-hour chart has sensible bars."""
    if m.work_window_local is None:
        # Weekend off-hours: one stream in the afternoon/evening.
        start_h = rng.randint(11, 15)
        start_m = rng.randint(0, 50)
        start = datetime(d.year, d.month, d.day, start_h, start_m, tzinfo=timezone.utc)
        end = start + timedelta(minutes=m.off_hours_minutes)
        n_msgs = max(3, m.off_hours_minutes // 25)
        msg_ts = tuple(
            start + timedelta(minutes=i * (m.off_hours_minutes / n_msgs))
            for i in range(n_msgs)
        )
        stream = StreamDayActivity(
            stream_id=f"s-{d}", project="-home-demo-project",
            first_ts=start, last_ts=end,
            user_msg_count=n_msgs, assistant_msg_count=n_msgs * 2,
            tool_use_count=max(1, n_msgs // 2),
            tool_result_count=max(1, n_msgs // 2),
            user_msg_timestamps=msg_ts,
        )
        # Weekend off-hours work happens outside the window, so it contributes
        # to the off-hours toll, not the resumption axis.
        return DayAggregate(day=d, streams=(stream,), peak_concurrent_streams=1)

    # Active workday: 1–3 streams spread across the work window.
    n_streams = min(m.codl_peak, 3) or 1
    streams: list[StreamDayActivity] = []
    for i in range(n_streams):
        start_h = rng.randint(9, 14)
        start_m = rng.choice([0, 15, 30, 45])
        duration_min = rng.randint(40, 240)
        start = datetime(d.year, d.month, d.day, start_h, start_m, tzinfo=timezone.utc)
        end = start + timedelta(minutes=duration_min)
        # Cap at end of work window for a tidy chart.
        end = min(end, datetime(d.year, d.month, d.day, 18, 30, tzinfo=timezone.utc))
        if end <= start:
            continue
        n_msgs = max(2, duration_min // 12)
        spacing = (end - start).total_seconds() / max(n_msgs, 1)
        msg_ts = tuple(
            start + timedelta(seconds=spacing * j) for j in range(n_msgs)
        )
        streams.append(StreamDayActivity(
            stream_id=f"s-{d}-{i}", project="-home-demo-project",
            first_ts=start, last_ts=end,
            user_msg_count=n_msgs, assistant_msg_count=n_msgs * 2,
            tool_use_count=int(duration_min / 15),
            tool_result_count=int(duration_min / 15),
            tool_error_count=int(rng.random() < 0.3),
            user_msg_timestamps=msg_ts,
        ))
    # Days with an off-hours toll get a matching evening session, so the
    # per-hour chart shows the bars outside the work window that the
    # off-hours nag is talking about.
    if m.off_hours_minutes:
        start = datetime(d.year, d.month, d.day, 20, rng.choice([0, 15, 30]),
                         tzinfo=timezone.utc)
        end = start + timedelta(minutes=m.off_hours_minutes)
        n_msgs = max(2, m.off_hours_minutes // 20)
        spacing = (end - start).total_seconds() / n_msgs
        streams.append(StreamDayActivity(
            stream_id=f"s-{d}-offhours", project="-home-demo-project",
            first_ts=start, last_ts=end,
            user_msg_count=n_msgs, assistant_msg_count=n_msgs * 2,
            tool_use_count=max(1, m.off_hours_minutes // 25),
            tool_result_count=max(1, m.off_hours_minutes // 25),
            user_msg_timestamps=tuple(
                start + timedelta(seconds=spacing * j) for j in range(n_msgs)
            ),
        ))
    # Synthesise in-window idle gaps (resumes) consistent with the day's target
    # closure_deficit, so the day-view recomputation matches the headline. The
    # axis is min(1, Σ severity / ceiling), so the severity budget to hit the
    # target is deficit × ceiling; emit fully-cold (full-decay) resumes plus a
    # fractional remainder, attached to the first stream.
    if streams:
        full = RESUME_FULL_DECAY_MINUTES * 60
        budget = m.closure_deficit * RESUMPTION_DAILY_CEILING
        gaps: list[tuple[datetime, int]] = []
        hour = 10
        while budget > 0.01 and hour < 18:
            sev = min(1.0, budget)
            gap_sec = max(120, int(round(sev * full)))
            gaps.append((datetime(d.year, d.month, d.day, hour, 0,
                                  tzinfo=timezone.utc), gap_sec))
            budget -= sev
            hour += 1
        streams[0] = _with_resume_gaps(streams[0], tuple(gaps))
    return DayAggregate(
        day=d, streams=tuple(streams),
        peak_concurrent_streams=m.codl_peak,
    )


def _with_resume_gaps(s: StreamDayActivity,
                      gaps: tuple[tuple[datetime, int], ...]) -> StreamDayActivity:
    """Return a copy of a (frozen, slotted) StreamDayActivity with resume_gaps set."""
    return StreamDayActivity(
        stream_id=s.stream_id, project=s.project,
        first_ts=s.first_ts, last_ts=s.last_ts,
        user_msg_count=s.user_msg_count, assistant_msg_count=s.assistant_msg_count,
        tool_use_count=s.tool_use_count, tool_result_count=s.tool_result_count,
        tool_error_count=s.tool_error_count,
        user_msg_timestamps=s.user_msg_timestamps, resume_gaps=gaps,
    )


def build_profile(year: int) -> tuple[StressProfile, dict[date, DayAggregate]]:
    rng = random.Random(SEED)
    peak_dates = _peak_dates(year)

    days: dict[date, DayMetrics] = {}
    aggregates: dict[date, DayAggregate] = {}

    d = date(year, 1, 1)
    while d <= date(year, 12, 31):
        m = _gen_day_metrics(d, peak_dates, rng)
        if m is not None:
            days[d] = m
            aggregates[d] = _build_aggregate(d, m, rng)
        d += timedelta(days=1)

    composites = sorted(
        m.composite for d, m in days.items()
        if m.composite > 0 and d.weekday() < 5
    )
    p50 = _percentile(composites, 0.5) if composites else None
    p75 = _percentile(composites, 0.75) if composites else None
    p90 = _percentile(composites, 0.9) if composites else None

    work_windows = {
        wd: WorkWindow(weekday=wd, start=time(9, 0), end=time(18, 0),
                       is_default=False)
        for wd in range(5)
    }
    work_windows.update({
        wd: WorkWindow(weekday=wd, start=time(9, 0), end=time(18, 0),
                       is_default=True)
        for wd in range(5, 7)
    })

    profile = StressProfile(
        days=days,
        work_windows=work_windows,
        local_tz_name="UTC",
        baseline_window_days=30,
        composite_p50=p50,
        composite_p75=p75,
        composite_p90=p90,
        personal_optimum=derive_personal_optimum(days),
    )
    return profile, aggregates


# The day shown in the widget screenshots — a fixed mid-March Wednesday.
WIDGET_DEMO_DAY = date(YEAR, 3, 11)


def _demo_widget_aggregate(d: date) -> DayAggregate:
    """Hand-crafted single day for the widget screenshot: four overlapping
    midday streams, scattered tool errors, a couple of cold resumes, and an
    evening off-hours session — so every widget feature is visible (chart,
    varied axis zones, off-hours nag). Its metrics are recomputed through the
    real pipeline (per_day_metrics), so the headline, axes, sparkline, and
    nag agree by construction — the year generator above only makes
    aggregates *statistically* consistent with its synthetic metrics."""
    def ts(h: int, mnt: int) -> datetime:
        return datetime(d.year, d.month, d.day, h, mnt, tzinfo=timezone.utc)

    def stream(i, start, end, every_min, errors=0, gaps=()):
        n = max(2, int((end - start).total_seconds() // (every_min * 60)))
        return StreamDayActivity(
            stream_id=f"s-demo-{i}", project="-home-demo-project",
            first_ts=start, last_ts=end,
            user_msg_count=n, assistant_msg_count=n * 2,
            tool_use_count=n * 2, tool_result_count=n * 2,
            tool_error_count=errors,
            user_msg_timestamps=tuple(
                start + timedelta(minutes=every_min * j) for j in range(n)
            ),
            resume_gaps=gaps,
        )

    streams = (
        stream(1, ts(9, 30), ts(17, 30), 14, errors=10,
               gaps=((ts(13, 30), 65 * 60),)),
        stream(2, ts(10, 0), ts(14, 30), 9, errors=7),
        stream(3, ts(10, 30), ts(15, 30), 11, errors=4,
               gaps=((ts(14, 45), 40 * 60),)),
        stream(4, ts(11, 30), ts(14, 0), 8),
        stream(5, ts(15, 0), ts(17, 15), 10, errors=2,
               gaps=((ts(16, 20), 50 * 60),)),
        stream(6, ts(19, 45), ts(21, 0), 9),  # off-hours evening session
    )
    return DayAggregate(day=d, streams=streams, peak_concurrent_streams=4)


def _demo_dayview():
    from ai_code_cognitive_stress.output.dayview import build_dayview
    from ai_code_cognitive_stress.pipeline.metrics import per_day_metrics

    profile, _ = build_profile(YEAR)
    day = WIDGET_DEMO_DAY
    agg = _demo_widget_aggregate(day)
    m = per_day_metrics(agg, profile.work_windows[day.weekday()], timezone.utc)
    return build_dayview(m, agg, profile, timezone.utc), m


def _report_dayview(path: Path, view, m) -> None:
    print(f"wrote {path}")
    print(f"  day:        {m.day} (composite {m.composite:.0f}, "
          f"peak {m.codl_peak} streams, off-hours {m.off_hours_minutes} min)")
    print(f"  nag shown:  {bool(view.off_hours_nag)}")


def write_dayview_json(path: Path) -> int:
    from ai_code_cognitive_stress.output.dayview import dayview_to_dict

    view, m = _demo_dayview()
    path.write_text(
        json.dumps(dayview_to_dict(view), default=str, indent=2) + "\n",
        encoding="utf-8",
    )
    _report_dayview(path, view, m)
    return 0


def write_dayview_card(path: Path) -> int:
    """The demo day rendered through the real widget renderer
    (`source/widget_card.py`) — the same HTML `aicogstress
    --emit-html-card` would print, for the widget screenshots/preview."""
    from ai_code_cognitive_stress.output.widget_card import render_card

    view, m = _demo_dayview()
    path.write_text(render_card(view) + "\n", encoding="utf-8")
    _report_dayview(path, view, m)
    return 0


# Anchor for the tabbed-card demo: a year-end day so the Year tab's 12 monthly
# bars are all populated. (The single-day demo above stays mid-March, where the
# per-hour chart is most interesting.)
TABBED_DEMO_DAY = date(YEAR, 12, 17)


def write_dayview_card_tabbed(path: Path, *, compact: bool = False) -> int:
    """The demo day rendered as the TABBED widget card (Today / Week / Month /
    Year) — the same HTML `aicogstress --emit-html-card` now prints. Anchored at
    a year-end day so every tab, including Year, is populated. The Today tab uses
    the hand-crafted rich day; Week/Month/Year aggregate the synthetic year.

    With ``compact`` the small (mini) widget is rendered: each tab keeps the
    composite headline and the timeframe's chart but drops the axis tiles — the
    same output `aicogstress --emit-html-card` prints when ``compact_widget`` is
    set in config."""
    from ai_code_cognitive_stress.output.dayview import (
        TimeframeView, build_dayview, build_period_view, build_year_view,
    )
    from ai_code_cognitive_stress.core.i18n import t
    from ai_code_cognitive_stress.pipeline.metrics import per_day_metrics
    from ai_code_cognitive_stress.output.widget_card import render_card_tabbed

    profile, _ = build_profile(YEAR)
    today = TABBED_DEMO_DAY
    agg = _demo_widget_aggregate(today)
    m = per_day_metrics(agg, profile.work_windows[today.weekday()], timezone.utc)
    today_view = build_dayview(m, agg, profile, timezone.utc)
    week_view, week_daily = build_period_view(profile, 7, t("tab.week_title"), today)
    month_view, month_daily = build_period_view(profile, 30, t("tab.month_title"), today)
    year_view, year_monthly = build_year_view(
        profile, profile, t("tab.year_title"), today,
    )
    views = [
        TimeframeView(key="today", tab_label=t("tab.today"), view=today_view),
        TimeframeView(key="week", tab_label=t("tab.week"), view=week_view, daily=week_daily),
        TimeframeView(key="month", tab_label=t("tab.month"), view=month_view, daily=month_daily),
        TimeframeView(key="year", tab_label=t("tab.year"), view=year_view, monthly=year_monthly),
    ]
    path.write_text(render_card_tabbed(views, compact=compact) + "\n", encoding="utf-8")
    print(f"wrote {path}{' (compact / mini)' if compact else ''}")
    print(f"  anchor day: {today} (today composite {m.composite:.0f})")
    print(f"  year active days: "
          f"{sum(1 for mm in profile.days.values() if mm.composite > 0)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dayview-json", metavar="PATH", type=Path,
        help="Write one representative demo day as dayview.v1 JSON (the "
             "--emit-json payload) instead of the HTML report.",
    )
    parser.add_argument(
        "--dayview-card", metavar="PATH", type=Path,
        help="Write one representative demo day as the rendered HTML widget "
             "card (the --emit-html-card payload, for the widget "
             "screenshots/preview) instead of the HTML report.",
    )
    parser.add_argument(
        "--dayview-card-tabbed", metavar="PATH", type=Path,
        help="Write the demo day as the TABBED widget card (Today / Week / "
             "Month / Year) — the current --emit-html-card payload — anchored "
             "at a year-end day so every tab is populated.",
    )
    parser.add_argument(
        "--compact", action="store_true",
        help="With --dayview-card-tabbed, render the small (mini) widget: "
             "composite headline + chart per tab, no axis tiles.",
    )
    args = parser.parse_args()
    if args.dayview_json:
        return write_dayview_json(args.dayview_json)
    if args.dayview_card:
        return write_dayview_card(args.dayview_card)
    if args.dayview_card_tabbed:
        return write_dayview_card_tabbed(args.dayview_card_tabbed, compact=args.compact)

    profile, aggregates = build_profile(YEAR)

    # Sample Top focus content — what an analyst panel might write after
    # looking at the synthetic data above. Two punchy actions, ranked.
    focus_md = (
        "- **Finish a loop before you walk away from it on heavy workdays.** "
        "Your closure deficit clears 0.55 on the four spike days of the year — "
        "those days are full of sessions parked for hours and reloaded cold, "
        "and the longer a loop sits the more it costs to resume (Monk et al. "
        "2008). CODL peaks stayed within working-memory capacity (Cowan 2001), "
        "so the load is from re-entering stale context, not parallelism.\n\n"
        "- **Cut weekend agent work.** "
        "Several Saturdays this year logged 2+ hours of off-hours activity. "
        "Detachment failure on weekends compounds the work-week load rather "
        "than relieving it (Sonnentag 2010); even one heavy Saturday erodes "
        "recovery for the days around it.\n"
    )
    analysis_html = md_to_html(focus_md)

    html = report(
        profile=profile,
        aggregates=aggregates,
        label=f"{YEAR} (demo)",
        ingest_stats=None,
        agent_analysis_html=analysis_html,
    )

    out = Path("/tmp/demo-report.html")
    out.write_text(html, encoding="utf-8")
    active = sum(1 for m in profile.days.values() if m.composite > 0)
    peak = max((m.composite for m in profile.days.values()), default=0)
    print(f"wrote {out}")
    print(f"  active workdays:  {active}")
    print(f"  peak composite:   {peak:.0f}")
    print(f"  p50/p75/p90:      {profile.composite_p50:.1f} / "
          f"{profile.composite_p75:.1f} / {profile.composite_p90:.1f}")
    print(f"  personal optimum: {profile.personal_optimum:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
