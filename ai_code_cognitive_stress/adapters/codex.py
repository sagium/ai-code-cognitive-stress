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
    payload.type = "message"   + payload.role = "assistant"  → AssistantMessageEvent
    payload.type = "function_call" | "custom_tool_call"      → ToolUseEvent
    payload.type = "local_shell_call"                        → ToolUseEvent + ToolResultEvent
    payload.type = "function_call_output"
                 | "custom_tool_call_output"                 → ToolResultEvent

Error detection: ``local_shell_call`` with ``status != "completed"``; or
``function_call_output`` with ``output.success == False`` (dict output); or,
for list-shaped ``output`` (the custom exec wrapper), a non-zero ``exit_code``
recovered from the trailing block's JSON ``text``.

Backward compatibility: the older TypeScript codex-cli wrote records with
``role`` at the top level (``{"role": "user", …}``). Those files are still
parsed by the legacy role-based path below.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .base import (
    AssistantMessageEvent,
    Event,
    IngestStats,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)
from .claude_code import _parse_iso_utc


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
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < since_mtime:
                continue
            stats.files_scanned += 1
            key = f"{self.name}:{path}"
            kept_any = False
            for event in self._parse_session(path, stats):
                if since <= event.ts <= until:
                    kept_any = True
                    yield event, key, mtime
            if kept_any:
                stats.files_kept += 1

    def _parse_session(self, path: Path, stats: IngestStats) -> Iterator[Event]:
        stream_id = _resolve_stream_id(path)
        try:
            fh = path.open("r", encoding="utf-8")
        except OSError:
            return
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
                for ev in _events_from_record(record, ts, stream_id, "codex"):
                    stats.events_emitted += 1
                    yield ev


def _resolve_stream_id(path: Path) -> str:
    """Stream identity for a rollout file: the ROOT session it belongs to.

    codex-rs writes a ``session_meta`` envelope as the first record of every
    rollout file. Its ``payload.session_id`` is the root session for the whole
    conversation tree — a subagent thread spawned mid-session carries the
    *parent's* session id here (its own per-thread id sits under ``payload.id``).
    Keying every thread on ``session_id`` folds an orchestrated fan-out (one
    operator command, N subagent rollout files) into the single logical session
    the operator drove, rather than counting it as N concurrent sessions.

    Falls back to ``payload.id`` when there is no ``session_id`` (older builds
    that predate subagents), and to the file stem when the first record is not a
    readable ``session_meta`` at all (the legacy flat-role format). Reads only
    the first non-empty line and never raises — stats are untouched here; the
    main parse loop counts every line exactly once.
    """
    try:
        fh = path.open("r", encoding="utf-8")
    except OSError:
        return path.stem
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
                return path.stem
            if record.get("type") != "session_meta":
                return path.stem
            payload = record.get("payload")
            if isinstance(payload, dict):
                sid = payload.get("session_id") or payload.get("id")
                if isinstance(sid, str) and sid:
                    return sid
            return path.stem
    return path.stem


def _events_from_record(
    record: dict,
    ts: datetime,
    stream_id: str,
    project: str,
) -> Iterator[Event]:
    record_type = record.get("type")

    # ── New codex-rs RolloutLine format ──────────────────────────────────────
    if record_type == "response_item":
        payload = record.get("payload")
        if isinstance(payload, dict):
            yield from _events_from_response_item(payload, ts, stream_id, project)
        return

    # Other RolloutLine types carry no conversation signal.
    if record_type in (
        "session_meta", "event_msg", "inter_agent_communication",
        "compacted", "turn_context",
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
        yield UserMessageEvent(ts=ts, stream_id=stream_id, project=project)
        for block in _iter_content_blocks(record.get("content")):
            if isinstance(block, dict) and block.get("type") in (
                "tool_result", "function_call_output",
            ):
                yield ToolResultEvent(
                    ts=ts, stream_id=stream_id, project=project,
                    tool_use_id=(
                        block.get("tool_use_id")
                        or block.get("tool_call_id")
                        or block.get("call_id")
                    ),
                    is_error=bool(block.get("is_error")),
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


def _events_from_response_item(
    payload: dict,
    ts: datetime,
    stream_id: str,
    project: str,
) -> Iterator[Event]:
    """Map a ResponseItem payload to zero or more Events."""
    ptype = payload.get("type")

    if ptype == "message":
        role = payload.get("role")
        if role == "user":
            yield UserMessageEvent(ts=ts, stream_id=stream_id, project=project)
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

    if ptype == "local_shell_call":
        # The shell-call item combines call + result: one line covers both the
        # invocation and its outcome. status="completed" means success.
        call_id = payload.get("call_id") or payload.get("id")
        yield ToolUseEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_name="shell",
            tool_use_id=call_id,
        )
        is_error = payload.get("status") not in ("completed", None)
        yield ToolResultEvent(
            ts=ts, stream_id=stream_id, project=project,
            tool_use_id=call_id,
            is_error=is_error,
        )
        return

    if ptype in ("function_call_output", "custom_tool_call_output"):
        call_id = payload.get("call_id")
        output = payload.get("output")
        code = _exit_code_from_output(output)
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


def _iter_content_blocks(content):
    """Yield dict blocks from a content value that may be str, list, or None."""
    if content is None or isinstance(content, str):
        return
    if isinstance(content, list):
        yield from content
