"""Tests for ClaudeCodeSessionSource."""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from ai_code_cognitive_stress.adapters import ClaudeCodeSessionSource
from ai_code_cognitive_stress.pipeline.ingest import (
    AssistantMessageEvent,
    IngestStats,
    SystemMessageEvent,
    ToolResultEvent,
    UserMessageEvent,
)

from .conftest import claude_record, utc


def test_yields_typed_events_with_keys(tmp_path):
    projects = tmp_path / "claude-projects"
    proj = projects / "-home-x"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            claude_record("user", "2026-05-15T10:00:00.000Z"),
            claude_record("assistant", "2026-05-15T10:00:05.000Z"),
        ]),
        encoding="utf-8",
    )
    src = ClaudeCodeSessionSource(projects_dir=projects)
    stats = IngestStats()
    items = list(src.collect(utc(2026, 5, 1), utc(2026, 5, 31), stats))
    assert len(items) == 2
    for event, key, version in items:
        assert key.startswith("claude-code:")
        assert isinstance(version, float)
    assert isinstance(items[0][0], UserMessageEvent)
    assert isinstance(items[1][0], AssistantMessageEvent)


def test_parse_iso_utc_normalises_naive_and_offset():
    from ai_code_cognitive_stress.adapters.claude_code import _parse_iso_utc
    assert _parse_iso_utc("2026-05-15T10:00:00Z").tzinfo is timezone.utc
    naive = _parse_iso_utc("2026-05-15T10:00:00")
    assert naive.tzinfo is timezone.utc and naive.hour == 10
    off = _parse_iso_utc("2026-05-15T12:00:00+02:00")
    assert off.hour == 10


def test_discovery_skips_non_dash_dirs_and_unreadable_files(tmp_path):
    projects = tmp_path / "claude-projects"
    (projects / "ignored").mkdir(parents=True)
    (projects / "ignored" / "s.jsonl").write_text(
        json.dumps(claude_record("user", "2026-05-15T10:00:00.000Z")),
        encoding="utf-8",
    )
    good = projects / "-home-x"
    good.mkdir()
    (good / "weird.jsonl").mkdir()  # a directory, not a file
    stats = IngestStats()
    items = list(ClaudeCodeSessionSource(projects_dir=projects).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), stats))
    assert items == []


# ---------------------------------------------------------------------------
# Operator-input classification

PROMPT_TS = "2026-05-15T09:00:00.000Z"
TS = "2026-05-15T10:00:00.000Z"


def _prompt(session_id="sess-1"):
    return claude_record(
        "user", PROMPT_TS, session_id=session_id,
        content=[{"type": "text", "text": "do the thing"}],
        origin={"kind": "human"},
    )


def _run(tmp_path, records, name="s.jsonl"):
    projects = tmp_path / "claude-projects"
    proj = projects / "-home-x"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / name).write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8",
    )
    stats = IngestStats()
    events = [
        e for e, _k, _v in ClaudeCodeSessionSource(projects_dir=projects).collect(
            utc(2026, 5, 1), utc(2026, 5, 31), stats)
    ]
    return events, stats


def _classify(tmp_path, record):
    """Event types the record at TS yields, next to one human prompt."""
    events, _ = _run(tmp_path, [_prompt(), record])
    return [type(e) for e in events if e.ts.hour == 10]


def _text_record(text, **extra):
    return claude_record(
        "user", TS, content=[{"type": "text", "text": text}], **extra,
    )


