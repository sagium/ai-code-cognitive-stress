"""Tests for the SessionSource plugin layer + concrete adapters.

ClaudeCodeSessionSource is the main one (its parsing is exercised
extensively by tests/test_ingest.py already, via the top-level
`collect()` orchestrator). These specs cover the protocol contract
plus the Codex and Aider adapters end-to-end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from stress_levels.ingest import (
    AssistantMessageEvent,
    IngestStats,
    ToolResultEvent,
    ToolUseEvent,
    UserMessageEvent,
)
from stress_levels.sources import (
    AiderSessionSource,
    ClaudeCodeSessionSource,
    CodexSessionSource,
    GitRepoClosureSource,
    SessionSource,
    default_sources,
)


UTC = timezone.utc


def _utc(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Protocol contract

def test_built_in_sources_all_satisfy_the_protocol():
    """isinstance() works via @runtime_checkable Protocol."""
    for klass in (ClaudeCodeSessionSource, CodexSessionSource, AiderSessionSource):
        inst = klass()
        assert isinstance(inst, SessionSource)
        assert isinstance(inst.name, str) and inst.name
        # is_available is a method, returns bool (without crashing)
        assert isinstance(inst.is_available(), bool)


def test_collect_yields_triples_event_key_version(tmp_path):
    """The plugin contract is `(Event, str, float)` per yielded item."""
    src = ClaudeCodeSessionSource(projects_dir=tmp_path / "no-such")
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 1, 1), _utc(2026, 12, 31), stats))
    # Empty dir → empty stream, but the type contract still holds.
    assert items == []


# ---------------------------------------------------------------------------
# ClaudeCodeSessionSource (sanity — full coverage is in test_ingest.py)

def _claude_record(rec_type, ts, session_id="sess-1", content=None):
    return {
        "type": rec_type, "timestamp": ts, "sessionId": session_id,
        "uuid": f"u-{rec_type}-{ts}", "cwd": "/home/test",
        "gitBranch": "main",
        "message": {"role": rec_type, "content": content or []},
    }


def test_claude_code_source_yields_typed_events_with_keys(tmp_path):
    projects = tmp_path / "claude-projects"
    proj = projects / "-home-x"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            _claude_record("user", "2026-05-15T10:00:00.000Z"),
            _claude_record("assistant", "2026-05-15T10:00:05.000Z"),
        ]),
        encoding="utf-8",
    )
    src = ClaudeCodeSessionSource(projects_dir=projects)
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert len(items) == 2
    for event, key, version in items:
        assert key.startswith("claude-code:")
        assert isinstance(version, float)
    assert isinstance(items[0][0], UserMessageEvent)
    assert isinstance(items[1][0], AssistantMessageEvent)


# ---------------------------------------------------------------------------
# CodexSessionSource

def test_codex_source_parses_role_based_jsonl(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "abc.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            {"role": "user", "content": "build a thing",
             "timestamp": "2026-05-15T10:00:00Z"},
            {"role": "assistant",
             "content": [
                 {"type": "function_call",
                  "function": {"name": "shell"},
                  "id": "call-1"},
             ],
             "timestamp": "2026-05-15T10:00:05Z"},
            {"role": "tool", "tool_use_id": "call-1",
             "content": "result", "timestamp": "2026-05-15T10:00:07Z"},
        ]),
        encoding="utf-8",
    )
    src = CodexSessionSource(sessions_dir=sessions)
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert "UserMessageEvent" in kinds
    assert "AssistantMessageEvent" in kinds
    assert "ToolUseEvent" in kinds
    assert "ToolResultEvent" in kinds


def test_codex_source_handles_string_content(tmp_path):
    """Codex sometimes writes assistant 'content' as a plain string —
    treat as a message with no per-block tool events."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({
            "role": "assistant", "content": "just a string reply",
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    src = CodexSessionSource(sessions_dir=sessions)
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert len(items) == 1
    assert isinstance(items[0][0], AssistantMessageEvent)


