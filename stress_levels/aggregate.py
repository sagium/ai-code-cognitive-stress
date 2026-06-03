"""Reduce raw events into per-day aggregates with disk caching.

The aggregate layer sits between ingest (event firehose) and metrics
(derived axes). It produces one DayAggregate per UTC day, holding enough
signal for the metrics layer to compute CODL, interruption rate, and the
Closure Deficit (resumption load — per-stream idle gaps where a parked loop
was picked back up) without re-parsing events.

Past days are cached on disk under
    ${XDG_CACHE_HOME:-~/.cache}/ai-code-cognitive-stress/<namespace>/<YYYY>/<YYYY-MM-DD>.json
where <namespace> = <schema>-<fingerprint> (e.g. "v4-a1b2c3d4e5f6").

  - <schema>       = CACHE_SCHEMA_VERSION — a manually-bumped prefix that
                     acts as a hard override lever (bump forces a new
                     namespace regardless of fingerprint).
  - <fingerprint>  = first 12 hex chars of SHA-256 over the source bytes of
                     the modules that determine cached DayAggregate *content*
                     (aggregate.py, ingest.py, every sources/*.py).
                     The fingerprint auto-rotates whenever the aggregate or
                     ingest layer changes, so a code change produces a clean
                     miss and recomputes without manual version bumps.
                     metrics.py is deliberately NOT part of the fingerprint:
                     build_profile recomputes metrics from aggregates on every
                     run, so an axis-formula change takes effect immediately
                     and must NOT trigger a full re-ingest of past logs.

Each run auto-prunes stale namespace dirs (those matching ^v\\d+(-[0-9a-f]+)?$
but NOT the current namespace) so orphaned directories from old schema or code
versions are removed automatically — no manual cache cleanup needed.

The per-day cache entry is also keyed by:
    - SHA-256 of sorted (source_key, mtime) pairs for session files that
      contributed events on that day — invalidates when source data changes
    - local TZ name — avoids serving mis-bucketed days when the user travels

Today is never cached: it is always in flux until midnight UTC.

Cache writes are best-effort: an OSError when writing (e.g. disk full) is
logged into IngestStats.cache_write_errors but does not fail the run.
Cache prune is best-effort: an OSError on removal is silently ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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

CACHE_SCHEMA_VERSION = "v4"

# Intra-day idle gaps at least this long (no event of any kind) are recorded on
# each StreamDayActivity as resume markers (see `resume_gaps`). Deliberately far
# below the metrics-layer scoring threshold (RESUME_THRESHOLD_MIN) so the
# threshold can be lowered via config without forcing a cache rebuild; only
# lowering it BELOW this floor would. 2 min is short enough to catch every real
# park yet long enough to exclude normal within-turn pauses.
RESUME_GAP_STORE_FLOOR_SEC = 120

# Module-level cache for _module_fingerprint so it is computed once per process.
_MODULE_FINGERPRINT: str | None = None


def _module_fingerprint() -> str:
    """Return a 12-char lowercase-hex SHA-256 fingerprint of the source bytes
    of every module that determines cached DayAggregate *content*:
      - aggregate.py  (this file)
      - ingest.py
      - every *.py in the sources/ package dir (sorted for determinism)

    metrics.py is deliberately excluded.  The cache stores per-day *aggregates*
    (stream counts, tool counts, idle gaps, etc.).  build_profile()
    recomputes the metric axes (CODL, interruption rate, Closure Deficit) from
    those aggregates on every run, so a change to an axis formula takes effect
    immediately and must NOT invalidate the aggregate cache — doing so would
    needlessly re-ingest all session logs on every axis tweak.

    If any source file cannot be read (e.g. a frozen/zipped install) the hash
    is computed over a constant fallback marker for that file, so this function
    never raises and always returns a stable 12-char token.

    The result is memoized in a module-level global because source files do not
    change within a running process.
    """
    global _MODULE_FINGERPRINT
    if _MODULE_FINGERPRINT is not None:
        return _MODULE_FINGERPRINT

    h = hashlib.sha256()

    def _read(p: Path) -> bytes:
        try:
            return p.read_bytes()
        except OSError:
            return b"<unreadable:" + str(p).encode("utf-8", errors="replace") + b">"

    # aggregate.py — this file
    h.update(_read(Path(__file__)))
    h.update(b"\n---\n")

    # ingest.py
    h.update(_read(Path(_ingest.__file__)))
    h.update(b"\n---\n")

    # sources/*.py — sorted for determinism
    sources_dir = Path(__file__).parent / "sources"
    try:
        source_files = sorted(sources_dir.glob("*.py"))
    except OSError:
        source_files = []
    for sf in source_files:
        h.update(_read(sf))
        h.update(b"\n---\n")

    _MODULE_FINGERPRINT = h.hexdigest()[:12]
    return _MODULE_FINGERPRINT


# Namespace used for cache path directories.
# Format: "<schema>-<fingerprint>", e.g. "v4-a1b2c3d4e5f6".
# Bumping CACHE_SCHEMA_VERSION forces a new namespace regardless of fingerprint.
CACHE_NAMESPACE: str = f"{CACHE_SCHEMA_VERSION}-{_module_fingerprint()}"

# Pattern for recognising cache namespace directories to prune.
_CACHE_NS_RE = re.compile(r"^v\d+(-[0-9a-f]+)?$")


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
    # User-message timestamps in UTC. Needed by metrics.py for CODL engagement
    # weighting: _stream_weight_at uses them to detect foreground vs background
    # activity within the grace window.
    user_msg_timestamps: tuple[datetime, ...] = ()
    # Intra-day idle gaps in this stream's timeline: (resume_ts, gap_seconds) for
    # every span between consecutive events (of ANY kind) that exceeds
    # RESUME_GAP_STORE_FLOOR_SEC. `resume_ts` is the first event AFTER the gap
    # (when work resumed). metrics.py reads these as the Closure Deficit's
    # resumption signal — a parked-and-resumed loop. The floor keeps the cache
    # compact; it sits well below the scoring threshold so the latter can be
    # tuned down without a re-ingest. Cross-day pickups (a session resumed on a
    # later calendar day) are NOT stored here — metrics.py derives those by
    # linking the same stream_id across consecutive DayAggregates.
    resume_gaps: tuple[tuple[datetime, int], ...] = ()

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
    _prune_stale_cache_namespaces(cache_dir, CACHE_NAMESPACE)
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
        cache_key = _make_cache_key(files, local_tz_name) if files else None
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

def _aggregate_events(
    day: date,
    events: list[Event],
) -> DayAggregate:
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
        "user_msg_timestamps": [],
        "event_timestamps": [],
    })

    for ev in events:
        s = per_stream[ev.stream_id]
        s["stream_id"] = ev.stream_id
        s["project"] = ev.project
        if s["first_ts"] is None or ev.ts < s["first_ts"]:
            s["first_ts"] = ev.ts
        if s["last_ts"] is None or ev.ts > s["last_ts"]:
            s["last_ts"] = ev.ts
        # Every event's timestamp feeds the idle-gap (resumption) scan below.
        s["event_timestamps"].append(ev.ts)
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
            user_msg_timestamps=tuple(sorted(v["user_msg_timestamps"])),
            resume_gaps=_idle_gaps(v["event_timestamps"]),
        )
        for v in sorted(per_stream.values(), key=lambda v: v["first_ts"])
    )

    return DayAggregate(
        day=day,
        streams=streams,
        peak_concurrent_streams=_peak_concurrent(streams),
    )


def _idle_gaps(event_timestamps: list[datetime]) -> tuple[tuple[datetime, int], ...]:
    """From one stream's event timestamps on a day, return the idle gaps that
    exceed RESUME_GAP_STORE_FLOOR_SEC as ``(resume_ts, gap_seconds)`` pairs.

    A gap is the span between two consecutive events (of any kind); the loop was
    dormant across it and ``resume_ts`` is the event that picked it back up.
    Sub-floor gaps (normal within-turn pauses) are dropped to keep the cache
    compact. Order follows the sorted timeline."""
    if len(event_timestamps) < 2:
        return ()
    ts = sorted(event_timestamps)
    gaps: list[tuple[datetime, int]] = []
    for prev, cur in zip(ts, ts[1:]):
        gap = int((cur - prev).total_seconds())
        if gap >= RESUME_GAP_STORE_FLOOR_SEC:
            gaps.append((cur, gap))
    return tuple(gaps)


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

def _prune_stale_cache_namespaces(cache_dir: Path, keep: str) -> None:
    """Remove stale namespace subdirs from *cache_dir*, keeping only *keep*.

    A directory is considered a cache namespace dir when its name matches
    ``^v\\d+(-[0-9a-f]+)?$`` — this covers:
      - old bare schema dirs like ``v3``, ``v4``
      - new namespaced dirs like ``v4-a1b2c3d4e5f6``

    Entries whose names do NOT match the pattern (e.g. a user's ``notes/``
    folder placed there by mistake) are left completely untouched.

    Each removal is wrapped in ``try/except OSError`` so a permission error or
    a racing deletion never aborts a run — cache cleanup is always best-effort.
    """
    if not cache_dir.exists():
        return
    for child in cache_dir.iterdir():
        if not child.is_dir():
            continue
        if not _CACHE_NS_RE.match(child.name):
            continue
        if child.name == keep:
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            pass


def _make_cache_key(
    files: dict[str, float],
    local_tz_name: str,
) -> str:
    """Hash the local TZ name + sorted (source_key, version) pairs into a stable
    cache key. `source_key` is a stable identifier emitted by the SessionSource
    plugin (e.g. "claude-code:/abs/path/sess.jsonl"); the version is mtime or a
    monotonic counter from the source.

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
        / CACHE_NAMESPACE
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
    if raw.get("cache_namespace") != CACHE_NAMESPACE:
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
        "cache_namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,   # informational
        "package_version": __version__,            # informational
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
    out = {
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
                "user_msg_timestamps": [t.isoformat() for t in s.user_msg_timestamps],
                "resume_gaps": [
                    [t.isoformat(), g] for t, g in s.resume_gaps
                ],
            }
            for s in a.streams
        ],
    }
    return out


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
            user_msg_timestamps=tuple(
                datetime.fromisoformat(t) for t in s.get("user_msg_timestamps", [])
            ),
            resume_gaps=tuple(
                (datetime.fromisoformat(t), int(g))
                for t, g in s.get("resume_gaps", [])
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
