"""Tests for Claude Code session ingestion."""

from __future__ import annotations

import json
import os
import time as _time
from datetime import date
from pathlib import Path

from ai_code_cognitive_stress.pipeline.ingest import (
    AssistantMessageEvent,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
    collect,
)


# ---------------------------------------------------------------------------
# fixtures

def _project(tmp_path: Path, name: str = "-home-test-proj") -> Path:
    p = tmp_path / "projects" / name
    p.mkdir(parents=True)
    return p


def _projects_root(tmp_path: Path) -> Path:
    return tmp_path / "projects"


def _session_record(
    rec_type: str,
    timestamp: str,
    session_id: str = "sess-1",
    content_blocks: list[dict] | None = None,
    branch: str = "main",
) -> dict:
    return {
        "type": rec_type,
        "timestamp": timestamp,
        "sessionId": session_id,
        "uuid": f"uuid-{rec_type}-{timestamp}",
        "cwd": "/home/test/proj",
        "gitBranch": branch,
        "message": {
            "role": rec_type,
            "content": content_blocks
            if content_blocks is not None
            else [{"type": "text", "text": "hi"}],
        },
    }


def _write_session(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Happy path

def test_missing_projects_dir_returns_empty(tmp_path):
    events, stats = collect(
        date(2026, 1, 1), date(2026, 12, 31),
        projects_dir=tmp_path / "no-such-dir",
    )
    assert events == []
    assert stats.files_scanned == 0


def test_empty_projects_dir_returns_empty(tmp_path):
    (tmp_path / "projects").mkdir()
    events, stats = collect(
        date(2026, 1, 1), date(2026, 12, 31),
        projects_dir=tmp_path / "projects",
    )
    assert events == []
    assert stats.files_scanned == 0


def test_user_and_assistant_messages_emit_typed_events(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
        _session_record("assistant", "2026-05-15T10:00:05.000Z"),
    ])
    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert isinstance(events[0], UserMessageEvent)
    assert isinstance(events[1], AssistantMessageEvent)
    assert stats.events_emitted == 2
    assert stats.files_kept == 1


def test_events_sorted_by_timestamp_across_files(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "a.jsonl", [
        _session_record("assistant", "2026-05-15T11:00:00.000Z"),
    ])
    _write_session(proj / "b.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert [e.ts.isoformat() for e in events] == [
        "2026-05-15T10:00:00+00:00",
        "2026-05-15T11:00:00+00:00",
    ]


