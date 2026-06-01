"""Tests for population calibration + the config-overridable scoring."""

from __future__ import annotations

import json

import pytest

from stress_levels.calibrate import (
    calibrate,
    load_exports,
    pool_day_records,
    render_report,
)


# ---------------------------------------------------------------------------
# Synthetic export fixtures

def _day(codl, interr, closure=0.0, composite=10.0, weekday="Mon",
         ww=("09:00", "17:00"), peak=None):
    d = {
        "date": "2025-07-14", "weekday": weekday,
        "codl_avg": codl, "interruption_rate": interr,
        "closure_deficit": closure, "composite": composite,
        "off_hours_minutes": 0, "work_window": list(ww),
    }
    if peak is not None:
        d["debug"] = {
            "peak_headcount": peak,
            "sessions": [{"duration_min": 30}, {"duration_min": 90}],
        }
    return d


def _export(days, participant="p1", schema="ai-code-cognitive-stress.research-export.v2"):
    return {
        "schema": schema, "participant": participant,
        "profile": {"days": days},
    }


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# load_exports

def test_load_accepts_v1_and_v2_skips_foreign(tmp_path):
    _write(tmp_path, "v2.json", _export([_day(1, 1, peak=2)]))
    _write(tmp_path, "v1.json", _export(
        [_day(1, 1)], schema="ai-code-cognitive-stress.research-export.v1"))
    _write(tmp_path, "foreign.json", {"schema": "something.else", "x": 1})
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    exports, warnings = load_exports([str(tmp_path)])
    assert len(exports) == 2  # v1 + v2 only
    assert any("foreign" in w for w in warnings)
    assert any("broken" in w for w in warnings)


def test_load_missing_path_warns(tmp_path):
    exports, warnings = load_exports([str(tmp_path / "nope.json")])
    assert exports == []
    assert any("not found" in w for w in warnings)


# ---------------------------------------------------------------------------
# ceilings

def test_suggested_ceiling_tracks_population_p95():
    # codl_avg 1..20 → p95 sits near the top of the range.
    days = [_day(float(c), 1.0) for c in range(1, 21)]
    result = calibrate(pool_day_records([_export(days)]))
    sugg = result["ceilings"]["suggested"]["codl"]
    dist = result["ceilings"]["distribution"]["codl_avg"]
    assert dist["p90"] <= sugg <= dist["max"]


# ---------------------------------------------------------------------------
# weights

def test_zero_variance_axis_gets_zero_weight():
    # closure is constant → no discriminative variance → weight 0; the other
    # two share the weight and the vector sums to 1.
    days = [_day(float(c), float(c % 5), closure=0.0) for c in range(1, 16)]
    w = calibrate(pool_day_records([_export(days)]))["weights"]
    codl_w, int_w, clo_w = w["suggested"]
    assert clo_w == 0.0
    assert codl_w > 0 and int_w > 0
    assert sum(w["suggested"]) == pytest.approx(1.0, abs=1e-3)
    assert set(w["correlations"]) == {
        "codl_interruption", "codl_closure", "interruption_closure",
    }


# ---------------------------------------------------------------------------
# coverage + reference percentiles + v1 graceful

def test_coverage_and_reference_populated():
    days = [_day(2.0, 3.0, weekday="Sat", peak=4),
            _day(1.0, 1.0, weekday="Mon", peak=1)]
    result = calibrate(pool_day_records([_export(days)]))
    cov = result["coverage"]
    assert cov["weekend_share"] == pytest.approx(0.5)
    assert cov["peak_headcount"]["max"] == 4
    assert cov["session_duration_min"] is not None
    assert cov["archetypes"]  # non-empty bucket map
    assert result["reference_percentiles"]["composite"]["p50"] is not None


def test_v1_only_input_degrades_gracefully():
    # No debug block → no peak/session coverage, but everything else works.
    days = [_day(2.0, 3.0), _day(1.0, 1.0)]
    result = calibrate(pool_day_records([_export(
        days, schema="ai-code-cognitive-stress.research-export.v1")]))
    assert result["coverage"]["peak_headcount"] is None
    assert result["coverage"]["session_duration_min"] is None
    assert result["n_day_records"] == 2


def test_report_text_has_caveat_and_scoring_block():
    result = calibrate(pool_day_records([_export([_day(2.0, 3.0, peak=2)])]))
    machine, text = render_report(result)
    assert "Unsupervised calibration" in text
    assert '"scoring"' in text
    assert machine["suggested_scoring_config"]["weights"]


def test_multiple_participants_counted():
    e1 = _export([_day(1, 1), _day(2, 2)], participant="a")
    e2 = _export([_day(3, 3)], participant="b")
    result = calibrate(pool_day_records([e1, e2]))
    assert result["n_participants"] == 2
    assert result["n_day_records"] == 3


# ---------------------------------------------------------------------------
# config-overridable scoring

def _config(tmp_path, body: dict):
    from stress_levels.config import load_config
    p = tmp_path / "config.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return load_config(p)


def test_scoring_defaults_when_absent(tmp_path):
    cfg = _config(tmp_path, {})
    assert cfg.scoring.codl_ceiling == 5.0
    assert cfg.scoring.interruption_ceiling == 10.0
    assert cfg.scoring.weights == pytest.approx((1 / 3, 1 / 3, 1 / 3))


def test_scoring_parsed_from_block(tmp_path):
    cfg = _config(tmp_path, {"scoring": {
        "codl_ceiling": 6.5, "interruption_ceiling": 8.0,
        "weights": [0.5, 0.3, 0.2],
    }})
    assert cfg.scoring.codl_ceiling == 6.5
    assert cfg.scoring.weights == (0.5, 0.3, 0.2)


def test_scoring_rejects_bad_values(tmp_path):
    with pytest.raises(ValueError, match="codl_ceiling"):
        _config(tmp_path, {"scoring": {"codl_ceiling": 0}})
    with pytest.raises(ValueError, match="weights"):
        _config(tmp_path, {"scoring": {"weights": [0.5, 0.5]}})


# ---------------------------------------------------------------------------
# metrics consume the scoring overrides

def test_composite_score_default_matches_legacy():
    from stress_levels.metrics import _composite_score
    # codl 0.5, int 0.5, closure 0, equal weights → 33.33
    assert _composite_score(2.5, 5.0, 0.0) == pytest.approx(100.0 / 3)


def test_composite_score_respects_ceiling_override():
    from stress_levels.metrics import _composite_score
    base = _composite_score(2.5, 5.0, 0.0)
    lower = _composite_score(2.5, 5.0, 0.0, codl_ceiling=2.5)  # codl_norm → 1.0
    assert lower > base
    assert lower == pytest.approx(50.0)


def test_composite_score_normalizes_arbitrary_weights():
    from stress_levels.metrics import _composite_score
    # all axes saturated, weights don't sum to 1 → still 100
    assert _composite_score(99, 99, 1.0, weights=(2, 2, 2)) == pytest.approx(100.0)
