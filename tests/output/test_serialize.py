"""Tests for the JSON serialization layer that feeds the LLM analysis."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from ai_code_cognitive_stress.pipeline.aggregate import AggregateStats
from ai_code_cognitive_stress.pipeline.metrics import DayMetrics, StressProfile, WorkWindow
from ai_code_cognitive_stress.output.serialize import _work_window_dict, profile_to_dict


def test_work_window_dict_is_none_for_missing_window():
    assert _work_window_dict(None) is None


def test_work_window_dict_serializes_window():
    d = _work_window_dict(WorkWindow(weekday=0, start=time(9), end=time(17)))
    assert d == {"weekday": "Mon", "start": "09:00", "end": "17:00",
                 "is_default": False}


def _stub_profile() -> StressProfile:
    days = {
        date(2026, 5, 14): DayMetrics(
            day=date(2026, 5, 14), composite=64.0,
            codl_avg=1.56, codl_peak=3,
            interruption_rate=6.04, closure_deficit=0.40,
            off_hours_minutes=0,
            work_window_local=(time(9), time(18)),
        ),
        date(2026, 5, 15): DayMetrics(
            day=date(2026, 5, 15), composite=0.0,  # empty / no activity
        ),
    }
    windows = {
        wd: WorkWindow(weekday=wd, start=time(9), end=time(18), is_default=False)
        for wd in range(7)
    }
    return StressProfile(
        days=days, work_windows=windows, local_tz_name="UTC+02:00",
        baseline_window_days=30, personal_optimum=1.5,
        composite_p50=45.0, composite_p75=52.0, composite_p90=56.0,
    )


def test_profile_to_dict_emits_schema_marker():
    out = profile_to_dict(
        _stub_profile(), label="May 2026",
        since=date(2026, 5, 1), until=date(2026, 5, 31),
        ingest_stats=None, package_version="0.1.0",
    )
    assert out["schema"] == "ai-code-cognitive-stress.profile.v1"
    assert out["package_version"] == "0.1.0"
    assert out["label"] == "May 2026"


def test_profile_to_dict_window_and_active_count():
    out = profile_to_dict(
        _stub_profile(), label="May 2026",
        since=date(2026, 5, 1), until=date(2026, 5, 31),
        ingest_stats=None, package_version="0.1.0",
    )
    assert out["window"] == {"since": "2026-05-01", "until": "2026-05-31"}
    # Only the day with composite > 0 should be in the days list
    assert out["profile"]["active_day_count"] == 1
    assert len(out["profile"]["days"]) == 1


def test_profile_to_dict_personal_optimum_and_percentiles():
    out = profile_to_dict(
        _stub_profile(), label="May 2026",
        since=date(2026, 5, 1), until=date(2026, 5, 31),
        ingest_stats=None, package_version="0.1.0",
    )
    p = out["profile"]
    assert p["personal_optimum"] == 1.5
    assert p["composite_percentiles"]["p50"] == 45.0
    assert p["composite_percentiles"]["p75"] == 52.0
    assert p["composite_percentiles"]["p90"] == 56.0


def test_profile_to_dict_includes_per_day_metric_fields():
    out = profile_to_dict(
        _stub_profile(), label="May 2026",
        since=date(2026, 5, 1), until=date(2026, 5, 31),
        ingest_stats=None, package_version="0.1.0",
    )
    day = out["profile"]["days"][0]
    assert day["day"] == "2026-05-14"
    assert day["weekday"] == "Thu"
    assert day["composite"] == 64.0
    assert day["codl_avg"] == 1.56
    assert day["codl_peak"] == 3
    assert "codl_peak_active" in day
    assert day["interruption_rate"] == 6.04
    assert day["closure_deficit"] == 0.40
    assert day["work_window_local"] == ["09:00", "18:00"]


def test_profile_to_dict_serializes_all_work_windows():
    out = profile_to_dict(
        _stub_profile(), label="May 2026",
        since=date(2026, 5, 1), until=date(2026, 5, 31),
        ingest_stats=None, package_version="0.1.0",
    )
    windows = out["profile"]["work_windows"]
    assert set(windows.keys()) == {"0", "1", "2", "3", "4", "5", "6"}
    assert windows["0"]["weekday"] == "Mon"
    assert windows["6"]["weekday"] == "Sun"
    assert windows["3"]["start"] == "09:00"
    assert windows["3"]["end"] == "18:00"


def test_profile_to_dict_handles_missing_ingest_stats():
    out = profile_to_dict(
        _stub_profile(), label="May 2026",
        since=date(2026, 5, 1), until=date(2026, 5, 31),
        ingest_stats=None, package_version="0.1.0",
    )
    assert out["ingest_stats"] is None


def test_profile_to_dict_serializes_ingest_stats_when_present():
    stats = AggregateStats()
    stats.ingest.files_kept = 127
    stats.ingest.events_emitted = 37560
    stats.ingest.lines_skipped_malformed = 0
    stats.days_in_window = 365
    stats.days_with_activity = 19
    stats.cache_hits = 18
    stats.cache_misses = 0
    out = profile_to_dict(
        _stub_profile(), label="2026",
        since=date(2026, 1, 1), until=date(2026, 12, 31),
        ingest_stats=stats, package_version="0.1.0",
    )
    s = out["ingest_stats"]
    assert s["files_kept"] == 127
    assert s["events_emitted"] == 37560
    assert s["days_with_activity"] == 19
    assert s["cache_hits"] == 18


def test_main_writes_json_sibling_to_output_path(tmp_path, monkeypatch):
    """The CLI end-to-end emits the JSON sibling alongside the HTML."""
    import json
    import ai_code_cognitive_stress.pipeline.ingest as ingest_mod
    from ai_code_cognitive_stress.__main__ import main

    monkeypatch.setattr(
        ingest_mod, "CLAUDE_PROJECTS_DIR", tmp_path / "no-projects",
    )
    out = tmp_path / "report.html"
    rc = main(["--month", "2026-05", "--output", str(out)])
    assert rc == 0
    sibling = out.with_suffix(".json")
    assert sibling.exists()
    payload = json.loads(sibling.read_text(encoding="utf-8"))
    assert payload["schema"] == "ai-code-cognitive-stress.profile.v1"
    assert payload["window"] == {"since": "2026-05-01", "until": "2026-05-31"}