def test_codex_source_missing_dir_returns_no_events(tmp_path):
    src = CodexSessionSource(sessions_dir=tmp_path / "nope")
    stats = IngestStats()
    assert list(src.collect(_utc(2026, 1, 1), _utc(2026, 12, 31), stats)) == []
    assert src.is_available() is False


def test_codex_source_drops_records_without_timestamp(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({"role": "user", "content": "hi"}),
        encoding="utf-8",
    )
    src = CodexSessionSource(sessions_dir=sessions)
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert items == []
    assert stats.lines_skipped_no_timestamp == 1


# ---------------------------------------------------------------------------
# AiderSessionSource

def test_aider_source_parses_markdown_turns(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    history = proj / ".aider.chat.history.md"
    history.write_text(
        "\n".join([
            "#### 2026-05-15 09:00:00",
            "> please refactor the helper",
            "",
            "#### 2026-05-15 09:00:10",
            "Sure, here's a diff:",
            "",
            "```diff",
            "- old",
            "+ new",
            "```",
            "",
            "#### 2026-05-15 09:01:00",
            "> looks good",
            "",
        ]),
        encoding="utf-8",
    )
    src = AiderSessionSource(roots=[proj])
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    # 2 user messages + 1 assistant message + 1 tool_use (the diff block)
    assert kinds.count("UserMessageEvent") == 2
    assert kinds.count("AssistantMessageEvent") == 1
    assert kinds.count("ToolUseEvent") == 1


def test_aider_source_skips_history_files_with_old_mtime(tmp_path):
    import os
    proj = tmp_path / "myproj"
    proj.mkdir()
    history = proj / ".aider.chat.history.md"
    history.write_text(
        "#### 2025-01-01 09:00:00\n> old\n", encoding="utf-8",
    )
    backdate = (datetime(2025, 1, 1, tzinfo=UTC)).timestamp()
    os.utime(history, (backdate, backdate))

    src = AiderSessionSource(roots=[proj])
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert items == []


def test_aider_source_returns_false_for_missing_history(tmp_path):
    src = AiderSessionSource(roots=[tmp_path / "no-such"])
    assert src.is_available() is False


# ---------------------------------------------------------------------------
# GitRepoClosureSource

def test_git_closure_source_emits_commit_events(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
           # Force a deterministic commit date in 2026.
           "GIT_AUTHOR_DATE": "2026-05-15T10:00:00+00:00",
           "GIT_COMMITTER_DATE": "2026-05-15T10:00:00+00:00",
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "first"],
                   check=True, env=env)

    src = GitRepoClosureSource(repos=[repo])
    stats = IngestStats()
    items = list(src.collect(_utc(2026, 1, 1), _utc(2026, 12, 31), stats))
    assert len(items) == 1
    ev = items[0]
    assert ev.kind == "commit"
    assert ev.ts.year == 2026 and ev.ts.month == 5


def test_git_closure_source_is_unavailable_without_repos():
    src = GitRepoClosureSource()
    assert src.is_available() is False


# ---------------------------------------------------------------------------
# default_sources()

def test_default_sources_always_returns_at_least_claude_code(tmp_path, monkeypatch):
    """Even when no source's data dir exists, default_sources falls back
    to a Claude Code source pointing at the default (likely empty) dir
    so the CLI behaves predictably."""
    # Override every source's discovery to return "not available".
    import stress_levels.sources as srcmod
    monkeypatch.setattr(
        srcmod.ClaudeCodeSessionSource, "is_available", lambda self: False,
    )
    monkeypatch.setattr(
        srcmod.CodexSessionSource, "is_available", lambda self: False,
    )
    monkeypatch.setattr(
        srcmod.AiderSessionSource, "is_available", lambda self: False,
    )
    result = default_sources()
    assert len(result) == 1
    assert isinstance(result[0], srcmod.ClaudeCodeSessionSource)


