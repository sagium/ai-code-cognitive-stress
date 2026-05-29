"""Local-git closure-event source.

Iterates `git log` output in one or more configured repositories to
emit ClosureEvents for commits and merges. These are the cleanest
closure markers we can get without depending on a remote provider.

For GitHub / GitLab PR/MR merges specifically, the parent `gh`-CLI and
`glab`-CLI adapters extend this signal — but a plain commit at HEAD of
a feature branch is already a strong "I closed a loop" indicator.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .base import ClosureEvent, IngestStats


class GitRepoClosureSource:
    """Closure-event source backed by `git log`.

    `repos`: list of git repo paths to scan. If None, no events are
    emitted (the user has to opt in explicitly — we don't walk the disk
    looking for repos automatically).
    """

    name = "git"

    def __init__(self, repos: Iterable[Path] | None = None) -> None:
        self.repos = [Path(r) for r in repos] if repos else []

    def is_available(self) -> bool:
        if not self.repos:
            return False
        if shutil.which("git") is None:
            return False
        return any(
            (r / ".git").exists() or (r / ".git").is_file()
            for r in self.repos
        )

    def collect(
        self,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[ClosureEvent]:
        if shutil.which("git") is None:
            return
        for repo in self.repos:
            yield from self._collect_one(repo, since, until, stats)

    def _collect_one(
        self,
        repo: Path,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[ClosureEvent]:
        if not (repo / ".git").exists() and not (repo / ".git").is_file():
            return
        stats.files_scanned += 1
        # Format: ISO timestamp \x1f branch-or-empty \x1f subject
        # %cI = committer-date ISO 8601 strict, %D = ref names, %s = subject
        fmt = "%cI%x1f%D%x1f%s"
        since_arg = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        until_arg = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        cmd = [
            "git", "-C", str(repo), "log",
            "--all",
            f"--since={since_arg}", f"--until={until_arg}",
            f"--pretty=format:{fmt}",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=30, check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return
        if result.returncode != 0:
            return
        kept_any = False
        for line in result.stdout.splitlines():
            stats.lines_total += 1
            parts = line.split("\x1f")
            if len(parts) < 3:
                stats.lines_skipped_malformed += 1
                continue
            ts_raw, refs, subject = parts[0], parts[1], parts[2]
            # Git's strict-ISO `%cI` emits a trailing 'Z' for UTC; Python's
            # fromisoformat only accepts that from 3.11 on, so normalise it
            # for 3.10 compatibility.
            if ts_raw.endswith("Z"):
                ts_raw = ts_raw[:-1] + "+00:00"
            try:
                ts = datetime.fromisoformat(ts_raw).astimezone(timezone.utc)
            except ValueError:
                stats.lines_skipped_no_timestamp += 1
                continue
            stats.lines_decoded += 1
            if not (since <= ts <= until):
                continue
            stats.events_emitted += 1
            kept_any = True
            branch = _branch_from_refs(refs)
            kind = "merge" if subject.lower().startswith("merge ") else "commit"
            yield ClosureEvent(
                ts=ts, kind=kind, repo=repo.name,
                branch=branch, title=subject,
            )
        if kept_any:
            stats.files_kept += 1


def _branch_from_refs(refs: str) -> str | None:
    """`git log --pretty=%D` produces e.g. 'HEAD -> main, origin/main,
    tag: v1.0'. Pull the first plain branch name, ignoring 'HEAD ->'
    prefix and tag entries."""
    if not refs:
        return None
    for ref in (r.strip() for r in refs.split(",")):
        if ref.startswith("HEAD -> "):
            return ref[len("HEAD -> ") :].strip() or None
        if ref.startswith("tag: "):
            continue
        if ref.startswith("HEAD") or "/" in ref:
            # e.g. "origin/main" — skip remote, prefer local.
            continue
        if ref:
            return ref
    return None
