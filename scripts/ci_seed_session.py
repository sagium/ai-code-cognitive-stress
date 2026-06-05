#!/usr/bin/env python3
"""CI: seed a deterministic Claude Code session log so the widget CLI has data.

A fresh CI runner has no agent-coding history, so `aicogstress --emit-html-card`
would render nothing. This writes a synthetic but realistic set of overlapping
Claude Code sessions into `~/.claude/projects/` (the location the built-in
ClaudeCodeSessionSource reads), spread across recent days up to *today*, so all
four timeframe views (Today / Week / Month / Year) and the per-hour concurrency
chart have something to show. It then exercises the real ingest → aggregate →
metrics → render pipeline — the point of running it on macOS.

The record schema mirrors tests/test_sources.py (`_claude_record`): a JSONL of
`{type, timestamp, sessionId, uuid, message:{role, content[...]}}` lines, with
`tool_use` / `tool_result` blocks for activity.

Usage (CI passes today's date in, since Date math must be deterministic per run):

    python scripts/ci_seed_session.py --today 2026-06-05
    python scripts/ci_seed_session.py            # defaults to the system date
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

UTC = dt.timezone.utc


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _record(rec_type: str, ts: dt.datetime, session_id: str, content: list) -> dict:
    return {
        "type": rec_type,
        "timestamp": _iso(ts),
        "sessionId": session_id,
        "uuid": f"u-{session_id}-{int(ts.timestamp())}",
        "cwd": "/home/ci/project",
        "gitBranch": "main",
        "message": {"role": rec_type, "content": content},
    }


def _session_lines(session_id: str, start: dt.datetime, turns: int) -> list[dict]:
    """A back-and-forth session: user → assistant(tool_use) → user(tool_result),
    one turn every ~6 minutes, so a session spans a realistic stretch of time."""
    out: list[dict] = []
    for i in range(turns):
        t = start + dt.timedelta(minutes=6 * i)
        out.append(_record("user", t, session_id, [{"type": "text", "text": "go"}]))
        out.append(_record(
            "assistant", t + dt.timedelta(seconds=20), session_id,
            [{"type": "tool_use", "name": "Bash", "id": f"tu-{session_id}-{i}"}],
        ))
        out.append(_record(
            "user", t + dt.timedelta(seconds=40), session_id,
            [{"type": "tool_result", "tool_use_id": f"tu-{session_id}-{i}",
              "is_error": False}],
        ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", help="anchor date YYYY-MM-DD (default: system date)")
    ap.add_argument("--projects-dir", type=Path,
                    help="default: ~/.claude/projects")
    args = ap.parse_args()

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.datetime.now(UTC).date())
    projects = args.projects_dir or (Path.home() / ".claude" / "projects")
    proj = projects / "-home-ci-project"
    proj.mkdir(parents=True, exist_ok=True)

    # Seed a handful of recent days (incl. today) so Week/Month/Year populate,
    # and on each day run two OVERLAPPING sessions so the concurrency axis and
    # the per-hour chart are non-trivial.
    written = 0
    for back in (0, 1, 3, 10, 30):
        day = today - dt.timedelta(days=back)
        base = dt.datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)
        for n, offset in enumerate((0, 18)):  # second session starts mid-first
            start = base + dt.timedelta(minutes=offset)
            sid = f"sess-{day.isoformat()}-{n}"
            lines = _session_lines(sid, start, turns=6)
            (proj / f"{sid}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in lines), encoding="utf-8",
            )
            written += len(lines)

    print(f"seeded {written} events across recent days into {proj}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
