"""Reduce per-day aggregates into the StressProfile consumed by the renderer.

Three axes (per day, computed during work hours only):
    CODL              avg / peak count of concurrently-active streams during
                      the day's work window. Sampled at 1-minute resolution
                      via sweep over per-stream (first_ts, last_ts) intervals.
                      Grounded in Cowan (2001) — WM capacity for concurrent
                      decision tracking — and Cummings & Mitchell (2008) on
                      supervisory-control fan-out.
    Interruption Idx  weighted attention-pulling events per work hour.
                      Weights from Mark, Gudith & Klocke (2008) and
                      Mark, Gonzalez & Harris (2005).
    Closure Deficit   share of the day's git-correlatable opened loops left
                      unclosed: clip(1 - closed / correlatable, 0, 1). A loop
                      (stream started in the work window) is closed by the
                      operator's OWN push/commit/merge in its repo within the
                      loop's active span + grace; loops we can't correlate to the
                      operator's git activity (no tracked repo, or a repo they
                      didn't touch that day) are dropped. An unclosed loop keeps
                      consuming the
                      cognitive resource (Masicampo & Baumeister 2011;
                      Leroy 2009); closure is itself a recovery resource
                      (Sonnentag & Fritz 2007) and demands without recoverable
                      resources are the JD-R burnout mechanism (Demerouti et
                      al. 2001). Independent of the concurrency shape C(t) by
                      construction. The axis has meaning ONLY on git repos: a day
                      with no git-correlatable activity (and the no-closure-source
                      case) yields None — omitted as data, not scored 0 — and the
                      composite renormalises over the remaining axes.

Composite stress = equal-weighted blend of the three axes mapped to 0..100.
Equal weights are the null hypothesis for v1; we don't have evidence to favor
one axis over another. The choice and its caveat are surfaced explicitly in
the rendered report's methodology footer.

Personal optimum (Yerkes-Dodson 1908 / Csíkszentmihályi 1990):
    bucket historical days by CODL, score each bucket by closure × inverse
    off-hours-engagement, and report the CODL midpoint of the best bucket
    as the user's flow channel target. Returns None when there's
    insufficient data ("calibrating").

The work window is inferred per-user by default: the p10–p90 band of the
user's own interaction-message hours (local time) across all days is used for
every calculation and chart, so the window adapts to real working patterns.
Inference requires at least WORK_WINDOW_MIN_SAMPLES distinct calendar dates
with user messages; below that threshold the literature default (09:00–19:00
local) is used as a cold-start fallback (is_default=True). A manual override
can be pinned via the config.json `work_window` block; when present it takes
precedence over inference for all 7 weekdays (is_default=False).
The band is interpreted in the user's local timezone — the only place TZ
enters the metrics pipeline.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterable

from .aggregate import DayAggregate, StreamDayActivity
from .config import load_config

# ---------------------------------------------------------------------------
# Citation-anchored constants. Bump tunables here, not at call sites.

# Cowan (2001) — WM cap ≈ 4. We use 5 as the CODL normalisation ceiling so
# values at the working-memory limit land at 0.8 (warning band) rather than
# saturating at 1.0.
CODL_NORMALISATION_CEILING: float = 5.0

# González & Mark (2004) found knowledge workers switch working sphere about
# every ~11 minutes (≈5/hour) in the field; Mark, Gonzalez & Harris (2005)
# document how fragmented that work is. We set the normalisation ceiling at
# 10/hr — roughly double that field baseline, well into "fragmented" territory —
# so a moderate day lands well below 1.0. (Note: the 2008 lab experiment shows
# interrupted work is faster but more stressful; it does not report a field
# interruption rate.)
INTERRUPTION_NORMALISATION_CEILING: float = 10.0

# Interruption-event weights — source-by-source.
#
# tool_use events are deliberately NOT counted as interruptions. When Claude
# invokes a tool inside an active session the supervisor is in a "Waiting"
# state — attention can detach until the result comes back. Counting every
# tool call as an interruption inflates the rate by 1-2 orders of magnitude
# on heavy Claude Code days and violates Mark's definition of interruption
# as "an unscheduled task event that requires immediate attention".
#
# Two events that genuinely do pull attention remain:
#   - tool_error: the tool failed and the user may need to intervene
#   - cross_stream_start: a new session lit up while another was active —
#     forces a context switch between two open conversations (the
#     LLM-supervision-specific signal, no direct prior in Mark/Leroy)
W_TOOL_ERROR: float = 1.5
W_CROSS_STREAM: float = 3.0

# Git rework events (amend / rebase / reset / cherry-pick, from the reflog) are
# history rewrites: a loop you thought was closed got reopened or churned. They
# pull attention the way Leroy's (2009) attention residue from an un-clean
# switch does, so they add to the interruption count rather than the closure
# axis. Weighted between a single tool error and a cross-stream context switch:
# more disruptive than one failed tool call, less than juggling two live
# sessions. A modeling prior, not a fitted value.
W_GIT_REWORK: float = 2.0

# ClosureEvent.kind routing. CLOSURE kinds net the day's opened loops (reduce
# the Closure Deficit); REWORK kinds feed the Interruption axis instead. Kept
# in sync with sources/git_closure.py and the ClosureEvent docstring in
# sources/base.py.
CLOSURE_KINDS: frozenset[str] = frozenset(
    {"push", "commit", "merge", "pr_merge", "mr_merge", "issue_close"}
)
REWORK_KINDS: frozenset[str] = frozenset(
    {"amend", "squash", "rebase", "reset", "revert", "cherry_pick"}
)

# CODL engagement weighting. A session counts at full weight (1.0) only while
# you're actively driving it — within FOREGROUND_GRACE of one of your messages.
# Outside that, it is alive but "cooking" in the background and counts at
# BACKGROUND_WEIGHT. A background/monitored locus is not free: holding a pending
# intention costs ~15-20% of ongoing-task capacity (Smith 2003), and open goals
# keep consuming working memory until closed (Masicampo & Baumeister 2011). We
# use 0.25 — the conservative top of that empirical range, erring toward a
# stronger anti-fire-and-forget nudge. These are code-level fallbacks; the
# runtime source of truth is config.json (codl block), surfaced via load_config.
FOREGROUND_GRACE_MINUTES_DEFAULT: int = 5
BACKGROUND_WEIGHT_DEFAULT: float = 0.25

# Closure Deficit — fraction of the day's git-correlatable *opened* loops left
# *unclosed*. An opened loop is a stream that started inside the work window; it
# is *closed* by one of the OPERATOR'S OWN closure events in the SAME repo whose
# timestamp lands within the loop's active span plus a grace tail. Closure
# events, strongest first: a `push` (work shipped off the machine; inherently
# self-scoped via this clone's reflog), then a `commit`/`merge` authored by the
# operator (a teammate's or merge-bot's commit in a shared repo is NOT the
# operator closing their own loop and is filtered out at the source). A loop we
# can't correlate to the operator's own git activity (no resolvable repo, or a
# repo the operator didn't touch that day) is dropped — we don't penalise what
# git can't speak to, and we don't credit closure to other people. Grounding:
#   - Masicampo & Baumeister (2011): an unfulfilled/open goal keeps consuming
#     working memory until it is closed or planned — so unclosed loops, not
#     mere presence of parallelism, are the load.
#   - Leroy (2009): closure removes attention residue; an unclosed switch
#     leaves residue that taxes the next task.
#   - Sonnentag & Fritz (2007): closure is a recovery resource.
#   - Demerouti et al. (2001), JD-R: burnout = demands without recoverable
#     resources; a day that opens many loops and closes few is exactly that.
# Each commit closes at most one loop (it correlates to one logical session, not
# an unbounded number). This axis uses per-session *correlation*, not the
# concurrency time-series C(t), so it is independent of the CODL shape.
#
# A commit closes a session only when it lands within the session's active span
# plus this grace tail — you typically commit shortly AFTER the agent finishes a
# loop. Kept tight so a commit correlates to that specific session rather than
# to "some session that ran that day".
CLOSURE_CORRELATION_GRACE_MINUTES: int = 30

# Minimum loop duration (last_ts - first_ts) for an UNCLOSED loop to count toward
# the Closure Deficit. A session shorter than this with no related commit/push is
# a trivial check — a quick open, not a real loop you abandoned — so it is dropped
# from the denominator entirely (it neither penalises nor flatters closure). A
# short session that DID get a commit still counts as closed; this filter only
# removes the sub-threshold *unclosed* noise. Applies to the Closure axis only.
CLOSURE_MIN_LOOP_MINUTES: int = 5

# v1 composite weights — equal (null hypothesis). The methodology footer of
# the rendered report names this choice and links to the relevant citations.
COMPOSITE_WEIGHTS: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)

# Literature cold-start default for the work window (09:00–19:00 local).
# Used when config.json carries no manual override AND there are too few
# interaction-timestamp samples to infer the user's personal band.
# This is a conventional day-shift band, not a fitted value; it only applies
# during cold start, before enough data exists to infer the personal window.
# Intentionally wide so it errs toward under-counting off-hours, not over.
LITERATURE_WORK_WINDOW: tuple[time, time] = (time(9, 0), time(19, 0))

# Minimum number of distinct weekday-dates (with at least one user message)
# required to infer the personal work window from interaction timestamps.
# Below this threshold the literature default is used (is_default=True).
WORK_WINDOW_MIN_SAMPLES: int = 5

# Off-hours additive load — an explicit modeling PRIOR (like the
# background_weight β), not a measured value.  Off-hours INTERACTION outside
# the work window (nights, early mornings) raises the daily score because
# disengagement from work is a recovery resource (Sonnentag & Fritz 2007);
# sustained off-hours work drains it.  Applies additively to the composite
# so that a day with zero in-window work but real off-hours interaction still
# scores > 0.  Anchored to USER/AGENT INTERACTION (within grace_seconds of
# a user message), not stream liveness — automated git commits and background
# sessions running while the human is away do NOT contribute.
# 90 min of off-hours engaged minutes is taken as the ceiling (beyond that,
# the signal saturates).  30 composite points is audible without dominating
# the three primary axes on moderate days.
OFF_HOURS_LOAD_CEILING_MIN: int = 90         # engaged off-hours minutes → max toll
OFF_HOURS_LOAD_MAX_POINTS: float = 30.0      # composite points added at/above the ceiling

# Personal optimum derivation — calibration period.
OPTIMUM_MIN_DAYS_OF_DATA: int = 14
OPTIMUM_BUCKET_WIDTH: float = 0.5  # CODL units


# ---------------------------------------------------------------------------
# Data shapes

@dataclass(frozen=True, slots=True)
class WorkWindow:
    """One weekday's configured work-hour band (in local time)."""
    weekday: int          # Mon=0 … Sun=6
    start: time
    end: time
    is_default: bool = False  # True when fallback default was used


