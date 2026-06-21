"""Reduce per-day aggregates into the StressProfile consumed by the renderer.

Three axes (per day, computed during work hours only):
    CODL              avg / peak count of concurrently-active streams during
                      the day's work window. Sampled at 1-minute resolution via
                      sweep over per-stream run intervals — a session's
                      (first_ts, last_ts) span minus any idle gap long enough
                      (IDLE_CLOSE_MINUTES) that the app was closed and relaunched,
                      so a session counts as open only while it plausibly was.
                      Grounded in Cowan (2001) — WM capacity for concurrent
                      decision tracking — and Cummings & Mitchell (2008) on
                      supervisory-control fan-out.
    Interruption Idx  weighted attention-pulling events per work hour.
                      Weights from Mark, Gudith & Klocke (2008) and
                      Mark, Gonzalez & Harris (2005).
    Closure Deficit   resumption load: a loop you couldn't finish in one sitting
                      and had to pick back up later. Each resume (a true-idle gap
                      in a session within [RESUME_THRESHOLD_MINUTES,
                      IDLE_CLOSE_MINUTES) — past the upper bound the app was closed
                      and the break is recovery, not a parked loop) carries a
                      severity that saturates with gap length —
                      min(1, gap / RESUME_FULL_DECAY_MINUTES) — and the day's value
                      is min(1, Σ severity / RESUMPTION_DAILY_CEILING) in [0, 1].
                      Resuming a parked goal reloads decayed activation (Altmann &
                      Trafton 2002) at a cost rising with the gap (Monk, Trafton &
                      Boehm-Davis 2008; in-domain, Parnin & Rugaber 2011); closure
                      is a recovery resource (Sonnentag & Fritz 2007). Independent
                      of the concurrency shape C(t) by construction. Scored on
                      every active day; None only when a day has no activity at
                      all.

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
from ..core.config import load_config

# ---------------------------------------------------------------------------
# Citation-anchored constants. Bump tunables here, not at call sites.

# Cowan (2001) — WM capacity ≈ 4 concurrent items. Used as the per-instant
# saturation point for capacity-utilisation: phi(t) = min(1, C(t) / CODL_CAPACITY).
# When a single sample hits 4 engagement-weighted concurrent streams, that
# sample is at full capacity (phi=1.0). Intentionally NOT used as a
# normalisation ceiling for a time-average (that was the prior mismatch).
CODL_CAPACITY: float = 4.0

# Dose horizon: the number of capacity-equivalent minutes in a day that
# saturates the CODL axis to 1.0.  A "capacity-equivalent minute" is one
# minute spent at phi(t)=1 (all 4 slots occupied); idle minutes contribute 0.
# codl_dose = min(1, raw_dose / CODL_DOSE_HORIZON_MINUTES) where
# raw_dose = Σ phi(t) over all 1-minute work-window samples.
# Value is a prior fitted to observed data (calibration target); override via
# config.json scoring.codl_dose_horizon_minutes.
CODL_DOSE_HORIZON_MINUTES: float = 240.0

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

# CODL engagement weighting. A session counts at full weight (1.0) only while
# you're actively driving it — within FOREGROUND_GRACE of one of your messages.
# Outside that, it is alive but "cooking" in the background and counts at
# BACKGROUND_WEIGHT. A background/monitored locus is not free: holding a pending
# intention costs ~15-20% of ongoing-task capacity (Smith 2003), and open goals
# keep consuming working memory until closed (Masicampo & Baumeister 2011). We
# use 0.20 — the top of that empirical range, erring toward a
# stronger anti-fire-and-forget nudge. These are code-level fallbacks; the
# runtime source of truth is config.json (codl block), surfaced via load_config.
FOREGROUND_GRACE_MINUTES_DEFAULT: int = 5
BACKGROUND_WEIGHT_DEFAULT: float = 0.20

# Silence longer than this inside a session is read as the coding app having
# been closed (no live process) and reopened — a relaunch — not left open and
# idle. The session logs record activity, not process liveness, so a long silent
# gap cannot be told apart from a closed app; this cutoff is the deliberate lean
# toward what a process-liveness (PID) metric would have shown. Both liveness-
# reading axes use it: CODL stops counting a session as alive across a cut gap
# (so two sessions overlap only while both were plausibly running), and Closure
# stops treating a gap this long as an unfinished-loop resume (a multi-hour break
# is closure, not a parked loop). Code-level fallback; the runtime source is
# config.json (idle_close_minutes), surfaced via load_config.
IDLE_CLOSE_MINUTES_DEFAULT: int = 180

# Closure Deficit — RESUMPTION LOAD. A loop you can't finish in one sitting and
# have to pick back up later is an unclosed loop; the cost of resuming it grows
# with how long it was parked, because a suspended goal's activation decays over
# the gap and must be reconstructed on return. We score the day by summing a
# per-resume severity that saturates with gap length, then normalising.
#
# A *resume* is a true-idle gap in a session's timeline (no user OR agent event)
# in the band [RESUME_THRESHOLD_MINUTES, IDLE_CLOSE_MINUTES): long enough that the
# loop was really suspended, but short enough that the app plausibly stayed open
# and you were still carrying the loop. Each resume's severity is
# min(1, gap / RESUME_FULL_DECAY_MINUTES): a gap at the full-decay horizon (or
# beyond) counts as a fully-cold reload (1.0); shorter parks count proportionally.
# The day's axis value is min(1, Σ severity / RESUMPTION_DAILY_CEILING), in [0, 1]
# — 0 means every loop was closed in one sitting (genuinely good), higher means
# more / colder reloads.
# Independent of the concurrency shape C(t): it nets resume events, not C(t).
# Grounding:
#   - Monk, Trafton & Boehm-Davis (2008): resumption time rises with interruption
#     DURATION — the warrant for grading each resume by its gap length.
#   - Altmann & Trafton (2002), Memory for Goals: a suspended goal's activation
#     decays over the gap and must be re-strengthened to be retrieved on return.
#   - Parnin & Rugaber (2011): in-domain — resuming an interrupted programming
#     task reliably incurs a context-reconstruction tax (only 10% resume coding
#     within 1 min; only 7% resume without first navigating to rebuild context).
#   - Sonnentag & Fritz (2007): closure / disengagement is a recovery resource;
#     a loop you keep reopening is one you never got to close.
#   - Ghibellini & Meier (2025) meta-analysis: the tendency to resume interrupted
#     tasks (Ovsiankina) replicates; the older "open loops are better remembered"
#     claim (Zeigarnik) does not — so we ground the axis on resumption cost, not
#     on memory-persistence of open goals.
# Scored on every active day. A gap of at least IDLE_CLOSE_MINUTES — and any
# cross-day pickup — is NOT a resume: that long a break means the app was closed
# and you got genuine closure (recovery), not a loop parked in your head
# (Sonnentag & Fritz 2007). CAVEAT: the lab studies measured short interruptions
# (seconds to ~1 min); even the sub-IDLE_CLOSE gaps we score extrapolate beyond
# that regime, with Parnin & Rugaber as the closest field bridge.

# A gap of at least this many minutes (no event of any kind) is a resume — a
# parked loop picked back up — rather than a short break. Below it, the loop was
# never really suspended. Calibratable via config; a modeling prior, not fitted.
RESUME_THRESHOLD_MINUTES: int = 30

# The gap length at which a resume counts as a fully-cold reload (severity 1.0).
# Severity is min(1, gap / this), so a 6-hour park is no colder than a 2-hour one
# once context is fully gone. Anchors the duration→cost curve of Monk et al.
# (2008). A modeling prior, not fitted.
RESUME_FULL_DECAY_MINUTES: int = 120

# Sum of per-resume severities that maps the daily axis to 1.0. ~4 fully-cold
# reloads in a day is taken as "heavy" — a loose Cowan (2001) anchor: cold-
# reloading more than ~4 distinct loops in a day means thrashing across more open
# contexts than working memory comfortably holds. A modeling prior (like the
# off-hours ceiling), calibratable, NOT validated against felt load.
RESUMPTION_DAILY_CEILING: float = 4.0

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
# the work window raises the daily score because disengagement from work is a
# recovery resource (Sonnentag & Fritz 2007); sustained off-hours work drains
# it.  Applies additively to the composite so that a day with zero in-window
# work but real off-hours interaction still scores > 0.  Anchored to
# USER/AGENT INTERACTION (within grace_seconds of a user message), not stream
# liveness — a background session running while the human is away does NOT
# contribute.
# The boundary is ASYMMETRIC: every engaged minute past the window END counts
# (failure to disengage is the burnout signal), but an early START is ordinary
# schedule shift, not overload — minutes before the window start count only
# when they are more than EARLY_START_GRACE_HOURS earlier than usual.  The
# grace also keeps genuinely nocturnal work visible: small-hours minutes
# (e.g. 01:30 against a noon start) sit far beyond any plausible early start
# and still count.
# 90 min of off-hours engaged minutes is taken as the ceiling (beyond that,
# the signal saturates).  30 composite points is audible without dominating
# the three primary axes on moderate days.
OFF_HOURS_LOAD_CEILING_MIN: int = 90         # engaged off-hours minutes → max toll
OFF_HOURS_LOAD_MAX_POINTS: float = 30.0      # composite points added at/above the ceiling
EARLY_START_GRACE_HOURS: int = 3             # early start ≤ this before window start is free

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
                                     # DESCRIPTIVE mean — used for personal-optimum bucketing
    codl_peak: int = 0               # peak headcount of sessions alive at once
    codl_peak_active: float = 0.0    # peak engagement-weighted load (drives fan-out rec)
    codl_raw_dose: float = 0.0       # capacity-equivalent minutes: Σ min(1, C(t)/κ) * 1 min
    codl_dose: float = 0.0           # SCORED axis: min(1, raw_dose / dose_horizon) ∈ [0,1]
    interruption_rate: float = 0.0   # per work hour
    closure_deficit: float | None = None  # 0..1 resumption load (severity-summed
                                     # resumes / ceiling); 0 = closed in one sitting.
                                     # None only when the day has no activity at all.
    off_hours_minutes: int = 0       # engaged minutes past the window end, or
                                     # outlier-early before its start (beyond the
                                     # early-start grace); interaction-anchored,
                                     # not stream liveness
    # When those minutes happened, as local time-of-day ranges (first–last
    # engaged minute per contiguous run) — lets displays say *when* the
    # off-hours work occurred rather than reading as "right now".
    off_hours_ranges_local: tuple[tuple[time, time], ...] = ()
    composite: float = 0.0           # 0..100
    work_window_local: tuple[time, time] | None = None


@dataclass(frozen=True, slots=True)
class StressProfile:
    """Per-day metrics + profile-level derivatives."""
    days: dict[date, DayMetrics] = field(default_factory=dict)
    work_windows: dict[int, WorkWindow] = field(default_factory=dict)
    local_tz_name: str = "UTC"
    baseline_window_days: int = 90
    personal_optimum: float | None = None
    # Personal percentiles over the baseline window (used to color days).
    composite_p50: float | None = None
    composite_p75: float | None = None
    composite_p90: float | None = None


# ---------------------------------------------------------------------------
# Public entry point

def build_profile(
    aggregates: dict[date, DayAggregate],
    baseline_days: int = 90,
    local_tz: tzinfo | None = None,
    as_of: datetime | None = None,
) -> StressProfile:
    """Reduce a window of DayAggregates into a StressProfile.

    `local_tz` is the timezone in which to interpret "work hours" and
    "weekday". Defaults to the system local timezone.
    """
    local_tz = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    work_windows = detect_work_windows(aggregates, local_tz=local_tz)
    cfg = load_config()
    codl_cfg = cfg.codl
    scoring = cfg.scoring
    resumption = cfg.resumption

    days: dict[date, DayMetrics] = {}
    for day, agg in sorted(aggregates.items()):
        weekday = day.weekday()
        window = work_windows.get(weekday) or _default_window(weekday)
        days[day] = per_day_metrics(
            agg, window, local_tz,
            foreground_grace_minutes=codl_cfg.foreground_grace_minutes,
            background_weight=codl_cfg.background_weight,
            codl_capacity=scoring.codl_capacity,
            codl_dose_horizon_minutes=scoring.codl_dose_horizon_minutes,
            interruption_ceiling=scoring.interruption_ceiling,
            weights=scoring.weights,
            resume_threshold_minutes=resumption.threshold_minutes,
            resume_full_decay_minutes=resumption.full_decay_minutes,
            resumption_daily_ceiling=resumption.daily_ceiling,
            idle_close_minutes=cfg.idle_close_minutes,
            as_of=(
                as_of if as_of is not None
                and day == as_of.astimezone(local_tz).date() else None
            ),
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
    codl_capacity: float = CODL_CAPACITY,
    codl_dose_horizon_minutes: float = CODL_DOSE_HORIZON_MINUTES,
    interruption_ceiling: float = INTERRUPTION_NORMALISATION_CEILING,
    weights: tuple[float, float, float] = COMPOSITE_WEIGHTS,
    resume_threshold_minutes: int = RESUME_THRESHOLD_MINUTES,
    resume_full_decay_minutes: int = RESUME_FULL_DECAY_MINUTES,
    resumption_daily_ceiling: float = RESUMPTION_DAILY_CEILING,
    idle_close_minutes: int = IDLE_CLOSE_MINUTES_DEFAULT,
    as_of: datetime | None = None,
) -> DayMetrics:
    """Compute the three axes + composite for one day.

    Every day is treated identically regardless of what day of the week it
    falls on. Off-hours interaction (engaged minutes past the window end, or
    more than the early-start grace before its start) is captured as
    `off_hours_minutes` and adds an additive load to the composite, whether it
    occurs on a weekday, Saturday, or Sunday.

    `idle_close_minutes` is the silence-to-app-closed cutoff (module constant
    `IDLE_CLOSE_MINUTES_DEFAULT`), shared by the CODL concurrency sweep — a gap
    this long stops counting a session as open — and the Closure Deficit band —
    a gap this long is closure, not an unfinished-loop resume.
    """
    if not agg.streams:
        return DayMetrics(
            day=agg.day,
            work_window_local=(work_window.start, work_window.end),
        )

    window_start_utc, window_end_utc = _window_utc_bounds(
        agg.day, work_window, local_tz,
    )
    # The early-start grace zone (the EARLY_START_GRACE_HOURS before the window
    # start) is ordinary early work, not off-hours and not nothing: if the day's
    # first engaged moment lands there, pull the SCORED window's start down to it
    # so that morning's load earns full in-window credit. The off-hours boundary
    # is unchanged (it stays at the grace cutoff), so this grants credit without
    # adding a penalty; activity earlier than the grace cutoff is left to the
    # off-hours path and does not move the scored start. On a day that starts on
    # time this is a no-op (scored_start == window_start), so existing scores
    # are untouched.
    scored_start_utc = _effective_scored_start(
        agg.streams, window_start_utc,
        early_start_grace_hours=EARLY_START_GRACE_HOURS,
    )
    # A live day must not be diluted by hours that have not happened yet.
    # Keep the inferred window for display/off-hours classification, but score
    # only through the supplied observation time.
    scored_end_utc = window_end_utc
    if as_of is not None:
        scored_end_utc = min(window_end_utc, as_of.astimezone(timezone.utc))
    work_seconds = max(1, int((scored_end_utc - scored_start_utc).total_seconds()))
    work_hours = work_seconds / 3600.0

    # Headcount sweep → peak "sessions open at once" (descriptive).
    headcounts = _codl_samples(
        agg.streams, scored_start_utc, scored_end_utc,
        idle_close_seconds=idle_close_minutes * 60,
    )
    # Engagement-weighted sweep → the scored axis. Foreground (you're actively
    # driving the session) counts 1.0; background ("cooking") counts w_bg.
    weighted = _codl_weighted_samples(
        agg.streams, scored_start_utc, scored_end_utc,
        grace_seconds=foreground_grace_minutes * 60,
        background_weight=background_weight,
        idle_close_seconds=idle_close_minutes * 60,
    )
    if weighted:
        codl_avg = sum(weighted) / len(weighted)
        codl_peak = max(headcounts)
        codl_peak_active = max(weighted)
        # Capacity-dose: each 1-minute sample contributes phi(t) = min(1, C(t)/κ).
        # raw_dose is capacity-equivalent minutes; idle samples (weight=0) contribute 0.
        raw_dose = sum(min(1.0, c / codl_capacity) for c in weighted)
        codl_dose = min(1.0, raw_dose / codl_dose_horizon_minutes)
    else:
        codl_avg = 0.0
        codl_peak = 0
        codl_peak_active = 0.0
        raw_dose = 0.0
        codl_dose = 0.0

    # Closure Deficit: resumption load — severity-summed resumes (parked loops
    # picked back up) normalised to [0, 1]. 0 means every loop closed in one
    # sitting; higher means more / colder reloads. Independent of the C(t) shape.
    closure_deficit = _resumption_load(
        agg, scored_start_utc, scored_end_utc,
        threshold_seconds=resume_threshold_minutes * 60,
        full_decay_seconds=resume_full_decay_minutes * 60,
        daily_ceiling=resumption_daily_ceiling,
        idle_close_seconds=idle_close_minutes * 60,
    )

    cross_starts = _count_cross_stream_starts(
        agg.streams, scored_start_utc, scored_end_utc,
    )
    # Count tool errors that fall inside the work window, at the exact instant
    # each occurred (tool_error_timestamps). An error logged off-hours adds
    # nothing to the work-hour rate even when its stream straddles the window
    # edge; streams from older archive entries without timestamps fall back to
    # uniform apportionment across their lifetime.
    in_window_errors = _in_window_tool_errors(
        agg.streams, scored_start_utc, scored_end_utc,
    )
    interruption_count = (
        in_window_errors * W_TOOL_ERROR
        + cross_starts * W_CROSS_STREAM
    )
    interruption_rate = interruption_count / work_hours

    # Off-hours ENGAGED minutes = minutes outside the work window during which
    # the operator was actively driving a session (within the foreground grace
    # of one of their own messages). Anchored to interaction, not stream
    # liveness: a background job that ran while the human was away contributes
    # nothing. The inferred window is the norm; engaged interaction PAST its
    # end — or outlier-early, beyond the early-start grace before its start —
    # is the off-hours load.
    off_hours_instants = _off_hours_engaged_instants(
        agg.streams, window_start_utc, window_end_utc,
        grace_seconds=foreground_grace_minutes * 60,
    )
    off_hours_minutes = len(off_hours_instants)

    # Additive off-hours toll: off-hours interaction always counts, even when
    # the in-window base is zero (a day worked entirely outside the window).
    base_composite = _composite_score(
        codl_dose, interruption_rate, closure_deficit,
        interruption_ceiling=interruption_ceiling,
        weights=weights,
    )
    composite = min(100.0, base_composite + _off_hours_load_points(off_hours_minutes))

    return DayMetrics(
        day=agg.day,
        codl_avg=round(codl_avg, 3),
        codl_peak=codl_peak,
        codl_peak_active=round(codl_peak_active, 3),
        codl_raw_dose=round(raw_dose, 1),
        codl_dose=round(codl_dose, 3),
        interruption_rate=round(interruption_rate, 3),
        closure_deficit=(
            round(closure_deficit, 3) if closure_deficit is not None else None
        ),
        off_hours_minutes=off_hours_minutes,
        off_hours_ranges_local=_off_hours_local_ranges(off_hours_instants, local_tz),
        composite=round(composite, 1),
        # Report the EFFECTIVE scored window: its start dips to the first
        # early-grace engaged minute when there is one (else the inferred start),
        # so the day card, score sparkline, and freeze logic all reflect the
        # span that was actually scored.
        work_window_local=(
            scored_start_utc.astimezone(local_tz).time(), work_window.end,
        ),
    )


def per_day_debug(
    agg: DayAggregate,
    work_window: WorkWindow,
    local_tz: tzinfo,
    foreground_grace_minutes: int = FOREGROUND_GRACE_MINUTES_DEFAULT,
    background_weight: float = BACKGROUND_WEIGHT_DEFAULT,
    resume_threshold_minutes: int = RESUME_THRESHOLD_MINUTES,
    resume_full_decay_minutes: int = RESUME_FULL_DECAY_MINUTES,
    resumption_daily_ceiling: float = RESUMPTION_DAILY_CEILING,
    idle_close_minutes: int = IDLE_CLOSE_MINUTES_DEFAULT,
) -> dict:
    """Per-day component breakdown behind the scores, for the research export's
    debug detail. Mirrors the inputs `per_day_metrics` reduces, so the two stay
    in sync. Returns only counts/durations and an hourly activity shape — no
    project names and no absolute timestamps (anonymized at the source).

    Computed on demand by the export only; not part of `build_profile`, so the
    report/widget path pays nothing for it.
    """
    if not agg.streams:
        return {}

    ws, we = _window_utc_bounds(agg.day, work_window, local_tz)
    # Scored-window start: dips into the early-start grace zone to credit an
    # early start, exactly as `per_day_metrics` does (keeps the two in sync).
    # The off-hours boundary below still uses the inferred start `ws`.
    ss = _effective_scored_start(agg.streams, ws)
    work_hours = max(1, int((we - ss).total_seconds())) / 3600.0
    grace = foreground_grace_minutes * 60

    headcounts = _codl_samples(
        agg.streams, ss, we, idle_close_seconds=idle_close_minutes * 60,
    )
    weighted = _codl_weighted_samples(
        agg.streams, ss, we, grace_seconds=grace,
        background_weight=background_weight,
        idle_close_seconds=idle_close_minutes * 60,
    )
    cross_starts = _count_cross_stream_starts(agg.streams, ss, we)
    in_window_errors = _in_window_tool_errors(agg.streams, ss, we)
    # Resumption components: the gap (minutes) of each qualifying resume and the
    # severity-summed load. The exported Closure Deficit
    # (min(1, Σ severity / ceiling)) is reproducible from these.
    resume_gap_seconds = _qualifying_resumes(
        agg, ss, we, resume_threshold_minutes * 60, idle_close_minutes * 60,
    )
    resume_gap_minutes = [round(g / 60, 1) for g in resume_gap_seconds]
    full_decay_seconds = resume_full_decay_minutes * 60
    resumption_severity_sum = sum(
        _resume_severity(g, full_decay_seconds) for g in resume_gap_seconds
    )
    resumption_load = (
        min(1.0, resumption_severity_sum / resumption_daily_ceiling)
        if resumption_daily_ceiling > 0 else 0.0
    )

    # Hourly activity shape: average engagement-weighted concurrency per local
    # hour. Samples are one-per-minute from ss, so sample i is ss + i minutes.
    # Sparse — only hours with non-zero load are emitted, keeping it compact.
    hourly: dict[str, list[float]] = {}
    for i, v in enumerate(weighted):
        if v <= 0:
            continue
        hour = (ss + timedelta(minutes=i)).astimezone(local_tz).hour
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
            + cross_starts * W_CROSS_STREAM, 3,
        ),
        "resumes": len(resume_gap_seconds),
        "resume_gap_minutes": resume_gap_minutes,
        "resumption_load": round(resumption_load, 3),
        "off_hours_minutes": _off_hours_engaged_minutes(
            agg.streams, ws, we, grace_seconds=grace,
        ),
        "hourly_concurrency": hourly_concurrency,
        "sessions": sessions,
    }


def _resume_severity(gap_seconds: float, full_decay_seconds: float) -> float:
    """Severity of one resume, in (0, 1]: ``min(1, gap / full_decay)``. Linear in
    gap length up to the full-decay horizon, then saturates — a much longer park
    is no colder once context is fully gone. Grounds the duration→cost curve of
    Monk, Trafton & Boehm-Davis (2008)."""
    if full_decay_seconds <= 0:
        return 1.0
    return min(1.0, gap_seconds / full_decay_seconds)


def _qualifying_resumes(
    agg: DayAggregate,
    window_start_utc: datetime,
    window_end_utc: datetime,
    threshold_seconds: int,
    idle_close_seconds: float,
) -> list[float]:
    """Gap lengths (seconds) of every intra-day resume that counts toward the
    day's Closure Deficit: an idle gap (``resume_gaps``) in the band
    ``[threshold_seconds, idle_close_seconds)`` whose resume instant lands in the
    work window.

    The lower bound drops ordinary within-turn pauses — below it the loop was
    never really suspended. The upper bound drops multi-hour breaks: a gap that
    long means the app was closed and you got genuine closure, so the pickup is a
    fresh sitting, not a parked loop carried in your head. Cross-day pickups are
    not counted for the same reason — an overnight or longer break is recovery,
    not an unfinished loop (Sonnentag & Fritz 2007). Shared by ``_resumption_load``
    (scores them) and ``per_day_debug`` (reports them) so the exported components
    reproduce the axis."""
    gaps: list[float] = []
    for s in agg.streams:
        for resume_ts, gap_seconds in s.resume_gaps:
            if threshold_seconds <= gap_seconds < idle_close_seconds and (
                window_start_utc <= resume_ts <= window_end_utc
            ):
                gaps.append(float(gap_seconds))
    return gaps


def _resumption_load(
    agg: DayAggregate,
    window_start_utc: datetime,
    window_end_utc: datetime,
    threshold_seconds: int = RESUME_THRESHOLD_MINUTES * 60,
    full_decay_seconds: int = RESUME_FULL_DECAY_MINUTES * 60,
    daily_ceiling: float = RESUMPTION_DAILY_CEILING,
    idle_close_seconds: float = IDLE_CLOSE_MINUTES_DEFAULT * 60,
) -> float | None:
    """Resumption load for the day, in [0, 1], or ``None`` only when the day has
    no activity at all.

    Each qualifying resume (see ``_qualifying_resumes``) contributes
    ``_resume_severity(gap)``; the day's value is
    ``min(1, Σ severity / daily_ceiling)``. ``0.0`` is a real, good score (every
    loop closed in one sitting) — distinct from ``None`` (no activity to assess).
    Independent of the concurrency time-series C(t): two days with identical
    C(t) score differently if one parked-and-reloaded its loops and the other
    ran them to completion in a sitting.
    """
    if not agg.streams:
        return None

    gaps = _qualifying_resumes(
        agg, window_start_utc, window_end_utc,
        threshold_seconds, idle_close_seconds,
    )
    total = sum(_resume_severity(g, full_decay_seconds) for g in gaps)
    if daily_ceiling <= 0:
        return 0.0
    return min(1.0, total / daily_ceiling)


def _composite_score(
    codl_dose: float,
    interruption_rate: float,
    closure_deficit: float | None,
    interruption_ceiling: float = INTERRUPTION_NORMALISATION_CEILING,
    weights: tuple[float, float, float] = COMPOSITE_WEIGHTS,
) -> float:
    """Weighted blend of the available axes, mapped to 0..100. Each axis is
    clamped to [0, 1] before weighting; weights are normalized by their sum, so
    a calibrated weight vector that doesn't sum to 1 still yields a 0..100 score.

    ``codl_dose`` is already in [0, 1] (the graded capacity-dose, computed in
    ``per_day_metrics``). ``interruption_rate`` is normalised here by
    ``interruption_ceiling``.

    When ``closure_deficit is None`` the Closure axis has no data for the day
    (``_resumption_load`` returns None only when the day has no activity at
    all), so it is dropped and the blend renormalises over the remaining axes —
    i.e. its weight is redistributed to CODL and Interruption rather than
    imputed as a perfect-closure 0."""
    codl_norm = max(0.0, min(1.0, codl_dose))  # already [0,1]; clamp for safety
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


def _off_hours_engaged_instants(
    streams: Iterable[StreamDayActivity],
    window_start_utc: datetime,
    window_end_utc: datetime,
    grace_seconds: float,
    early_start_grace_hours: int = EARLY_START_GRACE_HOURS,
) -> list[datetime]:
    """Distinct 1-minute instants OUTSIDE the work window during which the
    operator was engaged — i.e. within ``grace_seconds`` after one of their own
    messages (the same foreground notion as ``_stream_weight_at``).

    The boundary is asymmetric (see the EARLY_START_GRACE_HOURS prior): every
    minute at/after the window end counts, but minutes before the window start
    count only when they fall more than ``early_start_grace_hours`` before it —
    an early start within the grace is schedule shift, not off-hours load.

    Interaction-anchored: driven purely by ``user_msg_timestamps``, so a
    background session alive off-hours with no user messages contributes
    nothing. Overlapping grace windows are de-duplicated via a set of
    minute-floored instants. Returns the instants sorted (UTC); ``len()``
    of the result is the off-hours minute count.
    """
    grace = timedelta(seconds=grace_seconds)
    minute = timedelta(minutes=1)
    early_cutoff_utc = window_start_utc - timedelta(hours=early_start_grace_hours)
    engaged: set[datetime] = set()
    for stream in streams:
        for ts in stream.user_msg_timestamps:
            # Walk the grace window after each message at 1-minute resolution.
            t = ts.replace(second=0, microsecond=0)
            end = ts + grace
            while t <= end:
                if t < early_cutoff_utc or t >= window_end_utc:
                    engaged.add(t)
                t += minute
    return sorted(engaged)


def _off_hours_engaged_minutes(
    streams: Iterable[StreamDayActivity],
    window_start_utc: datetime,
    window_end_utc: datetime,
    grace_seconds: float,
    early_start_grace_hours: int = EARLY_START_GRACE_HOURS,
) -> int:
    """Count of distinct off-hours engaged minutes (see
    ``_off_hours_engaged_instants``)."""
    return len(_off_hours_engaged_instants(
        streams, window_start_utc, window_end_utc, grace_seconds,
        early_start_grace_hours,
    ))


def _off_hours_local_ranges(
    instants: list[datetime],
    local_tz: tzinfo,
    merge_gap_minutes: int = 5,
) -> tuple[tuple[time, time], ...]:
    """Compress sorted engaged instants into (first, last) local time-of-day
    ranges, merging runs separated by ≤ ``merge_gap_minutes`` so a banner can
    state *when* the off-hours work happened without fragmenting into noise.
    Display-only: the exact minute count stays ``len(instants)``."""
    if not instants:
        return ()
    merge_gap = timedelta(minutes=merge_gap_minutes)
    ranges: list[tuple[datetime, datetime]] = []
    start = prev = instants[0]
    for t in instants[1:]:
        if t - prev > merge_gap:
            ranges.append((start, prev))
            start = t
        prev = t
    ranges.append((start, prev))
    return tuple(
        (s.astimezone(local_tz).time(), e.astimezone(local_tz).time())
        for s, e in ranges
    )


def _window_utc_bounds(
    day: date, window: WorkWindow, local_tz: tzinfo,
) -> tuple[datetime, datetime]:
    """Convert a local-tz work-hour band on `day` into UTC datetimes."""
    local_start = datetime.combine(day, window.start, tzinfo=local_tz)
    local_end = datetime.combine(day, window.end, tzinfo=local_tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _effective_scored_start(
    streams: Iterable[StreamDayActivity],
    window_start_utc: datetime,
    early_start_grace_hours: int = EARLY_START_GRACE_HOURS,
) -> datetime:
    """Start instant of the SCORED window (minute-floored, UTC).

    Normally the inferred window start. But an early start within the grace zone
    ``[window_start - early_start_grace_hours, window_start)`` is ordinary work,
    not off-hours and not nothing — so when the day's first engaged moment (a
    user message, the same foreground anchor the off-hours and CODL paths use)
    lands in that zone, the scored start dips to it. That credits the morning's
    in-window load while the off-hours boundary stays put at the grace cutoff
    (the asymmetric early-start prior is preserved: early work is free of
    penalty, not erased). A user message earlier than the grace cutoff is left
    to the off-hours path and does NOT move the scored start. On a day that
    begins at/after the window start this returns the window start unchanged."""
    early_cutoff_utc = window_start_utc - timedelta(hours=early_start_grace_hours)
    earliest: datetime | None = None
    for stream in streams:
        for ts in stream.user_msg_timestamps:
            if early_cutoff_utc <= ts < window_start_utc:
                if earliest is None or ts < earliest:
                    earliest = ts
    if earliest is None:
        return window_start_utc
    return earliest.replace(second=0, microsecond=0)


def alive_intervals(
    stream: StreamDayActivity, idle_close_seconds: float,
) -> list[tuple[datetime, datetime]]:
    """A stream's wall-clock liveness, split into one interval per continuous
    app-run.

    A resume gap of at least ``idle_close_seconds`` is read as the coding app
    having been closed (no live process) and reopened: it ends the current run
    at the last event before the gap and starts a new run at the resume instant,
    so the dead span between is not counted as an open session. Shorter idle gaps
    stay inside one run — the app was open, just idle, and a process would still
    be alive. With no qualifying gap this is the single ``[first_ts, last_ts]``
    interval (including the zero-length ``[first_ts, first_ts]`` of a one-event
    session). The split points come straight from the stream's ``resume_gaps``,
    so the cutoff retunes against the existing cache — no re-ingest.
    """
    cuts = sorted(
        (resume_ts, gap) for resume_ts, gap in stream.resume_gaps
        if gap >= idle_close_seconds
    )
    intervals: list[tuple[datetime, datetime]] = []
    start = stream.first_ts
    for resume_ts, gap in cuts:
        # resume_gaps stores (event-after-gap, gap_seconds); the event before the
        # gap — where this run ends — is resume_ts - gap.
        seg_end = resume_ts - timedelta(seconds=gap)
        if seg_end < start:
            seg_end = start
        intervals.append((start, seg_end))
        start = resume_ts
    intervals.append((start, stream.last_ts))
    return intervals


def _alive_at(intervals: list[tuple[datetime, datetime]], t: datetime) -> bool:
    """True if instant `t` falls within any of a stream's alive intervals."""
    return any(a <= t <= b for a, b in intervals)


