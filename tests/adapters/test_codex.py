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


def _session_meta_line(
    ts: str, *, session_id=None, thread_id=None, thread_source=None,
) -> str:
    payload = {"cwd": "/home/user", "cli_version": "0.143.0", "timestamp": ts}
    if thread_id is not None:
        payload["id"] = thread_id
    if session_id is not None:
        payload["session_id"] = session_id
    if thread_source is not None:
        payload["thread_source"] = thread_source
    return json.dumps({"timestamp": ts, "type": "session_meta", "payload": payload})


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


def test_new_format_custom_tool_call_output_list_exit_code_error(tmp_path):
    """List-shaped output whose trailing JSON text block carries a non-zero
    exit_code (the real custom exec wrapper shape) counts as an error."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    output = [
        {"type": "input_text",
         "text": json.dumps({"chunk_id": "c-1", "exit_code": 127,
                              "original_token_count": 51, "output": "bash: nope: not found"})},
    ]
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:01Z", "custom_tool_call",
                          name="exec", call_id="ct-1", input="{}"),
            _rollout_line("2026-05-15T10:00:02Z", "custom_tool_call_output",
                          call_id="ct-1", output=output),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    result = next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))
    assert result.is_error is True


def test_new_format_custom_tool_call_output_list_exit_code_zero(tmp_path):
    """Same list shape but exit_code 0 stays non-error."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    output = [
        {"type": "input_text",
         "text": json.dumps({"chunk_id": "c-1", "exit_code": 0,
                              "original_token_count": 12, "output": "file1 file2"})},
    ]
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:01Z", "custom_tool_call",
                          name="exec", call_id="ct-1", input="{}"),
            _rollout_line("2026-05-15T10:00:02Z", "custom_tool_call_output",
                          call_id="ct-1", output=output),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    result = next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))
    assert result.is_error is False


# ---------------------------------------------------------------------------
# Stream identity: subagent threads fold into their root session
# ---------------------------------------------------------------------------

def _write(path, *lines):
    path.write_text("\n".join(lines), encoding="utf-8")


def test_subagent_threads_share_parent_stream_id(tmp_path):
    """A session that spawns subagents writes one rollout file per subagent
    thread, each carrying the parent's session_id. All threads — parent and
    subagents — must resolve to the SAME stream id so an orchestrated fan-out
    reads as one logical session, not N concurrent ones."""
    sessions = tmp_path / "codex"
    day = sessions / "2026" / "05" / "15"
    day.mkdir(parents=True)
    root = "019f4bae-root"
    _write(
        day / "rollout-2026-05-15T10-00-00-019f4bae-root.jsonl",
        _session_meta_line("2026-05-15T10:00:00Z", session_id=root,
                           thread_id=root, thread_source="user"),
        _rollout_line("2026-05-15T10:00:01Z", "message", role="user",
                      content=[{"type": "input_text", "text": "review the MR"}]),
    )
    for tid in ("019f4bb2-gibbs", "019f4bb2-curie"):
        _write(
            day / f"rollout-2026-05-15T10-00-05-{tid}.jsonl",
            _session_meta_line("2026-05-15T10:00:05Z", session_id=root,
                               thread_id=tid, thread_source="subagent"),
            _rollout_line("2026-05-15T10:00:06Z", "function_call",
                          name="shell", call_id=f"{tid}-c1", arguments="{}"),
        )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert {ev.stream_id for ev, _, _ in items} == {root}


def test_top_level_session_keyed_on_session_id(tmp_path):
    """A plain session (session_id == its own thread id) keys on the session
    id, not the filename stem."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    _write(
        sessions / "rollout-2026-05-15T10-00-00-somefile.jsonl",
        _session_meta_line("2026-05-15T10:00:00Z", session_id="sess-1",
                           thread_id="sess-1"),
        _rollout_line("2026-05-15T10:00:01Z", "message", role="user",
                      content=[{"type": "input_text", "text": "hi"}]),
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert {ev.stream_id for ev, _, _ in items} == {"sess-1"}


def test_independent_sessions_stay_separate_streams(tmp_path):
    """Two unrelated top-level sessions keep distinct stream ids — the fix must
    not over-collapse."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    for sid in ("sess-a", "sess-b"):
        _write(
            sessions / f"rollout-2026-05-15T10-00-00-{sid}.jsonl",
            _session_meta_line("2026-05-15T10:00:00Z", session_id=sid, thread_id=sid),
            _rollout_line("2026-05-15T10:00:01Z", "message", role="user",
                          content=[{"type": "input_text", "text": "hi"}]),
        )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert {ev.stream_id for ev, _, _ in items} == {"sess-a", "sess-b"}


