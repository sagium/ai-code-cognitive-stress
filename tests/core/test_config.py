"""Tests for configuration loading + validation (config.py)."""

from __future__ import annotations

import json
from datetime import time

import pytest

from ai_code_cognitive_stress.core.config import (
    _DEFAULTS_CONFIG_PATH, _parse_hhmm, load_config,
)


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
    cfg = load_config(_DEFAULTS_CONFIG_PATH)  # the tracked config.default.json
    assert cfg.codl.foreground_grace_minutes == 5
    assert cfg.codl.background_weight == 0.20


def test_shipped_config_has_no_work_window_override():
    """The shipped config.default.json deliberately omits work_window so that
    the default behaviour is personal inference (not a fixed pinned band)."""
    cfg = load_config(_DEFAULTS_CONFIG_PATH)
    assert cfg.work_window is None


# --- idle-close cutoff (shared liveness knob) ------------------------------

def test_shipped_config_has_idle_close_default():
    assert load_config(_DEFAULTS_CONFIG_PATH).idle_close_minutes == 180


def test_shipped_config_compact_widget_defaults_off():
    assert load_config(_DEFAULTS_CONFIG_PATH).compact_widget is False


# --- runtime config.json vs tracked defaults ------------------------------

def test_load_config_falls_back_to_defaults_when_runtime_absent(tmp_path, monkeypatch):
    """With no config.json present, load_config() reads config.default.json —
    so a fresh clone / installed wheel still works."""
    import ai_code_cognitive_stress.core.config as cfgmod
    monkeypatch.setattr(cfgmod, "_RUNTIME_CONFIG_PATH", tmp_path / "config.json")
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config()
    assert cfg.compact_widget is False          # the tracked default
    assert cfg.idle_close_minutes == 180


def test_load_config_prefers_runtime_over_defaults(tmp_path, monkeypatch):
    """When config.json exists it wins over config.default.json."""
    import ai_code_cognitive_stress.core.config as cfgmod
    runtime = tmp_path / "config.json"
    runtime.write_text(json.dumps({"compact_widget": True}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "_RUNTIME_CONFIG_PATH", runtime)
    cfgmod._CONFIG_CACHE.clear()
    assert load_config().compact_widget is True


def test_compact_widget_absent_falls_back_to_false(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({})), encoding="utf-8")
    assert load_config(cfg).compact_widget is False


def test_compact_widget_true_parsed(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"compact_widget": True})), encoding="utf-8")
    assert load_config(cfg).compact_widget is True


@pytest.mark.parametrize("bad", [1, "yes", None])
def test_compact_widget_rejects_non_bool(tmp_path, bad):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"compact_widget": bad})), encoding="utf-8")
    with pytest.raises(ValueError, match="compact_widget"):
        load_config(cfg)


def test_set_compact_widget_seeds_from_defaults_and_writes_runtime(tmp_path, monkeypatch):
    """With no config.json yet, set_compact_widget seeds it from the tracked
    defaults, flips only compact_widget, and the next load reflects it."""
    import ai_code_cognitive_stress.core.config as cfgmod
    runtime = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "_RUNTIME_CONFIG_PATH", runtime)
    cfgmod._CONFIG_CACHE.clear()

    assert cfgmod.set_compact_widget(True) is True
    assert runtime.exists()                       # seeded the live file
    written = json.loads(runtime.read_text(encoding="utf-8"))
    assert written["compact_widget"] is True
    # Other documented defaults are preserved (proves it seeded, not clobbered).
    assert "scoring" in written and "resumption" in written
    cfgmod._CONFIG_CACHE.clear()
    assert load_config(runtime).compact_widget is True


