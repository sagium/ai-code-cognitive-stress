"""Tests for the per-day aggregate + disk cache layer."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stress_levels.aggregate import (
    CACHE_SCHEMA_VERSION,
    DayAggregate,
    StreamDayActivity,
    _aggregate_events,
    _aggregate_from_dict,
    _aggregate_to_dict,
    _closure_fingerprint,
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
from stress_levels.sources.base import ClosureEvent
from stress_levels.sources.git_closure import GitRepoClosureSource


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


class _FakeClosureSource:
    """In-memory ClosureEventSource double for orchestration tests (no git)."""

    name = "fake-closure"

    def __init__(self, events, repos=("/fake/repo",)):
        self._events = list(events)
        self.repos = list(repos)

    def is_available(self):
        return True

    def collect(self, since, until, stats):
        for ev in self._events:
            if since <= ev.ts <= until:
                yield ev


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
# Closure events — tri-state on the aggregate

def test_closure_events_default_none_when_not_supplied():
    """No closure source wired → closure_events is None (distinct from [])."""
    agg = _aggregate_events(date(2026, 5, 15), [
        UserMessageEvent(ts=_utc(2026, 5, 15, 9), stream_id="s", project="p"),
    ])
    assert agg.closure_events is None
    assert agg.closure_count == 0


def test_closure_events_empty_list_preserved_as_empty_tuple():
    """A wired source that emitted nothing → () (not None) so the metrics
    layer knows real closure was in play."""
    agg = _aggregate_events(date(2026, 5, 15), [
        UserMessageEvent(ts=_utc(2026, 5, 15, 9), stream_id="s", project="p"),
    ], closures=[])
    assert agg.closure_events == ()
    assert agg.closure_count == 0


def test_closure_events_attached_and_sorted():
    closures = [
        ClosureEvent(ts=_utc(2026, 5, 15, 14), kind="merge", repo="r"),
        ClosureEvent(ts=_utc(2026, 5, 15, 10), kind="commit", repo="r"),
    ]
    agg = _aggregate_events(date(2026, 5, 15), [
        UserMessageEvent(ts=_utc(2026, 5, 15, 9), stream_id="s", project="p"),
    ], closures=closures)
    assert agg.closure_count == 2
    # Sorted by timestamp ascending.
    assert [c.ts for c in agg.closure_events] == [
        _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
    ]


def test_closure_events_on_empty_events_day():
    """An empty-event day still carries closures if a source emitted them
    (a commit on a day with no agent sessions)."""
    closures = [ClosureEvent(ts=_utc(2026, 5, 15, 10), kind="commit", repo="r")]
    agg = _aggregate_events(date(2026, 5, 15), [], closures=closures)
    assert agg.streams == ()
    assert agg.closure_count == 1


# ---------------------------------------------------------------------------
# Closure fingerprint (cache-key component)

def test_closure_fingerprint_empty_for_no_sources():
    assert _closure_fingerprint([]) == ""


def test_closure_fingerprint_changes_with_repos():
    a = _closure_fingerprint([_FakeClosureSource([], repos=["/a"])])
    b = _closure_fingerprint([_FakeClosureSource([], repos=["/b"])])
    assert a != b
    assert a == _closure_fingerprint([_FakeClosureSource([], repos=["/a"])])


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
    """A cache file written under a different namespace is ignored.

    Under the new scheme the cache_namespace field (stored in the JSON and
    embedded in the path) is the single gate.  A file planted under a
    bogus namespace dir will never be found by _cache_path (which uses
    CACHE_NAMESPACE), so it is irrelevant — we get a miss.
    """
    from stress_levels.aggregate import CACHE_NAMESPACE
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"
    # Plant a cache file under a stale namespace dir
    target = cache_dir / "v-bogus" / "2026" / "2026-05-15.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "cache_namespace": "v-bogus",
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
    # We should have missed (cache lookup uses CACHE_NAMESPACE path, so the
    # planted bogus file is at the wrong path and is irrelevant).
    assert stats.cache_misses == 1


def test_cache_invalidates_on_namespace_mismatch(tmp_path):
    """If a cache file's cache_namespace doesn't match CACHE_NAMESPACE, ignore it.

    Under the new scheme, cache_namespace is the single gate (replacing the
    old schema_version + package_version pair).  Altering it in the stored
    JSON causes a miss on the next read.
    """
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
    # Manually rewrite the cache file with a stale cache_namespace
    cache_files = list(cache_dir.rglob("*.json"))
    assert cache_files
    raw = json.loads(cache_files[0].read_text(encoding="utf-8"))
    raw["cache_namespace"] = "v4-000000000000"
    cache_files[0].write_text(json.dumps(raw), encoding="utf-8")

    # Second run — should miss because cache_namespace mismatches
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
    from stress_levels.aggregate import CACHE_NAMESPACE
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"
    target = cache_dir / CACHE_NAMESPACE / "2026" / "2026-05-15.json"
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
    assert json.loads(target.read_text(encoding="utf-8"))["cache_namespace"] == CACHE_NAMESPACE


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


# ---------------------------------------------------------------------------
# Closure-event collection + cache round-trip through get_day_aggregates

def test_closure_sources_fold_events_into_aggregate(tmp_path):
    """A wired closure source attaches that day's commits/merges to the
    DayAggregate; days with no closures get () (not None) since a source ran."""
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    closure = ClosureEvent(ts=_utc(2026, 5, 15, 11), kind="commit", repo="r")
    aggs, _ = get_day_aggregates(
        date(2026, 5, 14), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 16),
        local_tz=timezone.utc,
        closure_sources=[_FakeClosureSource([closure])],
    )
    assert aggs[date(2026, 5, 15)].closure_count == 1
    # 5-14 had a source but no closures → () not None.
    assert aggs[date(2026, 5, 14)].closure_events == ()


def test_no_closure_sources_leaves_closure_events_none(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    aggs, _ = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 16),
        local_tz=timezone.utc,
    )
    assert aggs[date(2026, 5, 15)].closure_events is None


def test_cache_roundtrip_preserves_closures(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    closure = ClosureEvent(ts=_utc(2026, 5, 15, 11), kind="merge", repo="r",
                           branch="main", title="Merge x")
    common = dict(
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 16),
        local_tz=timezone.utc,
        closure_sources=[_FakeClosureSource([closure])],
    )
    aggs1, s1 = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    assert s1.cache_misses == 1
    aggs2, s2 = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **common)
    assert s2.cache_hits == 1
    assert aggs1[date(2026, 5, 15)].closure_events == aggs2[date(2026, 5, 15)].closure_events
    assert aggs2[date(2026, 5, 15)].closure_count == 1


def test_cache_invalidates_when_closure_wiring_added(tmp_path):
    """A day cached without a closure source must not be served once a
    closure source is wired (the deficit definition would silently differ)."""
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    base = dict(
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 16),
        local_tz=timezone.utc,
    )
    # First run: no closure source.
    _, s1 = get_day_aggregates(date(2026, 5, 15), date(2026, 5, 15), **base)
    assert s1.cache_misses == 1
    # Second run: add a closure source → cache key moves → miss again.
    closure = ClosureEvent(ts=_utc(2026, 5, 15, 11), kind="commit", repo="r")
    _, s2 = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        closure_sources=[_FakeClosureSource([closure])], **base,
    )
    assert s2.cache_misses == 1
    assert s2.cache_hits == 0


def test_aggregate_dict_roundtrip_tristate():
    """_aggregate_to_dict/_from_dict preserve None vs () vs non-empty."""
    day = date(2026, 5, 15)
    # None → key omitted → restores None.
    none_d = _aggregate_to_dict(DayAggregate(day=day, closure_events=None))
    assert "closure_events" not in none_d
    assert _aggregate_from_dict(json.loads(json.dumps(none_d))).closure_events is None
    # () → key present, empty → restores ().
    empty_d = _aggregate_to_dict(DayAggregate(day=day, closure_events=()))
    assert empty_d["closure_events"] == []
    assert _aggregate_from_dict(json.loads(json.dumps(empty_d))).closure_events == ()
    # non-empty round-trips by value.
    ce = ClosureEvent(ts=_utc(2026, 5, 15, 10), kind="commit", repo="r",
                      branch="b", title="t")
    full_d = _aggregate_to_dict(DayAggregate(day=day, closure_events=(ce,)))
    restored = _aggregate_from_dict(json.loads(json.dumps(full_d))).closure_events
    assert restored == (ce,)


def test_real_git_closure_source_flows_through_aggregate(tmp_path):
    """End-to-end with a real GitRepoClosureSource (not the fake): a commit
    in the scanned repo lands as a closure on its day's aggregate."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
           "GIT_AUTHOR_DATE": "2026-05-15T11:00:00+00:00",
           "GIT_COMMITTER_DATE": "2026-05-15T11:00:00+00:00",
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "done"],
                   check=True, env=env)

    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    aggs, _ = get_day_aggregates(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 16),
        local_tz=timezone.utc,
        closure_sources=[GitRepoClosureSource(repos=[repo])],
    )
    assert aggs[date(2026, 5, 15)].closure_count == 1
    assert aggs[date(2026, 5, 15)].closure_events[0].kind == "commit"


