"""Tests for ClaudeCodeSessionSource."""

from __future__ import annotations

import json
from datetime import timezone

from ai_code_cognitive_stress.adapters import ClaudeCodeSessionSource
from ai_code_cognitive_stress.pipeline.ingest import (
    AssistantMessageEvent,
    IngestStats,
    UserMessageEvent,
)

from .conftest import claude_record, utc


def test_yields_typed_events_with_keys(tmp_path):
    projects = tmp_path / "claude-projects"
    proj = projects / "-home-x"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            claude_record("user", "2026-05-15T10:00:00.000Z"),
            claude_record("assistant", "2026-05-15T10:00:05.000Z"),
        ]),
        encoding="utf-8",
    )
    src = ClaudeCodeSessionSource(projects_dir=projects)
    stats = IngestStats()
    items = list(src.collect(utc(2026, 5, 1), utc(2026, 5, 31), stats))
    assert len(items) == 2
    for event, key, version in items:
        assert key.startswith("claude-code:")
        assert isinstance(version, float)
    assert isinstance(items[0][0], UserMessageEvent)
    assert isinstance(items[1][0], AssistantMessageEvent)


def test_parse_iso_utc_normalises_naive_and_offset():
    from ai_code_cognitive_stress.adapters.claude_code import _parse_iso_utc
    assert _parse_iso_utc("2026-05-15T10:00:00Z").tzinfo is timezone.utc
    naive = _parse_iso_utc("2026-05-15T10:00:00")
    assert naive.tzinfo is timezone.utc and naive.hour == 10
    off = _parse_iso_utc("2026-05-15T12:00:00+02:00")
    assert off.hour == 10


def test_discovery_skips_non_dash_dirs_and_unreadable_files(tmp_path):
    projects = tmp_path / "claude-projects"
    (projects / "ignored").mkdir(parents=True)
    (projects / "ignored" / "s.jsonl").write_text(
        json.dumps(claude_record("user", "2026-05-15T10:00:00.000Z")),
        encoding="utf-8",
    )
    good = projects / "-home-x"
    good.mkdir()
    (good / "weird.jsonl").mkdir()  # a directory, not a file
    stats = IngestStats()
    items = list(ClaudeCodeSessionSource(projects_dir=projects).collect(
        utc(2026, 5, 1), utc(2026, 5, 31), stats))
    assert items == []