def test_set_compact_widget_toggles_existing_runtime(tmp_path, monkeypatch):
    """An existing config.json is updated in place — only the one key changes,
    every other user setting is preserved — and the cache is invalidated."""
    import ai_code_cognitive_stress.core.config as cfgmod
    runtime = tmp_path / "config.json"
    runtime.write_text(
        json.dumps({"compact_widget": True, "idle_close_minutes": 240}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfgmod, "_RUNTIME_CONFIG_PATH", runtime)
    cfgmod._CONFIG_CACHE.clear()
    assert load_config(runtime).compact_widget is True   # warm the cache

    assert cfgmod.set_compact_widget(False) is False
    written = json.loads(runtime.read_text(encoding="utf-8"))
    assert written["compact_widget"] is False
    assert written["idle_close_minutes"] == 240          # untouched
    # Cache was cleared, so a re-read sees the new value (not the stale True).
    assert load_config(runtime).compact_widget is False


def test_idle_close_absent_falls_back_to_default(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({})), encoding="utf-8")
    assert load_config(cfg).idle_close_minutes == 180


def test_idle_close_custom_value_parsed(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"idle_close_minutes": 240})), encoding="utf-8")
    assert load_config(cfg).idle_close_minutes == 240


@pytest.mark.parametrize("bad", [0, -5, "soon"])
def test_idle_close_rejects_non_positive(tmp_path, bad):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"idle_close_minutes": bad})), encoding="utf-8")
    with pytest.raises(ValueError, match="idle_close_minutes"):
        load_config(cfg)


def test_codl_block_missing_falls_back_to_defaults(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({})), encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.codl.foreground_grace_minutes == 5
    assert loaded.codl.background_weight == 0.20


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
    cfg = load_config(_DEFAULTS_CONFIG_PATH).resumption  # config.default.json
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


# --- widget_view (active timeframe tab) ------------------------------------

def test_shipped_config_has_widget_view_default_today():
    assert load_config(_DEFAULTS_CONFIG_PATH).widget_view == "today"


def test_widget_view_absent_falls_back_to_today(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({})), encoding="utf-8")
    assert load_config(cfg).widget_view == "today"


@pytest.mark.parametrize("view", ["today", "week", "month", "year"])
def test_widget_view_valid_values_parsed(tmp_path, view):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"widget_view": view})), encoding="utf-8")
    assert load_config(cfg).widget_view == view


@pytest.mark.parametrize("bad", ["day", "annual", "", 0, None])
def test_widget_view_rejects_invalid(tmp_path, bad):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(_ww({"widget_view": bad})), encoding="utf-8")
    with pytest.raises(ValueError, match="widget_view"):
        load_config(cfg)


def test_set_widget_view_seeds_from_defaults_and_writes_runtime(tmp_path, monkeypatch):
    """With no config.json yet, set_widget_view seeds it from the tracked
    defaults, writes only widget_view, and the next load reflects it."""
    import ai_code_cognitive_stress.core.config as cfgmod
    runtime = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "_RUNTIME_CONFIG_PATH", runtime)
    cfgmod._CONFIG_CACHE.clear()

    assert cfgmod.set_widget_view("week") == "week"
    assert runtime.exists()                        # seeded the live file
    written = json.loads(runtime.read_text(encoding="utf-8"))
    assert written["widget_view"] == "week"
    # Other documented defaults are preserved (proves it seeded, not clobbered).
    assert "scoring" in written and "resumption" in written
    cfgmod._CONFIG_CACHE.clear()
    assert load_config(runtime).widget_view == "week"


def test_set_widget_view_updates_existing_runtime(tmp_path, monkeypatch):
    """An existing config.json is updated in place — only widget_view changes,
    every other user setting is preserved — and the cache is invalidated."""
    import ai_code_cognitive_stress.core.config as cfgmod
    runtime = tmp_path / "config.json"
    runtime.write_text(
        json.dumps({"widget_view": "month", "idle_close_minutes": 240}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfgmod, "_RUNTIME_CONFIG_PATH", runtime)
    cfgmod._CONFIG_CACHE.clear()

    assert cfgmod.set_widget_view("year") == "year"
    written = json.loads(runtime.read_text(encoding="utf-8"))
    assert written["widget_view"] == "year"
    assert written["idle_close_minutes"] == 240    # untouched
    # Cache was cleared, so a re-read sees the new value.
    assert load_config(runtime).widget_view == "year"


def test_set_widget_view_rejects_invalid():
    import ai_code_cognitive_stress.core.config as cfgmod
    with pytest.raises(ValueError, match="widget_view"):
        cfgmod.set_widget_view("quarterly")
