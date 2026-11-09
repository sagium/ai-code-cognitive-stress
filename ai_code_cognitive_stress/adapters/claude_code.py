"""Claude Code session-source plugin.

Walks `~/.claude/projects/*/*.jsonl` (top-level files only — sub-agent
files under `<session>/subagents/` are deliberately excluded) and yields
the shared Event vocabulary.

The on-disk format is one JSONL line per session record. Records with
type `user` or `assistant` are emitted as message events; tool_use /
tool_result blocks nested inside `message.content` produce their own
events with the right timestamp.

`UserMessageEvent` means the operator's own input. A `user` record that is a
tool result, a background-task or peer notification, injected context or a
compaction summary is not operator input and never becomes one; operator input
that is not a plain user record (a prompt queued while the agent was busy, a
slash command, an answer to AskUserQuestion, a permission approval or
rejection) is recovered
from the records that carry it. A session file with no operator input at all (a
headless `claude -p` run) is not an attentional stream and is dropped whole.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterator

from .base import (
    AssistantMessageEvent,
    Event,
    IngestStats,
    SystemMessageEvent,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)

# Record types we emit events for. Other types (mode, ai-title,
# permission-mode, file-history-snapshot, queue-operation, last-prompt) carry no
# supervisory-state signal even though some of them have timestamps;
# `attachment` is read only for queued operator prompts and `system` only for
# slash commands.
_EVENT_BEARING_TYPES = frozenset({"user", "assistant", "attachment", "system"})

# Tools whose result is the operator's answer to a question the agent asked.
_INTERACTIVE_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Command output echoed into a user record; never operator input even when the
# record carries a human turnOrigin.
_OUTPUT_PREFIXES = (
    "<local-command-stdout",
    "<local-command-stderr",
    "<local-command-caveat",
    "<bash-stdout",
    "<bash-stderr",
)

# Untagged user-record text that did not come from the operator.
_NON_HUMAN_PREFIXES = _OUTPUT_PREFIXES + (
    "<task-notification",
    "<teammate-message",
    "<agent-message",
    "Another Claude session sent a message",
    "<system-reminder",
    "This session is being continued",
)

# Values of `turnOrigin` / `promptSource` that mark the operator's own prompt.
_HUMAN_ORIGINS = frozenset({"human", "typed", "queued", "suggestion_accepted"})


class ClaudeCodeSessionSource:
    """Claude Code session-log adapter."""

    name = "claude-code"

    def __init__(self, projects_dir: Path | None = None) -> None:
        self.projects_dir = projects_dir or (Path.home() / ".claude" / "projects")

    def is_available(self) -> bool:
        return self.projects_dir.is_dir()

    def collect(
        self,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[tuple[Event, str, float]]:
        if not self.projects_dir.is_dir():
            return
        for path in self._discover_session_files(since):
            stats.files_scanned += 1
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            key = f"{self.name}:{path}"
            kept_any = False
            for event in self._parse_session(path, stats):
                if since <= event.ts <= until:
                    kept_any = True
                    yield event, key, mtime
            if kept_any:
                stats.files_kept += 1

    # ------------------------------------------------------------------
    # Discovery + parsing — kept as instance methods so a subclass could
    # point at a non-standard projects_dir or filter projects differently.

    def _discover_session_files(self, since: datetime) -> Iterator[Path]:
        """Yield top-level *.jsonl files whose mtime is on or after `since`.
        Sub-agent files (under <session>/subagents/) are excluded — they
        don't form independent attentional streams."""
        since_mtime = since.timestamp()
        try:
            project_dirs = sorted(self.projects_dir.iterdir())
        except OSError:
            return
        for project_dir in project_dirs:
            if not project_dir.is_dir():
                continue
            name = project_dir.name
            # On Unix/macOS, Claude Code encodes absolute paths by replacing
            # leading "/" with "-", so all project dirs start with "-".
            # On Windows, a path like "C:\foo" encodes as "C--foo", so the
            # first char is the drive letter and the second is "-".
            if not (name.startswith("-") or (len(name) >= 2 and name[1] == "-")):
                continue
            try:
                files = sorted(project_dir.glob("*.jsonl"))
            except OSError:
                continue
            for path in files:
                try:
                    if path.stat().st_mtime >= since_mtime:
                        yield path
                except OSError:
                    continue

    def _parse_session(self, path: Path, stats: IngestStats) -> Iterator[Event]:
        project = path.parent.name
        try:
            fh = path.open("r", encoding="utf-8")
        except OSError:
            return
        events: list[Event] = []
        stream_ids: set[str] = set()
        interactive_ids: set[str] = set()
        with fh:
            for raw_line in fh:
                stats.lines_total += 1
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    stats.lines_skipped_malformed += 1
                    continue
                stats.lines_decoded += 1
                if record.get("type") not in _EVENT_BEARING_TYPES:
                    continue
                ts_raw = record.get("timestamp")
                stream_id = record.get("sessionId")
                if not ts_raw or not stream_id:
                    stats.lines_skipped_no_timestamp += 1
                    continue
                try:
                    ts = _parse_iso_utc(ts_raw)
                except ValueError:
                    stats.lines_skipped_no_timestamp += 1
                    continue
                stream_ids.add(stream_id)
                events.extend(_events_from_record(
                    record, ts, stream_id, project, interactive_ids,
                ))
        if not any(isinstance(ev, UserMessageEvent) for ev in events):
            if stream_ids:
                stats.streams_skipped_unattended += 1
                stats.unattended_stream_ids.update(stream_ids)
            return
        for ev in events:
            stats.events_emitted += 1
            yield ev