def _tool_result_record(**extra):
    return claude_record(
        "user", TS,
        content=[{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        **extra,
    )


def test_tool_result_only_record_is_not_operator_input(tmp_path):
    assert _classify(tmp_path, _tool_result_record()) == [ToolResultEvent]


def test_user_approved_permission_is_operator_input(tmp_path):
    rec = _tool_result_record(
        permissionDecision={"decision": "accept", "source": "user_temporary"},
    )
    assert _classify(tmp_path, rec) == [ToolResultEvent, UserMessageEvent]


def test_config_permission_is_not_operator_input(tmp_path):
    rec = _tool_result_record(
        permissionDecision={"decision": "accept", "source": "config"},
    )
    assert _classify(tmp_path, rec) == [ToolResultEvent]


def test_user_rejected_tool_is_operator_input(tmp_path):
    rec = _tool_result_record(toolDenialKind="user-rejected")
    assert _classify(tmp_path, rec) == [ToolResultEvent, UserMessageEvent]


def test_rule_denied_tool_is_not_operator_input(tmp_path):
    rec = _tool_result_record(toolDenialKind="permission-rule")
    assert _classify(tmp_path, rec) == [ToolResultEvent]


@pytest.mark.parametrize("tool", ["AskUserQuestion", "ExitPlanMode"])
def test_interactive_tool_answer_is_operator_input(tmp_path, tool):
    ask = claude_record(
        "assistant", "2026-05-15T09:59:00.000Z",
        content=[{"type": "tool_use", "id": "t1", "name": tool, "input": {}}],
    )
    events, _ = _run(tmp_path, [_prompt(), ask, _tool_result_record()])
    assert [type(e) for e in events if e.ts.hour == 10] == [
        ToolResultEvent, UserMessageEvent,
    ]


def test_other_tool_result_after_ordinary_tool_use_is_not_operator_input(tmp_path):
    use = claude_record(
        "assistant", "2026-05-15T09:59:00.000Z",
        content=[{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
    )
    events, _ = _run(tmp_path, [_prompt(), use, _tool_result_record()])
    assert [type(e) for e in events if e.ts.hour == 10] == [ToolResultEvent]


@pytest.mark.parametrize("origin,expected", [
    ({"kind": "human"}, UserMessageEvent),
    ({"kind": "task-notification"}, SystemMessageEvent),
    ({"kind": "peer"}, SystemMessageEvent),
])
def test_origin_kind_decides(tmp_path, origin, expected):
    assert _classify(tmp_path, _text_record("x", origin=origin)) == [expected]


def test_meta_and_compact_summary_are_system(tmp_path):
    assert _classify(tmp_path, _text_record("x", isMeta=True)) == [SystemMessageEvent]
    assert _classify(tmp_path, _text_record("x", isMeta=True, origin={"kind": "human"})) == [SystemMessageEvent]
    assert _classify(tmp_path, _text_record("x", isCompactSummary=True)) == [SystemMessageEvent]
    assert _classify(tmp_path, _text_record("x", isVisibleInTranscriptOnly=True)) == [SystemMessageEvent]


def test_turn_origin_and_prompt_source(tmp_path):
    assert _classify(tmp_path, _text_record("x", turnOrigin="task_notification")) == [SystemMessageEvent]
    assert _classify(tmp_path, _text_record("x", turnOrigin="peer")) == [SystemMessageEvent]
    assert _classify(tmp_path, _text_record("x", promptSource="system")) == [SystemMessageEvent]
    assert _classify(tmp_path, _text_record("x", promptSource="typed")) == [UserMessageEvent]
    assert _classify(tmp_path, _text_record("x", promptSource="queued")) == [UserMessageEvent]
    assert _classify(tmp_path, _text_record("x", turnOrigin="human")) == [UserMessageEvent]


def test_sdk_prompt_outside_print_mode_is_operator_input(tmp_path):
    rec = _text_record("x", promptSource="sdk", entrypoint="sdk-ts")
    assert _classify(tmp_path, rec) == [UserMessageEvent]


def test_headless_session_is_dropped(tmp_path):
    records = [
        claude_record("user", PROMPT_TS, session_id="hl",
                      content=[{"type": "text", "text": "eval"}],
                      promptSource="sdk", entrypoint="sdk-cli"),
        claude_record("assistant", TS, session_id="hl"),
    ]
    events, stats = _run(tmp_path, records)
    assert events == []
    assert stats.streams_skipped_unattended == 1
    assert stats.unattended_stream_ids == {"hl"}
    assert stats.events_emitted == 0


def test_sdk_front_end_session_is_kept(tmp_path):
    records = [
        claude_record("user", PROMPT_TS,
                      content=[{"type": "text", "text": "hi"}],
                      promptSource="sdk", entrypoint="sdk-ts"),
        claude_record("assistant", TS),
    ]
    events, stats = _run(tmp_path, records)
    assert [type(e) for e in events] == [UserMessageEvent, AssistantMessageEvent]
    assert stats.streams_skipped_unattended == 0
    assert stats.events_emitted == 2


def test_notification_only_session_is_dropped(tmp_path):
    records = [
        _text_record("<task-notification>done</task-notification>"),
        claude_record("assistant", "2026-05-15T10:01:00.000Z"),
    ]
    events, stats = _run(tmp_path, records)
    assert events == []
    assert stats.streams_skipped_unattended == 1


@pytest.mark.parametrize("text", [
    "<task-notification>x</task-notification>",
    "<teammate-message from=a>x",
    '<agent-message from="a">x',
    "Another Claude session sent a message: hi",
    "<local-command-stdout>x</local-command-stdout>",
    "<local-command-stderr>x",
    "<local-command-caveat>x",
    "<bash-stdout>x</bash-stdout>",
    "<bash-stderr>x",
    "<system-reminder>x",
    "This session is being continued from a previous conversation",
    "  \n<task-notification>leading whitespace",
])
def test_untagged_non_human_text_is_system(tmp_path, text):
    assert _classify(tmp_path, _text_record(text)) == [SystemMessageEvent]


@pytest.mark.parametrize("text", [
    "<command-name>/clear</command-name>",
    "<bash-input>ls</bash-input>",
    "[Request interrupted by user for tool use]",
    "fix the failing test",
])
def test_untagged_human_text_is_operator_input(tmp_path, text):
    assert _classify(tmp_path, _text_record(text)) == [UserMessageEvent]


@pytest.mark.parametrize("text", [
    "<bash-stdout>x</bash-stdout>",
    "<bash-stderr>x",
    "<local-command-stdout>x</local-command-stdout>",
])
def test_command_output_with_human_turn_origin_is_system(tmp_path, text):
    rec = _text_record(text, turnOrigin="human")
    assert _classify(tmp_path, rec) == [SystemMessageEvent]


def _system_record(content, subtype="local_command"):
    return {
        "type": "system", "subtype": subtype, "timestamp": TS,
        "sessionId": "sess-1", "uuid": "s1", "content": content,
    }


def test_local_slash_command_system_record_is_operator_input(tmp_path):
    rec = _system_record("<command-name>/mcp</command-name>\n<command-args></command-args>")
    assert _classify(tmp_path, rec) == [UserMessageEvent]


@pytest.mark.parametrize("rec", [
    _system_record("<local-command-stdout></local-command-stdout>"),
    _system_record("<command-name>/mcp</command-name>", subtype="informational"),
    _system_record(None),
])
def test_other_system_records_yield_nothing(tmp_path, rec):
    assert _classify(tmp_path, rec) == []


def test_string_content_is_classified_by_text(tmp_path):
    human = claude_record("user", TS)
    human["message"]["content"] = "just typed"
    system = claude_record("user", TS)
    system["message"]["content"] = "<task-notification>x"
    assert _classify(tmp_path, human) == [UserMessageEvent]
    assert _classify(tmp_path, system) == [SystemMessageEvent]


def test_image_only_record_is_operator_input(tmp_path):
    rec = claude_record("user", TS, content=[{"type": "image", "source": {}}])
    assert _classify(tmp_path, rec) == [UserMessageEvent]


def _queued(**attachment):
    return {
        "type": "attachment", "timestamp": TS, "sessionId": "sess-1",
        "uuid": "a1",
        "attachment": {"type": "queued_command", **attachment},
    }


def test_human_queued_prompt_is_operator_input(tmp_path):
    rec = _queued(prompt="also do x", commandMode="prompt", origin={"kind": "human"})
    assert _classify(tmp_path, rec) == [UserMessageEvent]


@pytest.mark.parametrize("attachment", [
    {"prompt": "x", "commandMode": "prompt", "origin": {"kind": "peer"}, "isMeta": True},
    {"prompt": "x", "commandMode": "task-notification",
     "origin": {"kind": "task-notification"}},
    {"prompt": "x", "commandMode": "task-notification"},
    {"prompt": "<agent-message from=a>x", "commandMode": "prompt"},
    {"prompt": "x", "commandMode": "prompt", "isMeta": True},
])
def test_non_human_queued_command_yields_nothing(tmp_path, attachment):
    assert _classify(tmp_path, _queued(**attachment)) == []


def test_queued_prompt_without_origin_is_operator_input(tmp_path):
    assert _classify(tmp_path, _queued(prompt="x", commandMode="prompt")) == [UserMessageEvent]


def test_other_attachment_types_yield_nothing(tmp_path):
    rec = {"type": "attachment", "timestamp": TS, "sessionId": "sess-1",
           "attachment": {"type": "hook_success", "prompt": "x"}}
    assert _classify(tmp_path, rec) == []


def test_queued_prompt_alone_attends_a_session(tmp_path):
    rec = _queued(prompt="x", commandMode="prompt", origin={"kind": "human"})
    events, stats = _run(tmp_path, [rec, claude_record("assistant", TS)])
    assert [type(e) for e in events] == [UserMessageEvent, AssistantMessageEvent]
    assert stats.streams_skipped_unattended == 0