# ---------------------------------------------------------------------------
# Self-versioning cache: _module_fingerprint, CACHE_NAMESPACE, _cache_path

def test_module_fingerprint_returns_12_lowercase_hex():
    from stress_levels.aggregate import _module_fingerprint
    fp = _module_fingerprint()
    assert len(fp) == 12
    assert fp == fp.lower()
    assert all(c in "0123456789abcdef" for c in fp)


def test_module_fingerprint_is_deterministic():
    from stress_levels.aggregate import _module_fingerprint
    assert _module_fingerprint() == _module_fingerprint()


def test_cache_namespace_format():
    import re
    from stress_levels.aggregate import CACHE_NAMESPACE
    assert re.match(r"^v\d+-[0-9a-f]{12}$", CACHE_NAMESPACE), (
        f"CACHE_NAMESPACE {CACHE_NAMESPACE!r} does not match expected pattern"
    )


def test_cache_path_contains_namespace(tmp_path):
    from stress_levels.aggregate import CACHE_NAMESPACE, _cache_path
    p = _cache_path(tmp_path / "cache", date(2026, 5, 15))
    # CACHE_NAMESPACE must appear as a direct segment of the path
    assert CACHE_NAMESPACE in p.parts


# ---------------------------------------------------------------------------
# _prune_stale_cache_namespaces

