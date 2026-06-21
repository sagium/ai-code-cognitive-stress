"""Source plugins for ingesting agent-coding session activity.

Each source represents one agent-coding tool's session log format. The
plugin protocol (`SessionSource`) is in `base.py`; concrete sources
implementing it live in their own module:

    adapters/claude_code.py   Claude Code (~/.claude/projects/*.jsonl)
    adapters/codex.py         OpenAI Codex CLI (~/.codex/sessions/*.jsonl)

New sources can be added without changing any other module: implement
the `SessionSource` protocol and pass an instance to `ingest.collect()`
or `aggregate.get_day_aggregates()`.
"""

from .base import SessionSource
from .claude_code import ClaudeCodeSessionSource
from .codex import CodexSessionSource

__all__ = [
    "ClaudeCodeSessionSource",
    "CodexSessionSource",
    "SessionSource",
]


def default_sources() -> list[SessionSource]:
    """Best-effort enumeration of every supported session source whose data
    is present on disk. Returns sources that actually have a discoverable
    directory; the rest are skipped silently.

    Use this when the user hasn't specified `--source` and you want the
    skill to pick up whatever data is available."""
    out: list[SessionSource] = []
    for src in (
        ClaudeCodeSessionSource(),
        CodexSessionSource(),
    ):
        if src.is_available():
            out.append(src)
    # Always include at least Claude Code so the CLI's behavior is stable
    # for users who don't have any of the discoverable directories yet.
    if not out:
        out.append(ClaudeCodeSessionSource())
    return out
