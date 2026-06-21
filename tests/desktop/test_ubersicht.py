"""
Static-analysis guards for the Übersicht JSX widget host.

These tests read the widget source and assert structural properties that
must hold for the widget to work correctly in Übersicht's JSX runtime.

Each test documents the specific bug it guards against — all four were
caught only through manual testing and have no other coverage.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_JSX = Path(__file__).parents[2] / "desktop" / "ubersicht" / "ai-code-cognitive-stress.jsx"


@pytest.fixture(scope="module")
def src() -> str:
    return _JSX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Bug: run() was used without import — ReferenceError, silently swallowed
# ---------------------------------------------------------------------------

def test_run_is_imported_from_uebersicht(src):
    """In Übersicht JSX, `run` is not a global — it must be imported from
    'uebersicht'. Without the import every run() call throws ReferenceError
    that React's event system swallows; the DOM update before the call still
    works so the bug is invisible until the next refresh reverts the state."""
    assert (
        'import { run } from "uebersicht"' in src
        or "import { run } from 'uebersicht'" in src
    )


# ---------------------------------------------------------------------------
# Bug: Übersicht's /bin/sh doesn't source ~/.zshenv so ~/.local/bin is absent
# ---------------------------------------------------------------------------

def test_command_wrapped_in_login_shell(src):
    """Übersicht executes `command` and run() via /bin/sh which does not
    source ~/.zshenv or ~/.zprofile, so uv/pipx installs in ~/.local/bin are
    invisible. Every shell-out must use a login shell (zsh -lc / bash -lc)
    that does source those files."""
    assert "zsh -lc" in src or "zsh -l -c" in src or "bash -lc" in src or "bash -l -c" in src


def test_run_calls_use_same_login_shell_wrapper(src):
    """The login-shell wrapper must be applied to run() side-effect calls,
    not only to the main `command` export."""
    # The _sh helper (or equivalent) must be called inside run()
    assert "run(_sh(" in src or 'run(`/bin/zsh' in src or "run('/bin/zsh" in src


# ---------------------------------------------------------------------------
# Bug: no localStorage — state reverted on every 60-second card refresh
# ---------------------------------------------------------------------------

def test_localstorage_saves_view_on_click(src):
    """Clicking a tab writes to localStorage so the choice survives the next
    60-second card refresh (which replaces the DOM from a fresh CLI call).
    config.json alone is insufficient because run() may execute after the
    next refresh has already started."""
    assert "localStorage.setItem" in src
    # The key used for view storage
    assert "aicogstress:view" in src or "_KEY_VIEW" in src


def test_localstorage_saves_compact_on_click(src):
    """Compact state must also be written to localStorage on every toggle."""
    assert "aicogstress:compact" in src or "_KEY_COMPACT" in src


def test_localstorage_state_applied_after_render(src):
    """localStorage values must be re-applied after every render cycle, not
    only on mount. An inline callback ref `ref={(el) => ...}` creates a new
    function each render, causing React to call it after every
    dangerouslySetInnerHTML update."""
    assert "ref={(el)" in src or "ref={ (el)" in src


# ---------------------------------------------------------------------------
# Bug: grade chip had no visual feedback in Übersicht (in-page script inert)
# ---------------------------------------------------------------------------

def test_grade_chip_removes_ungraded_nag(src):
    """The card's in-page <script> is inert under dangerouslySetInnerHTML.
    The JSX click handler must replicate its immediate visual feedback:
    remove data-ungraded from the root so the nag disappears before the
    next CLI re-render arrives."""
    assert "data-ungraded" in src


def test_grade_chip_lights_selected_chip(src):
    """The JSX handler must toggle .sel on the clicked chip immediately,
    mirroring what the in-page script does on KDE/Windows/GTK."""
    assert '"sel"' in src or "'sel'" in src


# ---------------------------------------------------------------------------
# Security: validate inputs before shelling out
# ---------------------------------------------------------------------------

def test_grade_chip_validates_day_format(src):
    """day must match ISO date before it reaches the shell."""
    assert r"\d{4}-\d{2}-\d{2}" in src


def test_grade_chip_validates_grade_range(src):
    """grade must be validated as 0, 1, or 2 before shelling out."""
    assert r"[0-2]" in src


def test_tab_key_validated_before_run(src):
    """Tab key must be validated against the allowed set before run()."""
    assert "today|week|month|year" in src or "(today|week|month|year)" in src
