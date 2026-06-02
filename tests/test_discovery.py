"""Tests for repo auto-discovery from session working directories."""

from __future__ import annotations

from datetime import datetime, timezone

from stress_levels.discovery import (
    collect_session_cwds,
    discover_repo_roots,
    repo_map_as_str,
    repo_root_for,
)

UTC = timezone.utc


def _mkrepo(path):
    (path / ".git").mkdir(parents=True)
    return path


def test_repo_root_for_finds_enclosing_repo(tmp_path):
    repo = _mkrepo(tmp_path / "proj")
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    assert repo_root_for(nested) == repo.resolve()


def test_repo_root_for_returns_repo_itself_when_cwd_is_root(tmp_path):
    repo = _mkrepo(tmp_path / "proj")
    assert repo_root_for(repo) == repo.resolve()


def test_repo_root_for_none_outside_any_repo(tmp_path):
    plain = tmp_path / "not_a_repo" / "sub"
    plain.mkdir(parents=True)
    assert repo_root_for(plain) is None


def test_repo_root_for_handles_empty_and_bad_input():
    assert repo_root_for("") is None


def test_discover_repo_roots_dedupes_and_maps(tmp_path):
    a = _mkrepo(tmp_path / "a")
    b = _mkrepo(tmp_path / "b")
    (a / "sub").mkdir()
    cwds = [str(a), str(a / "sub"), str(b), str(tmp_path / "loose")]
    roots, cwd_map = discover_repo_roots(cwds)
    # Two distinct repos; the loose (non-repo) cwd is dropped.
    assert roots == sorted([a.resolve(), b.resolve()])
    # Both cwds inside repo A map to A's root.
    assert cwd_map[str(a)] == a.resolve()
    assert cwd_map[str(a / "sub")] == a.resolve()
    assert cwd_map[str(b)] == b.resolve()
    assert str(tmp_path / "loose") not in cwd_map


def test_repo_map_as_str_flattens_paths(tmp_path):
    a = _mkrepo(tmp_path / "a")
    _, cwd_map = discover_repo_roots([str(a)])
    flat = repo_map_as_str(cwd_map)
    assert flat == {str(a): str(a.resolve())}
    assert all(isinstance(v, str) for v in flat.values())


def test_collect_session_cwds_uses_optional_source_hook():
    class _Src:
        def discover_cwds(self, since, until):
            return {"/x", "/y"}

    class _NoHook:
        pass  # no discover_cwds → skipped, not fatal

    class _Boom:
        def discover_cwds(self, since, until):
            raise RuntimeError("boom")  # swallowed

    cwds = collect_session_cwds(
        [_Src(), _NoHook(), _Boom()],
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC),
    )
    assert cwds == {"/x", "/y"}