def test_session_meta_without_session_id_falls_back_to_thread_id(tmp_path):
    """Older builds predate subagents and write only ``id`` in session_meta:
    key on that thread id."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    _write(
        sessions / "rollout-2026-05-15T10-00-00-x.jsonl",
        _session_meta_line("2026-05-15T10:00:00Z", thread_id="old-id"),
        _rollout_line("2026-05-15T10:00:01Z", "message", role="user",
                      content=[{"type": "input_text", "text": "hi"}]),
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert {ev.stream_id for ev, _, _ in items} == {"old-id"}


def test_legacy_flat_format_keeps_stem_stream_id(tmp_path):
    """The legacy flat-role format has no session_meta header at all: fall back
    to the file stem, preserving the pre-subagent behaviour."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    _write(
        sessions / "legacy-stem.jsonl",
        json.dumps({"role": "user", "content": "hi",
                    "timestamp": "2026-05-15T10:00:00Z"}),
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert {ev.stream_id for ev, _, _ in items} == {"legacy-stem"}


# ---------------------------------------------------------------------------
# Real codex-rs shapes: string tool output with an exit-status header, and the
# tool-call types the earlier adapter dropped (verified against live logs).
# ---------------------------------------------------------------------------

def _one_result(sessions):
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    return next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))


# The exec_command header form: "Process exited with code N".
_EXEC_OK = "Chunk ID: f3aeac\nWall time: 0.02 seconds\nProcess exited with code 0\nOutput:\nok\n"
_EXEC_FAIL = "Chunk ID: aa11bb\nWall time: 0.01 seconds\nProcess exited with code 127\nOutput:\nbash: nope: command not found\n"
# The apply_patch header form: "Exit code: N".
_PATCH_OK = "Exit code: 0\nWall time: 0.2 seconds\nOutput:\nSuccess. Updated the following files:\nM foo.py\n"
_PATCH_FAIL = "Exit code: 1\nWall time: 0.2 seconds\nOutput:\nerror: patch did not apply\n"


def test_exec_command_output_exit_code_zero_from_text(tmp_path):
    """Real exec_command output is a plain string with 'Process exited with
    code 0' in its header — success."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "r.jsonl").write_text(
        _rollout_line("2026-05-15T10:00:02Z", "function_call_output",
                      call_id="c1", output=_EXEC_OK),
        encoding="utf-8",
    )
    assert _one_result(sessions).is_error is False


def test_exec_command_output_nonzero_exit_from_text_is_error(tmp_path):
    """'Process exited with code 127' in the string header must register as an
    error — the case the adapter previously missed entirely."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "r.jsonl").write_text(
        _rollout_line("2026-05-15T10:00:02Z", "function_call_output",
                      call_id="c1", output=_EXEC_FAIL),
        encoding="utf-8",
    )
    assert _one_result(sessions).is_error is True


def test_apply_patch_output_exit_code_from_text(tmp_path):
    """apply_patch (a custom_tool_call) uses the 'Exit code: N' header form."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "ok.jsonl").write_text(
        _rollout_line("2026-05-15T10:00:02Z", "custom_tool_call_output",
                      call_id="c1", output=_PATCH_OK),
        encoding="utf-8",
    )
    assert _one_result(sessions).is_error is False
    (sessions / "bad.jsonl").write_text(
        _rollout_line("2026-05-15T11:00:02Z", "custom_tool_call_output",
                      call_id="c2", output=_PATCH_FAIL),
        encoding="utf-8",
    )
    errs = [ev for ev, _, _ in CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats())
        if isinstance(ev, ToolResultEvent) and ev.is_error]
    assert len(errs) == 1


def test_exit_code_line_anchored_ignores_command_output(tmp_path):
    """A success header (code 0) whose captured output happens to contain a
    line like 'Exit code: 5' must NOT be read as an error: the header matches
    first and is line-anchored."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    tricky = ("Chunk ID: x\nProcess exited with code 0\nOutput:\n"
              "printed by the program: Exit code: 5\n")
    (sessions / "r.jsonl").write_text(
        _rollout_line("2026-05-15T10:00:02Z", "function_call_output",
                      call_id="c1", output=tricky),
        encoding="utf-8",
    )
    assert _one_result(sessions).is_error is False


