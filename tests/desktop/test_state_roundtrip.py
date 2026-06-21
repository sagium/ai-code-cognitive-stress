"""
CLI state → card HTML round-trip tests.

These tests cover the full persistence cycle that all widget hosts rely on:

  1. Host calls `aicogstress --set-view X` or `--set-compact Y`
     (in Python: set_widget_view / set_compact_widget)
  2. Host calls `aicogstress --emit-html-card`
     (in Python: render_card_tabbed with cfg.widget_view / cfg.compact_widget)
  3. The rendered card must reflect the persisted values.

This is the exact path that was silently broken in all hosts (caught only
by manual testing): run() calls that didn't execute left config.json
unchanged, so every card refresh reverted to stale defaults.
"""
from __future__ import annotations

import json
import re
from datetime import date, timezone
from pathlib import Path

import pytest

from ai_code_cognitive_stress.core.config import (
    load_config,
    set_compact_widget,
    set_widget_view,
)
from ai_code_cognitive_stress.output.dayview import TimeframeView, build_dayview
from ai_code_cognitive_stress.output.widget_card import render_card_tabbed
from ai_code_cognitive_stress.pipeline.metrics import DayMetrics, StressProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _profile() -> StressProfile:
    return StressProfile(
        days={}, work_windows={}, local_tz_name="UTC",
        baseline_window_days=30, personal_optimum=2.0,
        composite_p50=20.0, composite_p75=30.0, composite_p90=50.0,
    )


def _dayview():
    return build_dayview(
        DayMetrics(day=date(2026, 6, 21), composite=5.0),
        None, _profile(), timezone.utc,
    )


def _views(keys=("today", "week", "month", "year")):
    dv = _dayview()
    return [TimeframeView(key=k, tab_label=k.capitalize(), view=dv) for k in keys]


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point _RUNTIME_CONFIG_PATH at a temp file and clear the cache."""
    import ai_code_cognitive_stress.core.config as cfgmod
    runtime = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "_RUNTIME_CONFIG_PATH", runtime)
    cfgmod._CONFIG_CACHE.clear()
    yield runtime
    cfgmod._CONFIG_CACHE.clear()


# ---------------------------------------------------------------------------
# Helpers: inspect the card HTML
# ---------------------------------------------------------------------------

def _tab_is_active(html: str, key: str) -> bool:
    """Return True if the tab button for `key` has class 'tab active'."""
    return bool(
        re.search(r'class="tab active"[^>]*data-view="' + re.escape(key) + '"', html)
        or re.search(r'data-view="' + re.escape(key) + r'"[^>]*class="tab active"', html)
    )


def _view_is_visible(html: str, key: str) -> bool:
    """Return True if the view div for `key` is NOT hidden."""
    return bool(
        re.search(r'class="view"[^>]*data-view="' + re.escape(key) + '"', html)
        or re.search(r'data-view="' + re.escape(key) + r'"[^>]*class="view"', html)
    )


def _view_is_hidden(html: str, key: str) -> bool:
    """Return True if the view div for `key` has 'hidden' in its class."""
    return bool(
        re.search(r'class="view hidden"[^>]*data-view="' + re.escape(key) + '"', html)
        or re.search(r'data-view="' + re.escape(key) + r'"[^>]*class="view hidden"', html)
    )


def _compact_value(html: str) -> str | None:
    """Return the data-compact attribute value on the .cogstress root, or None."""
    m = (
        re.search(r'class="cogstress[^"]*"[^>]*data-compact="([^"]+)"', html)
        or re.search(r'data-compact="([^"]+)"[^>]*class="cogstress', html)
    )
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# View persistence: set_widget_view → render_card_tabbed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("view", ["today", "week", "month", "year"])
def test_set_view_makes_tab_active_in_next_card(isolated_config, view):
    """After --set-view X the next card render must show X's tab as active."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_widget_view(view)
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    html = render_card_tabbed(_views(), active_view=cfg.widget_view, compact=cfg.compact_widget)

    assert _tab_is_active(html, view), f"tab for {view!r} not active after --set-view {view!r}"


@pytest.mark.parametrize("view", ["today", "week", "month", "year"])
def test_set_view_makes_view_visible_in_next_card(isolated_config, view):
    """The view div for the chosen key must not have the 'hidden' class."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_widget_view(view)
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    html = render_card_tabbed(_views(), active_view=cfg.widget_view, compact=cfg.compact_widget)

    assert _view_is_visible(html, view), f"view div for {view!r} is hidden after --set-view {view!r}"


@pytest.mark.parametrize("view", ["today", "week", "month", "year"])
def test_set_view_hides_all_other_views(isolated_config, view):
    """All views OTHER than the chosen one must have class 'view hidden'."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_widget_view(view)
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    html = render_card_tabbed(_views(), active_view=cfg.widget_view, compact=cfg.compact_widget)

    for other in ("today", "week", "month", "year"):
        if other != view:
            assert _view_is_hidden(html, other), (
                f"view div for {other!r} should be hidden when active view is {view!r}"
            )


