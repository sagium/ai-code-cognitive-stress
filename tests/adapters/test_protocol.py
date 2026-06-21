"""Tests for the SessionSource protocol contract and default_sources()."""

from __future__ import annotations

import json
from datetime import datetime

from ai_code_cognitive_stress.adapters import (
    ClaudeCodeSessionSource,
    CodexSessionSource,
    SessionSource,
    default_sources,
)
from ai_code_cognitive_stress.pipeline.ingest import IngestStats

from .conftest import UTC, utc


def test_built_in_sources_all_satisfy_the_protocol():
    """isinstance() works via @runtime_checkable Protocol."""
    for klass in (ClaudeCodeSessionSource, CodexSessionSource):
        inst = klass()
        assert isinstance(inst, SessionSource)
        assert isinstance(inst.name, str) and inst.name
        assert isinstance(inst.is_available(), bool)


def test_collect_yields_triples_event_key_version(tmp_path):
    """The plugin contract is `(Event, str, float)` per yielded item."""
    src = ClaudeCodeSessionSource(projects_dir=tmp_path / "no-such")
    stats = IngestStats()
    items = list(src.collect(utc(2026, 1, 1), utc(2026, 12, 31), stats))
    assert items == []


def test_default_sources_always_returns_at_least_claude_code(monkeypatch):
    """Even when no source's data dir exists, default_sources falls back
    to a Claude Code source so the CLI behaves predictably."""
    import ai_code_cognitive_stress.adapters as srcmod
    monkeypatch.setattr(
        srcmod.ClaudeCodeSessionSource, "is_available", lambda self: False,
    )
    monkeypatch.setattr(
        srcmod.CodexSessionSource, "is_available", lambda self: False,
    )
    result = default_sources()
    assert len(result) == 1
    assert isinstance(result[0], srcmod.ClaudeCodeSessionSource)


def test_default_sources_picks_up_available_tools(monkeypatch):
    import ai_code_cognitive_stress.adapters as srcmod
    monkeypatch.setattr(
        srcmod.ClaudeCodeSessionSource, "is_available", lambda self: False,
    )
    monkeypatch.setattr(
        srcmod.CodexSessionSource, "is_available", lambda self: True,
    )
    result = default_sources()
    assert [s.name for s in result] == ["codex"]


def test_get_day_aggregates_uses_supplied_sources(tmp_path):
    """Pass an explicit single-source list; the aggregate orchestrator
    runs that and only that, ignoring the default Claude Code source."""
    from datetime import date

    from ai_code_cognitive_stress.pipeline.aggregate import get_day_aggregates

    sessions = tmp_path / "codex"
    sessions.mkdir()
    (sessions / "abc.jsonl").write_text(
        json.dumps({
            "role": "user", "content": "hi",
            "timestamp": "2026-05-15T10:00:00Z",
        }),
        encoding="utf-8",
    )
    aggs, stats = get_day_aggregates(
        date(2026, 5, 1), date(2026, 5, 31),
        cache_dir=tmp_path / "cache",
        now=utc(2026, 5, 28),
        local_tz=UTC,
        sources=[CodexSessionSource(sessions_dir=sessions)],
    )
    assert aggs[datetime(2026, 5, 15).date()].stream_count == 1
