"""Tests for the shared scale/zone/colour logic."""

from __future__ import annotations

import pytest

from ai_code_cognitive_stress.output.scales import (
    CODL_ZONES,
    ZONE_COLORS,
    composite_label,
    composite_status,
    zone_color,
    zone_for,
)


@pytest.mark.parametrize("score,expected", [
    (-1.0, "0"), (0.0, "0"), (0.1, "1"), (35.0, "35"),
    (35.1, "36"), (99.1, "100"), (101.0, "100"),
])
def test_composite_label_ceil_rounds_and_caps(score, expected):
    assert composite_label(score) == expected


@pytest.mark.parametrize("value,expected_status", [
    (0.0, "good"),
    (1.5, "good"),     # upper bound is inclusive
    (1.51, "moderate"),
    (3.0, "moderate"),
    (3.01, "caution"),
    (4.0, "caution"),
    (4.01, "high"),
    (50.0, "high"),
])
def test_zone_for_codl_boundaries(value, expected_status):
    status, label = zone_for(value, CODL_ZONES)
    assert status == expected_status
    assert isinstance(label, str) and label


def test_zone_color_known_and_unknown():
    assert zone_color("good") == ZONE_COLORS["good"]
    assert zone_color("high") == ZONE_COLORS["high"]
    assert zone_color("nonsense") == "#999999"


# --- composite_status -------------------------------------------------------

def test_composite_status_zero_is_empty():
    assert composite_status(0.0, 25.0, 40.0) == ""
    assert composite_status(-5.0, None, None) == ""


@pytest.mark.parametrize("score,expected", [
    (10.0, "good"),     # < 40
    (39.9, "good"),
    (40.0, "caution"),  # < 70
    (69.9, "caution"),
    (70.0, "high"),
    (95.0, "high"),
])
def test_composite_status_calibrating_absolute_bands(score, expected):
    # No percentiles yet → absolute bands.
    assert composite_status(score, None, None) == expected


@pytest.mark.parametrize("score,expected", [
    (10.0, "good"),     # < p75 (25)
    (24.9, "good"),
    (25.0, "caution"),  # < p90 (40)
    (39.9, "caution"),
    (40.0, "high"),
    (80.0, "high"),
])
def test_composite_status_uses_personal_percentiles(score, expected):
    assert composite_status(score, 25.0, 40.0) == expected


def test_composite_status_needs_both_percentiles_or_falls_back():
    # Only one percentile present → treated as calibrating (absolute bands).
    assert composite_status(50.0, 25.0, None) == "caution"  # 40<=50<70
    assert composite_status(50.0, None, 40.0) == "caution"


def test_zone_for_returns_first_match_then_last_on_overflow():
    zones = [(1.0, "good", "low"), (2.0, "caution", "mid")]
    assert zone_for(0.5, zones) == ("good", "low")
    assert zone_for(1.0, zones) == ("good", "low")  # inclusive upper bound
    # Above every zone's upper bound → falls through to the last zone.
    assert zone_for(99.0, zones) == ("caution", "mid")