def test_prune_removes_stale_namespaces_keeps_current(tmp_path):
    """Old bare and namespaced dirs are removed; current namespace and
    non-matching entries (files, unrelated dirs) are preserved."""
    import re
    from stress_levels.aggregate import CACHE_NAMESPACE, _prune_stale_cache_namespaces

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # Create stale dirs matching the pattern
    stale = ["v3", "v4", "v4-deadbeefcafe"]
    for name in stale:
        (cache_dir / name).mkdir()

    # Create current namespace dir — must be preserved
    (cache_dir / CACHE_NAMESPACE).mkdir()

    # Create a non-matching dir — must be preserved
    (cache_dir / "notes").mkdir()

    # Create a stray file at the top level — must be preserved
    (cache_dir / "stray.txt").write_text("keep me", encoding="utf-8")

    _prune_stale_cache_namespaces(cache_dir, CACHE_NAMESPACE)

    remaining = {p.name for p in cache_dir.iterdir()}
    assert CACHE_NAMESPACE in remaining, "Current namespace dir must survive"
    assert "notes" in remaining, "Non-matching dir must survive"
    assert (cache_dir / "stray.txt").exists(), "Stray file must survive"
    for name in stale:
        assert name not in remaining, f"Stale dir {name!r} should have been pruned"


def test_prune_is_noop_when_cache_dir_absent(tmp_path):
    """_prune_stale_cache_namespaces silently does nothing when cache_dir
    does not yet exist — no exception raised."""
    from stress_levels.aggregate import CACHE_NAMESPACE, _prune_stale_cache_namespaces
    _prune_stale_cache_namespaces(tmp_path / "nonexistent", CACHE_NAMESPACE)


# ---------------------------------------------------------------------------
# Round-trip and cross-namespace rejection

def test_write_then_read_cache_roundtrip(tmp_path):
    """_write_cache followed by _read_cache returns the original aggregate."""
    from stress_levels.aggregate import _read_cache, _write_cache
    cache_dir = tmp_path / "cache"
    day = date(2026, 5, 15)
    cache_key = "testkey12345678"
    agg = DayAggregate(day=day, streams=(), peak_concurrent_streams=0,
                       closure_events=None)
    _write_cache(cache_dir, day, cache_key, agg)
    result = _read_cache(cache_dir, day, cache_key)
    assert result is not None
    assert result.day == agg.day
    assert result.streams == agg.streams
    assert result.closure_events == agg.closure_events


def test_read_cache_rejects_wrong_namespace(tmp_path):
    """A JSON file whose cache_namespace differs from CACHE_NAMESPACE is rejected."""
    import json as _json
    from stress_levels.aggregate import (
        CACHE_NAMESPACE, _cache_path, _read_cache, _write_cache,
    )
    cache_dir = tmp_path / "cache"
    day = date(2026, 5, 15)
    cache_key = "testkey12345678"
    agg = DayAggregate(day=day, streams=(), peak_concurrent_streams=0,
                       closure_events=None)

    # Write a valid cache entry, then tamper with cache_namespace
    _write_cache(cache_dir, day, cache_key, agg)
    path = _cache_path(cache_dir, day)
    raw = _json.loads(path.read_text(encoding="utf-8"))
    raw["cache_namespace"] = "v4-000000000000"
    path.write_text(_json.dumps(raw), encoding="utf-8")

    assert _read_cache(cache_dir, day, cache_key) is None


# ---------------------------------------------------------------------------
# End-to-end: stale namespace dir pruned during get_day_aggregates

def test_get_day_aggregates_prunes_stale_v3_dir(tmp_path):
    """A pre-existing stale v3/ dir under cache_dir is removed after
    get_day_aggregates runs."""
    from stress_levels.aggregate import CACHE_NAMESPACE

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stale_dir = cache_dir / "v3"
    stale_dir.mkdir()

    get_day_aggregates(
        date(2026, 5, 1), date(2026, 5, 1),
        sources=[],  # no session source — empty aggregates
        cache_dir=cache_dir,
        now=_utc(2026, 5, 10),
        local_tz=timezone.utc,
    )

    assert not stale_dir.exists(), "v3/ should have been pruned"
    # Current namespace dir may or may not exist (no events → no cache written),
    # but the stale one is definitely gone.
