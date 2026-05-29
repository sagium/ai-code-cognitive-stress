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


# --- codl engagement-weighting block ---------------------------------------

def _ww(data: dict) -> dict:
    data.setdefault("work_window", {"start": "09:00", "end": "19:00"})
    return data


def test_shipped_config_has_codl_defaults():
    cfg = load_config()  # the real config.json
    assert cfg.codl.foreground_grace_minutes == 5
    assert cfg.codl.background_weight == 0.25


def test_codl_block_missing_falls_back_to_defaults(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({})), encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.codl.foreground_grace_minutes == 5
    assert loaded.codl.background_weight == 0.25


def test_codl_block_custom_values_parsed(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({
        "codl": {"foreground_grace_minutes": 10, "background_weight": 0.4},
    })), encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.codl.foreground_grace_minutes == 10
    assert loaded.codl.background_weight == 0.4


@pytest.mark.parametrize("weight", [-0.1, 1.5, "lots"])
def test_codl_rejects_out_of_range_weight(tmp_path, weight):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"codl": {"background_weight": weight}})),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="background_weight"):
        load_config(cfg)


def test_codl_rejects_negative_grace(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"codl": {"foreground_grace_minutes": -3}})),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="foreground_grace_minutes"):
        load_config(cfg)