# ---------------------------------------------------------------------------
# Compact persistence: set_compact_widget → render_card_tabbed
# ---------------------------------------------------------------------------

def test_set_compact_true_sets_data_compact_attribute(isolated_config):
    """After --set-compact true the card root must carry data-compact=\"true\"."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_compact_widget(True)
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    html = render_card_tabbed(_views(), active_view=cfg.widget_view, compact=cfg.compact_widget)

    assert _compact_value(html) == "true", (
        f"data-compact should be 'true' after --set-compact true, got {_compact_value(html)!r}"
    )


def test_set_compact_false_sets_data_compact_attribute(isolated_config):
    """After --set-compact false the card root must carry data-compact=\"false\"."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_compact_widget(False)
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    html = render_card_tabbed(_views(), active_view=cfg.widget_view, compact=cfg.compact_widget)

    assert _compact_value(html) == "false", (
        f"data-compact should be 'false' after --set-compact false, got {_compact_value(html)!r}"
    )


# ---------------------------------------------------------------------------
# Back-to-back: view + compact must not clobber each other
# ---------------------------------------------------------------------------

def test_set_view_does_not_reset_compact(isolated_config):
    """set_widget_view must preserve compact_widget — both write to the same
    config.json, and a read-modify-write race can drop the other key."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_compact_widget(True)
    set_widget_view("month")
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    assert cfg.widget_view == "month"
    assert cfg.compact_widget is True


def test_set_compact_does_not_reset_view(isolated_config):
    """set_compact_widget must preserve widget_view."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_widget_view("week")
    set_compact_widget(True)
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    assert cfg.widget_view == "week"
    assert cfg.compact_widget is True


def test_view_and_compact_both_reflected_in_card(isolated_config):
    """Both settings must appear correctly in the rendered card simultaneously."""
    import ai_code_cognitive_stress.core.config as cfgmod

    set_widget_view("month")
    set_compact_widget(True)
    cfgmod._CONFIG_CACHE.clear()
    cfg = load_config(isolated_config)

    html = render_card_tabbed(_views(), active_view=cfg.widget_view, compact=cfg.compact_widget)

    assert _tab_is_active(html, "month")
    assert _view_is_visible(html, "month")
    assert _view_is_hidden(html, "today")
    assert _compact_value(html) == "true"


# ---------------------------------------------------------------------------
# Config.json preservation: writes must not drop unrelated keys
# ---------------------------------------------------------------------------

def test_set_view_preserves_other_config_keys(isolated_config):
    isolated_config.write_text(
        json.dumps({"compact_widget": True, "widget_view": "today", "locale": "fr"}),
        encoding="utf-8",
    )
    import ai_code_cognitive_stress.core.config as cfgmod
    cfgmod._CONFIG_CACHE.clear()

    set_widget_view("week")

    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["widget_view"] == "week"
    assert data["compact_widget"] is True
    assert data["locale"] == "fr"


def test_set_compact_preserves_other_config_keys(isolated_config):
    isolated_config.write_text(
        json.dumps({"compact_widget": False, "widget_view": "month", "locale": "de"}),
        encoding="utf-8",
    )
    import ai_code_cognitive_stress.core.config as cfgmod
    cfgmod._CONFIG_CACHE.clear()

    set_compact_widget(True)

    data = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert data["compact_widget"] is True
    assert data["widget_view"] == "month"
    assert data["locale"] == "de"


# ---------------------------------------------------------------------------
# Fallback: unknown active_view gracefully falls back to first tab
# ---------------------------------------------------------------------------

def test_unknown_active_view_falls_back_to_first_tab():
    """If a host passes a view key not in the views list (e.g. after a card
    schema change), render_card_tabbed must fall back to the first view rather
    than leaving all tabs inactive. load_config already rejects unknown keys,
    so this tests the renderer's own guard directly."""
    html = render_card_tabbed(_views(), active_view="quarterly", compact=False)

    # Exactly one tab must be active
    active_count = len(re.findall(r'class="tab active"', html))
    assert active_count == 1, f"Expected exactly 1 active tab, got {active_count}"
    # Exactly one view must be visible
    visible_views = [
        m.group(1)
        for m in re.finditer(r'class="view"[^>]*data-view="([^"]+)"', html)
    ]
    assert len(visible_views) == 1, f"Expected exactly 1 visible view, got {visible_views}"
