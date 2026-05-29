"""Tests for configuration loading + validation (config.py)."""

from __future__ import annotations

import json
from datetime import time

import pytest

from stress_levels.config import _parse_hhmm, load_config


def test_parse_hhmm_accepts_hhmm_and_hhmmss():
    assert _parse_hhmm("09:30") == time(9, 30)
    assert _parse_hhmm("09:30:15") == time(9, 30, 15)


@pytest.mark.parametrize("bad", ["25:00", "09:99", "1:2:3:4"])
def test_parse_hhmm_rejects_out_of_range_or_malformed(bad):
    with pytest.raises(ValueError, match="invalid time"):
        _parse_hhmm(bad)


def test_load_config_rejects_end_before_start(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"work_window": {"start": "18:00", "end": "09:00"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be after start"):
        load_config(cfg)
