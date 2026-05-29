"""Reduce raw events into per-day aggregates with disk caching.

The aggregate layer sits between ingest (event firehose) and metrics
(derived axes). It produces one DayAggregate per UTC day, holding enough
signal for the metrics layer to compute CODL, interruption rate, and
closure proxies without re-parsing events.

Past days are cached on disk under
    ${XDG_CACHE_HOME:-~/.cache}/ai-code-cognitive-stress/<schema>/<YYYY>/<YYYY-MM-DD>.json
keyed by:
    1. schema version (CACHE_SCHEMA_VERSION below — bump when DayAggregate
       shape changes)
    2. stress_levels package __version__ (bump invalidates when metric
       definitions change even if the shape didn't)
    3. SHA-256 of sorted (path, mtime) pairs of session files that produced
       events on that day — invalidates when source data changes

Today is never cached: it is always in flux until midnight UTC.

Cache writes are best-effort: an OSError when writing (e.g. disk full) is
logged into IngestStats.cache_write_errors but does not fail the run.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone, tzinfo
from pathlib import Path
from typing import Iterable

from . import __version__
from . import ingest as _ingest
from .ingest import (
    AssistantMessageEvent,
    Event,
    IngestStats,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)

CACHE_SCHEMA_VERSION = "v3"


def _default_cache_dir() -> Path:
    """Platform-native cache directory.

    Resolution order:
      1. `XDG_CACHE_HOME` env var (respects user overrides on any OS).
      2. Per-OS native convention:
         - Linux / BSD: `~/.cache/` (XDG default).
         - macOS:       `~/Library/Caches/`.
         - Windows:     `%LOCALAPPDATA%` then `~/AppData/Local/`.
      3. Append `ai-code-cognitive-stress` segment.
    """
    import sys
    override = os.environ.get("XDG_CACHE_HOME")
    if override:
        return Path(override) / "ai-code-cognitive-stress"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ai-code-cognitive-stress"
    if sys.platform == "win32":  # pragma: no cover — Windows-only path
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "ai-code-cognitive-stress" / "Cache"
        return Path.home() / "AppData" / "Local" / "ai-code-cognitive-stress" / "Cache"
    return Path.home() / ".cache" / "ai-code-cognitive-stress"


DEFAULT_CACHE_DIR = _default_cache_dir()


# ---------------------------------------------------------------------------
# Data shapes

@dataclass(frozen=True, slots=True)
class StreamDayActivity:
    """One stream's activity within a single UTC day.

    A stream is one Claude Code session (one .jsonl file). If a session
    spans multiple days, it appears as multiple StreamDayActivity objects —
    one per day it touched.
    """
    stream_id: str
    project: str
    first_ts: datetime          # UTC, tz-aware
    last_ts: datetime           # UTC, tz-aware
    user_msg_count: int = 0
    assistant_msg_count: int = 0
    tool_use_count: int = 0
    tool_result_count: int = 0
    tool_error_count: int = 0
    branches: tuple[str, ...] = ()
    # User-message timestamps in UTC. Needed by metrics.py to auto-detect
    # work windows from the distribution of when the user types.
    user_msg_timestamps: tuple[datetime, ...] = ()

    @property
    def active_seconds(self) -> int:
        """Wall-clock span between first and last event. Over-states true
        engagement when there are long idle gaps; metrics.py applies an
        engagement-weighted CODL (foreground vs background) so those gaps
        count at a reduced weight rather than as active supervision."""
        return int((self.last_ts - self.first_ts).total_seconds())


@dataclass(frozen=True, slots=True)
class DayAggregate:
    """All activity recorded on a single UTC day, grouped by stream.

    The aggregate is self-contained: the metrics layer derives every axis
    from a sequence of DayAggregates without re-parsing source events.
    """
    day: date
    streams: tuple[StreamDayActivity, ...] = ()
    peak_concurrent_streams: int = 0

    @property
    def stream_count(self) -> int:
        return len(self.streams)

    @property
    def user_msg_count(self) -> int:
        return sum(s.user_msg_count for s in self.streams)

    @property
    def assistant_msg_count(self) -> int:
        return sum(s.assistant_msg_count for s in self.streams)

    @property
    def tool_use_count(self) -> int:
        return sum(s.tool_use_count for s in self.streams)

    @property
    def tool_result_count(self) -> int:
        return sum(s.tool_result_count for s in self.streams)

    @property
    def tool_error_count(self) -> int:
        return sum(s.tool_error_count for s in self.streams)

    @property
    def total_active_seconds(self) -> int:
        return sum(s.active_seconds for s in self.streams)


@dataclass(slots=True)
class AggregateStats:
    """Extends IngestStats with cache-layer counters."""
    ingest: IngestStats = field(default_factory=IngestStats)
    days_in_window: int = 0
    days_with_activity: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_write_errors: int = 0


# ---------------------------------------------------------------------------
# Public entry point

def get_day_aggregates(
    since: date,
    until: date,
    projects_dir: Path | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
    local_tz: tzinfo | None = None,
    sources=None,
) -> tuple[dict[date, DayAggregate], AggregateStats]:
    """Return DayAggregates for each *local* day in [since, until] inclusive,
    plus stats. Past days hit disk cache when source mtimes haven't changed;
    today is always recomputed.

    Day bucketing is **local** rather than UTC: an event at 22:00 UTC in a
    UTC+12 timezone falls on the next local day. The since/until bounds are
    also interpreted as local dates, then converted to UTC for the event
    filter. The cache key embeds the local TZ name so cached values from a
    different TZ context aren't mis-served (e.g. when travelling).

    `now` and `local_tz` are injectable for testability — without them they
    default to the system local clock + zone.
    """
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    local_tz = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    local_tz_name = str(local_tz)

    # Resolve sources. Default: a single Claude Code source pointing at
    # the current CLAUDE_PROJECTS_DIR — preserves v0 behaviour for
    # callers that don't pass an explicit `sources` list.
    if sources is None:
        from .sources.claude_code import ClaudeCodeSessionSource
        sources = [ClaudeCodeSessionSource(
            projects_dir=projects_dir or _ingest.CLAUDE_PROJECTS_DIR,
        )]

    today_local = (now or datetime.now(local_tz)).astimezone(local_tz).date()
    stats = AggregateStats()

    if since > until:
        return {}, stats

    # `since` and `until` are LOCAL days — convert to UTC bounds for the
    # event-timestamp filter.
    since_ts = datetime.combine(since, time.min, tzinfo=local_tz).astimezone(timezone.utc)
    until_ts = datetime.combine(until, time.max, tzinfo=local_tz).astimezone(timezone.utc)

    # Walk every configured source, tracking per-day which (source_key,
    # version) pairs contributed events. Days are LOCAL dates.
    per_day_events: dict[date, list[Event]] = defaultdict(list)
    per_day_files: dict[date, dict[str, float]] = defaultdict(dict)

    for source in sources:
        for event, key, version in source.collect(since_ts, until_ts, stats.ingest):
            if since_ts <= event.ts <= until_ts:
                day = event.ts.astimezone(local_tz).date()
                per_day_events[day].append(event)
                per_day_files[day][key] = version
                stats.ingest.events_emitted += 1

    aggregates: dict[date, DayAggregate] = {}
    for day in _iter_dates(since, until):
        stats.days_in_window += 1
        files = per_day_files.get(day, {})
        events = per_day_events.get(day, [])
        if events:
            stats.days_with_activity += 1
        cache_key = (
            _make_cache_key(files, local_tz_name) if files else None
        )
        is_today = day >= today_local

        if not is_today and cache_key is not None:
            cached = _read_cache(cache_dir, day, cache_key)
            if cached is not None:
                aggregates[day] = cached
                stats.cache_hits += 1
                continue
            stats.cache_misses += 1

        aggregate = _aggregate_events(day, events)
        aggregates[day] = aggregate

        if not is_today and cache_key is not None:
            try:
                _write_cache(cache_dir, day, cache_key, aggregate)
            except OSError:
                stats.cache_write_errors += 1

    return aggregates, stats


# ---------------------------------------------------------------------------
# Reduction

def _aggregate_events(day: date, events: list[Event]) -> DayAggregate:
    """Reduce a day's events into a DayAggregate. Pure — no I/O."""
    if not events:
        return DayAggregate(day=day)

    per_stream: dict[str, dict] = defaultdict(lambda: {
        "stream_id": "",
        "project": "",
        "first_ts": None,
        "last_ts": None,
        "user_msg_count": 0,
        "assistant_msg_count": 0,
        "tool_use_count": 0,
        "tool_result_count": 0,
        "tool_error_count": 0,
        "branches": set(),
        "user_msg_timestamps": [],
    })

    for ev in events:
        s = per_stream[ev.stream_id]
        s["stream_id"] = ev.stream_id
        s["project"] = ev.project
        if s["first_ts"] is None or ev.ts < s["first_ts"]:
            s["first_ts"] = ev.ts
        if s["last_ts"] is None or ev.ts > s["last_ts"]:
            s["last_ts"] = ev.ts
        if ev.branch:
            s["branches"].add(ev.branch)
        if isinstance(ev, UserMessageEvent):
            s["user_msg_count"] += 1
            s["user_msg_timestamps"].append(ev.ts)
        elif isinstance(ev, AssistantMessageEvent):
            s["assistant_msg_count"] += 1
        elif isinstance(ev, ToolUseEvent):
            s["tool_use_count"] += 1
        elif isinstance(ev, ToolResultEvent):
            s["tool_result_count"] += 1
            if ev.is_error:
                s["tool_error_count"] += 1

    streams = tuple(
        StreamDayActivity(
            stream_id=v["stream_id"],
            project=v["project"],
            first_ts=v["first_ts"],
            last_ts=v["last_ts"],
            user_msg_count=v["user_msg_count"],
            assistant_msg_count=v["assistant_msg_count"],
            tool_use_count=v["tool_use_count"],
            tool_result_count=v["tool_result_count"],
            tool_error_count=v["tool_error_count"],
            branches=tuple(sorted(v["branches"])),
            user_msg_timestamps=tuple(sorted(v["user_msg_timestamps"])),
        )
        for v in sorted(per_stream.values(), key=lambda v: v["first_ts"])
    )

    return DayAggregate(
        day=day,
        streams=streams,
        peak_concurrent_streams=_peak_concurrent(streams),
    )


