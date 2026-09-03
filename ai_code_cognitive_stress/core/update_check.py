"""Offline "is this checkout behind upstream?" check for the widget card.

Runs local git plumbing only — ``rev-list`` counts commits already on disk, so
this never touches the network (the tool's core constraint). Refreshing the
remote-tracking ref is the desktop hosts' job: each widget runs ``git fetch``
on a slow timer, keyed off ``--print-checkout``.

Every failure mode — no git binary, a plain (non-checkout) install, a detached
HEAD, a branch with no upstream — resolves to "nothing to report", so the card
renders exactly as before.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import load_config

_TIMEOUT_SECONDS = 3.0

# Never let git prompt: a credential prompt on a widget's 60s timer would hang
# the render with no terminal to answer it.
_GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


def checkout_root() -> Path | None:
    """The git checkout this package is installed from, or None.

    An editable/source install puts the package inside the repo; a wheel from
    an index does not, and then there is nothing to pull.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():  # dir, or a file for a linked worktree
            return parent
    return None


def _git(root: Path, *args: str) -> str | None:
    """Run a local git command in ``root``; None on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, env=_GIT_ENV,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def commits_behind(root: Path | None = None) -> int:
    """Commits the checkout's branch is behind its upstream — 0 when there is
    nothing to pull, or when the question doesn't apply."""
    root = root if root is not None else checkout_root()
    if root is None:
        return 0
    count = _git(root, "rev-list", "--count", "HEAD..@{upstream}")
    try:
        return max(0, int(count or 0))
    except ValueError:
        return 0


def update_notice() -> str:
    """The card's bottom warning line, or "" when there's nothing to say.

    Gated by ``update_check`` in config so anyone deliberately parked on an old
    commit can silence it.
    """
    from .i18n import tn

    if not load_config().update_check:
        return ""
    behind = commits_behind()
    return tn("card.update_available", behind) if behind else ""
