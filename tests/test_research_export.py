"""Tests for the anonymized research-data export."""

from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timezone
from types import SimpleNamespace

from stress_levels.aggregate import AggregateStats, DayAggregate, StreamDayActivity
from stress_levels.metrics import DayMetrics, StressProfile, WorkWindow
from stress_levels.research_export import (
    CONSENT_TEXT,
    CONSENT_VERSION,
    SCHEMA,
    build_research_export,
    consent_satisfied,
)

# A clearly-identifying timezone name that must never survive into the export.
_TZ_NAME = "Europe/Brussels"
_ACTIVE_DAY = date(2026, 5, 14)  # a Thursday


def _stub_profile() -> StressProfile:
    days = {
        _ACTIVE_DAY: DayMetrics(
            day=_ACTIVE_DAY, composite=64.0,
            codl_avg=1.56, codl_peak=3, codl_peak_active=2.1,
            interruption_rate=6.04, closure_deficit=0.40,
            off_hours_minutes=12,
            work_window_local=(time(9), time(18)),
        ),
        date(2026, 5, 15): DayMetrics(day=date(2026, 5, 15), composite=0.0),
    }
    windows = {
        wd: WorkWindow(weekday=wd, start=time(9), end=time(18))
        for wd in range(7)
    }
    return StressProfile(
        days=days, work_windows=windows, local_tz_name=_TZ_NAME,
        baseline_window_days=30, personal_optimum=1.5,
        composite_p50=45.0, composite_p75=52.0, composite_p90=56.0,
    )


def _export(**overrides) -> dict:
    defaults = dict(
        since=date(2026, 1, 1), until=date(2026, 12, 31),
        package_version="0.1.0", ingest_stats=None,
        rng=random.Random(1234), participant_id="fixedpid",
        generated_on=date(2026, 6, 1),
    )
    defaults.update(overrides)
    return build_research_export(_stub_profile(), **defaults)


# --- envelope --------------------------------------------------------------

def test_schema_version_and_stamps():
    out = _export()
    assert out["schema"] == SCHEMA
    assert out["package_version"] == "0.1.0"
    assert out["participant"] == "fixedpid"
    assert out["generated_on"] == "2026-06-01"
    assert out["window_days"] == 365


# --- anonymization ---------------------------------------------------------

def test_timezone_name_never_leaks():
    out = _export()
    # No tz field at the profile level...
    assert "local_tz" not in out["profile"]
    assert "tz_name" not in out["profile"]
    # ...and the name appears nowhere in the serialized payload.
    assert _TZ_NAME not in json.dumps(out)


def test_dates_shifted_by_whole_weeks_preserving_weekday():
    day = _export()["profile"]["days"][0]
    assert day["weekday"] == "Thu"
    shifted = date.fromisoformat(day["date"])
    # Shift is a whole number of weeks → same weekday, and not the real date.
    assert shifted.weekday() == _ACTIVE_DAY.weekday()
    assert (shifted - _ACTIVE_DAY).days % 7 == 0
    assert shifted != _ACTIVE_DAY  # Random(1234) yields a non-zero offset


def test_numeric_metrics_preserved_verbatim():
    day = _export()["profile"]["days"][0]
    assert day["composite"] == 64.0
    assert day["codl_avg"] == 1.56
    assert day["codl_peak"] == 3
    assert day["codl_peak_active"] == 2.1
    assert day["interruption_rate"] == 6.04
    assert day["closure_deficit"] == 0.40
    assert day["off_hours_minutes"] == 12


def test_work_window_clock_times_kept():
    day = _export()["profile"]["days"][0]
    assert day["work_window"] == ["09:00", "18:00"]


def test_only_active_days_included():
    p = _export()["profile"]
    assert p["active_day_count"] == 1
    assert len(p["days"]) == 1  # the composite==0 day is dropped


def test_profile_aggregates_and_work_windows_kept():
    p = _export()["profile"]
    assert p["personal_optimum"] == 1.5
    assert p["composite_percentiles"] == {"p50": 45.0, "p75": 52.0, "p90": 56.0}
    assert set(p["work_windows"].keys()) == {str(i) for i in range(7)}
    assert p["work_windows"]["3"] == {
        "weekday": "Thu", "start": "09:00", "end": "18:00", "is_default": False,
    }


def test_consent_record_embedded():
    c = _export()["consent"]
    assert c["acknowledged"] is True
    assert c["version"] == CONSENT_VERSION
    assert c["statement"] == CONSENT_TEXT
    assert c["acknowledged_on"] == "2026-06-01"


