"""Event-fabric primitives + multi-source orchestration.

This module defines the shared Event vocabulary (UserMessageEvent /
AssistantMessageEvent / ToolUseEvent / ToolResultEvent) and the
`collect()` orchestration that merges one or more `SessionSource`
plugins into a single time-sorted event stream.

The actual per-tool parsing lives in `stress_levels/sources/*` —
ClaudeCodeSessionSource, CodexSessionSource, AiderSessionSource. Add
new agent-coding tools by implementing the `SessionSource` protocol
(see `sources/base.py`); no changes are needed in this module.

Timezone convention: all event timestamps are UTC. The metrics layer
converts to the user's local time when bucketing by day.

All ingestion is read-only and local. Nothing leaves the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import ClassVar, Iterator, Sequence, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from .sources.base import SessionSource

# Backwards-compatibility shim — kept so the aggregate layer and existing
# tests can still import (and monkeypatch) `CLAUDE_PROJECTS_DIR`. New code
# should instantiate `ClaudeCodeSessionSource(projects_dir=…)` directly.
CLAUDE_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# Shared Event vocabulary — used by every SessionSource implementation.

@dataclass(frozen=True, slots=True)
class UserMessageEvent:
    """A user-typed message landed in a session."""
    KIND: ClassVar[str] = "user_msg"
    ts: datetime
    stream_id: str
    project: str
    uuid: str | None = None
    branch: str | None = None
    # Absolute working directory the session ran in, when the source records
    # it (Claude Code does). Used downstream to discover the git repo a stream
    # belonged to and attribute closures per-repo. None when unknown.
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class AssistantMessageEvent:
    """An assistant response landed in a session."""
    KIND: ClassVar[str] = "assistant_msg"
    ts: datetime
    stream_id: str
    project: str
    uuid: str | None = None
    branch: str | None = None
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class ToolUseEvent:
    """The assistant invoked a tool."""
    KIND: ClassVar[str] = "tool_use"
    ts: datetime
    stream_id: str
    project: str
    tool_name: str | None = None
    tool_use_id: str | None = None
    uuid: str | None = None
    branch: str | None = None
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    """A tool result was returned to the assistant."""
    KIND: ClassVar[str] = "tool_result"
    ts: datetime
    stream_id: str
    project: str
    tool_use_id: str | None = None
    is_error: bool = False
    uuid: str | None = None
    branch: str | None = None
    cwd: str | None = None


Event = Union[
    UserMessageEvent,
    AssistantMessageEvent,
    ToolUseEvent,
    ToolResultEvent,
]


@dataclass(slots=True)
class IngestStats:
    """Per-run ingestion stats. Returned alongside events so callers can
    surface coverage gaps in the report's methodology footer. Mutable
    while collecting; treat as a value object once returned."""
    files_scanned: int = 0
    files_kept: int = 0
    lines_total: int = 0
    lines_decoded: int = 0
    lines_skipped_malformed: int = 0
    lines_skipped_no_timestamp: int = 0
    events_emitted: int = 0


# ---------------------------------------------------------------------------
# Multi-source collect()

def collect(
    since: date,
    until: date,
    sources: Sequence["SessionSource"] | None = None,
    projects_dir: Path | None = None,
) -> tuple[list[Event], IngestStats]:
    """Read all configured agent-coding session activity in
    [since, until] (UTC inclusive) and return a single time-sorted event
    stream plus per-run stats.

    `sources` is an explicit list of SessionSource plugins to query. If
    omitted, a single ClaudeCodeSessionSource is used (with
    `projects_dir` if given) — preserves the v0 behaviour for callers
    that don't yet know about the plugin layer.

    Sources run sequentially; the orchestrator de-duplicates nothing
    (each source is assumed to emit only its own data).
    """
    if sources is None:
        from .sources.claude_code import ClaudeCodeSessionSource
        sources = [ClaudeCodeSessionSource(projects_dir=projects_dir)]

    since_ts = datetime.combine(since, time.min, tzinfo=timezone.utc)
    until_ts = datetime.combine(until, time.max, tzinfo=timezone.utc)

    events: list[Event] = []
    stats = IngestStats()
    for src in sources:
        for ev, _key, _version in src.collect(since_ts, until_ts, stats):
            if since_ts <= ev.ts <= until_ts:
                events.append(ev)
    events.sort(key=lambda e: e.ts)
    return events, stats
