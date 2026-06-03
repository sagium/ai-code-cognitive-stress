"""Tests for the i18n layer: catalog lookup, fallback, and locale selection."""

from __future__ import annotations

from datetime import date

import pytest

from stress_levels import i18n


@pytest.fixture(autouse=True)
def _reset_locale():
    """Every test starts and ends on the default (config) locale."""
    i18n.set_locale(None)
    yield
    i18n.set_locale(None)
    # Drop any synthetic catalogs a test injected.
    i18n._catalog_cache.pop("xx", None)


def test_en_catalog_resolves_keys():
    assert i18n.t("advice.good") == "Chill"
    assert i18n.t("axis.unit.codl", peak=3) == "avg · peak 3 streams"


def test_unknown_key_returns_the_key_itself():
    assert i18n.t("no.such.key") == "no.such.key"


def test_unknown_locale_falls_back_to_english():
    i18n.set_locale("xx")
    assert i18n.t("advice.good") == "Chill"


def test_partial_catalog_overrides_then_falls_back():
    i18n._catalog_cache["xx"] = {"advice.good": "Tranquilo"}
    i18n.set_locale("xx")
    assert i18n.t("advice.good") == "Tranquilo"
    assert i18n.t("advice.high") == "Cooked"  # missing key → English


def test_malformed_locale_code_rejected():
    with pytest.raises(ValueError):
        i18n.set_locale("../etc/passwd")


def test_plurals():
    assert i18n.tn("report.days_active", 1) == "1 day active"
    assert i18n.tn("report.days_active", 2) == "2 days active"


def test_date_helpers_match_previous_strftime_output():
    d = date(2026, 5, 29)  # a Friday
    assert i18n.day_label(d) == d.strftime("%A %d %B %Y")
    assert i18n.day_month_label(d) == d.strftime("%a %d %b")
    assert i18n.month_year_label(2026, 5) == "May 2026"
    assert i18n.weekday_names_short() == [
        "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
    ]


def test_en_catalog_has_no_stray_placeholders():
    """Every {param} in the en catalog must survive a format() with dummy
    values — catches typoed braces that would crash at render time."""
    catalog = i18n._catalog("en")

    class _Any(dict):
        def __missing__(self, key):
            return "x"

    for key, value in catalog.items():
        if isinstance(value, str) and not key.startswith("_"):
            value.format_map(_Any())
