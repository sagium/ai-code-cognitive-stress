"""Generate a synthetic full-year stress profile for the README screenshots.

This bypasses the ingest pipeline entirely — no real session data is touched —
and writes a deterministic demo report at /tmp/demo-report.html that the
screenshot-capture step uses to refresh docs/screenshots/.

Seeded random so the same shape comes back every run. Tweak the constants
near the top of the file if you want a different visual.
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# Ensure the package on PYTHONPATH when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stress_levels.aggregate import DayAggregate, StreamDayActivity  # noqa: E402
from stress_levels.sources.base import ClosureEvent  # noqa: E402
from stress_levels.markdown_min import to_html as md_to_html  # noqa: E402
from stress_levels.metrics import (  # noqa: E402
    DayMetrics,
    StressProfile,
    WorkWindow,
    _composite_score,
    _percentile,
    derive_personal_optimum,
)
from stress_levels.render import report  # noqa: E402

YEAR = 2026
SEED = 7  # deterministic shape

# Tweakables for the synthetic shape — kept honest within the metric scales.
ACTIVE_WORKDAY_PROB = 0.82           # most workdays the demo user is active
WEEKEND_OFFHOURS_PROB = 0.06         # rare weekend incursions
PEAK_WEEKS_PER_YEAR = 4              # spikes scattered across the year

# Realistic distributions for a moderate-load knowledge worker who uses
# Claude every day but not always intensely.
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
        # Weekend off-hours: a source is "wired" but no commits land (the demo
        # treats weekend tinkering as unclosed) — empty tuple, not None.
        return DayAggregate(day=d, streams=(stream,), peak_concurrent_streams=1,
                            closure_events=())

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
            branches=("main",),
        ))
    # Synthesise commits consistent with the day's closure_deficit so the
    # aggregate matches the new opened-vs-closed definition: of the loops
    # opened today, close round((1 - deficit) * loops) of them.
    loops_opened = len(streams)
    closures: list[ClosureEvent] = []
    if loops_opened:
        n_closed = int(round((1.0 - m.closure_deficit) * loops_opened))
        n_closed = max(0, min(n_closed, loops_opened))
        for j in range(n_closed):
            # Land each commit late in its stream's life, inside the window.
            s = streams[j]
            commit_ts = min(
                s.last_ts,
                datetime(d.year, d.month, d.day, 18, 0, tzinfo=timezone.utc),
            )
            closures.append(ClosureEvent(
                ts=commit_ts, kind="commit",
                repo="demo-project", branch="main",
                title=f"close loop {j}",
            ))
    return DayAggregate(
        day=d, streams=tuple(streams),
        peak_concurrent_streams=m.codl_peak,
        closure_events=tuple(closures),
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


def main() -> int:
    profile, aggregates = build_profile(YEAR)

    # Sample Top focus content — what an analyst panel might write after
    # looking at the synthetic data above. Two punchy actions, ranked.
    focus_md = (
        "- **Close one session before opening the next on heavy workdays.** "
        "Your closure deficit clears 0.55 on the four spike days of the year — "
        "you spent more than half those work hours juggling overlapping Claude "
        "sessions. CODL peaks stayed within working-memory capacity "
        "(Cowan 2001), so the load is from leaving sessions open, not from "
        "running too many in parallel.\n\n"
        "- **Cut weekend Claude work.** "
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