def test_default_sources_picks_up_available_tools(tmp_path, monkeypatch):
    import stress_levels.sources as srcmod
    # Pretend Codex is available, others are not.
    monkeypatch.setattr(
        srcmod.ClaudeCodeSessionSource, "is_available", lambda self: False,
    )
    monkeypatch.setattr(
        srcmod.CodexSessionSource, "is_available", lambda self: True,
    )
    monkeypatch.setattr(
        srcmod.AiderSessionSource, "is_available", lambda self: False,
    )
    result = default_sources()
    names = [s.name for s in result]
    assert names == ["codex"]


# ---------------------------------------------------------------------------
# get_day_aggregates accepts a sources list

def test_get_day_aggregates_uses_supplied_sources(tmp_path):
    """Pass an explicit single-source list; the aggregate orchestrator
    runs that and only that, ignoring the default Claude Code source."""
    from stress_levels.aggregate import get_day_aggregates
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "abc.jsonl").write_text(
        json.dumps({
            "role": "user", "content": "hi",
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    from datetime import date
    aggs, stats = get_day_aggregates(
        date(2026, 5, 1), date(2026, 5, 31),
        cache_dir=tmp_path / "cache",
        now=_utc(2026, 5, 28),
        local_tz=UTC,
        sources=[CodexSessionSource(sessions_dir=sessions)],
    )
    assert aggs[datetime(2026, 5, 15).date()].stream_count == 1


# ---------------------------------------------------------------------------
# GitRepoClosureSource — internals exercised via synthetic `git log` output
# (the happy path uses a real repo above; these cover the parse/skip branches
# that real git won't reliably produce).

def test_git_closure_is_available_true_with_repo(tmp_path, monkeypatch):
    from stress_levels.sources import git_closure
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(git_closure.shutil, "which", lambda n: "/usr/bin/git")
    assert git_closure.GitRepoClosureSource(repos=[repo]).is_available() is True


def test_git_closure_is_unavailable_when_git_binary_missing(tmp_path, monkeypatch):
    from stress_levels.sources import git_closure
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(git_closure.shutil, "which", lambda n: None)
    src = git_closure.GitRepoClosureSource(repos=[repo])
    assert src.is_available() is False
    # collect() also short-circuits when git is absent.
    assert list(src.collect(_utc(2026, 1, 1), _utc(2026, 12, 31), IngestStats())) == []


def test_git_closure_skips_dir_without_dotgit(tmp_path, monkeypatch):
    from stress_levels.sources import git_closure
    repo = tmp_path / "repo"
    repo.mkdir()  # no .git
    monkeypatch.setattr(git_closure.shutil, "which", lambda n: "/usr/bin/git")
    stats = IngestStats()
    src = git_closure.GitRepoClosureSource(repos=[repo])
    assert list(src.collect(_utc(2026, 1, 1), _utc(2026, 12, 31), stats)) == []
    assert stats.files_scanned == 0


def test_git_closure_parses_crafted_log_branches(tmp_path, monkeypatch):
    from stress_levels.sources import git_closure
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    SEP = "\x1f"
    lines = [
        "malformed-no-separators",                       # < 3 fields → malformed
        f"not-a-timestamp{SEP}{SEP}bad ts",              # unparseable ts → skipped
        f"2020-01-01T00:00:00+00:00{SEP}{SEP}too old",   # outside window → dropped
        f"2026-05-15T10:00:00Z{SEP}HEAD -> main{SEP}fix the bug",
        f"2026-05-15T11:00:00Z{SEP}{SEP}Merge branch 'x'",
    ]

    class _Fake:
        returncode = 0
        stdout = "\n".join(lines)

    monkeypatch.setattr(git_closure.shutil, "which", lambda n: "/usr/bin/git")
    monkeypatch.setattr(git_closure.subprocess, "run", lambda *a, **k: _Fake())
    stats = IngestStats()
    src = git_closure.GitRepoClosureSource(repos=[repo])
    items = list(src.collect(_utc(2026, 1, 1), _utc(2026, 12, 31), stats))
    kinds = {(e.kind, e.branch) for e in items}
    assert ("commit", "main") in kinds
    assert ("merge", None) in kinds
    assert len(items) == 2
    assert stats.lines_skipped_malformed == 1
    assert stats.lines_skipped_no_timestamp == 1


def test_git_closure_empty_on_nonzero_exit(tmp_path, monkeypatch):
    from stress_levels.sources import git_closure
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    class _Fail:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(git_closure.shutil, "which", lambda n: "/usr/bin/git")
    monkeypatch.setattr(git_closure.subprocess, "run", lambda *a, **k: _Fail())
    assert list(git_closure.GitRepoClosureSource(repos=[repo]).collect(
        _utc(2026, 1, 1), _utc(2026, 12, 31), IngestStats())) == []


def test_git_closure_empty_when_subprocess_raises(tmp_path, monkeypatch):
    from stress_levels.sources import git_closure
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    def _boom(*a, **k):
        raise OSError("git blew up")

    monkeypatch.setattr(git_closure.shutil, "which", lambda n: "/usr/bin/git")
    monkeypatch.setattr(git_closure.subprocess, "run", _boom)
    assert list(git_closure.GitRepoClosureSource(repos=[repo]).collect(
        _utc(2026, 1, 1), _utc(2026, 12, 31), IngestStats())) == []


def test_branch_from_refs_variants():
    from stress_levels.sources.git_closure import _branch_from_refs
    assert _branch_from_refs("") is None
    assert _branch_from_refs("HEAD -> main, origin/main") == "main"
    assert _branch_from_refs("tag: v1.0") is None
    assert _branch_from_refs("origin/main") is None
    assert _branch_from_refs("HEAD") is None
    assert _branch_from_refs("feature-x") == "feature-x"


# ---------------------------------------------------------------------------
# CodexSessionSource — skip/error branches

def test_codex_skips_blank_and_malformed_lines(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        "\n".join([
            "",                       # blank → skipped
            "{not valid json",        # malformed → skipped
            json.dumps({"role": "user", "content": "ok",
                        "timestamp": "2026-05-15T10:00:00Z"}),
        ]),
        encoding="utf-8",
    )
    stats = IngestStats()
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert len(items) == 1
    assert stats.lines_skipped_malformed == 1


def test_codex_user_tool_result_block(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1",
                         "is_error": True}],
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["UserMessageEvent", "ToolResultEvent"]


def test_codex_assistant_skips_non_dict_blocks(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({
            "role": "assistant",
            "content": ["a plain string block",
                        {"type": "tool_call", "name": "shell", "id": "c1"}],
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), IngestStats()))
    kinds = [type(ev).__name__ for ev, _, _ in items]
    assert kinds == ["AssistantMessageEvent", "ToolUseEvent"]


def test_codex_accepts_numeric_epoch_timestamp(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    epoch = _utc(2026, 5, 15, 10).timestamp()
    (sessions / "s.jsonl").write_text(
        json.dumps({"role": "user", "content": "hi", "timestamp": epoch}),
        encoding="utf-8",
    )
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), IngestStats()))
    assert len(items) == 1


