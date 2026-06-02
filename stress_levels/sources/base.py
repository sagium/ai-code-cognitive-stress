"""Source plugin protocol.

A SessionSource ingests one agent-coding tool's session activity into the
shared `Event` vocabulary (UserMessageEvent / AssistantMessageEvent /
ToolUseEvent / ToolResultEvent). A ClosureEventSource emits closure
markers (commit / merge / PR) that the metrics layer uses to compute the
Closure Deficit honestly.

Both protocols intentionally take a mutable IngestStats so the
orchestrator can accumulate file/line/skip counters across sources
without per-source bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
    "ClosureEvent",
    "ClosureEventSource",
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


@dataclass(frozen=True, slots=True)
class ClosureEvent:
    """A git/VCS marker the metrics layer routes by ``kind``:

    * Closure kinds — a loop was closed. ``push``, ``commit``, ``merge``,
      ``pr_merge``, ``mr_merge``, ``issue_close``. These net against the day's
      opened loops to compute the Closure Deficit as a real signal, not a load
      proxy. ``push`` is the strongest: it means the work left the machine, and
      it is *inherently the operator's own* — only the local user's pushes write
      ``update by push`` to this clone's remote-tracking reflog, so a server-side
      bot merge never forges one.
    * Rework kinds — history was rewritten; the loop was reopened or churned.
      ``amend``, ``squash``, ``rebase``, ``reset``, ``revert``, ``cherry_pick``.
      These raise the Interruption axis (self-interruption / attention
      residue), not the Closure axis.

    Sourced locally: ``commit``/``merge`` from ``git log`` (scoped to the
    operator's own identities so a shared monorepo's teammate/bot commits don't
    spuriously close the operator's loops), ``push`` and the rework kinds from
    ``git reflog`` (the only place pushes and history-rewrite operations are
    recorded). ``author`` carries the commit author email for commit/merge
    events; it is None for push/rework, which are self-scoped by where the
    reflog physically lives (this clone)."""

    ts: datetime           # UTC
    kind: str              # see class docstring for the closure/rework split
    repo: str              # stable repo-root key (resolved abs path)
    branch: str | None = None
    title: str | None = None
    author: str | None = None  # commit author email (commit/merge); None otherwise


@runtime_checkable
class ClosureEventSource(Protocol):
    """One VCS-or-platform adapter that emits closure markers."""

    name: str

    def is_available(self) -> bool:
        """True when this source can run (e.g. `gh` is on PATH, or the
        configured repos exist)."""

    def collect(
        self,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[ClosureEvent]:
        """Yield ClosureEvents in [since, until] UTC."""