def test_stream_id_taken_from_session_id_field(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z", session_id="my-sess"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events[0].stream_id == "my-sess"


def test_project_token_is_directory_name(tmp_path):
    proj = _project(tmp_path, name="-home-test-my-proj")
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events[0].project == "-home-test-my-proj"


def test_event_carries_uuid_typed(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events[0].uuid.startswith("uuid-user-")


def test_kind_class_var_matches_event_type():
    assert UserMessageEvent.KIND == "user_msg"
    assert AssistantMessageEvent.KIND == "assistant_msg"
    assert ToolUseEvent.KIND == "tool_use"
    assert ToolResultEvent.KIND == "tool_result"


def test_timestamps_are_timezone_aware_utc(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events[0].ts.tzinfo is not None
    assert events[0].ts.utcoffset().total_seconds() == 0


def test_non_z_timestamp_with_offset_is_normalised_to_utc(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        # 2026-05-15T12:00:00+02:00 == 2026-05-15T10:00:00Z
        _session_record("user", "2026-05-15T12:00:00+02:00"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events[0].ts.isoformat() == "2026-05-15T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Tool-use / tool-result extraction

def test_tool_use_emitted_from_assistant_content(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record(
            "assistant",
            "2026-05-15T10:00:00.000Z",
            content_blocks=[
                {"type": "text", "text": "I'll read a file."},
                {"type": "tool_use", "id": "use-1", "name": "Read", "input": {}},
            ],
        ),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    uses = [e for e in events if isinstance(e, ToolUseEvent)]
    assert len(uses) == 1
    assert uses[0].tool_name == "Read"
    assert uses[0].tool_use_id == "use-1"


def test_multiple_tool_uses_in_one_assistant_record(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record(
            "assistant",
            "2026-05-15T10:00:00.000Z",
            content_blocks=[
                {"type": "tool_use", "id": "u1", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "u2", "name": "Bash", "input": {}},
            ],
        ),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    uses = [e for e in events if isinstance(e, ToolUseEvent)]
    assert [u.tool_name for u in uses] == ["Read", "Bash"]


def test_tool_result_emitted_from_user_content(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record(
            "user",
            "2026-05-15T10:00:00.000Z",
            content_blocks=[
                {"type": "tool_result", "tool_use_id": "u1",
                 "content": "ok", "is_error": False},
            ],
        ),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(results) == 1
    assert results[0].tool_use_id == "u1"
    assert results[0].is_error is False


def test_tool_result_carries_is_error_true(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record(
            "user",
            "2026-05-15T10:00:00.000Z",
            content_blocks=[
                {"type": "tool_result", "tool_use_id": "u1", "is_error": True},
            ],
        ),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results[0].is_error is True


# ---------------------------------------------------------------------------
# Filtering

def test_records_outside_window_are_filtered(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-04-30T23:00:00.000Z"),
        _session_record("user", "2026-05-15T10:00:00.000Z"),
        _session_record("user", "2026-06-01T10:00:00.000Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert [e.ts.isoformat() for e in events] == [
        "2026-05-15T10:00:00+00:00",
    ]


def test_window_is_inclusive_at_utc_day_boundaries(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-05-01T00:00:00.000Z"),
        _session_record("assistant", "2026-05-31T23:59:59.999999Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert len(events) == 2


def test_single_day_window_keeps_only_that_day(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        _session_record("user", "2026-05-14T23:59:59Z"),
        _session_record("user", "2026-05-15T00:00:00Z"),
        _session_record("user", "2026-05-15T23:59:59Z"),
        _session_record("user", "2026-05-16T00:00:00Z"),
    ])
    events, _ = collect(
        date(2026, 5, 15), date(2026, 5, 15),
        projects_dir=_projects_root(tmp_path),
    )
    assert len(events) == 2


def test_files_with_mtime_before_window_are_skipped(tmp_path):
    """Files whose mtime is strictly before `since` cannot contain in-window
    events because mtime tracks the last write."""
    proj = _project(tmp_path)
    old = proj / "old.jsonl"
    _write_session(old, [_session_record("user", "2025-01-01T00:00:00.000Z")])
    backdate = _time.mktime((2025, 1, 1, 0, 0, 0, 0, 0, -1))
    os.utime(old, (backdate, backdate))

    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events == []
    assert stats.files_scanned == 0


def test_subagent_files_are_excluded(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "main.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    sub_dir = proj / "sess-1" / "subagents"
    sub_dir.mkdir(parents=True)
    _write_session(sub_dir / "agent-x.jsonl", [
        _session_record("user", "2026-05-15T10:30:00.000Z", session_id="sub"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert all(e.stream_id != "sub" for e in events)
    assert len(events) == 1


def test_non_dash_dirs_are_ignored(tmp_path):
    # Real project dirs are named like `-home-...` (encoded cwd).
    # A directory with any other name should be skipped.
    stray = tmp_path / "projects" / "not-a-project"
    stray.mkdir(parents=True)
    _write_session(stray / "sess.jsonl", [
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events == []


# ---------------------------------------------------------------------------
# Robustness — malformed input

def test_malformed_json_lines_are_counted_and_skipped(tmp_path):
    proj = _project(tmp_path)
    path = proj / "sess.jsonl"
    body = (
        json.dumps(_session_record("user", "2026-05-15T10:00:00.000Z")) + "\n"
        + "{not json\n"
        + json.dumps(_session_record("assistant", "2026-05-15T10:00:05.000Z")) + "\n"
    )
    path.write_text(body, encoding="utf-8")
    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert len(events) == 2
    assert stats.lines_skipped_malformed == 1


def test_blank_lines_are_ignored_silently(tmp_path):
    proj = _project(tmp_path)
    path = proj / "sess.jsonl"
    body = (
        "\n"
        + json.dumps(_session_record("user", "2026-05-15T10:00:00.000Z")) + "\n"
        + "\n\n"
    )
    path.write_text(body, encoding="utf-8")
    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert len(events) == 1
    assert stats.lines_skipped_malformed == 0


def test_metadata_record_types_are_skipped(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        {"type": "mode", "mode": "normal", "sessionId": "sess-1"},
        {"type": "last-prompt", "leafUuid": "x", "sessionId": "sess-1"},
        {"type": "ai-title", "sessionId": "sess-1"},
        {"type": "attachment", "sessionId": "sess-1"},
        {"type": "permission-mode", "sessionId": "sess-1"},
        {"type": "system", "sessionId": "sess-1"},
        {"type": "file-history-snapshot", "sessionId": "sess-1"},
        {"type": "queue-operation", "sessionId": "sess-1"},
        _session_record("user", "2026-05-15T10:00:00.000Z"),
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert len(events) == 1
    assert isinstance(events[0], UserMessageEvent)


def test_record_without_timestamp_is_counted(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        {"type": "user", "sessionId": "sess-1"},
    ])
    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events == []
    assert stats.lines_skipped_no_timestamp == 1


def test_record_with_unparseable_timestamp_is_counted(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        {"type": "user", "sessionId": "s", "timestamp": "not a real timestamp"},
    ])
    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events == []
    assert stats.lines_skipped_no_timestamp == 1


def test_record_with_missing_session_id_is_counted(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        {"type": "user", "timestamp": "2026-05-15T10:00:00Z"},
    ])
    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert events == []
    assert stats.lines_skipped_no_timestamp == 1


def test_assistant_with_non_list_content_yields_only_msg(tmp_path):
    proj = _project(tmp_path)
    record = _session_record("assistant", "2026-05-15T10:00:00.000Z")
    record["message"]["content"] = "just a string"  # type: ignore[index]
    _write_session(proj / "sess.jsonl", [record])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert len(events) == 1
    assert isinstance(events[0], AssistantMessageEvent)


def test_record_without_message_dict_still_emits_top_level(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "sess.jsonl", [
        {"type": "user", "sessionId": "s",
         "timestamp": "2026-05-15T10:00:00.000Z"},
    ])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert len(events) == 1
    assert isinstance(events[0], UserMessageEvent)


def test_content_block_thats_not_a_dict_is_skipped(tmp_path):
    proj = _project(tmp_path)
    record = _session_record("assistant", "2026-05-15T10:00:00.000Z")
    record["message"]["content"] = [
        "stringy block",
        {"type": "tool_use", "id": "u1", "name": "Read"},
        42,
    ]
    _write_session(proj / "sess.jsonl", [record])
    events, _ = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    uses = [e for e in events if isinstance(e, ToolUseEvent)]
    assert [u.tool_name for u in uses] == ["Read"]


def test_stats_files_kept_counts_only_files_with_in_window_events(tmp_path):
    proj = _project(tmp_path)
    _write_session(proj / "in.jsonl", [
        _session_record("user", "2026-05-15T10:00:00Z"),
    ])
    # The mtime of the freshly-written file is "now", so this file IS scanned
    # but its only event is out of window — should not be counted in files_kept.
    out = proj / "out.jsonl"
    _write_session(out, [
        _session_record("user", "2026-06-15T10:00:00Z"),
    ])
    events, stats = collect(
        date(2026, 5, 1), date(2026, 5, 31),
        projects_dir=_projects_root(tmp_path),
    )
    assert stats.files_scanned == 2
    assert stats.files_kept == 1
    assert len(events) == 1


# NOTE: ingest is covered entirely by hermetic tests above (synthetic fixtures
# under tmp_path). There is deliberately no test that reads the real
# ~/.claude/projects dataset — specs must be predictable and must not depend on
# whatever happens to be on the machine running them.
