"""Local-git closure + rework event source.

Reads two local signals per configured repository, both fully local and
read-only (no remote provider, nothing leaves the machine):

  * `git log`    → closure events (`commit`, `merge`) — "I closed a loop".
  * `git reflog` → rework events (`amend`, `rebase`, `reset`, `cherry_pick`)
    — history rewrites are recorded only in the reflog, never in `git log`.
    The metrics layer routes these to the Interruption axis as rework /
    self-interruption rather than to the Closure axis.

For GitHub / GitLab PR/MR merges specifically, the parent `gh`-CLI and
`glab`-CLI adapters extend the closure signal — but a plain commit at HEAD
of a feature branch is already a strong "I closed a loop" indicator.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .base import ClosureEvent, IngestStats

# Reflog selector with --date=… renders the time inside braces, e.g.
# "HEAD@{2026-06-02T10:00:00+00:00}". Pull the timestamp back out.
_REFLOG_SELECTOR_RE = re.compile(r"@\{(?P<ts>[^}]+)\}")


def _reflog_kind(subject: str) -> str | None:
    """Map a reflog subject (``%gs``) to a rework kind, or None to skip.

    Only history-*rewriting* operations are rework. Plain ``commit:`` /
    ``commit (initial):`` / ``merge …:`` / ``checkout:`` / ``pull:`` entries
    return None — commits and merges are sourced from ``git log`` instead, so
    counting them here too would double-count closures. We count one event per
    logical operation: rebases collapse to their single terminal ``(finish)``
    entry rather than one per replayed commit."""
    s = subject.strip()
    if s.startswith("commit (amend)"):
        return "amend"
    if s.startswith("reset:"):
        return "reset"
    if s.startswith("cherry-pick"):
        return "cherry_pick"
    # Rebases emit many entries (start / pick / squash / finish); count only
    # the terminal finish so one rebase = one rework event.
    if s.startswith("rebase") and "(finish)" in s:
        return "rebase"
    return None


def _parse_git_iso(ts_raw: str) -> datetime | None:
    """Parse a git ISO-8601 timestamp to a UTC-aware datetime, normalising the
    trailing 'Z' that Python < 3.11 won't accept. None on failure."""
    if ts_raw.endswith("Z"):
        ts_raw = ts_raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts_raw).astimezone(timezone.utc)
    except ValueError:
        return None