def _codl_samples(
    streams: tuple[StreamDayActivity, ...],
    window_start_utc: datetime,
    window_end_utc: datetime,
    idle_close_seconds: float = IDLE_CLOSE_MINUTES_DEFAULT * 60,
    sample_interval_seconds: int = 60,
) -> list[int]:
    """Sample the raw headcount of concurrently-alive streams at fixed
    intervals within the window.

    A stream is alive across its run intervals (``alive_intervals``): its
    [first_ts, last_ts] span minus any gap long enough (``idle_close_seconds``)
    that the app was closed and relaunched. This is the descriptive "how many
    sessions were open at once" count — it feeds the peak KPI and the hourly
    chart. The *scored* CODL axis uses the engagement-weighted sweep
    (`_codl_weighted_samples`), which further discounts background/idle time so
    that leaving a session open while you're away doesn't read as active
    supervision.
    """
    if window_end_utc <= window_start_utc:
        return []
    intervals_by_stream = [alive_intervals(s, idle_close_seconds) for s in streams]
    samples: list[int] = []
    t = window_start_utc
    step = timedelta(seconds=sample_interval_seconds)
    while t <= window_end_utc:
        count = sum(1 for ivs in intervals_by_stream if _alive_at(ivs, t))
        samples.append(count)
        t += step
    return samples