@dataclass(frozen=True, slots=True)
class DayMetrics:
    """The three axes plus composite, computed for a single UTC day."""
    day: date
    codl_avg: float = 0.0            # engagement-weighted (foreground 1.0, background w_bg)
    codl_peak: int = 0               # peak headcount of sessions alive at once
    codl_peak_active: float = 0.0    # peak engagement-weighted load (drives fan-out rec)
    interruption_rate: float = 0.0   # per work hour
    closure_deficit: float | None = None  # 0..1 unclosed share of opened loops,
                                     # or None when the day has no git-correlatable
                                     # activity (omitted as data, NOT scored as 0)
    off_hours_minutes: int = 0       # engaged minutes outside the work window
                                     # (interaction-anchored, not stream liveness)
    composite: float = 0.0           # 0..100
    work_window_local: tuple[time, time] | None = None


@dataclass(frozen=True, slots=True)
class StressProfile:
    """Per-day metrics + profile-level derivatives."""
    days: dict[date, DayMetrics] = field(default_factory=dict)
    work_windows: dict[int, WorkWindow] = field(default_factory=dict)
    local_tz_name: str = "UTC"
    baseline_window_days: int = 30
    personal_optimum: float | None = None
    # Personal percentiles over the baseline window (used to color days).
    composite_p50: float | None = None
    composite_p75: float | None = None
    composite_p90: float | None = None


