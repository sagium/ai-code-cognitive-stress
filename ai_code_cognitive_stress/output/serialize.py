"""Serialize a StressProfile + ingest stats into a JSON-friendly dict.

Emitted as a sibling to the HTML report so the Claude Code skill — or any
other LLM / analysis layer — can read structured data without parsing the
rendered HTML. The HTML stays the human-facing artifact; this dict is the
machine-readable view of the same StressProfile.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from ..pipeline.aggregate import AggregateStats
from ..pipeline.metrics import DayMetrics, StressProfile, WorkWindow

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def profile_to_dict(
    profile: StressProfile,
    *,
    label: str,
    since: date,
    until: date,
    ingest_stats: AggregateStats | None,
    package_version: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a JSON-friendly dict capturing the full profile + ingest summary."""
    return {
        "schema": "ai-code-cognitive-stress.profile.v1",
        "package_version": package_version,
        "label": label,
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "generated_at": (
            generated_at or datetime.now(timezone.utc)
        ).isoformat(),
        "ingest_stats": _ingest_stats_dict(ingest_stats),
        "profile": {
            "local_tz": profile.local_tz_name,
            "baseline_window_days": profile.baseline_window_days,
            "personal_optimum": profile.personal_optimum,
            "composite_percentiles": {
                "p50": profile.composite_p50,
                "p75": profile.composite_p75,
                "p90": profile.composite_p90,
            },
            "work_windows": {
                str(wd): _work_window_dict(profile.work_windows.get(wd))
                for wd in range(7)
            },
            # Days include all days with non-zero composite or off-hours
            # activity, regardless of what day of the week they fall on.
            "days": [
                _day_metrics_dict(m)
                for d, m in sorted(profile.days.items())
                if m.composite > 0 or m.off_hours_minutes > 0
            ],
            "active_day_count": sum(
                1 for m in profile.days.values() if m.composite > 0
            ),
        },
    }


def _ingest_stats_dict(stats: AggregateStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    i = stats.ingest
    return {
        "files_scanned": i.files_scanned,
        "files_kept": i.files_kept,
        "lines_total": i.lines_total,
        "lines_decoded": i.lines_decoded,
        "lines_skipped_malformed": i.lines_skipped_malformed,
        "lines_skipped_no_timestamp": i.lines_skipped_no_timestamp,
        "events_emitted": i.events_emitted,
        "days_in_window": stats.days_in_window,
        "days_with_activity": stats.days_with_activity,
        "cache_hits": stats.cache_hits,
        "cache_misses": stats.cache_misses,
        "cache_write_errors": stats.cache_write_errors,
    }


def _work_window_dict(window: WorkWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "weekday": _WEEKDAY_NAMES[window.weekday],
        "start": window.start.strftime("%H:%M"),
        "end": window.end.strftime("%H:%M"),
        "is_default": window.is_default,
    }


def _day_metrics_dict(m: DayMetrics) -> dict[str, Any]:
    return {
        "day": m.day.isoformat(),
        "weekday": _WEEKDAY_NAMES[m.day.weekday()],
        "composite": m.composite,
        "codl_avg": m.codl_avg,
        "codl_peak": m.codl_peak,
        "codl_peak_active": m.codl_peak_active,
        "interruption_rate": m.interruption_rate,
        "closure_deficit": m.closure_deficit,
        "off_hours_minutes": m.off_hours_minutes,
        "work_window_local": (
            [
                m.work_window_local[0].strftime("%H:%M"),
                m.work_window_local[1].strftime("%H:%M"),
            ]
            if m.work_window_local else None
        ),
    }