def _stream_weight_at(
    s: StreamDayActivity,
    t: datetime,
    grace_seconds: float,
    background_weight: float,
    idle_close_seconds: float = IDLE_CLOSE_MINUTES_DEFAULT * 60,
) -> float:
    """Engagement weight of one stream at instant `t`.

    1.0 (foreground) when `t` falls within `grace_seconds` AFTER one of the
    user's messages in this stream — you're actively driving it. Otherwise
    `background_weight` while the stream is alive (cooking / parked), and 0 once
    it's outside the stream's run intervals (`alive_intervals`) — before it
    opened, after it closed, or inside a gap long enough (`idle_close_seconds`)
    that the app was closed and relaunched. Grounded in Smith (2003) and
    Masicampo & Baumeister (2011): a background locus costs less than an active
    one, but more than nothing.
    """
    if not _alive_at(alive_intervals(s, idle_close_seconds), t):
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
    idle_close_seconds: float = IDLE_CLOSE_MINUTES_DEFAULT * 60,
    sample_interval_seconds: int = 60,
) -> list[float]:
    """Sample engagement-weighted concurrency within the window: at each
    instant, sum every stream's weight (see `_stream_weight_at`). A session
    you're actively driving contributes 1.0; one cooking in the background
    contributes `background_weight`; one inside a closed-app gap contributes 0."""
    if window_end_utc <= window_start_utc:
        return []
    samples: list[float] = []
    t = window_start_utc
    step = timedelta(seconds=sample_interval_seconds)
    while t <= window_end_utc:
        samples.append(
            sum(
                _stream_weight_at(
                    s, t, grace_seconds, background_weight, idle_close_seconds,
                )
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


def _in_window_tool_errors(
    streams: tuple[StreamDayActivity, ...],
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Number of tool errors that occurred inside the work window.

    Each error is counted at the exact instant it was logged
    (``tool_error_timestamps``), so an error that fired off-hours contributes
    nothing to the work-hour interruption rate even when its stream straddled
    the window edge, and a burst of errors clustered at one end of a stream is
    attributed to that end rather than smeared across the whole lifetime.

    A stream carrying errors but no timestamps (aggregated before the field
    existed, e.g. an older archive entry) falls back to uniform apportionment
    across its [first_ts, last_ts] lifetime (``_apportion_to_window``).
    """
    total = 0
    legacy: list[StreamDayActivity] = []
    for s in streams:
        if s.tool_error_count <= 0:
            continue
        if s.tool_error_timestamps:
            total += sum(
                1 for t in s.tool_error_timestamps
                if window_start <= t <= window_end
            )
        else:
            legacy.append(s)
    if legacy:
        total += _apportion_to_window(
            tuple(legacy), "tool_error_count", window_start, window_end,
        )
    return total


def _apportion_to_window(
    streams: tuple[StreamDayActivity, ...],
    count_attr: str,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Apportion a per-stream-aggregated event count by the fraction of each
    stream's lifetime that falls inside the window, assuming the events are
    uniformly distributed across [first_ts, last_ts]. A stream entirely outside
    the window contributes zero; a stream half-in contributes half.

    Used as the fallback for tool errors on streams that carry no per-error
    timestamps (see ``_in_window_tool_errors``); exact timing is preferred
    whenever ``tool_error_timestamps`` is present.

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
        # Average resumption load over the days that HAVE it — None (a no-activity
        # day) is omitted, not treated as 0. A bucket with no such data contributes
        # a neutral closure factor so the band is judged on off-hours alone.
        clo_vals = [m.closure_deficit for m in metrics if m.closure_deficit is not None]
        closure_factor = (1.0 - sum(clo_vals) / len(clo_vals)) if clo_vals else 1.0
        avg_off = sum(m.off_hours_minutes for m in metrics) / len(metrics)
        # Higher is better — low resumption load AND low off-hours interaction.
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
