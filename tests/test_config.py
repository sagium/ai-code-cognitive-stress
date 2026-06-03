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


def test_load_config_work_window_absent_yields_none(tmp_path):
    """Omitting work_window entirely → work_window is None (inference mode)."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.work_window is None


def test_load_config_work_window_null_yields_none(tmp_path):
    """Explicitly setting work_window to null → work_window is None."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"work_window": None}), encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.work_window is None


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


def test_shipped_config_has_no_work_window_override():
    """The shipped config.json deliberately omits work_window so that the
    default behaviour is personal inference (not a fixed pinned band)."""
    cfg = load_config()
    assert cfg.work_window is None


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


# --- resumption block --------------------------------------------------------

def test_shipped_config_has_resumption_defaults():
    """The shipped default carries the citation-anchored resumption priors."""
    cfg = load_config().resumption  # the real config.json
    assert cfg.threshold_minutes == 30
    assert cfg.full_decay_minutes == 120
    assert cfg.daily_ceiling == 4.0


def test_resumption_block_missing_falls_back_to_defaults(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({})), encoding="utf-8")
    r = load_config(cfg).resumption
    assert (r.threshold_minutes, r.full_decay_minutes, r.daily_ceiling) == (30, 120, 4.0)


def test_resumption_custom_values_parsed(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({
        "resumption": {"threshold_minutes": 45, "full_decay_minutes": 90,
                       "daily_ceiling": 6},
    })), encoding="utf-8")
    r = load_config(cfg).resumption
    assert (r.threshold_minutes, r.full_decay_minutes, r.daily_ceiling) == (45, 90, 6.0)


@pytest.mark.parametrize("key", ["threshold_minutes", "full_decay_minutes", "daily_ceiling"])
def test_resumption_rejects_non_positive(tmp_path, key):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"resumption": {key: 0}})), encoding="utf-8")
    with pytest.raises(ValueError, match=f"resumption.{key}"):
        load_config(cfg)
