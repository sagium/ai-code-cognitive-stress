"""Anonymized research-data export.

Produces a *full-year, anonymized* JSON snapshot of a StressProfile that the
operator can voluntarily upload to the maintainer's anonymous research form, to
help calibrate the index's borrowed thresholds against real multi-developer
data (see the paper's validation roadmap).

This module is the only data-sharing path in the tool, and it preserves the
local-only invariant by design: it **writes a file to local disk and nothing
else**. There is no network I/O here — the human carries the file to the form.
Do not add an auto-upload/network step.

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
from typing import Any

from .aggregate import AggregateStats, DayAggregate
from .metrics import DayMetrics, StressProfile, WorkWindow, per_day_debug

SCHEMA = "ai-code-cognitive-stress.research-export.v3"

CONSENT_VERSION = "3"

# Affirmed by the operator before an export is written, and embedded verbatim in
# the file so the acknowledgment travels with the data. Kept in sync with the
# acceptance text on the upload form.
CONSENT_TEXT = (
    "I voluntarily share this file for research calibration and debugging of "
    "the cognitive-stress index. I understand that it contains only derived, "
    "non-identifying data computed on my own machine: daily metrics and the "
    "components behind them, per-session activity counts (message and "
    "tool-call tallies and durations), per-day counts of local git activity "
    "(commits/merges and history-rewrite operations such as amend/rebase/reset, "
    "as counts only), and an hourly activity-load shape, plus my typical "
    "working-hour ranges. It contains no source code, file paths, repository "
    "or branch names, commit messages, session text, usernames, or timezone; "
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
    local_tz=None,
    codl_cfg=None,
    repo_map: dict[str, str] | None = None,
    rng: random.Random | None = None,
    participant_id: str | None = None,
    generated_on: date | None = None,
) -> dict[str, Any]:
    """Return the anonymized, JSON-friendly research export dict.

    When ``aggregates`` is supplied, each emitted day carries a ``debug`` block
    (the per-axis component breakdown, hourly activity shape, and anonymized
    per-session counts) for calibrating data collection and the metrics —
    still free of names, paths, and absolute timestamps. ``local_tz`` /
    ``codl_cfg`` default to the same system timezone and config the profile was
    built with. ``repo_map`` (cwd→repo-root, no paths emitted) lets the debug
    block report per-repo netted closures so the exported Closure Deficit is
    reproducible from its counts.

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
        if codl_cfg is None:
            from .config import load_config
            codl_cfg = load_config().codl

    shift = timedelta(days=7 * rng.randint(-_MAX_SHIFT_WEEKS, _MAX_SHIFT_WEEKS))

    days_out: list[dict[str, Any]] = []
    for d, m in sorted(profile.days.items()):
        if not (m.composite > 0 or m.off_hours_minutes > 0):
            continue
        entry = _day_metrics_dict(m, shift)
        if aggregates is not None:
            agg = aggregates.get(d)
            window = profile.work_windows.get(d.weekday())
            if agg is not None and window is not None:
                entry["debug"] = per_day_debug(
                    agg, window, local_tz,
                    foreground_grace_minutes=codl_cfg.foreground_grace_minutes,
                    background_weight=codl_cfg.background_weight,
                    repo_map=repo_map,
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


def _day_metrics_dict(m: DayMetrics, shift: timedelta) -> dict[str, Any]:
    # weekday is taken from the real day; the shift is a whole number of weeks,
    # so the shifted date carries the same weekday.
    return {
        "date": (m.day + shift).isoformat(),
        "weekday": _WEEKDAY_NAMES[m.day.weekday()],
        "composite": m.composite,
        "codl_avg": m.codl_avg,
        "codl_peak": m.codl_peak,
        "codl_peak_active": m.codl_peak_active,
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