def test_codex_drops_record_with_unparseable_timestamp(tmp_path):
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(
        json.dumps({"role": "user", "content": "hi", "timestamp": "garbage"}),
        encoding="utf-8",
    )
    stats = IngestStats()
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert items == []
    assert stats.lines_skipped_no_timestamp == 1


def test_codex_skips_file_with_old_mtime(tmp_path):
    import os
    sessions = tmp_path / "codex"
    sessions.mkdir()
    f = sessions / "old.jsonl"
    f.write_text(
        json.dumps({"role": "user", "content": "hi",
                    "timestamp": "2026-05-15T10:00:00Z"}),
        encoding="utf-8",
    )
    backdate = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    os.utime(f, (backdate, backdate))
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), IngestStats()))
    assert items == []


def test_codex_handles_unreadable_session_file(tmp_path):
    """A path matching *.jsonl that is actually a directory raises OSError on
    open — the adapter swallows it and moves on."""
    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "weird.jsonl").mkdir()  # a directory, not a file
    stats = IngestStats()
    items = list(CodexSessionSource(sessions_dir=sessions).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert items == []


# ---------------------------------------------------------------------------
# AiderSessionSource — default $HOME discovery + timestamp parsing

def test_aider_default_discovery_walks_home(tmp_path, monkeypatch):
    from pathlib import Path as _Path
    home = tmp_path / "home"
    # One level deep and two levels deep both discovered; hidden dir skipped.
    (home / "proj1").mkdir(parents=True)
    (home / "proj1" / ".aider.chat.history.md").write_text(
        "#### 2026-05-15 10:00:00\n> hi there\n", encoding="utf-8")
    (home / "ws" / "sub").mkdir(parents=True)
    (home / "ws" / "sub" / ".aider.chat.history.md").write_text(
        "#### 2026-05-16 10:00:00\n> deeper\n", encoding="utf-8")
    (home / ".hidden").mkdir()
    monkeypatch.setattr(_Path, "home", lambda: home)

    src = AiderSessionSource()  # roots=None → default walk
    assert src.is_available() is True
    items = list(src.collect(_utc(2026, 5, 1), _utc(2026, 5, 31), IngestStats()))
    assert len(items) == 2


def test_aider_uses_file_mtime_when_header_lacks_timestamp(tmp_path):
    import os
    proj = tmp_path / "p"
    proj.mkdir()
    history = proj / ".aider.chat.history.md"
    history.write_text("#### >\n> hello there\n", encoding="utf-8")
    intime = _utc(2026, 5, 15, 10).timestamp()
    os.utime(history, (intime, intime))
    items = list(AiderSessionSource(roots=[proj]).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), IngestStats()))
    # One user turn, timestamp falls back to the file's mtime.
    assert len(items) == 1
    assert isinstance(items[0][0], UserMessageEvent)


