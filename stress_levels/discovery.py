"""Local repo discovery from agent-session working directories.

Auto-discovery is deliberately bounded to the cwds the agent sessions
themselves recorded — we never walk the disk hunting for repos. For each
distinct session cwd we walk up to the nearest ancestor that contains a
``.git`` entry; that ancestor is the repo root. Fully local, read-only
``stat()`` calls, no network — same invariant as the rest of the tool.

Two consumers:
  * ``__main__`` unions the discovered roots with any explicit
    ``closure.repos`` and feeds them to the git source.
  * ``metrics`` uses the ``cwd → root`` map to attribute each stream's
    opened loops to the repo it ran in (per-repo closure netting).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable


def repo_root_for(cwd: str | Path) -> Path | None:
    """Nearest ancestor of *cwd* (inclusive) that contains a ``.git`` entry,
    resolved to an absolute path; or None when *cwd* is not inside a git repo.

    Walks parents rather than calling ``git`` — pure filesystem ``stat()``s,
    no subprocess, no network. Missing/odd paths yield None rather than raise."""
    if not cwd:
        return None  # empty/unknown cwd — don't fall through to the process cwd
    try:
        p = Path(cwd).expanduser()
    except (OSError, ValueError):
        return None
    for d in (p, *p.parents):
        try:
            if (d / ".git").exists():
                try:
                    return d.resolve()
                except OSError:
                    return d
        except OSError:
            continue
    return None


def discover_repo_roots(
    cwds: Iterable[str | Path],
) -> tuple[list[Path], dict[str, Path]]:
    """Resolve session cwds to unique git-repo roots.

    Returns ``(sorted unique roots, cwd→root map)``. cwds outside any git repo
    are omitted. The map is keyed by the original cwd *string* so metrics.py
    can look up a stream's repo by the cwd it recorded."""
    roots: dict[Path, None] = {}
    cwd_map: dict[str, Path] = {}
    for cwd in cwds:
        if not cwd:
            continue
        root = repo_root_for(cwd)
        if root is None:
            continue
        roots[root] = None
        cwd_map[str(cwd)] = root
    return sorted(roots), cwd_map


def collect_session_cwds(
    sources,
    since: datetime,
    until: datetime,
) -> set[str]:
    """Gather distinct working directories from the given session sources.

    Sources opt in by implementing ``discover_cwds(since, until) -> set[str]``
    (Claude Code does; Codex/Aider don't and are skipped). Kept separate from
    the main ingest pass because closure sources must be constructed *before*
    aggregation runs. A misbehaving source is skipped, never fatal."""
    cwds: set[str] = set()
    for source in sources:
        fn = getattr(source, "discover_cwds", None)
        if fn is None:
            continue
        try:
            cwds |= set(fn(since, until))
        except Exception:
            continue
    return cwds


def repo_map_as_str(cwd_map: dict[str, Path]) -> dict[str, str]:
    """Flatten a cwd→root Path map to cwd→str(root), matching the repo key the
    git source stamps on each ClosureEvent (``str(repo.resolve())``)."""
    return {cwd: str(root) for cwd, root in cwd_map.items()}
