"""Anonymized research-data export.

Produces a *full-year, anonymized* JSON snapshot of a StressProfile that the
operator can voluntarily upload to the maintainer's anonymous research form, to
help calibrate the index's borrowed thresholds against real multi-developer
data (see the paper's validation roadmap).

This module is the only data-sharing path in the tool: it **writes a file to
disk and nothing else**. The user carries the file to the form manually.
Do not add an auto-upload step.

Anonymization (the "Balanced" profile):
  - the timezone name is dropped entirely;
  - every calendar date is shifted by a hidden random whole-week offset, fresh
    per export, so weekday and relative spacing survive but the real calendar
    does not (the offset is never written to the file — that is what makes it
    one-way);
  - a fresh random participant id groups one submission's days without
    identifying the person;
  - work-window clock times and the numeric axis metrics are kept verbatim —
    they are the calibration signal and carry no direct identifier.
No source, file paths, repo names, commit messages, session text, or usernames
ever enter the profile pipeline, so none can leak here.
"""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..pipeline.aggregate import AggregateStats, DayAggregate
from ..pipeline.metrics import DayMetrics, StressProfile, WorkWindow, per_day_debug
from ..pipeline.subjective import read_grade

SCHEMA = "ai-code-cognitive-stress.research-export.v4"

CONSENT_VERSION = "6"

# Affirmed by the operator before an export is written, and embedded verbatim in
# the file so the acknowledgment travels with the data. Kept in sync with the
# acceptance text on the upload form.
CONSENT_TEXT = (
    "I voluntarily share this file for research calibration and debugging of "
    "the cognitive-stress index. I understand that it contains only derived, "
    "non-identifying data computed on my own machine: daily metrics and the "
    "components behind them, per-session activity counts (message and "
    "tool-call tallies and durations), session-resumption timing (how long "
    "parked sessions sat idle before I picked them back up, in minutes), an "
    "hourly activity-load shape, plus my typical "
    "working-hour ranges, and — for days I chose to grade — an optional "
    "self-reported felt-stress rating (a single integer: 0 = chill, "
    "1 = heated, 2 = cooked). Most days carry no grade; the field is always "
    "present but null when ungraded. It contains no source code, file paths, "
    "repository or branch names, session text, usernames, or timezone; "
    "calendar dates are randomly shifted and a random per-export id is used, so "
    "the data is not personally identifying. Sharing is entirely optional, and "
    "because the submission is anonymous it cannot be traced back and withdrawn "
    "once uploaded."
)

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Date shift is a whole number of weeks so the weekday is preserved; the band is
# wide enough (±half a year) to decouple the export from the real calendar.
_MAX_SHIFT_WEEKS = 26


def consent_satisfied(
    *,
    flag: bool,
    isatty: bool,
    prompt_fn=input,
) -> bool:
    """Decide whether the operator has consented to the export.

    ``flag`` (the ``--i-consent`` switch) satisfies consent non-interactively.
    Otherwise, in an interactive terminal, the operator must type ``yes`` at the
    prompt. A non-interactive run without the flag is refused (returns False) so
    an export never happens silently in a script or pipe.
    """
    if flag:
        return True
    if not isatty:
        return False
    answer = prompt_fn("Type 'yes' to consent to this anonymized export: ")
    return answer.strip().lower() == "yes"


