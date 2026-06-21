"""Tests for CodexSessionSource."""

from __future__ import annotations

import json
import os
from datetime import datetime

from ai_code_cognitive_stress.adapters import CodexSessionSource
from ai_code_cognitive_stress.pipeline.ingest import (
    AssistantMessageEvent,
    IngestStats,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)

from .conftest import UTC, utc


# ---------------------------------------------------------------------------
# Backward-compat: old TypeScript codex-cli flat role format
# ---------------------------------------------------------------------------

def test_parses_role_based_jsonl(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "abc.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            {"role": "user", "content": "build a thing",
             "timestamp": "2026-05-15T10:00:00Z"},
            {"role": "assistant",
             "content": [{"type": "function_call",
                           "function": {"name": "shell"}, "id": "call-1"}],
             "timestamp": "2026-05-15T10:00:05Z"},
            {"role": "tool", "tool_use_id": "call-1",
             "content": "result", "timestamp": "2026-05-15T10:00:07Z"},
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert "UserMessageEvent" in kinds
    assert "AssistantMessageEvent" in kinds
    assert "ToolUseEvent" in kinds
    assert "ToolResultEvent" in kinds


def test_handles_string_content(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({
            "role": "assistant", "content": "just a string reply",
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert len(items) == 1
    assert isinstance(items[0][0], AssistantMessageEvent)


def test_missing_dir_returns_no_events(tmp_path):
    src = CodexSessionSource(sessions_dir=tmp_path / "nope")
    assert list(src.collect(utc(2026, 1, 1), utc(2026, 12, 31), IngestStats())) == []
    assert src.is_available() is False


def test_drops_records_without_timestamp(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({"role": "user", "content": "hi"}), encoding="utf-8",
    )
    stats = IngestStats()
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), stats))
    assert items == []
    assert stats.lines_skipped_no_timestamp == 1


def test_skips_blank_and_malformed_lines(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        "\n".join([
            "",
            "{not valid json",
            json.dumps({"role": "user", "content": "ok",
                        "timestamp": "2026-05-15T10:00:00Z"}),
        ]),
        encoding="utf-8",
    )
    stats = IngestStats()
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), stats))
    assert len(items) == 1
    assert stats.lines_skipped_malformed == 1


def test_user_tool_result_block(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1",
                         "is_error": True}],
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert [type(ev).__name__ for ev, _, _ in items] == ["UserMessageEvent", "ToolResultEvent"]