# ---------------------------------------------------------------------------
# Public entry point

def build_profile(
    aggregates: dict[date, DayAggregate],
    baseline_days: int = 30,
    local_tz: tzinfo | None = None,
    repo_map: dict[str, str] | None = None,
) -> StressProfile:
    """Reduce a window of DayAggregates into a StressProfile.

    `local_tz` is the timezone in which to interpret "work hours" and
    "weekday". Defaults to the system local timezone.

    `repo_map` (cwd→repo-root) attributes each stream's opened loops to the
    git repo it ran in, so the Closure Deficit nets closures per-repo. When
    None/empty the deficit nets globally (the prior, repo-agnostic behaviour).
    """
    local_tz = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    work_windows = detect_work_windows(aggregates, local_tz=local_tz)
    cfg = load_config()
    codl_cfg = cfg.codl
    scoring = cfg.scoring

    days: dict[date, DayMetrics] = {}
    for day, agg in sorted(aggregates.items()):
        weekday = day.weekday()
        window = work_windows.get(weekday) or _default_window(weekday)
        days[day] = per_day_metrics(
            agg, window, local_tz,
            foreground_grace_minutes=codl_cfg.foreground_grace_minutes,
            background_weight=codl_cfg.background_weight,
            codl_ceiling=scoring.codl_ceiling,
            interruption_ceiling=scoring.interruption_ceiling,
            weights=scoring.weights,
            repo_map=repo_map,
        )

    # Percentiles are computed across all active days.
    composites = [
        m.composite for d, m in days.items()
        if m.composite > 0
    ]
    p50 = _percentile(composites, 0.5) if composites else None
    p75 = _percentile(composites, 0.75) if composites else None
    p90 = _percentile(composites, 0.9) if composites else None

    optimum = derive_personal_optimum(days)

    return StressProfile(
        days=days,
        work_windows=work_windows,
        local_tz_name=str(local_tz),
        baseline_window_days=baseline_days,
        personal_optimum=optimum,
        composite_p50=p50,
        composite_p75=p75,
        composite_p90=p90,
    )


# ---------------------------------------------------------------------------
# Work-window detection

def detect_work_windows(
    aggregates: dict[date, DayAggregate],
    local_tz: tzinfo,
    min_samples: int = WORK_WINDOW_MIN_SAMPLES,
) -> dict[int, WorkWindow]:
    """Return the effective work-window band for every weekday (0=Mon … 6=Sun).

    Priority order:

    1. OVERRIDE — if config.json supplies a ``work_window`` block, that band
       is returned for all 7 weekdays (``is_default=False``).

    2. INFER — pool the local-time fractional-hour of every user-message
       timestamp from all aggregates (any day of the week).  If at least
       ``min_samples`` *distinct* calendar dates contributed timestamps,
       derive a single stable band applied to all 7 weekdays:
         * start = floor(p10) clamped to [0, 23]
         * end   = ceil(p90)  clamped to [1, 23] (24 → 23)
       If the rounded band is degenerate (end <= start), widen end by 1 h
       (capped at 23).  If still degenerate, fall through to (3).

    3. FALLBACK — fewer than ``min_samples`` distinct sample-dates, or the
       inferred band is still degenerate after widening → literature default
       09:00–19:00 for all 7 weekdays (``is_default=True``).
    """
    # --- 1. Config override ---
    cfg_ww = load_config().work_window
    if cfg_ww is not None:
        return {
            wd: WorkWindow(weekday=wd, start=cfg_ww.start, end=cfg_ww.end,
                           is_default=False)
            for wd in range(7)
        }

    # --- 2. Infer from interaction timestamps ---
    # Collect fractional hours for all user messages on any day of the week.
    hours: list[float] = []
    sample_dates: set[date] = set()
    for day, agg in aggregates.items():
        for stream in agg.streams:
            for ts in stream.user_msg_timestamps:
                local_dt = ts.astimezone(local_tz)
                hours.append(local_dt.hour + local_dt.minute / 60.0)
                sample_dates.add(local_dt.date())

    if len(sample_dates) >= min_samples and hours:
        hours_sorted = sorted(hours)
        p10 = _percentile(hours_sorted, 0.10)
        p90 = _percentile(hours_sorted, 0.90)
        start_h = max(0, min(23, int(math.floor(p10))))
        end_h = int(math.ceil(p90))
        if end_h >= 24:
            end_h = 23
        end_h = max(1, min(23, end_h))
        # Ensure end > start; widen by 1 h if degenerate.
        if end_h <= start_h:
            end_h = min(23, start_h + 1)
        if end_h > start_h:
            inferred_start = time(start_h, 0)
            inferred_end = time(end_h, 0)
            return {
                wd: WorkWindow(weekday=wd, start=inferred_start,
                               end=inferred_end, is_default=False)
                for wd in range(7)
            }

    # --- 3. Literature default ---
    return {wd: _default_window(wd) for wd in range(7)}