def build_research_export(
    profile: StressProfile,
    *,
    since: date,
    until: date,
    package_version: str,
    ingest_stats: AggregateStats | None,
    aggregates: dict[date, DayAggregate] | None = None,
    archive_dir: Path | None = None,
    local_tz=None,
    codl_cfg=None,
    resumption_cfg=None,
    idle_close_minutes: int | None = None,
    rng: random.Random | None = None,
    participant_id: str | None = None,
    generated_on: date | None = None,
) -> dict[str, Any]:
    """Return the anonymized, JSON-friendly research export dict.

    When ``aggregates`` is supplied, each emitted day carries a ``debug`` block
    (the per-axis component breakdown, hourly activity shape, and anonymized
    per-session counts) for calibrating data collection and the metrics —
    still free of names, paths, and absolute timestamps. ``local_tz`` /
    ``codl_cfg`` / ``resumption_cfg`` default to the same system timezone and
    config the profile was built with. The debug block reports the resumption
    components (resume count, per-resume gap minutes, severity-summed load) so
    the exported Closure Deficit is reproducible from them. Closure counts only
    intra-day resumes in the band [threshold, idle_close); cross-day pickups and
    longer breaks are recovery, not resumes, exactly as ``build_profile`` scores.

    ``rng`` / ``participant_id`` / ``generated_on`` are injectable so the output
    is deterministic under test; in production they default to system entropy,
    a fresh UUID, and today's date respectively.
    """
    rng = rng if rng is not None else random.SystemRandom()
    participant_id = participant_id or uuid.uuid4().hex
    generated_on = generated_on or datetime.now(timezone.utc).date()
    if aggregates is not None:
        if local_tz is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        if codl_cfg is None or resumption_cfg is None or idle_close_minutes is None:
            from ..core.config import load_config
            _cfg = load_config()
            codl_cfg = codl_cfg or _cfg.codl
            resumption_cfg = resumption_cfg or _cfg.resumption
            if idle_close_minutes is None:
                idle_close_minutes = _cfg.idle_close_minutes

    shift = timedelta(days=7 * rng.randint(-_MAX_SHIFT_WEEKS, _MAX_SHIFT_WEEKS))

    days_out: list[dict[str, Any]] = []
    for d, m in sorted(profile.days.items()):
        agg = aggregates.get(d) if aggregates is not None else None
        if m.composite > 0 or m.off_hours_minutes > 0:
            grade = read_grade(archive_dir, d) if archive_dir is not None else None
            entry = _day_metrics_dict(m, shift, grade)
            window = profile.work_windows.get(d.weekday())
            if agg is not None and window is not None:
                entry["debug"] = per_day_debug(
                    agg, window, local_tz,
                    foreground_grace_minutes=codl_cfg.foreground_grace_minutes,
                    background_weight=codl_cfg.background_weight,
                    resume_threshold_minutes=resumption_cfg.threshold_minutes,
                    resume_full_decay_minutes=resumption_cfg.full_decay_minutes,
                    resumption_daily_ceiling=resumption_cfg.daily_ceiling,
                    idle_close_minutes=idle_close_minutes,
                )
            days_out.append(entry)

    return {
        "schema": SCHEMA,
        "package_version": package_version,
        "generated_on": generated_on.isoformat(),
        "participant": participant_id,
        "consent": {
            "version": CONSENT_VERSION,
            "acknowledged": True,
            "acknowledged_on": generated_on.isoformat(),
            "statement": CONSENT_TEXT,
        },
        # Length of the requested span only — no absolute calendar boundary, so
        # the real year is not pinned down by the window itself.
        "window_days": (until - since).days + 1,
        "profile": {
            "baseline_window_days": profile.baseline_window_days,
            "personal_optimum": _round(profile.personal_optimum, 3),
            "composite_percentiles": {
                "p50": _round(profile.composite_p50, 2),
                "p75": _round(profile.composite_p75, 2),
                "p90": _round(profile.composite_p90, 2),
            },
            "work_windows": {
                str(wd): _work_window_dict(profile.work_windows.get(wd))
                for wd in range(7)
            },
            "active_day_count": sum(
                1 for m in profile.days.values() if m.composite > 0
            ),
            "days": days_out,
        },
        "ingest_stats": _ingest_stats_dict(ingest_stats),
    }


def _round(value: float | None, ndigits: int) -> float | None:
    return round(value, ndigits) if value is not None else None


def _work_window_dict(window: WorkWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "weekday": _WEEKDAY_NAMES[window.weekday],
        "start": window.start.strftime("%H:%M"),
        "end": window.end.strftime("%H:%M"),
        "is_default": window.is_default,
    }


def _day_metrics_dict(
    m: DayMetrics, shift: timedelta, subjective_rating: int | None = None,
) -> dict[str, Any]:
    # weekday is taken from the real day; the shift is a whole number of weeks,
    # so the shifted date carries the same weekday.
    # subjective_rating uses the REAL m.day (read before calling this function)
    # so the grade attaches to the already-shifted date and cannot be mapped
    # back to the true calendar date.
    return {
        "date": (m.day + shift).isoformat(),
        "weekday": _WEEKDAY_NAMES[m.day.weekday()],
        "composite": m.composite,
        "codl_avg": m.codl_avg,
        "codl_peak": m.codl_peak,
        "codl_peak_active": m.codl_peak_active,
        "codl_raw_dose": m.codl_raw_dose,
        "codl_dose": m.codl_dose,
        "interruption_rate": m.interruption_rate,
        "closure_deficit": m.closure_deficit,
        "off_hours_minutes": m.off_hours_minutes,
        "work_window": (
            [
                m.work_window_local[0].strftime("%H:%M"),
                m.work_window_local[1].strftime("%H:%M"),
            ]
            if m.work_window_local else None
        ),
        "subjective_rating": subjective_rating,
    }


def _ingest_stats_dict(stats: AggregateStats | None) -> dict[str, Any] | None:
    # Non-identifying volume counts only — useful to weight/quality-filter a
    # submission. No paths, names, or content are present in AggregateStats.
    if stats is None:
        return None
    i = stats.ingest
    return {
        "files_kept": i.files_kept,
        "events_emitted": i.events_emitted,
        "days_in_window": stats.days_in_window,
        "days_with_activity": stats.days_with_activity,
    }