def test_assistant_skips_non_dict_blocks(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({
            "role": "assistant",
            "content": ["a plain string block",
                        {"type": "tool_call", "name": "shell", "id": "c1"}],
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert [type(ev).__name__ for ev, _, _ in items] == ["AssistantMessageEvent", "ToolUseEvent"]


def test_accepts_numeric_epoch_timestamp(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    epoch = utc(2026, 5, 15, 10).timestamp()
    (sessions / "s.jsonl").write_text(
        json.dumps({"role": "user", "content": "hi", "timestamp": epoch}),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert len(items) == 1


def test_drops_record_with_unparseable_timestamp(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({"role": "user", "content": "hi", "timestamp": "garbage"}),
        encoding="utf-8",
    )
    stats = IngestStats()
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), stats))
    assert items == []
    assert stats.lines_skipped_no_timestamp == 1


def test_skips_file_with_old_mtime(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    f = sessions / "old.jsonl"
    f.write_text(
        json.dumps({"role": "user", "content": "hi",
                    "timestamp": "2026-05-15T10:00:00Z"}),
        encoding="utf-8",
    )
    backdate = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    os.utime(f, (backdate, backdate))
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert items == []


def test_handles_unreadable_session_file(tmp_path):
    """A path matching *.jsonl that is actually a directory raises OSError —
    the adapter swallows it and moves on."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "weird.jsonl").mkdir()
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert items == []


# ---------------------------------------------------------------------------
# Real codex-rs RolloutLine format
# ---------------------------------------------------------------------------

def _rollout_line(ts: str, payload_type: str, **payload_fields) -> str:
    payload = {"type": payload_type, **payload_fields}
    return json.dumps({"timestamp": ts, "type": "response_item", "payload": payload})


def test_new_format_user_and_assistant_messages(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:00Z", "message", role="user",
                          content=[{"type": "input_text", "text": "hello"}]),
            _rollout_line("2026-05-15T10:00:05Z", "message", role="assistant",
                          content=[{"type": "output_text", "text": "hi there"}]),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["UserMessageEvent", "AssistantMessageEvent"]


def test_new_format_function_call_and_output(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:00Z", "message", role="user",
                          content=[{"type": "input_text", "text": "run ls"}]),
            _rollout_line("2026-05-15T10:00:01Z", "function_call",
                          name="shell", call_id="call-1",
                          arguments='{"command":"ls"}'),
            _rollout_line("2026-05-15T10:00:02Z", "function_call_output",
                          call_id="call-1", output="file1 file2"),
            _rollout_line("2026-05-15T10:00:03Z", "message", role="assistant",
                          content=[{"type": "output_text", "text": "done"}]),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == [
        "UserMessageEvent", "ToolUseEvent", "ToolResultEvent", "AssistantMessageEvent",
    ]
    tool_use = next(ev for ev, _, _ in items if isinstance(ev, ToolUseEvent))
    tool_result = next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))
    assert tool_use.tool_use_id == "call-1"
    assert tool_result.tool_use_id == "call-1"
    assert tool_result.is_error is False


def test_new_format_function_call_output_error(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:01Z", "function_call",
                          name="shell", call_id="call-1", arguments="{}"),
            _rollout_line("2026-05-15T10:00:02Z", "function_call_output",
                          call_id="call-1",
                          output={"body": "error: permission denied", "success": False}),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    result = next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))
    assert result.is_error is True


def test_new_format_local_shell_call_success(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "rollout-abc.jsonl").write_text(
        _rollout_line("2026-05-15T10:00:01Z", "local_shell_call",
                      call_id="sh-1", status="completed",
                      action={"type": "exec", "command": ["ls"]}),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["ToolUseEvent", "ToolResultEvent"]
    result = next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))
    assert result.is_error is False


def test_new_format_local_shell_call_error(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "rollout-abc.jsonl").write_text(
        _rollout_line("2026-05-15T10:00:01Z", "local_shell_call",
                      call_id="sh-1", status="incomplete",
                      action={"type": "exec", "command": ["rm", "-rf", "/"]}),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    result = next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))
    assert result.is_error is True


def test_new_format_skips_session_meta_and_event_msg(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            json.dumps({"timestamp": "2026-05-15T09:59:00Z", "type": "session_meta",
                        "payload": {"id": "uuid-1", "cwd": "/home/user",
                                    "originator": "user@host",
                                    "cli_version": "0.50.0",
                                    "timestamp": "2026-05-15T09:59:00Z"}}),
            _rollout_line("2026-05-15T10:00:00Z", "message", role="user",
                          content=[{"type": "input_text", "text": "hello"}]),
            json.dumps({"timestamp": "2026-05-15T10:00:10Z", "type": "event_msg",
                        "payload": {"kind": "agent_reasoning", "text": "thinking..."}}),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["UserMessageEvent"]


def test_new_format_finds_files_in_date_subdirectory(tmp_path):
    """Files placed in YYYY/MM/DD/ subdirectories — the real codex-rs on-disk
    layout — are found via the recursive glob (rglob handles any depth)."""
    sessions = tmp_path / "codex"
    date_dir = sessions / "2026" / "05" / "15"
    date_dir.mkdir(parents=True)
    (date_dir / "rollout-2026-05-15T10-00-00-some-uuid.jsonl").write_text(
        _rollout_line("2026-05-15T10:00:00Z", "message", role="user",
                      content=[{"type": "input_text", "text": "hi"}]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert len(items) == 1
    assert isinstance(items[0][0], UserMessageEvent)


def test_new_format_custom_tool_call(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:01Z", "custom_tool_call",
                          name="my_tool", call_id="ct-1", input="{}"),
            _rollout_line("2026-05-15T10:00:02Z", "custom_tool_call_output",
                          call_id="ct-1", output="ok"),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["ToolUseEvent", "ToolResultEvent"]
    assert next(ev for ev, _, _ in items if isinstance(ev, ToolUseEvent)).tool_name == "my_tool"