def _default_window(weekday: int) -> WorkWindow:
    """Return the literature-default work window (09:00–19:00, is_default=True)."""
    lit_start, lit_end = LITERATURE_WORK_WINDOW
    return WorkWindow(weekday=weekday, start=lit_start, end=lit_end,
                      is_default=True)


# ---------------------------------------------------------------------------
# Per-day reduction

def per_day_metrics(
    agg: DayAggregate,
    work_window: WorkWindow,
    local_tz: tzinfo,
    foreground_grace_minutes: int = FOREGROUND_GRACE_MINUTES_DEFAULT,
    background_weight: float = BACKGROUND_WEIGHT_DEFAULT,
    codl_ceiling: float = CODL_NORMALISATION_CEILING,
    interruption_ceiling: float = INTERRUPTION_NORMALISATION_CEILING,
    weights: tuple[float, float, float] = COMPOSITE_WEIGHTS,
    repo_map: dict[str, str] | None = None,
) -> DayMetrics:
    """Compute the three axes + composite for one day.

    Every day is treated identically regardless of what day of the week it
    falls on. Off-hours interaction (engaged minutes outside the work window)
    is captured as `off_hours_minutes` and adds an additive load to the
    composite, whether it occurs on a weekday, Saturday, or Sunday.
    """
    if not agg.streams:
        return DayMetrics(
            day=agg.day,
            work_window_local=(work_window.start, work_window.end),
        )

    window_start_utc, window_end_utc = _window_utc_bounds(
        agg.day, work_window, local_tz,
    )
    work_seconds = max(1, int((window_end_utc - window_start_utc).total_seconds()))
    work_hours = work_seconds / 3600.0

    # Headcount sweep → peak "sessions open at once" (descriptive).
    headcounts = _codl_samples(agg.streams, window_start_utc, window_end_utc)
    # Engagement-weighted sweep → the scored axis. Foreground (you're actively
    # driving the session) counts 1.0; background ("cooking") counts w_bg.
    weighted = _codl_weighted_samples(
        agg.streams, window_start_utc, window_end_utc,
        grace_seconds=foreground_grace_minutes * 60,
        background_weight=background_weight,
    )
    if weighted:
        codl_avg = sum(weighted) / len(weighted)
        codl_peak = max(headcounts)
        codl_peak_active = max(weighted)
    else:
        codl_avg = 0.0
        codl_peak = 0
        codl_peak_active = 0.0

    # Closure Deficit: share of the operator's git-correlatable opened loops
    # left unclosed, or None when the day has no git-correlatable activity
    # (omitted as data — the axis has meaning only on git repos). Independent of
    # the C(t) shape (it nets loop-open COUNTS against closure COUNTS).
    closure_deficit = _closure_deficit(
        agg, window_start_utc, window_end_utc, repo_map,
    )

    cross_starts = _count_cross_stream_starts(
        agg.streams, window_start_utc, window_end_utc,
    )
    # Apportion per-stream tool errors by the fraction of each stream's
    # lifetime that overlaps the work window. A stream that lived entirely
    # outside the window contributes none of its errors to the work-hour
    # rate; a stream half-in contributes half. The old formula counted
    # every error all day, inflating the work-hour rate when the user had
    # off-hours activity with errors.
    in_window_errors = _apportion_to_window(
        agg.streams, "tool_error_count",
        window_start_utc, window_end_utc,
    )
    # Git rework events (reflog amend/rebase/reset/cherry-pick) in the window
    # are self-interruptions — history rewrites that reopen a closed loop.
    rework_in_window = _count_rework(agg, window_start_utc, window_end_utc)
    interruption_count = (
        in_window_errors * W_TOOL_ERROR
        + cross_starts * W_CROSS_STREAM
        + rework_in_window * W_GIT_REWORK
    )
    interruption_rate = interruption_count / work_hours

    # Off-hours ENGAGED minutes = minutes outside the work window during which
    # the operator was actively driving a session (within the foreground grace
    # of one of their own messages). Anchored to interaction, not stream
    # liveness: a background job that ran while the human was away contributes
    # nothing, and git commits never enter here. The inferred window is the
    # norm; engaged interaction outside it is the off-hours abuse.
    off_hours_minutes = _off_hours_engaged_minutes(
        agg.streams, window_start_utc, window_end_utc,
        grace_seconds=foreground_grace_minutes * 60,
    )

    # Additive off-hours toll: off-hours interaction always counts, even when
    # the in-window base is zero (a day worked entirely outside the window).
    base_composite = _composite_score(
        codl_avg, interruption_rate, closure_deficit,
        codl_ceiling=codl_ceiling, interruption_ceiling=interruption_ceiling,
        weights=weights,
    )
    composite = min(100.0, base_composite + _off_hours_load_points(off_hours_minutes))

    return DayMetrics(
        day=agg.day,
        codl_avg=round(codl_avg, 3),
        codl_peak=codl_peak,
        codl_peak_active=round(codl_peak_active, 3),
        interruption_rate=round(interruption_rate, 3),
        closure_deficit=(
            round(closure_deficit, 3) if closure_deficit is not None else None
        ),
        off_hours_minutes=off_hours_minutes,
        composite=round(composite, 1),
        work_window_local=(work_window.start, work_window.end),
    )


