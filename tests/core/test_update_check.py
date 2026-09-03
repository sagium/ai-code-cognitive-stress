"""Tests for the offline behind-upstream check (core/update_check.py).

Everything here runs against throwaway local repos — the "remote" is another
directory on disk, so no test ever touches the network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ai_code_cognitive_stress.core.update_check import (
    checkout_root, commits_behind,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True,
        capture_output=True, text=True,
    )


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


@pytest.fixture
def clone_with_upstream(tmp_path: Path) -> tuple[Path, Path]:
    """A clone tracking an origin, both one commit in."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    _commit(origin, "first")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(clone)],
        check=True, capture_output=True, text=True,
    )
    return origin, clone


def test_up_to_date_checkout_is_not_behind(clone_with_upstream):
    _origin, clone = clone_with_upstream
    assert commits_behind(clone) == 0


def test_counts_commits_the_upstream_ref_is_ahead_by(clone_with_upstream):
    origin, clone = clone_with_upstream
    _commit(origin, "second")
    _commit(origin, "third")
    # The count only moves once the remote-tracking ref is refreshed — that
    # refresh is the desktop hosts' job, never the tool's.
    assert commits_behind(clone) == 0
    _git(clone, "fetch", "--quiet")
    assert commits_behind(clone) == 2


def test_local_commits_ahead_do_not_count_as_behind(clone_with_upstream):
    _origin, clone = clone_with_upstream
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    _commit(clone, "local-work")
    assert commits_behind(clone) == 0


def test_branch_without_an_upstream_reports_nothing(tmp_path: Path):
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "only")
    assert commits_behind(repo) == 0


def test_non_checkout_install_reports_nothing(tmp_path: Path):
    assert commits_behind(tmp_path) == 0


def test_checkout_root_finds_the_repo_the_package_lives_in():
    root = checkout_root()
    # None for a wheel install; a real repo root when run from a source tree.
    assert root is None or (root / ".git").exists()