def _peak_concurrent(streams: tuple[StreamDayActivity, ...]) -> int:
    """Sweep-line over stream intervals to find the maximum number of
    concurrently-active streams. A stream is active across [first_ts, last_ts]."""
    if not streams:
        return 0
    # +1 at first_ts, -1 just after last_ts (so a single-event stream still
    # contributes one to peak).
    points: list[tuple[datetime, int]] = []
    for s in streams:
        points.append((s.first_ts, +1))
        points.append((s.last_ts, -1))
    # On ties, process +1 before -1 so a stream that opens and another that
    # closes at the same instant overlap (conservative — favors counting).
    points.sort(key=lambda p: (p[0], -p[1]))
    peak = 0
    running = 0
    for _, delta in points:
        running += delta
        if running > peak:
            peak = running
    return peak


# ---------------------------------------------------------------------------
# Cache I/O

def _make_cache_key(files: dict[str, float], local_tz_name: str) -> str:
    """Hash the local TZ name + sorted (source_key, version) pairs into a
    stable cache key. `source_key` is a stable identifier emitted by the
    SessionSource plugin (e.g. "claude-code:/abs/path/sess.jsonl"); the
    version is mtime or a monotonic counter from the source.

    Including the TZ name ensures cached aggregates aren't mis-served
    when the user switches timezones (since day bucketing is local — a
    UTC+12 user and a UTC-8 user partition the same events into different
    local days)."""
    h = hashlib.sha256()
    h.update(local_tz_name.encode("utf-8"))
    h.update(b"\n")
    for key in sorted(files):
        h.update(str(key).encode("utf-8"))
        h.update(b"\0")
        h.update(f"{int(files[key] * 1000)}".encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def _cache_path(cache_dir: Path, day: date) -> Path:
    return (
        cache_dir
        / CACHE_SCHEMA_VERSION
        / f"{day.year:04d}"
        / f"{day.isoformat()}.json"
    )


def _read_cache(
    cache_dir: Path,
    day: date,
    expected_key: str,
) -> DayAggregate | None:
    path = _cache_path(cache_dir, day)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if raw.get("package_version") != __version__:
        return None
    if raw.get("cache_key") != expected_key:
        return None
    try:
        return _aggregate_from_dict(raw["aggregate"])
    except (KeyError, TypeError, ValueError):
        return None


def _write_cache(
    cache_dir: Path,
    day: date,
    cache_key: str,
    aggregate: DayAggregate,
) -> None:
    path = _cache_path(cache_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "package_version": __version__,
        "cache_key": cache_key,
        "day": day.isoformat(),
        "aggregate": _aggregate_to_dict(aggregate),
    }
    # Write atomically — partial writes during a crash would otherwise look
    # like valid JSON with junk content.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _aggregate_to_dict(a: DayAggregate) -> dict:
    return {
        "day": a.day.isoformat(),
        "peak_concurrent_streams": a.peak_concurrent_streams,
        "streams": [
            {
                "stream_id": s.stream_id,
                "project": s.project,
                "first_ts": s.first_ts.isoformat(),
                "last_ts": s.last_ts.isoformat(),
                "user_msg_count": s.user_msg_count,
                "assistant_msg_count": s.assistant_msg_count,
                "tool_use_count": s.tool_use_count,
                "tool_result_count": s.tool_result_count,
                "tool_error_count": s.tool_error_count,
                "branches": list(s.branches),
                "user_msg_timestamps": [t.isoformat() for t in s.user_msg_timestamps],
            }
            for s in a.streams
        ],
    }


def _aggregate_from_dict(d: dict) -> DayAggregate:
    streams = tuple(
        StreamDayActivity(
            stream_id=s["stream_id"],
            project=s["project"],
            first_ts=datetime.fromisoformat(s["first_ts"]),
            last_ts=datetime.fromisoformat(s["last_ts"]),
            user_msg_count=int(s.get("user_msg_count", 0)),
            assistant_msg_count=int(s.get("assistant_msg_count", 0)),
            tool_use_count=int(s.get("tool_use_count", 0)),
            tool_result_count=int(s.get("tool_result_count", 0)),
            tool_error_count=int(s.get("tool_error_count", 0)),
            branches=tuple(s.get("branches", [])),
            user_msg_timestamps=tuple(
                datetime.fromisoformat(t) for t in s.get("user_msg_timestamps", [])
            ),
        )
        for s in d.get("streams", [])
    )
    return DayAggregate(
        day=date.fromisoformat(d["day"]),
        streams=streams,
        peak_concurrent_streams=int(d.get("peak_concurrent_streams", 0)),
    )


def _iter_dates(since: date, until: date) -> Iterable[date]:
    from datetime import timedelta
    d = since
    one_day = timedelta(days=1)
    while d <= until:
        yield d
        d += one_day
