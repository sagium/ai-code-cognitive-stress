"""Shared fixtures and helpers for adapter tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone


UTC = timezone.utc


def utc(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def claude_record(rec_type, ts, session_id="sess-1", content=None):
    return {
        "type": rec_type, "timestamp": ts, "sessionId": session_id,
        "uuid": f"u-{rec_type}-{ts}", "cwd": "/home/test",
        "gitBranch": "main",
        "message": {"role": rec_type, "content": content or []},
    }