def _parse_iso_utc(s: str) -> datetime:
    """Parse an ISO-8601 timestamp ending in `Z` (or with explicit offset)
    into a UTC-aware datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _events_from_record(
    record: dict,
    ts: datetime,
    stream_id: str,
    project: str,
    interactive_tool_ids: set[str] | None = None,
) -> Iterator[Event]:
    """Events for one record. `interactive_tool_ids` accumulates the ids of the
    file's AskUserQuestion / ExitPlanMode tool_use blocks so their results can
    be recognised as the operator's answer."""
    rec_type = record["type"]
    uuid = record.get("uuid")

    if rec_type == "assistant":
        yield AssistantMessageEvent(
            ts=ts, stream_id=stream_id, project=project, uuid=uuid,
        )
        for block in _iter_content_blocks(record):
            if block.get("type") == "tool_use":
                if (
                    interactive_tool_ids is not None
                    and block.get("name") in _INTERACTIVE_TOOLS
                    and block.get("id")
                ):
                    interactive_tool_ids.add(block["id"])
                yield ToolUseEvent(
                    ts=ts, stream_id=stream_id, project=project,
                    tool_name=block.get("name"),
                    tool_use_id=block.get("id"),
                    uuid=uuid,
                )
    elif rec_type == "system":
        if _is_slash_command(record):
            yield UserMessageEvent(
                ts=ts, stream_id=stream_id, project=project, uuid=uuid,
            )
    elif rec_type == "attachment":
        if _is_human_queued_prompt(record.get("attachment")):
            yield UserMessageEvent(
                ts=ts, stream_id=stream_id, project=project, uuid=uuid,
            )
    else:
        yield from _events_from_user_record(
            record, ts, stream_id, project, uuid, interactive_tool_ids or set(),
        )


def _events_from_user_record(
    record: dict,
    ts: datetime,
    stream_id: str,
    project: str,
    uuid: str | None,
    interactive_tool_ids: set[str],
) -> Iterator[Event]:
    blocks = list(_iter_content_blocks(record))
    results = [b for b in blocks if b.get("type") == "tool_result"]
    if results:
        for block in results:
            yield ToolResultEvent(
                ts=ts, stream_id=stream_id, project=project,
                tool_use_id=block.get("tool_use_id"),
                is_error=bool(block.get("is_error")),
                uuid=uuid,
            )
        if _operator_answered(record, results, interactive_tool_ids):
            yield UserMessageEvent(
                ts=ts, stream_id=stream_id, project=project, uuid=uuid,
            )
        return
    if _is_operator_record(record):
        yield UserMessageEvent(
            ts=ts, stream_id=stream_id, project=project, uuid=uuid,
        )
    else:
        yield SystemMessageEvent(
            ts=ts, stream_id=stream_id, project=project, uuid=uuid,
        )


def _operator_answered(
    record: dict, results: list[dict], interactive_tool_ids: set[str],
) -> bool:
    """True when a tool-result record carries the operator's own decision: a
    permission prompt they answered, a rejection, or the answer to a question
    the agent asked."""
    decision = record.get("permissionDecision")
    if isinstance(decision, dict) and str(decision.get("source")).startswith("user"):
        return True
    if record.get("toolDenialKind") == "user-rejected":
        return True
    return any(b.get("tool_use_id") in interactive_tool_ids for b in results)


def _is_operator_record(record: dict) -> bool:
    """Classify a `user` record that carries no tool result."""
    if (
        record.get("isMeta")
        or record.get("isCompactSummary")
        or record.get("isVisibleInTranscriptOnly")
    ):
        return False
    if _record_text(record).lstrip().startswith(_OUTPUT_PREFIXES):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin["kind"] == "human"
    turn_origin = record.get("turnOrigin") or record.get("promptSource")
    if isinstance(turn_origin, str) and turn_origin:
        if turn_origin == "sdk":
            return record.get("entrypoint") != "sdk-cli"
        return turn_origin in _HUMAN_ORIGINS
    return not _text_is_non_human(_record_text(record))


def _is_slash_command(record: dict) -> bool:
    """Slash commands run locally are logged only as `system` records."""
    content = record.get("content")
    return (
        record.get("subtype") == "local_command"
        and isinstance(content, str)
        and content.lstrip().startswith("<command-name>")
    )


def _is_human_queued_prompt(attachment: object) -> bool:
    """A prompt the operator typed while the agent was busy is logged only as a
    `queued_command` attachment."""
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return False
    origin = attachment.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin["kind"] == "human"
    prompt = attachment.get("prompt")
    return (
        attachment.get("commandMode") == "prompt"
        and not attachment.get("isMeta")
        and isinstance(prompt, str)
        and bool(prompt)
        and not _text_is_non_human(prompt)
    )


def _text_is_non_human(text: str) -> bool:
    return text.lstrip().startswith(_NON_HUMAN_PREFIXES)


def _record_text(record: dict) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        b["text"] for b in content
        if isinstance(b, dict) and b.get("type") == "text"
        and isinstance(b.get("text"), str)
    )


def _iter_content_blocks(record: dict) -> Iterator[dict]:
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict):
            yield block