def per_day_debug(
    agg: DayAggregate,
    work_window: WorkWindow,
    local_tz: tzinfo,
    foreground_grace_minutes: int = FOREGROUND_GRACE_MINUTES_DEFAULT,
    background_weight: float = BACKGROUND_WEIGHT_DEFAULT,
    repo_map: dict[str, str] | None = None,
) -> dict:
    """Per-day component breakdown behind the scores, for the research export's
    debug detail. Mirrors the inputs `per_day_metrics` reduces, so the two stay
    in sync. Returns only counts/durations and an hourly activity shape — no
    project/branch names and no absolute timestamps (anonymized at the source).

    Computed on demand by the export only; not part of `build_profile`, so the
    report/widget path pays nothing for it.
    """
    if not agg.streams:
        return {}

    ws, we = _window_utc_bounds(agg.day, work_window, local_tz)
    work_hours = max(1, int((we - ws).total_seconds())) / 3600.0
    grace = foreground_grace_minutes * 60

    headcounts = _codl_samples(agg.streams, ws, we)
    weighted = _codl_weighted_samples(
        agg.streams, ws, we, grace_seconds=grace,
        background_weight=background_weight,
    )
    cross_starts = _count_cross_stream_starts(agg.streams, ws, we)
    in_window_errors = _apportion_to_window(
        agg.streams, "tool_error_count", ws, we,
    )
    rework = _count_rework(agg, ws, we)
    loops_opened = sum(1 for s in agg.streams if ws <= s.first_ts <= we)
    # All CLOSURE-kind commits on the day (descriptive); correlation decides
    # which actually close a loop.
    closures = sum(
        1 for c in (agg.closure_events or ()) if c.kind in CLOSURE_KINDS
    )
    # Per-session git correlation: closed_loops of correlatable_loops. The
    # exported Closure Deficit (1 - closed_loops/correlatable_loops) is
    # reproducible from these debug counts.
    closed_loops, correlatable_loops = _closure_correlation(agg, ws, we, repo_map)

    # Hourly activity shape: average engagement-weighted concurrency per local
    # hour. Samples are one-per-minute from ws, so sample i is ws + i minutes.
    # Sparse — only hours with non-zero load are emitted, keeping it compact.
    hourly: dict[str, list[float]] = {}
    for i, v in enumerate(weighted):
        if v <= 0:
            continue
        hour = (ws + timedelta(minutes=i)).astimezone(local_tz).hour
        hourly.setdefault(str(hour), []).append(v)
    hourly_concurrency = {
        h: round(sum(vs) / len(vs), 3) for h, vs in sorted(hourly.items())
    }

    sessions = [
        {
            "start_hour": s.first_ts.astimezone(local_tz).hour,
            "duration_min": round(s.active_seconds / 60),
            "user_msgs": s.user_msg_count,
            "assistant_msgs": s.assistant_msg_count,
            "tool_uses": s.tool_use_count,
            "tool_results": s.tool_result_count,
            "tool_errors": s.tool_error_count,
        }
        for s in agg.streams
    ]

    return {
        "stream_count": agg.stream_count,
        "peak_headcount": max(headcounts) if headcounts else 0,
        "peak_weighted": round(max(weighted), 3) if weighted else 0.0,
        "work_hours": round(work_hours, 2),
        "cross_stream_starts": cross_starts,
        "in_window_tool_errors": in_window_errors,
        "total_tool_errors": sum(s.tool_error_count for s in agg.streams),
        "interruption_numerator": round(
            in_window_errors * W_TOOL_ERROR
            + cross_starts * W_CROSS_STREAM
            + rework * W_GIT_REWORK, 3,
        ),
        "loops_opened": loops_opened,
        "correlatable_loops": correlatable_loops,
        "closures": closures,
        "closed_loops": closed_loops,
        "reworks": rework,
        "off_hours_minutes": _off_hours_engaged_minutes(
            agg.streams, ws, we, grace_seconds=grace,
        ),
        "hourly_concurrency": hourly_concurrency,
        "sessions": sessions,
    }


