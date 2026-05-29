"""OpenAI Codex CLI session-source plugin.

The Codex CLI (the open-source `codex` command-line agent from OpenAI)
stores per-session logs in `~/.codex/sessions/*.jsonl`. Each line is a
record; the most common shapes we map into the shared Event vocabulary:

    {"role": "user", "content": "...", "timestamp": "..."}
    {"role": "assistant", "content": [...], "timestamp": "..."}
    {"role": "tool", "name": "shell", "content": "...", "timestamp": "..."}

`role` is mapped to UserMessageEvent / AssistantMessageEvent. When an
assistant record's `content` is a list of structured blocks, any block
with `type == "function_call"` or `type == "tool_use"` becomes a
ToolUseEvent. Tool results (records with `role == "tool"` OR blocks with
`type == "tool_result"` inside a user record) become ToolResultEvents.

If the upstream format evolves (Codex CLI is moving), update the record
shape mappers in `_events_from_record`. The protocol-level interface
stays stable.
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
            files = sorted(self.sessions_dir.glob("*.jsonl"))
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
        # Codex CLI sessions use the filename as the conversation id.
        stream_id = path.stem
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


def _events_from_record(
    record: dict,
    ts: datetime,
    stream_id: str,
    project: str,
) -> Iterator[Event]:
    role = record.get("role") or record.get("type")
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


def _iter_content_blocks(content):
    """Codex content can be a string (plain text), a list of blocks, or
    absent. Yield only dict blocks; strings carry no per-block events."""
    if content is None or isinstance(content, str):
        return
    if isinstance(content, list):
        for block in content:
            yield block
