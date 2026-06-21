"""Tests for the durable per-day stats archive.

The cache layer (test_aggregate.py) is keyed on live source files + mtimes, so
it cannot serve a day whose session logs have been recycled. The archive fixes
that: a per-day store keyed by local day only, merged into the live computation
so recycled history is recovered rather than lost. These tests pin that
behaviour — full recycle recovery, partial-recycle merge, today-never-archived,
the disabled path, cache-wipe independence, and the per-stream merge rule.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

from ai_code_cognitive_stress.pipeline.aggregate import (
    DayAggregate,
    StreamDayActivity,
    _merge_day,
    get_day_aggregates,
)


# ---------------------------------------------------------------------------
# fixtures (mirrors test_aggregate.py)

def _project(tmp_path: Path, name: str = "-home-test-proj") -> Path:
    p = tmp_path / "projects" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_record(rec_type, ts, session_id="sess-1", content=None):
    return {
        "type": rec_type,
        "timestamp": ts,
        "sessionId": session_id,
        "uuid": f"u-{session_id}-{rec_type}-{ts}",
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


def _run(tmp_path, *, archive_dir, day=date(2026, 5, 14), now=_utc(2026, 5, 16)):
    """Run get_day_aggregates over a single past day with UTC bucketing."""
    return get_day_aggregates(
        day, day,
        projects_dir=tmp_path / "projects",
        cache_dir=tmp_path / "cache",
        archive_dir=archive_dir,
        now=now,
        local_tz=timezone.utc,
    )


# ---------------------------------------------------------------------------
# Integration: recovery after source-log recycling

def test_full_recycle_is_recovered_from_archive(tmp_path):
    proj = _project(tmp_path)
    sess = proj / "s.jsonl"
    _write_session(sess, [
        _session_record("user", "2026-05-14T09:00:00.000Z"),
        _session_record("assistant", "2026-05-14T09:00:05.000Z"),
    ])
    archive_dir = tmp_path / "data" / "archive"

    # Run 1 — observe the day and write it to the durable archive.
    aggs1, stats1 = _run(tmp_path, archive_dir=archive_dir)
    day = date(2026, 5, 14)
    assert aggs1[day].user_msg_count == 1
    assert aggs1[day].assistant_msg_count == 1
    assert stats1.archive_writes == 1
    assert list(archive_dir.rglob("*.json")), "archive file should exist"

    # The agent tool recycles the session log.
    sess.unlink()

    # Run 2 — live logs no longer carry the day; the archive must recover it.
    aggs2, stats2 = _run(tmp_path, archive_dir=archive_dir)
    assert aggs2[day].stream_count == 1
    assert aggs2[day].user_msg_count == 1
    assert aggs2[day].assistant_msg_count == 1
    assert stats2.archive_recovered_days == 1


def test_partial_recycle_merges_surviving_and_archived_streams(tmp_path):
    proj = _project(tmp_path)
    s1 = proj / "s1.jsonl"
    s2 = proj / "s2.jsonl"
    # Two overlapping sessions on the same day → peak concurrency of 2.
    _write_session(s1, [
        _session_record("user", "2026-05-14T09:00:00.000Z", session_id="sess-1"),
        _session_record("assistant", "2026-05-14T09:30:00.000Z", session_id="sess-1"),
    ])
    _write_session(s2, [
        _session_record("user", "2026-05-14T09:10:00.000Z", session_id="sess-2"),
        _session_record("assistant", "2026-05-14T09:40:00.000Z", session_id="sess-2"),
    ])
    archive_dir = tmp_path / "data" / "archive"
    day = date(2026, 5, 14)

    aggs1, _ = _run(tmp_path, archive_dir=archive_dir)
    assert aggs1[day].stream_count == 2
    assert aggs1[day].peak_concurrent_streams == 2

    # Only s2 gets recycled.
    s2.unlink()

    aggs2, stats2 = _run(tmp_path, archive_dir=archive_dir)
    # The merge keeps the surviving stream (live) AND the recycled one (archive).
    assert aggs2[day].stream_count == 2
    assert {s.stream_id for s in aggs2[day].streams} == {"sess-1", "sess-2"}
    # peak_concurrent is recomputed from the merged union, not just the survivor.
    assert aggs2[day].peak_concurrent_streams == 2
    assert stats2.archive_recovered_days == 1


def test_today_is_never_archived(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-15T09:00:00.000Z"),
    ])
    archive_dir = tmp_path / "data" / "archive"
    # now is still on 2026-05-15 UTC → that day is "today" (in flux).
    _run(tmp_path, archive_dir=archive_dir,
         day=date(2026, 5, 15), now=_utc(2026, 5, 15, 23))
    assert list(archive_dir.rglob("*.json")) == []


def test_archive_disabled_when_archive_dir_none(tmp_path):
    """archive_dir=None reproduces the cache-only layer exactly: a fully
    recycled day comes back empty and nothing is written to disk for it."""
    proj = _project(tmp_path)
    sess = proj / "s.jsonl"
    _write_session(sess, [
        _session_record("user", "2026-05-14T09:00:00.000Z"),
    ])
    _run(tmp_path, archive_dir=None)
    sess.unlink()
    aggs2, stats2 = _run(tmp_path, archive_dir=None)
    assert aggs2[date(2026, 5, 14)].stream_count == 0
    assert stats2.archive_recovered_days == 0
    assert not (tmp_path / "data").exists()


def test_archive_survives_cache_wipe_and_lives_apart_from_cache(tmp_path):
    proj = _project(tmp_path)
    sess = proj / "s.jsonl"
    _write_session(sess, [
        _session_record("user", "2026-05-14T09:00:00.000Z"),
        _session_record("assistant", "2026-05-14T09:05:00.000Z"),
    ])
    cache_dir = tmp_path / "cache"
    archive_dir = tmp_path / "data" / "archive"
    day = date(2026, 5, 14)

    _run(tmp_path, archive_dir=archive_dir)
    # Archive and cache are distinct stores in distinct trees.
    archive_files = list(archive_dir.rglob("*.json"))
    cache_files = list(cache_dir.rglob("*.json"))
    assert archive_files and cache_files
    assert not any(str(p).startswith(str(cache_dir)) for p in archive_files)

    # Simulate `~/.cache` being cleaned AND the source log recycled — only the
    # durable archive can serve the day now.
    shutil.rmtree(cache_dir)
    sess.unlink()

    aggs2, stats2 = _run(tmp_path, archive_dir=archive_dir)
    assert aggs2[day].user_msg_count == 1
    assert aggs2[day].assistant_msg_count == 1
    assert stats2.archive_recovered_days == 1


def test_steady_state_run_does_not_rewrite_archive(tmp_path):
    """An unchanged past day must not churn the archive on every run."""
    proj = _project(tmp_path)
    _write_session(proj / "s.jsonl", [
        _session_record("user", "2026-05-14T09:00:00.000Z"),
    ])
    archive_dir = tmp_path / "data" / "archive"
    _, stats1 = _run(tmp_path, archive_dir=archive_dir)
    assert stats1.archive_writes == 1
    _, stats2 = _run(tmp_path, archive_dir=archive_dir)
    assert stats2.archive_writes == 0
    assert stats2.archive_recovered_days == 0


# ---------------------------------------------------------------------------
# Unit: the per-stream merge rule (_merge_day)

def _sda(stream_id, project, first, last, u=0, a=0, tu=0, tr=0):
    return StreamDayActivity(
        stream_id=stream_id, project=project,
        first_ts=first, last_ts=last,
        user_msg_count=u, assistant_msg_count=a,
        tool_use_count=tu, tool_result_count=tr,
    )


DAY = date(2026, 5, 14)


def test_merge_none_archive_returns_live_unchanged():
    live = DayAggregate(day=DAY, streams=(_sda("x", "p", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 10), u=1),))
    assert _merge_day(DAY, None, live) is live


def test_merge_keeps_archived_only_stream():
    archived = DayAggregate(day=DAY, streams=(
        _sda("a", "p", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 10), u=2),
        _sda("b", "p", _utc(2026, 5, 14, 11), _utc(2026, 5, 14, 12), u=3),
    ))
    live = DayAggregate(day=DAY, streams=(
        _sda("a", "p", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 10), u=2),
    ))
    merged = _merge_day(DAY, archived, live)
    assert {s.stream_id for s in merged.streams} == {"a", "b"}


def test_merge_live_wins_when_richer():
    archived = DayAggregate(day=DAY, streams=(
        _sda("x", "old", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 10), u=2),
    ))
    live = DayAggregate(day=DAY, streams=(
        _sda("x", "new", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 11), u=5, a=3),
    ))
    merged = _merge_day(DAY, archived, live)
    (x,) = merged.streams
    assert x.project == "new" and x.user_msg_count == 5


def test_merge_archived_wins_when_live_truncated():
    """A compacted/truncated live read (fewer events) must not shrink the day."""
    archived = DayAggregate(day=DAY, streams=(
        _sda("x", "full", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 12), u=8, a=4),
    ))
    live = DayAggregate(day=DAY, streams=(
        _sda("x", "trunc", _utc(2026, 5, 14, 11), _utc(2026, 5, 14, 12), u=1),
    ))
    merged = _merge_day(DAY, archived, live)
    (x,) = merged.streams
    assert x.project == "full" and x.user_msg_count == 8


def test_merge_tie_prefers_live():
    archived = DayAggregate(day=DAY, streams=(
        _sda("x", "old", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 10), u=2),
    ))
    live = DayAggregate(day=DAY, streams=(
        _sda("x", "new", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 10), u=2),
    ))
    merged = _merge_day(DAY, archived, live)
    assert merged.streams[0].project == "new"


def test_merge_recomputes_peak_concurrent_from_union():
    # archived stream A and live stream B overlap → peak 2 after merge, even
    # though each side alone has peak 1.
    archived = DayAggregate(
        day=DAY,
        streams=(_sda("A", "p", _utc(2026, 5, 14, 9), _utc(2026, 5, 14, 11), u=1),),
        peak_concurrent_streams=1,
    )
    live = DayAggregate(
        day=DAY,
        streams=(_sda("B", "p", _utc(2026, 5, 14, 10), _utc(2026, 5, 14, 12), u=1),),
        peak_concurrent_streams=1,
    )
    merged = _merge_day(DAY, archived, live)
    assert merged.stream_count == 2
    assert merged.peak_concurrent_streams == 2