def test_no_debug_block_without_aggregates():
    # Back-compat: omitting aggregates yields the lean per-day rows only.
    day = _export()["profile"]["days"][0]
    assert "debug" not in day


def test_ingest_stats_optional_and_counts_only():
    assert _export(ingest_stats=None)["ingest_stats"] is None
    stats = AggregateStats()
    stats.ingest.files_kept = 42
    stats.ingest.events_emitted = 9001
    stats.days_in_window = 365
    stats.days_with_activity = 20
    s = _export(ingest_stats=stats)["ingest_stats"]
    assert s == {
        "files_kept": 42, "events_emitted": 9001,
        "days_in_window": 365, "days_with_activity": 20,
    }


def test_participant_id_is_random_per_export_by_default():
    a = build_research_export(
        _stub_profile(), since=date(2026, 1, 1), until=date(2026, 12, 31),
        package_version="0.1.0", ingest_stats=None,
    )
    b = build_research_export(
        _stub_profile(), since=date(2026, 1, 1), until=date(2026, 12, 31),
        package_version="0.1.0", ingest_stats=None,
    )
    assert a["participant"] != b["participant"]


# --- consent gate ----------------------------------------------------------

def test_consent_flag_satisfies_non_interactively():
    assert consent_satisfied(flag=True, isatty=False) is True


def test_consent_refused_when_non_interactive_without_flag():
    assert consent_satisfied(flag=False, isatty=False) is False


def test_consent_prompt_accepts_yes():
    assert consent_satisfied(
        flag=False, isatty=True, prompt_fn=lambda _p: "  YES \n",
    ) is True


def test_consent_prompt_rejects_anything_else():
    assert consent_satisfied(
        flag=False, isatty=True, prompt_fn=lambda _p: "no",
    ) is False


# --- debug detail (with aggregates) ---------------------------------------

_PROJECT_NAME = "secret-internal-project"
_STREAM_ID = "sess-deadbeef"


def _stub_aggregates() -> dict:
    def _ts(h, m=0):
        return datetime(2026, 5, 14, h, m, tzinfo=timezone.utc)
    s1 = StreamDayActivity(
        stream_id=_STREAM_ID, project=_PROJECT_NAME,
        first_ts=_ts(10), last_ts=_ts(12),
        user_msg_count=8, assistant_msg_count=9, tool_use_count=20,
        tool_result_count=18, tool_error_count=2,
        user_msg_timestamps=(_ts(10), _ts(11)),
        resume_gaps=((_ts(11, 30), 3600),),  # one 60-min in-window resume
    )
    s2 = StreamDayActivity(
        stream_id="sess-2", project="another-project",
        first_ts=_ts(11), last_ts=_ts(13),
        user_msg_count=3, assistant_msg_count=4, tool_use_count=6,
        tool_result_count=6, tool_error_count=0,
        user_msg_timestamps=(_ts(11, 30),),
    )
    agg = DayAggregate(
        day=_ACTIVE_DAY, streams=(s1, s2), peak_concurrent_streams=2,
    )
    return {_ACTIVE_DAY: agg}


def _export_with_debug() -> dict:
    return build_research_export(
        _stub_profile(), since=date(2026, 1, 1), until=date(2026, 12, 31),
        package_version="0.1.0", ingest_stats=None,
        aggregates=_stub_aggregates(),
        local_tz=timezone.utc,
        codl_cfg=SimpleNamespace(foreground_grace_minutes=5, background_weight=0.25),
        rng=random.Random(1234), participant_id="fixedpid",
        generated_on=date(2026, 6, 1),
    )


def test_debug_block_present_with_components():
    dbg = _export_with_debug()["profile"]["days"][0]["debug"]
    assert dbg["stream_count"] == 2
    assert dbg["peak_headcount"] == 2          # both streams overlap 11:00–12:00
    assert dbg["cross_stream_starts"] == 1     # s2 starts while s1 is active
    assert dbg["total_tool_errors"] == 2
    # Resumption components: s1's single 60-min in-window resume.
    assert dbg["resumes"] == 1
    assert dbg["resume_gap_minutes"] == [60.0]
    assert dbg["resumption_load"] == 0.125     # min(1, 60/120) / 4
    for key in ("peak_weighted", "work_hours", "in_window_tool_errors",
                "interruption_numerator", "off_hours_minutes",
                "hourly_concurrency", "sessions"):
        assert key in dbg


def test_consent_text_discloses_resumption_timing():
    """The export ships session-resumption timing, so the consent statement must
    disclose it — and must promise no identifying detail."""
    assert "resumption" in CONSENT_TEXT
    assert "git" not in CONSENT_TEXT  # the tool reads no git data
    assert "no source code" in CONSENT_TEXT or "no source" in CONSENT_TEXT
    assert "repository or branch names" in CONSENT_TEXT


