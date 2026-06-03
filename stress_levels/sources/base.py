"""Source plugin protocol.

A SessionSource ingests one agent-coding tool's session activity into the
shared `Event` vocabulary (UserMessageEvent / AssistantMessageEvent /
ToolUseEvent / ToolResultEvent).

The protocol intentionally takes a mutable IngestStats so the orchestrator can
accumulate file/line/skip counters across sources without per-source bookkeeping.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Protocol, runtime_checkable

# Re-export from ingest at module load time to avoid a circular import.
# (sources/* modules import these; ingest imports the source classes.)
from ..ingest import (
    AssistantMessageEvent,
    Event,
    IngestStats,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)

__all__ = [
    "AssistantMessageEvent",
    "Event",
    "IngestStats",
    "SessionSource",
    "ToolResultEvent",
    "ToolUseEvent",
    "UserMessageEvent",
]


@runtime_checkable
class SessionSource(Protocol):
    """One agent-coding tool's session log adapter.

    Implementations must yield (event, source_key, version) triples in any
    order (the caller sorts). `source_key` is a stable string identifier
    for the underlying source data (e.g. the absolute file path prefixed
    with the source name); `version` is mtime or any monotonic counter
    used by the cache layer to detect changes. Together they let the
    aggregate layer build per-day cache keys without parsing files twice.

    Implementations must update `stats.lines_total`, `lines_decoded`,
    `lines_skipped_*`, `events_emitted`, `files_scanned`, and
    `files_kept` consistently with the shared event-fabric semantics.
    """

    #: Short, stable identifier (e.g. "claude-code", "codex", "aider").
    name: str

    def is_available(self) -> bool:
        """True when this source's data directory exists on disk. Used by
        `default_sources()` to skip absent tools silently."""

    def collect(
        self,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[tuple[Event, str, float]]:
        """Yield (event, source_key, version) triples for events whose
        `ts` falls within [since, until] UTC."""
