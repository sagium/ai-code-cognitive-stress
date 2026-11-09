"""OpenAI Codex CLI session-source plugin.

The Codex CLI (codex-rs, the current Rust-based open-source agent from OpenAI)
stores per-session logs under ``~/.codex/sessions/``. Files are named
``rollout-<ISO8601>-<uuid>.jsonl`` and live in ``YYYY/MM/DD/`` date
subdirectories (older builds wrote them flat in the sessions directory). A
recursive glob handles any nesting depth, so both layouts are supported.

Stream identity is the *root session*, not the file. Each rollout file opens
with a ``session_meta`` envelope whose ``payload.session_id`` is the id of the
root session for the whole conversation tree. A single top-level session is one
file whose ``session_id`` equals its own ``payload.id``. But a session that
spawns subagents (a reviewer team, parallel workers) writes one *additional*
rollout file per subagent thread, each carrying the *parent's* ``session_id``
(with its own per-thread id under ``payload.id`` and ``thread_source ==
"subagent"``). Keying every thread on ``session_id`` (see `_resolve_stream_id`)
folds an orchestrated fan-out back into the one logical session the operator
actually drove — one command, N autonomous subagents — instead of counting it
as N concurrent sessions, which would inflate the concurrency and
cross-session-switch signals with load the operator never bore.

Resuming a session (``codex resume``) reopens the same rollout file in the
recorder's ``append(true)`` mode, so its ``session_meta`` — and therefore its
stream id — is unchanged across resumes. Forking is the one over-count: it
writes a *new* rollout file with a fresh ``session_id`` seeded with a copy of
the parent's turns, so a forked conversation's pre-fork events are counted once
per branch. Forks are rare, so we accept that small over-count rather than
dedupe across files.

Each JSONL line is a RolloutLine envelope (codex-rs protocol.rs):

    {"timestamp": "...", "type": "response_item", "payload": {...}}
    {"timestamp": "...", "type": "session_meta",  "payload": {...}}
    {"timestamp": "...", "type": "event_msg",      "payload": {...}}
    ... (other types skipped)

For ``response_item`` lines, the ``payload`` is a ResponseItem (models.rs)
with its own ``type`` discriminator:

    payload.type = "message"   + payload.role = "user"       → UserMessageEvent
                                                               | SystemMessageEvent
    payload.type = "message"   + payload.role = "assistant"  → AssistantMessageEvent
    payload.type = "function_call" | "custom_tool_call"
                 | "tool_search_call"                        → ToolUseEvent
    payload.type = "web_search_call" | "local_shell_call"    → ToolUseEvent + ToolResultEvent
    payload.type = "function_call_output"
                 | "custom_tool_call_output"
                 | "tool_search_output"                      → ToolResultEvent

`UserMessageEvent` means the operator's own input. A user-role message that is
injected context (AGENTS.md instructions, environment context, skill text, a
subagent notification) is a `SystemMessageEvent`, as is every user-role message
in a subagent thread's rollout file (it is the parent agent's task prompt, not
the operator). Operator interrupts (``<turn_aborted>``, or a bare
``turn_aborted`` event_msg with ``reason == "interrupted"`` when no such
message accompanies it) and actions
(``<user_action>``, ``<user_shell_command>``) are operator input. A root
session file with no operator input at all is not an attentional stream and is
dropped, together with its subagent files.

Known gap: a headless ``codex exec`` run is not separated from an interactive
one, so its prompt counts as operator input. ``session_meta`` carries
``payload.originator`` (``codex-tui`` on every rollout observed), and the binary
lists a ``codex_exec`` originator, but no exec rollout has been observed to
confirm it is written, so it is not used to classify.

Non-conversation ResponseItems carry no supervision signal and are dropped:
``reasoning`` (the model's private thinking) and ``message`` with
``role == "developer"`` (injected scaffolding, not the operator). Envelope
types other than ``response_item`` (``session_meta``, ``event_msg``,
``turn_context``, ``world_state``, …) are skipped.

Error detection reflects what codex-rs actually writes. Tool *output* is a
plain string whose header records the shell exit status, in one of two forms:
``Process exited with code N`` (``exec_command`` / ``write_stdin``) or
``Exit code: N`` (the ``apply_patch`` custom tool); a non-zero N is an error.
A ``web_search_call`` (self-contained, like ``local_shell_call``) and a
``tool_search_output`` are errors when their ``status != "completed"``. The
structured shapes — ``output.success == False`` (dict) and a JSON ``exit_code``
in a list-shaped ``output`` — are also honoured for forward/other-build
compatibility, but real logs use the string form.

Backward compatibility: the older TypeScript codex-cli wrote records with
``role`` at the top level (``{"role": "user", …}``). Those files are still
parsed by the legacy role-based path below.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
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
from .claude_code import _parse_iso_utc

# An interrupt is written as a `<turn_aborted>` user message and, milliseconds
# later, as a `turn_aborted` event_msg; only one of them is the operator's input.
_ABORT_PAIR_WINDOW = timedelta(seconds=1)

# User-role message text that is injected context rather than operator input.
_INJECTED_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<skill>",
    "<subagent_notification>",
)


class CodexSessionSource:
    """OpenAI Codex CLI session-log adapter."""

    name = "codex"

    def __init__(self, sessions_dir: Path | None = None) -> None:
        self.sessions_dir = sessions_dir or (Path.home() / ".codex" / "sessions")

    def is_available(self) -> bool:
        return self.sessions_dir.is_dir()

    def collect(
        self,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[tuple[Event, str, float]]:
        if not self.sessions_dir.is_dir():
            return
        since_mtime = since.timestamp()
        try:
            # rglob picks up both flat sessions/ and YYYY/MM/ subdirectory layouts.
            files = sorted(self.sessions_dir.rglob("*.jsonl"))
        except OSError:
            return
        unattended_roots: set[str] = set()
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < since_mtime:
                continue
            stream_id, is_subagent = _read_session_meta(path)
            if is_subagent and stream_id in unattended_roots:
                continue
            stats.files_scanned += 1
            key = f"{self.name}:{path}"
            events = list(self._parse_session(path, stats, stream_id, is_subagent))
            if not is_subagent and not any(
                isinstance(ev, UserMessageEvent) for ev in events
            ):
                unattended_roots.add(stream_id)
                stats.streams_skipped_unattended += 1
                stats.unattended_stream_ids.add(stream_id)
                continue
            kept_any = False
            for event in events:
                stats.events_emitted += 1
                if since <= event.ts <= until:
                    kept_any = True
                    yield event, key, mtime
            if kept_any:
                stats.files_kept += 1

    def _parse_session(
        self,
        path: Path,
        stats: IngestStats,
        stream_id: str,
        is_subagent: bool = False,
    ) -> Iterator[Event]:
        try:
            fh = path.open("r", encoding="utf-8")
        except OSError:
            return
        aborted_message_ts: datetime | None = None
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
                ts_raw = (
                    record.get("timestamp")
                    or record.get("created_at")
                    or record.get("time")
                )
                if not ts_raw:
                    stats.lines_skipped_no_timestamp += 1
                    continue
                try:
                    ts = _parse_iso_utc(ts_raw) if isinstance(ts_raw, str) else \
                        datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
                except (ValueError, OSError, OverflowError):
                    stats.lines_skipped_no_timestamp += 1
                    continue
                if _is_aborted_message(record):
                    aborted_message_ts = ts
                elif _is_interrupt_event(record):
                    paired = (
                        aborted_message_ts is not None
                        and abs(ts - aborted_message_ts) <= _ABORT_PAIR_WINDOW
                    )
                    if not is_subagent and not paired:
                        yield UserMessageEvent(
                            ts=ts, stream_id=stream_id, project="codex",
                        )
                    continue
                yield from _events_from_record(
                    record, ts, stream_id, "codex", is_subagent,
                )


def _is_aborted_message(record: dict) -> bool:
    payload = record.get("payload")
    return (
        record.get("type") == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "user"
        and _content_text(payload.get("content")).lstrip().startswith("<turn_aborted>")
    )


def _is_interrupt_event(record: dict) -> bool:
    payload = record.get("payload")
    return (
        record.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "turn_aborted"
        and payload.get("reason") == "interrupted"
    )


def _resolve_stream_id(path: Path) -> str:
    """Stream identity for a rollout file: the ROOT session it belongs to.
    See `_read_session_meta`."""
    return _read_session_meta(path)[0]


def _read_session_meta(path: Path) -> tuple[str, bool]:
    """``(stream_id, is_subagent)`` for a rollout file.

    codex-rs writes a ``session_meta`` envelope as the first record of every
    rollout file. Its ``payload.session_id`` is the root session for the whole
    conversation tree — a subagent thread spawned mid-session carries the
    *parent's* session id here (its own per-thread id sits under ``payload.id``).
    Keying every thread on ``session_id`` folds an orchestrated fan-out (one
    operator command, N subagent rollout files) into the single logical session
    the operator drove, rather than counting it as N concurrent sessions.

    ``is_subagent`` is true for a subagent thread, marked by
    ``payload.thread_source == "subagent"`` or a ``payload.source`` dict keyed
    ``subagent``.

    Falls back to ``payload.id`` when there is no ``session_id`` (older builds
    that predate subagents), and to the file stem when the first record is not a
    readable ``session_meta`` at all (the legacy flat-role format). Reads only
    the first non-empty line and never raises — stats are untouched here; the
    main parse loop counts every line exactly once.
    """
    try:
        fh = path.open("r", encoding="utf-8")
    except OSError:
        return path.stem, False
    with fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            # Only the first non-empty record can be the session_meta header;
            # if it is anything else, this file has no root-session id to key on.
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return path.stem, False
            if record.get("type") != "session_meta":
                return path.stem, False
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return path.stem, False
            source = payload.get("source")
            is_subagent = payload.get("thread_source") == "subagent" or (
                isinstance(source, dict) and "subagent" in source
            )
            sid = payload.get("session_id") or payload.get("id")
            if isinstance(sid, str) and sid:
                return sid, is_subagent
            return path.stem, is_subagent
    return path.stem, False


def _events_from_record(
    record: dict,
    ts: datetime,
    stream_id: str,
    project: str,
    is_subagent: bool = False,
) -> Iterator[Event]:
    record_type = record.get("type")

    # ── New codex-rs RolloutLine format ──────────────────────────────────────
    if record_type == "response_item":
        payload = record.get("payload")
        if isinstance(payload, dict):
            yield from _events_from_response_item(
                payload, ts, stream_id, project, is_subagent,
            )
        return

    # Other RolloutLine types carry no conversation signal.
    if record_type in (
        "session_meta", "event_msg", "inter_agent_communication",
        "compacted", "turn_context", "world_state",
    ):
        return

    # ── Backward compat: old TypeScript codex-cli flat role format ───────────
    role = record.get("role")
    if role == "tool":
        yield ToolResultEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_use_id=record.get("tool_use_id") or record.get("tool_call_id"),
            is_error=bool(record.get("is_error")),
        )
        return
    if role == "user":
        has_result = False
        for block in _iter_content_blocks(record.get("content")):
            if isinstance(block, dict) and block.get("type") in (
                "tool_result", "function_call_output",
            ):
                has_result = True
                yield ToolResultEvent(
                    ts=ts, stream_id=stream_id, project=project,
                    tool_use_id=(
                        block.get("tool_use_id")
                        or block.get("tool_call_id")
                        or block.get("call_id")
                    ),
                    is_error=bool(block.get("is_error")),
                )
        if not has_result:
            yield _user_role_event(
                record.get("content"), ts, stream_id, project, is_subagent,
            )
        return
    if role == "assistant":
        yield AssistantMessageEvent(ts=ts, stream_id=stream_id, project=project)
        for block in _iter_content_blocks(record.get("content")):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in ("tool_use", "function_call", "tool_call"):
                yield ToolUseEvent(
                    ts=ts, stream_id=stream_id, project=project,
                    tool_name=(
                        block.get("name")
                        or (block.get("function") or {}).get("name")
                    ),
                    tool_use_id=(
                        block.get("id")
                        or block.get("call_id")
                        or block.get("tool_use_id")
                    ),
                )


def _exit_code_from_output(output) -> int | None:
    """Best-effort recovery of a shell exit code from a list-shaped tool
    output. The custom exec wrapper emits ``output`` as a list of blocks,
    with the last block's ``text`` sometimes carrying a JSON string like
    ``{"exit_code": 127, ...}``. Returns None on any shape mismatch or
    parse failure — never raises."""
    if not isinstance(output, list) or not output:
        return None
    last = output[-1]
    if not isinstance(last, dict):
        return None
    text = last.get("text")
    if not isinstance(text, str):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get("exit_code")


# The exec tools write a plain-string output whose header records the exit
# status: "Process exited with code N" (exec_command / write_stdin) or
# "Exit code: N" (the apply_patch custom tool). Line-anchored so a matching
# line inside the command's own captured output can't be mistaken for it.
_EXIT_CODE_LINE = re.compile(
    r"^(?:Process exited with code|Exit code:)\s+(-?\d+)", re.MULTILINE
)


def _exit_code_from_text(output) -> int | None:
    """Recover a shell exit code from the plain-string tool output codex-rs
    actually writes (see ``_EXIT_CODE_LINE``). Returns the first match — the
    metadata header always precedes the command's captured output — or None
    when there is no exit-status line. Never raises."""
    if not isinstance(output, str):
        return None
    m = _EXIT_CODE_LINE.search(output)
    return int(m.group(1)) if m else None


def _events_from_response_item(
    payload: dict,
    ts: datetime,
    stream_id: str,
    project: str,
    is_subagent: bool = False,
) -> Iterator[Event]:
    """Map a ResponseItem payload to zero or more Events."""
    ptype = payload.get("type")

    if ptype == "message":
        role = payload.get("role")
        if role == "user":
            yield _user_role_event(
                payload.get("content"), ts, stream_id, project, is_subagent,
            )
        elif role == "assistant":
            yield AssistantMessageEvent(ts=ts, stream_id=stream_id, project=project)
        return

    if ptype in ("function_call", "custom_tool_call"):
        yield ToolUseEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_name=payload.get("name"),
            tool_use_id=payload.get("call_id") or payload.get("id"),
        )
        return

    if ptype == "tool_search_call":
        # The agent searching the tool registry — a tool invocation paired with
        # a tool_search_output. It carries no "name" field, so label it.
        yield ToolUseEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_name="tool_search",
            tool_use_id=payload.get("call_id") or payload.get("id"),
        )
        return

    if ptype in ("web_search_call", "local_shell_call"):
        # Self-contained: one item covers both the invocation and its outcome,
        # with status="completed" meaning success. web_search_call keys on `id`;
        # local_shell_call on call_id (id as fallback).
        call_id = payload.get("call_id") or payload.get("id")
        tool_name = "web_search" if ptype == "web_search_call" else "shell"
        yield ToolUseEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_name=tool_name,
            tool_use_id=call_id,
        )
        is_error = payload.get("status") not in ("completed", None)
        yield ToolResultEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_use_id=call_id,
            is_error=is_error,
        )
        return

    if ptype == "tool_search_output":
        # Result of a tool_search_call; error is signalled by status only.
        yield ToolResultEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_use_id=payload.get("call_id"),
            is_error=payload.get("status") not in ("completed", None),
        )
        return

    if ptype in ("function_call_output", "custom_tool_call_output"):
        call_id = payload.get("call_id")
        output = payload.get("output")
        # Real logs write a plain string with the exit status in its header;
        # the list/dict shapes are kept for forward/other-build compatibility.
        code = _exit_code_from_output(output)
        if code is None:
            code = _exit_code_from_text(output)
        is_error = (
            bool(payload.get("is_error"))
            or (code is not None and code != 0)
            or (isinstance(output, dict) and output.get("success") is False)
        )
        yield ToolResultEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_use_id=call_id,
            is_error=is_error,
        )


def _user_role_event(
    content,
    ts: datetime,
    stream_id: str,
    project: str,
    is_subagent: bool,
) -> Event:
    """A user-role message is operator input unless it is injected context or
    sits in a subagent thread (where it is the parent agent's task prompt)."""
    if is_subagent or _content_text(content).lstrip().startswith(_INJECTED_PREFIXES):
        return SystemMessageEvent(ts=ts, stream_id=stream_id, project=project)
    return UserMessageEvent(ts=ts, stream_id=stream_id, project=project)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        b["text"] for b in content
        if isinstance(b, dict) and isinstance(b.get("text"), str)
    )


def _iter_content_blocks(content):
    """Yield dict blocks from a content value that may be str, list, or None."""
    if content is None or isinstance(content, str):
        return
    if isinstance(content, list):
        yield from content