def test_debug_resumption_reproduces_deficit():
    """The debug block carries the resumption components (resume count, per-resume
    gap minutes, severity-summed load) so the day's closure_deficit is
    reproducible from them."""
    from stress_levels.metrics import (
        build_profile, RESUME_FULL_DECAY_MINUTES, RESUMPTION_DAILY_CEILING,
    )

    def _ts(h):
        return datetime(2026, 5, 14, h, tzinfo=timezone.utc)
    streams = (
        # one 120-min (fully-cold) in-window resume, picked back up at 14:00
        StreamDayActivity(stream_id="s1", project="p",
                          first_ts=_ts(10), last_ts=_ts(16),
                          resume_gaps=((_ts(14), 7200),)),
        StreamDayActivity(stream_id="s2", project="p",
                          first_ts=_ts(11), last_ts=_ts(13)),
    )
    aggs = {_ACTIVE_DAY: DayAggregate(day=_ACTIVE_DAY, streams=streams,
                                      peak_concurrent_streams=2)}
    windows = {wd: WorkWindow(weekday=wd, start=time(9), end=time(18))
               for wd in range(7)}
    profile = build_profile(aggs)
    profile = StressProfile(days=profile.days, work_windows=windows)

    out = build_research_export(
        profile, since=date(2026, 1, 1), until=date(2026, 12, 31),
        package_version="t", ingest_stats=None, aggregates=aggs,
        local_tz=timezone.utc,
        codl_cfg=SimpleNamespace(foreground_grace_minutes=5, background_weight=0.25),
        rng=random.Random(1), participant_id="pid", generated_on=date(2026, 6, 1),
    )
    day = out["profile"]["days"][0]
    dbg = day["debug"]
    assert dbg["resumes"] == 1
    assert dbg["resume_gap_minutes"] == [120.0]
    assert dbg["resumption_load"] == 0.25  # min(1, 120/120) / 4
    # Deficit reconstructs from the debug gap minutes.
    reconstructed = round(min(
        1.0,
        sum(min(1.0, g / RESUME_FULL_DECAY_MINUTES) for g in dbg["resume_gap_minutes"])
        / RESUMPTION_DAILY_CEILING,
    ), 3)
    assert day["closure_deficit"] == reconstructed


def test_debug_sessions_are_anonymized():
    dbg = _export_with_debug()["profile"]["days"][0]["debug"]
    assert len(dbg["sessions"]) == 2
    row = dbg["sessions"][0]
    assert set(row.keys()) == {
        "start_hour", "duration_min", "user_msgs", "assistant_msgs",
        "tool_uses", "tool_results", "tool_errors",
    }
    # counts preserved, identifiers absent
    assert row["tool_errors"] == 2
    assert row["duration_min"] == 120
    assert "project" not in row
    assert "stream_id" not in row and "first_ts" not in row


def test_debug_hourly_concurrency_is_sparse_and_keyed_by_hour():
    dbg = _export_with_debug()["profile"]["days"][0]["debug"]
    hc = dbg["hourly_concurrency"]
    assert hc  # non-empty
    assert all(0 <= int(h) <= 23 for h in hc)
    assert all(v > 0 for v in hc.values())  # only active hours emitted


def test_debug_leaks_no_project_or_stream_id():
    dump = json.dumps(_export_with_debug())
    for token in (_PROJECT_NAME, _STREAM_ID, "another-project"):
        assert token not in dump


# --- CLI integration -------------------------------------------------------

def _empty_projects(monkeypatch, tmp_path):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(
        ingest_mod, "CLAUDE_PROJECTS_DIR", tmp_path / "no-projects",
    )


def test_cli_refuses_export_without_consent(tmp_path, monkeypatch, capsys):
    from stress_levels.__main__ import main
    _empty_projects(monkeypatch, tmp_path)
    out = tmp_path / "r.json"
    # pytest runs non-interactively (isatty False); no --i-consent → refused.
    rc = main(["--export-research", str(out), "--year", "2026"])
    assert rc == 1
    assert not out.exists()
    assert "consent not given" in capsys.readouterr().err


def test_cli_writes_anonymized_export_with_consent(tmp_path, monkeypatch):
    from stress_levels.__main__ import main
    _empty_projects(monkeypatch, tmp_path)
    out = tmp_path / "r.json"
    rc = main(["--export-research", str(out), "--i-consent", "--year", "2026"])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA
    assert payload["consent"]["acknowledged"] is True
    assert "local_tz" not in payload["profile"]
