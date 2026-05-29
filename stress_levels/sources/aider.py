"""Aider session-source plugin.

Aider stores its conversation history in `.aider.chat.history.md` files
at the root of each project. The format is markdown-like: turns are
separated by lines beginning with `####`, with timestamps optionally
encoded in the same line. Tool invocations show up as edit blocks
(```diff … ```) within assistant turns.

This adapter discovers project roots from a configurable list (or walks
the user's `$HOME` looking for the file) and produces the shared Event
vocabulary at coarser granularity than Claude Code or Codex — one event
per user turn and one per assistant turn, plus tool_use events when an
edit block is detected.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .base import (
    AssistantMessageEvent,
    Event,
    IngestStats,
    ToolUseEvent,
    UserMessageEvent,
)

# Aider history turn delimiter. Variants seen in the wild:
#   "#### 2026-05-15 10:00:00\n"
#   "#### >\n" (user input continuation)
_TURN_RE = re.compile(r"^####\s+(.*)$")
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[\sT](\d{2}):(\d{2})(?::(\d{2}))?")
_EDIT_BLOCK_RE = re.compile(r"^```diff\b", re.MULTILINE)


class AiderSessionSource:
    """Aider conversation-history adapter.

    `roots`: list of project directories to scan for `.aider.chat.history.md`.
    If None, defaults to scanning a couple of common locations under $HOME
    (one level deep) — keep it small to avoid surprise filesystem walks.
    """

    name = "aider"

    def __init__(self, roots: Iterable[Path] | None = None) -> None:
        self.roots = list(roots) if roots is not None else None

    def is_available(self) -> bool:
        return bool(self._discover_history_files(check_only=True))

    def collect(
        self,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[tuple[Event, str, float]]:
        for path in self._discover_history_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < since.timestamp():
                continue
            stats.files_scanned += 1
            key = f"{self.name}:{path}"
            kept_any = False
            for event in self._parse_history(path, stats):
                if since <= event.ts <= until:
                    kept_any = True
                    yield event, key, mtime
            if kept_any:
                stats.files_kept += 1

    # ------------------------------------------------------------------

    def _discover_history_files(self, check_only: bool = False) -> list[Path]:
        seen: set[Path] = set()
        if self.roots is not None:
            for root in self.roots:
                target = root / ".aider.chat.history.md"
                if target.is_file():
                    seen.add(target)
            return sorted(seen)
        # Default: shallow walk of $HOME one level deep (workspaces /
        # projects typically live there). Avoid recursing into hidden dirs.
        home = Path.home()
        try:
            for entry in home.iterdir():
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                # Check entry itself, then one level deeper.
                f = entry / ".aider.chat.history.md"
                if f.is_file():
                    seen.add(f)
                    if check_only:
                        return list(seen)
                    continue
                try:
                    for sub in entry.iterdir():
                        if not sub.is_dir():
                            continue
                        f = sub / ".aider.chat.history.md"
                        if f.is_file():
                            seen.add(f)
                            if check_only:
                                return list(seen)
                except OSError:
                    continue
        except OSError:
            pass
        return sorted(seen)

    def _parse_history(self, path: Path, stats: IngestStats) -> Iterator[Event]:
        project = path.parent.name
        stream_id = f"aider/{project}"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        # Split into turn blocks at `####` headers. Each turn keeps the
        # body so we can detect tool/edit blocks.
        turns: list[tuple[str, str]] = []
        current_header: str | None = None
        body_lines: list[str] = []
        for raw_line in text.splitlines():
            stats.lines_total += 1
            m = _TURN_RE.match(raw_line)
            if m:
                if current_header is not None:
                    turns.append((current_header, "\n".join(body_lines)))
                current_header = m.group(1).strip()
                body_lines = []
                continue
            body_lines.append(raw_line)
        if current_header is not None:
            turns.append((current_header, "\n".join(body_lines)))

        # Determine role + timestamp for each turn. Role lives in the BODY,
        # not the header — user turns are quoted with `> ` (markdown
        # blockquote); assistant turns are plain prose.
        for header, body in turns:
            stats.lines_decoded += 1
            ts = _extract_timestamp(header) or _fallback_ts(path)
            if ts is None:
                stats.lines_skipped_no_timestamp += 1
                continue
            non_empty = [
                ln for ln in body.splitlines()
                if ln.strip() and not ln.lstrip().startswith("```")
            ]
            role = (
                "user"
                if non_empty and all(ln.lstrip().startswith(">") for ln in non_empty)
                else "assistant"
            )
            if role == "user":
                stats.events_emitted += 1
                yield UserMessageEvent(
                    ts=ts, stream_id=stream_id, project=project,
                )
            else:
                stats.events_emitted += 1
                yield AssistantMessageEvent(
                    ts=ts, stream_id=stream_id, project=project,
                )
                # Treat each edit block in the assistant's body as a tool_use.
                for _ in _EDIT_BLOCK_RE.finditer(body):
                    stats.events_emitted += 1
                    yield ToolUseEvent(
                        ts=ts, stream_id=stream_id, project=project,
                        tool_name="edit",
                    )


def _extract_timestamp(header: str) -> datetime | None:
    m = _TS_RE.search(header)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups()[:5])
    s = int(m.group(6) or 0)
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        return None


def _fallback_ts(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