def _count_rework(
    agg: DayAggregate,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> int:
    """Number of git rework events (reflog amend/rebase/reset/cherry-pick) in
    the work window. 0 when no closure source is wired."""
    if not agg.closure_events:
        return 0
    return sum(
        1 for c in agg.closure_events
        if c.kind in REWORK_KINDS
        and window_start_utc <= c.ts <= window_end_utc
    )


def _closure_deficit(
    agg: DayAggregate,
    window_start_utc: datetime,
    window_end_utc: datetime,
    repo_map: dict[str, str] | None = None,
    grace_seconds: int = CLOSURE_CORRELATION_GRACE_MINUTES * 60,
) -> float | None:
    """Share of the day's git-correlatable opened loops left unclosed, in [0, 1],
    or ``None`` when the day has no git-correlatable activity at all.

    The Closure Deficit has meaning ONLY on git repositories, so a day git can't
    speak to yields ``None`` — *omitted as data*, never scored as ``0.0``. The
    distinction is load-bearing: ``0.0`` means "you opened loops and closed them
    all" (genuine perfect closure), whereas ``None`` means "there was no git work
    to assess". Collapsing the second into the first would credit a pure
    debugging or generic-chat day with perfect closure and dilute its composite.
    Callers must treat ``None`` as no-data (the composite renormalises over the
    remaining axes; rollups and the personal optimum skip it).

    Only CLOSURE-kind events (push/commit/merge) close loops here; REWORK-kind
    events (amend/rebase/reset/…) are routed to the Interruption axis instead.

    Each opened loop (a stream started in the work window) is correlated to one
    of the operator's own closures in the SAME repo whose timestamp falls within
    the loop's active span plus a grace tail (see ``_closure_correlation``).
    Loops we cannot correlate to the operator's git activity (no resolvable repo,
    or a repo the operator didn't touch that day) are dropped from both numerator
    and denominator; closures that correlate to no loop are ignored.

      * ``correlatable > 0`` → ``clip(1 - closed / correlatable, 0, 1)``.
      * ``correlatable == 0`` (no git loops to speak of) → ``None``.
      * no closure source wired at all (``agg.closure_events is None``) → ``None``;
        the Closure Deficit simply does not exist without git (the former
        ``C(t)>1`` concurrency proxy was removed — it was not a git signal).

    Built from per-session *correlation*, so it carries information the
    concurrency time-series C(t) does not: two days with identical concurrency
    shapes score differently if one closed its loops and the other left them
    open.
    """
    # No git closure source wired → the axis has no basis to exist. Omit as data.
    if agg.closure_events is None:
        return None

    closed, correlatable = _closure_correlation(
        agg, window_start_utc, window_end_utc, repo_map, grace_seconds,
    )
    if correlatable <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - closed / correlatable))


# Single repo bucket used when no repo_map is available (no-cwd sources): we
# can't attribute by repo, so all commits/loops share one bucket and correlate
# by time alone. Never collides with a real repo path.
_GLOBAL_REPO_KEY = "*"


def _closure_correlation(
    agg: DayAggregate,
    window_start_utc: datetime,
    window_end_utc: datetime,
    repo_map: dict[str, str] | None = None,
    grace_seconds: int = CLOSURE_CORRELATION_GRACE_MINUTES * 60,
    min_loop_seconds: int = CLOSURE_MIN_LOOP_MINUTES * 60,
) -> tuple[int, int]:
    """Correlate the day's commits to the loops they closed, per session.

    A *loop* is a stream that started inside the work window. A commit *closes*
    a loop when it is in the loop's repo and its timestamp falls within the
    loop's active span plus a grace tail: ``[first_ts, last_ts + grace]``. Each
    commit closes at most one loop (greedy, earliest-ending loop first), so a
    commit correlates to one logical session rather than masking several.

    Only loops we can correlate to git activity count. A loop whose repo git
    never touched that day (no commit, merge, or rework) — or whose cwd doesn't
    resolve to a tracked repo — is *dropped* (excluded from both closed and
    correlatable): git can't speak to its closure, so we don't penalise it. A
    loop in a repo that WAS git-active that day but caught no time-correlated
    commit stays *unclosed* — the real deficit signal. Commits that correlate to
    no loop are ignored.

    Trivial-session filter: a loop that stays *unclosed* AND lasted less than
    ``min_loop_seconds`` (default 5 min) is a quick check, not a real loop you
    abandoned, so it is dropped from the denominator too — it shouldn't penalise
    closure. A short loop that DID catch a commit still counts as closed (a quick
    commit-and-push is a genuine closure); only sub-threshold *unclosed* noise is
    removed.

    Returns ``(closed, correlatable)``; the Closure Deficit is
    ``1 - closed/correlatable``. ``per_day_debug`` reports both so the exported
    deficit is reproducible from the debug counts.

    With no ``repo_map`` (no-cwd sources) every loop and commit shares one
    global bucket and correlation is by time alone — best-effort back-compat.
    """
    keyed = bool(repo_map)

    # Walk the day's git events once (already day-bucketed upstream). A repo is
    # "git-visible" today if it saw ANY event — commit, merge, OR rework — so a
    # loop in a repo that was rebased/amended but not committed is correlatable
    # (and stays unclosed), while a loop in a repo git never touched is dropped.
    # Only CLOSURE-kind events can actually close a loop.
    active_repos: set[str] = set()
    commits_by_repo: dict[str, list[datetime]] = defaultdict(list)
    for c in (agg.closure_events or ()):
        key = c.repo if keyed else _GLOBAL_REPO_KEY
        active_repos.add(key)
        if c.kind in CLOSURE_KINDS:
            commits_by_repo[key].append(c.ts)

    # Loops opened in the work window, resolved to a repo bucket, with their span.
    loops: list[tuple[str | None, datetime, datetime]] = []
    for s in agg.streams:
        if window_start_utc <= s.first_ts <= window_end_utc:
            key = ((repo_map or {}).get(s.cwd) if s.cwd else None) if keyed \
                else _GLOBAL_REPO_KEY
            loops.append((key, s.first_ts, s.last_ts))

    # Drop loops we can't correlate to git activity: no repo bucket, or a repo
    # git never touched today.
    correlatable = [
        (key, first, last) for (key, first, last) in loops
        if key is not None and key in active_repos
    ]
    if not correlatable:
        return 0, 0

    # Greedy 1:1 matching by repo + time overlap. Earliest-ending loop first, so
    # a commit closes the loop it most tightly follows.
    grace = timedelta(seconds=grace_seconds)
    min_loop = timedelta(seconds=min_loop_seconds)
    used: dict[str, list[bool]] = {
        key: [False] * len(times) for key, times in commits_by_repo.items()
    }
    closed = 0
    correlatable_count = 0
    for key, first, last in sorted(correlatable, key=lambda lp: lp[2]):
        hi = last + grace
        is_closed = False
        for i, cts in enumerate(commits_by_repo.get(key, ())):
            if not used[key][i] and first <= cts <= hi:
                used[key][i] = True
                is_closed = True
                break
        if is_closed:
            closed += 1
            correlatable_count += 1
        elif (last - first) >= min_loop:
            # A genuine unclosed loop — the real deficit signal.
            correlatable_count += 1
        # else: a sub-min-duration session that caught no commit is a trivial
        # check; drop it from the denominator (don't let noise raise the deficit).
    return closed, correlatable_count