def test_web_search_call_emits_use_and_result(tmp_path):
    """web_search_call is self-contained (call + result); keyed on `id`, error
    when status != completed."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "r.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:01Z", "web_search_call",
                          id="ws_1", status="completed",
                          action={"type": "open_page", "url": "https://x"}),
            _rollout_line("2026-05-15T10:00:03Z", "web_search_call",
                          id="ws_2", status="failed",
                          action={"type": "search"}),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["ToolUseEvent", "ToolResultEvent",
                     "ToolUseEvent", "ToolResultEvent"]
    uses = [ev for ev, _, _ in items if isinstance(ev, ToolUseEvent)]
    assert uses[0].tool_name == "web_search" and uses[0].tool_use_id == "ws_1"
    results = [ev for ev, _, _ in items if isinstance(ev, ToolResultEvent)]
    assert results[0].is_error is False and results[1].is_error is True


def test_tool_search_call_and_output(tmp_path):
    """tool_search_call → ToolUse; tool_search_output → ToolResult (error by
    status). Paired on call_id."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "r.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:01Z", "tool_search_call",
                          call_id="call_1", status="completed",
                          arguments={"query": "spawn subagent", "limit": 8}),
            _rollout_line("2026-05-15T10:00:02Z", "tool_search_output",
                          call_id="call_1", status="completed", tools=[]),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["ToolUseEvent", "ToolResultEvent"]
    use = next(ev for ev, _, _ in items if isinstance(ev, ToolUseEvent))
    assert use.tool_name == "tool_search" and use.tool_use_id == "call_1"
    assert next(ev for ev, _, _ in items
                if isinstance(ev, ToolResultEvent)).is_error is False


def test_world_state_and_reasoning_and_developer_are_dropped(tmp_path):
    """world_state envelopes, `reasoning` items, and developer-role messages
    carry no supervision signal and must emit nothing — only the real user
    message survives."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "r.jsonl").write_text(
        "\n".join([
            json.dumps({"timestamp": "2026-05-15T09:59:00Z", "type": "world_state",
                        "payload": {"full": True, "state": {}}}),
            _rollout_line("2026-05-15T10:00:00Z", "reasoning",
                          summary=[{"type": "summary_text", "text": "thinking"}]),
            _rollout_line("2026-05-15T10:00:01Z", "message", role="developer",
                          content=[{"type": "input_text", "text": "system note"}]),
            _rollout_line("2026-05-15T10:00:02Z", "message", role="user",
                          content=[{"type": "input_text", "text": "hi"}]),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    assert [type(ev).__name__ for ev, _, _ in items] == ["UserMessageEvent"]


def test_new_format_custom_tool_call_output_list_no_exit_code(tmp_path):
    """List output whose trailing block is plain non-JSON text: no exit code
    can be recovered, so it must NOT be treated as an error (no false
    positive)."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    output = [
        {"type": "input_text", "text": "just some plain output, not JSON"},
    ]
    (sessions / "rollout-abc.jsonl").write_text(
        "\n".join([
            _rollout_line("2026-05-15T10:00:01Z", "custom_tool_call",
                          name="exec", call_id="ct-1", input="{}"),
            _rollout_line("2026-05-15T10:00:02Z", "custom_tool_call_output",
                          call_id="ct-1", output=output),
        ]),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), IngestStats()))
    result = next(ev for ev, _, _ in items if isinstance(ev, ToolResultEvent))
    assert result.is_error is False
