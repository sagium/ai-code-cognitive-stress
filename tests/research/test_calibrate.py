"""Tests for population calibration + the config-overridable scoring."""

from __future__ import annotations

import json

import pytest

from ai_code_cognitive_stress.research.calibrate import (
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
# sample-size gate

def _pool(n_participants, days_each, codl_of):
    """Build a pool of n_participants, each with days_each days; codl_of(i, j)
    sets the codl_avg so the population has real spread."""
    exports = []
    for i in range(n_participants):
        days = [_day(float(codl_of(i, j)), 1.0 + j % 4) for j in range(days_each)]
        exports.append(_export(days, participant=f"p{i}"))
    return pool_day_records(exports)


def test_thin_sample_keeps_current_priors_and_warns():
    # 2 participants / 10 day-records is well under the floor → no change advised.
    records = _pool(2, 5, lambda i, j: 1 + 3 * j)
    result = calibrate(records)
    rel = result["reliability"]
    assert rel["meets_minimums"] is False
    assert rel["warnings"]  # explains why
    # The pasteable block keeps the literature priors verbatim...
    cfg = result["suggested_scoring_config"]
    assert cfg["codl_ceiling"] == 5.0
    assert cfg["interruption_ceiling"] == 10.0
    assert cfg["weights"] == pytest.approx([1 / 3, 1 / 3, 1 / 3], abs=1e-3)
    # ...while the fitted values stay visible for inspection.
    assert result["ceilings"]["suggested"]["codl"] != 5.0
    _, text = render_report(result)
    assert "INSUFFICIENT SAMPLE" in text


def test_sufficient_sample_graduates_suggestions():
    # 20 participants x 6 days = 120 records, closure present on all → clears the
    # floor, so the fitted values flow into the pasteable block.
    records = _pool(20, 6, lambda i, j: 1 + (i + j) % 18)
    result = calibrate(records)
    rel = result["reliability"]
    assert rel["meets_minimums"] is True
    assert rel["warnings"] == []
    cfg = result["suggested_scoring_config"]
    assert cfg["codl_ceiling"] == result["ceilings"]["suggested"]["codl"]
    assert cfg["weights"] == result["weights"]["suggested"]
    _, text = render_report(result)
    assert "INSUFFICIENT SAMPLE" not in text
    assert '"scoring"' in text


def test_thin_closure_axis_flagged_even_when_pool_is_large():
    # Plenty of participants/day-records, but almost no days carry a closure
    # value → the closure-specific warning fires.
    exports = []
    for i in range(20):
        days = [
            _day(float(1 + (i + j) % 18), 2.0,
                 closure=(0.4 if (i == 0 and j < 2) else None))
            for j in range(6)
        ]
        exports.append(_export(days, participant=f"p{i}"))
    result = calibrate(pool_day_records(exports))
    rel = result["reliability"]
    assert rel["n_closure_records"] < rel["minimums"]["closure_records"]
    assert any("closure" in w for w in rel["warnings"])


# ---------------------------------------------------------------------------
# config-overridable scoring

def _config(tmp_path, body: dict):
    from ai_code_cognitive_stress.core.config import load_config
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
    from ai_code_cognitive_stress.pipeline.metrics import _composite_score
    # codl 0.5, int 0.5, closure 0, equal weights → 33.33
    assert _composite_score(2.5, 5.0, 0.0) == pytest.approx(100.0 / 3)


def test_composite_score_respects_ceiling_override():
    from ai_code_cognitive_stress.pipeline.metrics import _composite_score
    base = _composite_score(2.5, 5.0, 0.0)
    lower = _composite_score(2.5, 5.0, 0.0, codl_ceiling=2.5)  # codl_norm → 1.0
    assert lower > base
    assert lower == pytest.approx(50.0)


def test_composite_score_normalizes_arbitrary_weights():
    from ai_code_cognitive_stress.pipeline.metrics import _composite_score
    # all axes saturated, weights don't sum to 1 → still 100
    assert _composite_score(99, 99, 1.0, weights=(2, 2, 2)) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# felt-load / subjective_rating (phase 2)

def _day_with_rating(codl, interr, closure=0.0, composite=10.0, rating=None):
    d = _day(codl, interr, closure, composite)
    d["subjective_rating"] = rating
    return d


def test_pool_day_records_carries_subjective_rating():
    """pool_day_records passes subjective_rating through to DayRecord."""
    days = [
        _day_with_rating(1.0, 2.0, rating=0),
        _day_with_rating(2.0, 3.0, rating=2),
        _day_with_rating(1.5, 2.5, rating=None),
    ]
    records = pool_day_records([_export(days)])
    assert records[0].subjective_rating == 0
    assert records[1].subjective_rating == 2
    assert records[2].subjective_rating is None


def test_v3_export_no_field_loads_as_none():
    """v3-style exports (no subjective_rating field) load with rating=None."""
    days = [_day(1.0, 2.0)]  # no rating field
    records = pool_day_records([_export(days, schema="ai-code-cognitive-stress.research-export.v3")])
    assert records[0].subjective_rating is None


def _labeled_pool(n_participants=6, days_each=6):
    """Pool where every day has a rating — enough for felt-load correlations."""
    exports = []
    for i in range(n_participants):
        days = []
        for j in range(days_each):
            codl = float(1 + (i + j) % 5)
            interr = float(2 + j % 3)
            rating = (i + j) % 3  # 0, 1, or 2
            days.append(_day_with_rating(codl, interr, rating=rating))
        exports.append(_export(days, participant=f"p{i}"))
    return pool_day_records(exports)


def test_felt_load_below_gate_no_correlations():
    """Fewer than _MIN_LABELED_RECORDS labeled records → correlations=None,
    coverage count reported, unsupervised caveat preserved."""
    from ai_code_cognitive_stress.research.calibrate import CAVEAT, _MIN_LABELED_RECORDS
    # Only 2 labeled records — well below gate.
    days = [
        _day_with_rating(1.0, 2.0, rating=0),
        _day_with_rating(2.0, 3.0, rating=1),
        _day_with_rating(1.5, 2.5, rating=None),
    ]
    records = pool_day_records([_export(days)])
    assert len([r for r in records if r.subjective_rating is not None]) < _MIN_LABELED_RECORDS
    result = calibrate(records)
    fl = result["felt_load"]
    assert fl["n_labeled"] == 2
    assert fl["correlations_with_felt_load"] is None
    assert result["caveat"] == CAVEAT  # unsupervised caveat unchanged


def test_felt_load_above_gate_has_correlations():
    """Enough labeled records → felt_load includes correlation dict."""
    from ai_code_cognitive_stress.research.calibrate import _CAVEAT_WITH_LABELS
    records = _labeled_pool(n_participants=6, days_each=6)
    n_labeled = sum(1 for r in records if r.subjective_rating is not None)
    assert n_labeled >= 30  # gate
    result = calibrate(records)
    fl = result["felt_load"]
    assert fl["n_labeled"] == n_labeled
    assert fl["label_coverage"] is not None
    corr = fl["correlations_with_felt_load"]
    assert corr is not None
    assert set(corr) == {"codl_norm", "interruption_norm", "closure_norm", "composite_norm"}
    # All correlations must be floats or None (None only if a series is constant).
    for v in corr.values():
        assert v is None or isinstance(v, float)
    assert result["caveat"] == _CAVEAT_WITH_LABELS


def test_felt_load_section_in_report_text_with_labels():
    """render_report output includes the felt-load section with correlations."""
    records = _labeled_pool()
    result = calibrate(records)
    _, text = render_report(result)
    assert "Felt-load labels" in text
    assert "correlations with felt load" in text
    assert "codl_norm" in text


def test_felt_load_section_in_report_text_without_labels():
    """render_report output includes felt-load section even when below gate."""
    days = [_day(1.0, 2.0)]  # no rating
    records = pool_day_records([_export(days)])
    result = calibrate(records)
    _, text = render_report(result)
    assert "Felt-load labels" in text
    # Should mention needing more data, not show correlation table.
    assert "correlations with felt load" not in text


def test_v3_exports_still_calibrate_without_felt_load():
    """v3 exports (no field) round-trip through calibrate without errors."""
    days = [_day(float(c), 1.0 + c % 3) for c in range(1, 10)]
    records = pool_day_records([_export(
        days, schema="ai-code-cognitive-stress.research-export.v3")])
    result = calibrate(records)
    assert result["felt_load"]["n_labeled"] == 0
    assert result["felt_load"]["correlations_with_felt_load"] is None
    # Unsupervised path intact.
    assert result["n_day_records"] == 9