def _composite_score(
    codl_avg: float,
    interruption_rate: float,
    closure_deficit: float | None,
    codl_ceiling: float = CODL_NORMALISATION_CEILING,
    interruption_ceiling: float = INTERRUPTION_NORMALISATION_CEILING,
    weights: tuple[float, float, float] = COMPOSITE_WEIGHTS,
) -> float:
    """Weighted blend of the available axes, mapped to 0..100. Each axis is
    clamped to [0, 1] before weighting; weights are normalized by their sum, so
    a calibrated weight vector that doesn't sum to 1 still yields a 0..100 score.

    When ``closure_deficit is None`` the Closure axis has no data for the day
    (no git-correlatable activity), so it is dropped and the blend renormalises
    over the remaining axes — i.e. its weight is redistributed to CODL and
    Interruption rather than imputed as a perfect-closure 0. A heavy
    debugging/chat day is then scored on the load and interruption it actually
    carried, not discounted for closing loops it never opened."""
    codl_norm = min(1.0, codl_avg / codl_ceiling)
    int_norm = min(1.0, interruption_rate / interruption_ceiling)
    w_codl, w_int, w_clo = weights
    terms = [(w_codl, codl_norm), (w_int, int_norm)]
    if closure_deficit is not None:
        terms.append((w_clo, max(0.0, min(1.0, closure_deficit))))
    w_total = sum(w for w, _ in terms)
    if w_total <= 0:
        return 0.0
    blend = sum(w * v for w, v in terms) / w_total
    return 100.0 * blend


def _off_hours_load_points(off_hours_minutes: int) -> float:
    """Additive composite points for off-hours interaction.

    Returns a value in [0.0, OFF_HOURS_LOAD_MAX_POINTS] that scales linearly
    with off-hours engaged minutes up to OFF_HOURS_LOAD_CEILING_MIN, then
    saturates.  Added (not multiplied) to the 3-axis base so off-hours work
    always counts, even on a day with zero in-window load.  Grounding:
    disengagement from work is a recovery resource (Sonnentag & Fritz 2007);
    sustained off-hours work drains it.  An explicit modeling PRIOR (like
    background_weight β), not a measured value.
    """
    return OFF_HOURS_LOAD_MAX_POINTS * min(
        1.0, off_hours_minutes / OFF_HOURS_LOAD_CEILING_MIN
    )


def _off_hours_engaged_minutes(
    streams: Iterable[StreamDayActivity],
    window_start_utc: datetime,
    window_end_utc: datetime,
    grace_seconds: float,
) -> int:
    """Distinct 1-minute instants OUTSIDE the work window during which the
    operator was engaged — i.e. within ``grace_seconds`` after one of their own
    messages (the same foreground notion as ``_stream_weight_at``).

    Interaction-anchored: driven purely by ``user_msg_timestamps``, so a
    background session alive off-hours with no user messages contributes
    nothing, and closure/git events never enter. Overlapping grace windows are
    de-duplicated via a set of minute-floored instants.
    """
    grace = timedelta(seconds=grace_seconds)
    minute = timedelta(minutes=1)
    engaged: set[datetime] = set()
    for stream in streams:
        for ts in stream.user_msg_timestamps:
            # Walk the grace window after each message at 1-minute resolution.
            t = ts.replace(second=0, microsecond=0)
            end = ts + grace
            while t <= end:
                if t < window_start_utc or t >= window_end_utc:
                    engaged.add(t)
                t += minute
    return len(engaged)


