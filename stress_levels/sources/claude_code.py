"""Claude Code session-source plugin.

Walks `~/.claude/projects/*/*.jsonl` (top-level files only — sub-agent
files under `<session>/subagents/` are deliberately excluded) and yields
the shared Event vocabulary.

The on-disk format is one JSONL line per session record. Records with
type `user` or `assistant` are emitted as message events; tool_use /
tool_result blocks nested inside `message.content` produce their own
events with the right timestamp.
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
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)

# Record types we emit events for. Other types (mode, ai-title,
# permission-mode, system, file-history-snapshot, queue-operation,
# last-prompt, attachment) carry no supervisory-state signal even though
# some of them have timestamps.
_EVENT_BEARING_TYPES = frozenset({"user", "assistant"})


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

    def discover_cwds(self, since: datetime, until: datetime) -> set[str]:
        """Distinct working directories recorded across sessions touched in the
        window. Feeds repo auto-discovery (discovery.py). Lightweight: reads
        only each record's `cwd` field, never the message content. All cwds a
        session visited are collected, so a stream's recorded cwd is always a
        key in the resulting repo map regardless of mid-session `cd`."""
        cwds: set[str] = set()
        if not self.projects_dir.is_dir():
            return cwds
        for path in self._discover_session_files(since):
            try:
                fh = path.open("r", encoding="utf-8")
            except OSError:
                continue
            with fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = record.get("cwd")
                    if cwd:
                        cwds.add(cwd)
        return cwds

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
            if not project_dir.is_dir() or not project_dir.name.startswith("-"):
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
                cwd = record.get("cwd") or None
                for ev in _events_from_record(record, ts, stream_id, project, cwd):
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
    cwd: str | None = None,
) -> Iterator[Event]:
    rec_type = record["type"]
    uuid = record.get("uuid")
    branch = record.get("gitBranch") or None

    if rec_type == "user":
        yield UserMessageEvent(
            ts=ts, stream_id=stream_id, project=project,
            uuid=uuid, branch=branch, cwd=cwd,
        )
        for block in _iter_content_blocks(record):
            if block.get("type") == "tool_result":
                yield ToolResultEvent(
                    ts=ts, stream_id=stream_id, project=project,
                    tool_use_id=block.get("tool_use_id"),
                    is_error=bool(block.get("is_error")),
                    uuid=uuid, branch=branch, cwd=cwd,
                )
    else:  # assistant
        yield AssistantMessageEvent(
            ts=ts, stream_id=stream_id, project=project,
            uuid=uuid, branch=branch, cwd=cwd,
        )
        for block in _iter_content_blocks(record):
            if block.get("type") == "tool_use":
                yield ToolUseEvent(
                    ts=ts, stream_id=stream_id, project=project,
                    tool_name=block.get("name"),
                    tool_use_id=block.get("id"),
                    uuid=uuid, branch=branch, cwd=cwd,
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