class GitRepoClosureSource:
    """Closure-event source backed by local `git log` + `git reflog`.

    `repos`: list of git repo paths to scan. If None, no events are
    emitted (the user has to opt in explicitly — we don't walk the disk
    looking for repos automatically).

    `identities`: the operator's own author identities (emails and/or names).
    Commit/merge events are emitted only when the commit's author matches one of
    them, so a shared-monorepo's teammate and merge-bot commits do NOT count as
    the operator's closures. When None/empty, identity scoping is off and every
    author's commits are emitted (legacy behaviour — only sane on solo repos).
    Pushes and rework are unaffected: they are read from this clone's own reflog
    and are self-scoped by construction regardless of `identities`.
    """

    name = "git"

    def __init__(
        self,
        repos: Iterable[Path] | None = None,
        identities: Iterable[str] | None = None,
    ) -> None:
        self.repos = [Path(r) for r in repos] if repos else []
        # Lower-cased for case-insensitive author matching.
        self.identities = {i.lower() for i in identities if i} if identities else set()

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
        # Stable per-repo key, used by metrics.py to match a stream's resolved
        # repo root against this repo's closures. Resolve to an absolute path so
        # same-basename repos don't collide.
        try:
            repo_key = str(repo.resolve())
        except OSError:
            repo_key = str(repo)
        # The log pass bumps files_scanned/kept; the reflog passes share the same
        # repo and only emit events, so file-level stats aren't double-counted.
        yield from self._collect_log(repo, repo_key, since, until, stats)
        yield from self._collect_pushes(repo, repo_key, since, until, stats)
        yield from self._collect_reflog(repo, repo_key, since, until, stats)

    def _collect_log(
        self,
        repo: Path,
        repo_key: str,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[ClosureEvent]:
        # Format: ISO timestamp \x1f branch-or-empty \x1f author-email \x1f
        #         author-name \x1f subject
        # %cI = committer-date ISO 8601 strict, %D = ref names, %ae/%an =
        # author email/name, %s = subject. Author is captured so we can scope
        # closures to the operator's own identities (see __init__): in a shared
        # monorepo, teammate and merge-bot commits must not close our loops.
        fmt = "%cI%x1f%D%x1f%ae%x1f%an%x1f%s"
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
            if len(parts) < 5:
                stats.lines_skipped_malformed += 1
                continue
            ts_raw, refs, email, name, subject = parts[:5]
            ts = _parse_git_iso(ts_raw)
            if ts is None:
                stats.lines_skipped_no_timestamp += 1
                continue
            stats.lines_decoded += 1
            if not (since <= ts <= until):
                continue
            # Identity scoping: drop commits not authored by the operator. A
            # teammate's or merge-bot's commit in a shared repo is real git
            # activity but is NOT the operator closing their own loop. Match on
            # either author email or author name (case-insensitive), since the
            # operator may commit under multiple accounts.
            if self.identities and not (
                email.lower() in self.identities
                or name.lower() in self.identities
            ):
                continue
            stats.events_emitted += 1
            kept_any = True
            branch = _branch_from_refs(refs)
            kind = "merge" if subject.lower().startswith("merge ") else "commit"
            yield ClosureEvent(
                ts=ts, kind=kind, repo=repo_key,
                branch=branch, title=subject, author=email or None,
            )
        if kept_any:
            stats.files_kept += 1

    def _collect_pushes(
        self,
        repo: Path,
        repo_key: str,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[ClosureEvent]:
        """Emit ``push`` closure events from this clone's remote-tracking
        reflogs. A `git push` writes an ``update by push`` entry to the reflog
        of the pushed ``refs/remotes/<remote>/<branch>`` ref; a `git fetch`/
        `pull` writes ``fetch``/``pull`` instead, so filtering on the push
        subject isolates the operator's own outbound shipments.

        Inherently self-scoped: only pushes performed FROM this clone land here,
        so no identity filter is needed (a server-side merge bot never writes to
        our local reflog). Note this signal is lossy in MR workflows — when a
        merged feature branch is pruned (``fetch --prune``), its remote-tracking
        ref and reflog go with it — so own-authored commits remain the primary
        denominator and push is the strongest signal *where it survives*.

        One push reflects onto several remote-tracking refs at once — the branch
        ref (``origin/main``) and the remote-HEAD pseudo-ref (``origin``) both get
        an identical ``update by push`` entry at the same instant — so we dedupe
        by timestamp to count one push per shipment, not one per touched ref.

        Parse failures are silent: the reflog is git-internal, not session data,
        so we don't pollute the ingest malformed/no-timestamp counters."""
        fmt = "%gd%x1f%gs"
        cmd = [
            "git", "-C", str(repo), "reflog",
            "--date=iso-strict",
            f"--format={fmt}",
            "--glob=refs/remotes/*",
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
        seen: set[datetime] = set()
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 2:
                continue
            selector, subject = parts[0], parts[1]
            if "update by push" not in subject.lower():
                continue
            m = _REFLOG_SELECTOR_RE.search(selector)
            if not m:
                continue
            ts = _parse_git_iso(m.group("ts"))
            if ts is None:
                continue
            if not (since <= ts <= until):
                continue
            if ts in seen:  # same push reflected onto another remote-tracking ref
                continue
            seen.add(ts)
            stats.events_emitted += 1
            yield ClosureEvent(
                ts=ts, kind="push", repo=repo_key,
                branch=None, title=subject.strip(),
            )

    def _collect_reflog(
        self,
        repo: Path,
        repo_key: str,
        since: datetime,
        until: datetime,
        stats: IngestStats,
    ) -> Iterator[ClosureEvent]:
        """Emit rework events (amend/rebase/reset/cherry_pick) from the HEAD
        reflog. History-rewriting operations live only here, never in git log.

        Parse failures are silent: the reflog is git-internal, not session
        data, so we don't pollute the ingest malformed/no-timestamp counters
        (which track session-log line health) with reflog noise."""
        # %gd = reflog selector; with --date=iso-strict it renders the entry's
        # own action time inside braces. %gs = reflog subject (the operation).
        fmt = "%gd%x1f%gs"
        cmd = [
            "git", "-C", str(repo), "reflog",
            "--date=iso-strict",
            f"--format={fmt}",
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
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 2:
                continue
            selector, subject = parts[0], parts[1]
            kind = _reflog_kind(subject)
            if kind is None:
                continue
            m = _REFLOG_SELECTOR_RE.search(selector)
            if not m:
                continue
            ts = _parse_git_iso(m.group("ts"))
            if ts is None:
                continue
            if not (since <= ts <= until):
                continue
            stats.events_emitted += 1
            yield ClosureEvent(
                ts=ts, kind=kind, repo=repo_key,
                branch=None, title=subject.strip(),
            )


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