def test_aider_extract_timestamp_handles_invalid_and_missing():
    from stress_levels.sources.aider import _extract_timestamp
    assert _extract_timestamp("no timestamp here") is None
    # Regex matches but the date is impossible → ValueError → None.
    assert _extract_timestamp("2026-13-45 10:00:00") is None
    ts = _extract_timestamp("2026-05-15 10:00:00")
    assert ts is not None and ts.year == 2026 and ts.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# ClaudeCodeSessionSource — timestamp parsing + discovery edge cases

def test_claude_parse_iso_utc_normalises_naive_and_offset():
    from stress_levels.sources.claude_code import _parse_iso_utc
    # Trailing Z.
    assert _parse_iso_utc("2026-05-15T10:00:00Z").tzinfo is timezone.utc
    # Naive (no tz) → assumed UTC.
    naive = _parse_iso_utc("2026-05-15T10:00:00")
    assert naive.tzinfo is timezone.utc and naive.hour == 10
    # Explicit offset → converted to UTC.
    off = _parse_iso_utc("2026-05-15T12:00:00+02:00")
    assert off.hour == 10


def test_claude_discovery_skips_non_dash_dirs_and_unreadable_files(tmp_path):
    projects = tmp_path / "claude-projects"
    # A normal (non "-"-prefixed) project dir is ignored.
    (projects / "ignored").mkdir(parents=True)
    (projects / "ignored" / "s.jsonl").write_text(
        json.dumps(_claude_record("user", "2026-05-15T10:00:00.000Z")),
        encoding="utf-8")
    # A "-"-prefixed dir with an unreadable (directory) *.jsonl is swallowed.
    good = projects / "-home-x"
    good.mkdir()
    (good / "weird.jsonl").mkdir()
    stats = IngestStats()
    items = list(ClaudeCodeSessionSource(projects_dir=projects).collect(
        _utc(2026, 5, 1), _utc(2026, 5, 31), stats))
    assert items == []
