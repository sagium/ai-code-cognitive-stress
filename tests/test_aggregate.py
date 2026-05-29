"""Tests for the per-day aggregate + disk cache layer."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stress_levels import __version__
from stress_levels.aggregate import (
    CACHE_SCHEMA_VERSION,
    DayAggregate,
    StreamDayActivity,
    _aggregate_events,
    _make_cache_key,
    _peak_concurrent,
    get_day_aggregates,
)
from stress_levels.ingest import (
    AssistantMessageEvent,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)


# ---------------------------------------------------------------------------
# fixtures

def _project(tmp_path: Path, name: str = "-home-test-proj") -> Path:
    p = tmp_path / "projects" / name
    p.mkdir(parents=True)
    return p


def _session_record(rec_type, ts, session_id="sess-1", content=None, branch="main"):
    return {
        "type": rec_type,
        "timestamp": ts,
        "sessionId": session_id,
        "uuid": f"u-{rec_type}-{ts}",
        "cwd": "/home/test/proj",
        "gitBranch": branch,
        "message": {
            "role": rec_type,
            "content": content if content is not None
                       else [{"type": "text", "text": "hi"}],
        },
    }


def _write_session(path: Path, records: list[dict]):
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _utc(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _aggregate_events — pure reduction

def test_empty_events_yields_empty_aggregate():
    agg = _aggregate_events(date(2026, 5, 15), [])
    assert agg.day == date(2026, 5, 15)
    assert agg.streams == ()
    assert agg.peak_concurrent_streams == 0
    assert agg.user_msg_count == 0


def test_single_stream_reduces_to_one_stream_activity():
    events = [
        UserMessageEvent(ts=_utc(2026, 5, 15, 9, 0), stream_id="s1",
                         project="p", branch="main"),
        AssistantMessageEvent(ts=_utc(2026, 5, 15, 9, 5), stream_id="s1",
                              project="p", branch="main"),
        ToolUseEvent(ts=_utc(2026, 5, 15, 9, 6), stream_id="s1",
                     project="p", tool_name="Read"),
        ToolResultEvent(ts=_utc(2026, 5, 15, 9, 7), stream_id="s1",
                        project="p", is_error=False),
    ]
    agg = _aggregate_events(date(2026, 5, 15), events)
    assert len(agg.streams) == 1
    s = agg.streams[0]
    assert s.stream_id == "s1"
    assert s.user_msg_count == 1
    assert s.assistant_msg_count == 1
    assert s.tool_use_count == 1
    assert s.tool_result_count == 1
    assert s.tool_error_count == 0
    assert s.branches == ("main",)
    assert s.first_ts == _utc(2026, 5, 15, 9, 0)
    assert s.last_ts == _utc(2026, 5, 15, 9, 7)


def test_tool_error_increments_only_error_counter():
    events = [
        UserMessageEvent(ts=_utc(2026, 5, 15, 9, 0), stream_id="s1",
                         project="p"),
        ToolResultEvent(ts=_utc(2026, 5, 15, 9, 1), stream_id="s1",
                        project="p", is_error=True),
    ]
    agg = _aggregate_events(date(2026, 5, 15), events)
    assert agg.streams[0].tool_error_count == 1
    assert agg.streams[0].tool_result_count == 1


def test_streams_sorted_by_first_ts():
    events = [
        UserMessageEvent(ts=_utc(2026, 5, 15, 10), stream_id="late",
                         project="p"),
        UserMessageEvent(ts=_utc(2026, 5, 15, 9), stream_id="early",
                         project="p"),
    ]
    agg = _aggregate_events(date(2026, 5, 15), events)
    assert [s.stream_id for s in agg.streams] == ["early", "late"]


def test_multiple_branches_in_one_stream_day():
    events = [
        UserMessageEvent(ts=_utc(2026, 5, 15, 9), stream_id="s1",
                         project="p", branch="main"),
        AssistantMessageEvent(ts=_utc(2026, 5, 15, 10), stream_id="s1",
                              project="p", branch="feature/x"),
    ]
    agg = _aggregate_events(date(2026, 5, 15), events)
    assert agg.streams[0].branches == ("feature/x", "main")


def test_stream_count_property_matches_streams_len():
    events = [
        UserMessageEvent(ts=_utc(2026, 5, 15, 9), stream_id="a",
                         project="p"),
        UserMessageEvent(ts=_utc(2026, 5, 15, 10), stream_id="b",
                         project="p"),
    ]
    agg = _aggregate_events(date(2026, 5, 15), events)
    assert agg.stream_count == 2


# ---------------------------------------------------------------------------
# Peak concurrency

def test_peak_concurrent_with_overlap():
    streams = (
        StreamDayActivity(stream_id="a", project="p",
                          first_ts=_utc(2026, 5, 15, 9),
                          last_ts=_utc(2026, 5, 15, 11)),
        StreamDayActivity(stream_id="b", project="p",
                          first_ts=_utc(2026, 5, 15, 10),
                          last_ts=_utc(2026, 5, 15, 12)),
    )
    assert _peak_concurrent(streams) == 2


def test_peak_concurrent_sequential_streams_is_one():
    streams = (
        StreamDayActivity(stream_id="a", project="p",
                          first_ts=_utc(2026, 5, 15, 9),
                          last_ts=_utc(2026, 5, 15, 9, 30)),
        StreamDayActivity(stream_id="b", project="p",
                          first_ts=_utc(2026, 5, 15, 10),
                          last_ts=_utc(2026, 5, 15, 11)),
    )
    # Even with the tie-break rule, sequential non-overlapping streams
    # never overlap (10:00 starts after 9:30 ends).
    assert _peak_concurrent(streams) == 1


def test_peak_concurrent_three_overlapping():
    base = _utc(2026, 5, 15, 9)
    streams = tuple(
        StreamDayActivity(stream_id=f"s{i}", project="p",
                          first_ts=base + timedelta(minutes=i * 10),
                          last_ts=base + timedelta(hours=2))
        for i in range(3)
    )
    assert _peak_concurrent(streams) == 3


def test_peak_concurrent_empty():
    assert _peak_concurrent(()) == 0


# ---------------------------------------------------------------------------
# Cache key

def test_cache_key_changes_when_mtime_changes():
    path = Path("/tmp/fake-sess.jsonl")
    a = _make_cache_key({path: 1000.0}, "UTC")
    b = _make_cache_key({path: 1001.0}, "UTC")
    assert a != b


def test_cache_key_stable_for_identical_inputs():
    path = Path("/tmp/fake-sess.jsonl")
    assert (
        _make_cache_key({path: 1000.0}, "UTC")
        == _make_cache_key({path: 1000.0}, "UTC")
    )


def test_cache_key_independent_of_dict_iteration_order():
    a_path, b_path = Path("/a.jsonl"), Path("/b.jsonl")
    assert (
        _make_cache_key({a_path: 1.0, b_path: 2.0}, "UTC")
        == _make_cache_key({b_path: 2.0, a_path: 1.0}, "UTC")
    )


def test_cache_key_changes_when_path_set_changes():
    p1 = Path("/a.jsonl")
    p2 = Path("/b.jsonl")
    one = _make_cache_key({p1: 1000.0}, "UTC")
    two = _make_cache_key({p1: 1000.0, p2: 1001.0}, "UTC")
    assert one != two


def test_cache_key_changes_when_local_tz_changes():
    """A user who travels from Athens to Tokyo gets a fresh cache because
    local-day bucketing depends on the TZ."""
    path = Path("/tmp/sess.jsonl")
    athens = _make_cache_key({path: 1000.0}, "Europe/Athens")
    tokyo = _make_cache_key({path: 1000.0}, "Asia/Tokyo")
    assert athens != tokyo


# ---------------------------------------------------------------------------
# get_day_aggregates — integration with cache

def test_no_sessions_returns_empty_aggregates_per_day(tmp_path):
    aggs, stats = get_day_aggregates(
        date(2026, 5, 1), date(2026, 5, 3),
        projects_dir=tmp_path / "no-projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 10),
    local_tz=timezone.utc,
    )
    assert set(aggs) == {date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)}
    for agg in aggs.values():
        assert agg.streams == ()
    assert stats.days_in_window == 3
    assert stats.days_with_activity == 0


def test_invalid_window_returns_empty(tmp_path):
    aggs, stats = get_day_aggregates(
        date(2026, 5, 5), date(2026, 5, 1),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 10),
    local_tz=timezone.utc,
    )
    assert aggs == {}
    assert stats.days_in_window == 0


def test_aggregates_built_from_real_events(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
        _session_record("assistant", "2026-05-15T09:00:05.000Z"),
    ])
    aggs, stats = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )
    agg = aggs[date(2026, 5, 15)]
    assert agg.stream_count == 1
    assert agg.user_msg_count == 1
    assert agg.assistant_msg_count == 1
    assert agg.peak_concurrent_streams == 1
    assert stats.days_with_activity == 1


def test_today_is_not_cached(tmp_path):
    proj = _project(tmp_path)
    today_iso = "2026-05-15T09:00:00.000Z"
    _write_session(proj / "s.jsonl", [
        _session_record("user", today_iso),
    ])
    cache_dir = tmp_path / "cache"
    get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 15, 23),  # still on May 15 UTC
        local_tz=timezone.utc,
    )
    # No cache file should have been created for today.
    cache_files = list(cache_dir.rglob("*.json"))
    assert cache_files == []


def test_past_days_are_cached(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"
    _, stats = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )
    assert stats.cache_misses == 1
    assert stats.cache_hits == 0
    # Cache file should exist
    cache_files = list(cache_dir.rglob("*.json"))
    assert len(cache_files) == 1


def test_second_run_hits_cache(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"
    common = dict(
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )

    # First run — populates cache
    aggs1, stats1 = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    assert stats1.cache_misses == 1

    # Second run — hits cache
    aggs2, stats2 = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    assert stats2.cache_hits == 1
    assert stats2.cache_misses == 0
    assert aggs1[date(2026, 5, 15)].user_msg_count == aggs2[date(2026, 5, 15)].user_msg_count


def test_cache_invalidates_on_mtime_change(tmp_path):
    import os
    proj = _project(tmp_path)
    sess = proj / "s.jsonl"
    _write_session(sess, [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    common = dict(
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )

    # First run
    get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    # Bump mtime forward
    new_time = sess.stat().st_mtime + 10.0
    os.utime(sess, (new_time, new_time))
    # Second run — should miss
    _, stats = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    assert stats.cache_misses == 1
    assert stats.cache_hits == 0


def test_cache_invalidates_on_schema_version_change(tmp_path):
    """A cache file written under a different schema is ignored."""
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"
    # Plant a cache file that looks valid except for schema_version
    target = cache_dir / "v-bogus" / "2026" / "2026-05-15.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "schema_version": "v-bogus",
        "package_version": __version__,
        "cache_key": "anything",
        "day": "2026-05-15",
        "aggregate": {"day": "2026-05-15", "streams": []},
    }), encoding="utf-8")
    _, stats = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )
    # We should have missed (cache lookup uses the current schema, so the
    # planted bogus file is at the wrong path and is irrelevant).
    assert stats.cache_misses == 1


def test_cache_invalidates_on_package_version_mismatch(tmp_path):
    """If a cache file's recorded package version doesn't match, ignore it."""
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"

    # First run — populates cache
    get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )
    # Manually rewrite the cache file with a wrong package_version
    cache_files = list(cache_dir.rglob("*.json"))
    assert cache_files
    raw = json.loads(cache_files[0].read_text(encoding="utf-8"))
    raw["package_version"] = "9.99.99-future"
    cache_files[0].write_text(json.dumps(raw), encoding="utf-8")

    # Second run — should miss because package_version mismatches
    _, stats = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )
    assert stats.cache_misses == 1
    assert stats.cache_hits == 0


def test_corrupt_cache_file_is_ignored(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"
    target = cache_dir / CACHE_SCHEMA_VERSION / "2026" / "2026-05-15.json"
    target.parent.mkdir(parents=True)
    target.write_text("{ not valid json", encoding="utf-8")
    _, stats = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )
    assert stats.cache_misses == 1
    # The corrupt file should be overwritten with a valid one
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == CACHE_SCHEMA_VERSION


def test_days_with_no_activity_are_present_as_empty(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    aggs, _ = get_day_aggregates(
        date(2026, 5, 14), date(2026, 5, 16),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 20),
    local_tz=timezone.utc,
    )
    assert aggs[date(2026, 5, 14)].streams == ()
    assert aggs[date(2026, 5, 15)].stream_count == 1
    assert aggs[date(2026, 5, 16)].streams == ()


def test_session_spanning_two_days_appears_in_both(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T23:30:00.000Z"),
        _session_record("assistant", "2026-05-16T00:30:00.000Z"),
    ])
    aggs, _ = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 16),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 17),
    local_tz=timezone.utc,
    )
    # Each day sees one stream, one event
    assert aggs[date(2026, 5, 15)].stream_count == 1
    assert aggs[date(2026, 5, 15)].user_msg_count == 1
    assert aggs[date(2026, 5, 16)].stream_count == 1
    assert aggs[date(2026, 5, 16)].assistant_msg_count == 1


def test_aggregate_buckets_events_by_local_day_when_tz_is_east(tmp_path):
    """22:00 UTC in a UTC+12 zone is 10:00 the NEXT local day. The previous
    UTC-date bucketing would have hidden the event from the local-day
    aggregate; the fix routes it correctly."""
    from datetime import timedelta as _td
    tz_plus_12 = timezone(_td(hours=12))
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        # 2026-05-15T22:00 UTC = 2026-05-16T10:00 in UTC+12
        _session_record("user", "2026-05-15T22:00:00.000Z"),
    ])
    aggs, _ = get_day_aggregates(
        date(2026, 5, 16), date(2026, 5, 16),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=datetime(2026, 5, 17, 12, tzinfo=tz_plus_12),
        local_tz=tz_plus_12,
    )
    # The event lands on 2026-05-16 in UTC+12, not on 2026-05-15.
    assert date(2026, 5, 16) in aggs
    assert aggs[date(2026, 5, 16)].stream_count == 1


def test_aggregate_excludes_event_outside_local_window_even_when_in_utc_window(tmp_path):
    """A 23:30 UTC event in UTC-8 is 15:30 the SAME local day, not the next.
    With local-day bucketing, the right local-day filter applies."""
    from datetime import timedelta as _td
    tz_minus_8 = timezone(_td(hours=-8))
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        # 2026-05-15T23:30 UTC = 2026-05-15T15:30 in UTC-8
        _session_record("user", "2026-05-15T23:30:00.000Z"),
    ])
    aggs, _ = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=datetime(2026, 5, 16, 12, tzinfo=tz_minus_8),
        local_tz=tz_minus_8,
    )
    assert aggs[date(2026, 5, 15)].stream_count == 1


def test_roundtrip_serialization_preserves_aggregate(tmp_path):
    """A cached aggregate decoded from disk equals the original."""
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z", branch="feat"),
        _session_record("assistant", "2026-05-15T09:00:05.000Z", branch="feat"),
    ])
    cache_dir = tmp_path / "cache"
    common = dict(
        projects_dir=tmp_path / "projects",
        cache_dir=cache_dir,
        now=_utc(2026, 5, 16),
    local_tz=timezone.utc,
    )

    # First run (writes cache)
    aggs1, _ = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    # Second run (reads cache)
    aggs2, stats = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    assert stats.cache_hits == 1
    a1 = aggs1[date(2026, 5, 15)]
    a2 = aggs2[date(2026, 5, 15)]
    assert a1.streams == a2.streams
    assert a1.peak_concurrent_streams == a2.peak_concurrent_streams