def _window_utc_bounds(
    day: date, window: WorkWindow, local_tz: tzinfo,
) -> tuple[datetime, datetime]:
    """Convert a local-tz work-hour band on `day` into UTC datetimes."""
    local_start = datetime.combine(day, window.start, tzinfo=local_tz)
    local_end = datetime.combine(day, window.end, tzinfo=local_tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _codl_samples(
    streams: tuple[StreamDayActivity, ...],
    window_start_utc: datetime,
    window_end_utc: datetime,
    sample_interval_seconds: int = 60,
) -> list[int]:
    """Sample the raw headcount of concurrently-alive streams at fixed
    intervals within the window.

    Each stream is treated as alive across [first_ts, last_ts]. This is the
    descriptive "how many sessions were open at once" count — it feeds the
    peak KPI and the hourly chart. The *scored* CODL axis uses the
    engagement-weighted sweep (`_codl_weighted_samples`), which discounts
    background/idle time so that leaving a session open while you're away
    doesn't read as active supervision.
    """
    if window_end_utc <= window_start_utc:
        return []
    samples: list[int] = []
    t = window_start_utc
    step = timedelta(seconds=sample_interval_seconds)
    while t <= window_end_utc:
        count = 0
        for s in streams:
            if s.first_ts <= t <= s.last_ts:
                count += 1
        samples.append(count)
        t += step
    return samples


def _stream_weight_at(
    s: StreamDayActivity,
    t: datetime,
    grace_seconds: float,
    background_weight: float,
) -> float:
    """Engagement weight of one stream at instant `t`.

    1.0 (foreground) when `t` falls within `grace_seconds` AFTER one of the
    user's messages in this stream — you're actively driving it. Otherwise
    `background_weight` while the stream is alive (cooking / parked), and 0
    once it's outside [first_ts, last_ts]. Grounded in Smith (2003) and
    Masicampo & Baumeister (2011): a background locus costs less than an
    active one, but more than nothing.
    """
    if not (s.first_ts <= t <= s.last_ts):
        return 0.0
    for ts in s.user_msg_timestamps:
        if 0 <= (t - ts).total_seconds() <= grace_seconds:
            return 1.0
    return background_weight


def _codl_weighted_samples(
    streams: tuple[StreamDayActivity, ...],
    window_start_utc: datetime,
    window_end_utc: datetime,
    grace_seconds: float,
    background_weight: float,
    sample_interval_seconds: int = 60,
) -> list[float]:
    """Sample engagement-weighted concurrency within the window: at each
    instant, sum every stream's weight (see `_stream_weight_at`). A session
    you're actively driving contributes 1.0; one cooking in the background
    contributes `background_weight`."""
    if window_end_utc <= window_start_utc:
        return []
    samples: list[float] = []
    t = window_start_utc
    step = timedelta(seconds=sample_interval_seconds)
    while t <= window_end_utc:
        samples.append(
            sum(
                _stream_weight_at(s, t, grace_seconds, background_weight)
                for s in streams
            )
        )
        t += step
    return samples


def _count_cross_stream_starts(
    streams: tuple[StreamDayActivity, ...],
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> int:
    """Count stream openings (first_ts) that occurred during the work window
    AND while at least one other stream was already active."""
    in_window = [s for s in streams
                 if window_start_utc <= s.first_ts <= window_end_utc]
    cross = 0
    for s in in_window:
        for other in streams:
            if other.stream_id == s.stream_id:
                continue
            if other.first_ts < s.first_ts <= other.last_ts:
                cross += 1
                break
    return cross


def _apportion_to_window(
    streams: tuple[StreamDayActivity, ...],
    count_attr: str,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Apportion a per-stream-aggregated event count (e.g. tool_error_count)
    by the fraction of each stream's lifetime that falls inside the window.

    Errors are assumed uniformly distributed across the stream's
    [first_ts, last_ts] interval — defensible default since the aggregate
    doesn't track per-event timestamps for tool events. A stream entirely
    outside the window contributes zero of its events to the in-window
    total; a stream half-in contributes half.

    Returns the rounded integer count for the window.
    """
    total = 0.0
    for s in streams:
        n = getattr(s, count_attr)
        if n <= 0:
            continue
        dur = max(1, int((s.last_ts - s.first_ts).total_seconds()))
        overlap_start = max(s.first_ts, window_start)
        overlap_end = min(s.last_ts, window_end)
        if overlap_end <= overlap_start:
            continue
        overlap_seconds = int((overlap_end - overlap_start).total_seconds())
        total += n * (overlap_seconds / dur)
    return int(round(total))


# ---------------------------------------------------------------------------
# Personal optimum

def derive_personal_optimum(
    days: dict[date, DayMetrics],
    min_days: int = OPTIMUM_MIN_DAYS_OF_DATA,
    bucket_width: float = OPTIMUM_BUCKET_WIDTH,
) -> float | None:
    """Find the CODL band that historically yielded the lowest combined
    (closure_deficit + off-hours pressure). Anchored in Yerkes-Dodson (1908)
    and Csíkszentmihályi (1990): performance follows an inverted-U with
    cognitive load. Returns None when there is insufficient data — the
    rendered report treats that as "calibrating"."""
    active_days = [
        m for m in days.values()
        if m.codl_avg > 0
    ]
    if len(active_days) < min_days:
        return None

    buckets: dict[int, list[DayMetrics]] = defaultdict(list)
    for m in active_days:
        bucket_idx = int(m.codl_avg / bucket_width)
        buckets[bucket_idx].append(m)

    best_score = -1.0
    best_midpoint: float | None = None
    for idx, metrics in buckets.items():
        # Need at least 2 days in a bucket to trust the average.
        if len(metrics) < 2:
            continue
        # Average closure over only the days that HAVE closure data — None
        # (no git-correlatable activity) is omitted, not treated as 0. A bucket
        # with no closure evidence at all contributes a neutral closure factor
        # so the band is judged on off-hours alone rather than rewarded for the
        # absence of git work.
        clo_vals = [m.closure_deficit for m in metrics if m.closure_deficit is not None]
        closure_factor = (1.0 - sum(clo_vals) / len(clo_vals)) if clo_vals else 1.0
        avg_off = sum(m.off_hours_minutes for m in metrics) / len(metrics)
        # Higher is better — low closure-deficit AND low off-hours interaction.
        score = closure_factor / (1.0 + avg_off / 60.0)
        if score > best_score:
            best_score = score
            best_midpoint = (idx + 0.5) * bucket_width

    return best_midpoint


# ---------------------------------------------------------------------------
# Small helpers

def _percentile(sorted_values: Iterable[float], q: float) -> float:
    """Linear-interpolation percentile on a sorted (ascending) iterable.
    For values that aren't sorted, the caller should sort first."""
    xs = list(sorted_values)
    if not xs:
        return 0.0
    if not _is_sorted(xs):
        xs.sort()
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def _is_sorted(xs: list[float]) -> bool:
    return all(xs[i] <= xs[i + 1] for i in range(len(xs) - 1))
