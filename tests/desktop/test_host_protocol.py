"""
Bridge-protocol consistency tests across all desktop widget hosts.

The card's in-page <script> (widget_card.py _TAB_SCRIPT) signals events to
the host by setting document.title to strings of the form:

  cogstress:rate:<YYYY-MM-DD>:<0|1|2>:<nonce>
  cogstress:compact:<0|1>:<nonce>
  cogstress:view:<today|week|month|year>:<nonce>
  cogstress:h:<px>

Three hosts parse these signals via regex (KDE QML, GTK Python, Windows PS1).
The Übersicht JSX host uses direct click delegation instead of the title bridge,
so it is not included here.

These tests verify that:
  1. The card actually emits strings in those formats (script-source check).
  2. Each host's regex accepts valid signals — including nonce-less variants
     that the Übersicht in-page script could emit in older widget card versions.
  3. Each host's regex rejects invalid inputs (injection guard).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).parents[2]
_CARD = _REPO / "ai_code_cognitive_stress" / "output" / "widget_card.py"
_KDE  = _REPO / "desktop" / "plasmoid" / "org.ai-code-cognitive-stress.plasmoid" / "contents" / "ui" / "main.qml"
_GTK  = _REPO / "desktop" / "gtk" / "cognitive-stress.py"
_WIN  = _REPO / "desktop" / "windows" / "cognitive-stress.ps1"

# Canonical signal examples produced by the card's in-page script.
_RATE_WITH_NONCE    = "cogstress:rate:2026-06-21:1:42"
_COMPACT_WITH_NONCE = "cogstress:compact:1:99"
_VIEW_WITH_NONCE    = "cogstress:view:month:7"
_HEIGHT             = "cogstress:h:420"

# Nonce-less variants (hosts must tolerate both).
_RATE_NO_NONCE    = "cogstress:rate:2026-06-21:2"
_COMPACT_NO_NONCE = "cogstress:compact:0"
_VIEW_NO_NONCE    = "cogstress:view:today"

# Strings that must NOT match (injection / typo guard).
_RATE_INVALID_GRADE  = "cogstress:rate:2026-06-21:3"   # grade out of range
_RATE_INVALID_DATE   = "cogstress:rate:20260621:1"      # date format wrong
_VIEW_INVALID_KEY    = "cogstress:view:quarterly"       # unknown view
_COMPACT_INVALID_BIT = "cogstress:compact:2"            # bit must be 0 or 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _py_regexes(text: str, fragment: str) -> list[str]:
    """Extract Python re.match() pattern strings that contain `fragment`."""
    found = re.findall(r're\.match\(r?"([^"]+)"', text)
    found += re.findall(r"re\.match\(r?'([^']+)'", text)
    return [p for p in found if fragment in p]


def _qml_regexes(text: str, fragment: str) -> list[str]:
    """Extract JS regex literal patterns /.../ used in QML that contain `fragment`."""
    # JS regexes in QML appear as /pattern/.exec(...)
    found = re.findall(r'/([^/\n]+)/\.exec', text)
    return [p for p in found if fragment in p]


def _ps_regexes(text: str, fragment: str) -> list[str]:
    """Extract PowerShell -match patterns that contain `fragment`."""
    found = re.findall(r"-match\s+'([^']+)'", text)
    return [p for p in found if fragment in p]


def _assert_matches(patterns: list[str], signal: str, label: str) -> None:
    assert patterns, f"{label}: no matching regex found"
    for p in patterns:
        assert re.match(p, signal), (
            f"{label} regex {p!r} did not match {signal!r}"
        )


def _assert_no_match(patterns: list[str], signal: str, label: str) -> None:
    assert patterns, f"{label}: no matching regex found"
    for p in patterns:
        assert not re.match(p, signal), (
            f"{label} regex {p!r} incorrectly matched {signal!r}"
        )


# ---------------------------------------------------------------------------
# Card: in-page script emits the correct signal strings
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def card_src() -> str:
    return _CARD.read_text(encoding="utf-8")


def test_card_emits_rate_signal(card_src):
    assert "cogstress:rate:" in card_src


def test_card_emits_compact_signal(card_src):
    assert "cogstress:compact:" in card_src


def test_card_emits_view_signal(card_src):
    assert "cogstress:view:" in card_src


def test_card_emits_height_signal(card_src):
    assert "cogstress:h:" in card_src


def test_card_rate_includes_nonce(card_src):
    """A nonce suffix on the rate signal lets hosts detect re-clicks on the
    same chip (same day + grade → identical string without the nonce)."""
    assert "gradeNonce" in card_src or "Date.now()" in card_src


def test_card_compact_includes_nonce(card_src):
    assert "resizeNonce" in card_src or "Date.now()" in card_src


# ---------------------------------------------------------------------------
# GTK Python host
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gtk_src() -> str:
    return _GTK.read_text(encoding="utf-8")


def test_gtk_rate_regex_accepts_signal_with_nonce(gtk_src):
    _assert_matches(_py_regexes(gtk_src, "cogstress:rate"), _RATE_WITH_NONCE, "GTK rate")


def test_gtk_rate_regex_accepts_signal_without_nonce(gtk_src):
    _assert_matches(_py_regexes(gtk_src, "cogstress:rate"), _RATE_NO_NONCE, "GTK rate (no nonce)")


def test_gtk_compact_regex_accepts_signal_with_nonce(gtk_src):
    _assert_matches(_py_regexes(gtk_src, "cogstress:compact"), _COMPACT_WITH_NONCE, "GTK compact")


def test_gtk_compact_regex_accepts_signal_without_nonce(gtk_src):
    _assert_matches(_py_regexes(gtk_src, "cogstress:compact"), _COMPACT_NO_NONCE, "GTK compact (no nonce)")


def test_gtk_view_regex_accepts_all_valid_keys(gtk_src):
    pats = _py_regexes(gtk_src, "cogstress:view")
    for key in ("today", "week", "month", "year"):
        _assert_matches(pats, f"cogstress:view:{key}", f"GTK view ({key})")
        _assert_matches(pats, f"cogstress:view:{key}:5", f"GTK view ({key} + nonce)")


def test_gtk_height_regex_accepts_signal(gtk_src):
    _assert_matches(_py_regexes(gtk_src, "cogstress:h"), _HEIGHT, "GTK height")


def test_gtk_rate_regex_rejects_invalid_grade(gtk_src):
    _assert_no_match(_py_regexes(gtk_src, "cogstress:rate"), _RATE_INVALID_GRADE, "GTK rate")


def test_gtk_rate_regex_rejects_invalid_date(gtk_src):
    _assert_no_match(_py_regexes(gtk_src, "cogstress:rate"), _RATE_INVALID_DATE, "GTK rate")


def test_gtk_view_regex_rejects_unknown_key(gtk_src):
    _assert_no_match(_py_regexes(gtk_src, "cogstress:view"), _VIEW_INVALID_KEY, "GTK view")


def test_gtk_compact_regex_rejects_invalid_bit(gtk_src):
    _assert_no_match(_py_regexes(gtk_src, "cogstress:compact"), _COMPACT_INVALID_BIT, "GTK compact")


# ---------------------------------------------------------------------------
# Windows PowerShell host
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def win_src() -> str:
    return _WIN.read_text(encoding="utf-8")


def test_windows_rate_regex_accepts_signal_with_nonce(win_src):
    _assert_matches(_ps_regexes(win_src, "cogstress:rate"), _RATE_WITH_NONCE, "Windows rate")


def test_windows_rate_regex_accepts_signal_without_nonce(win_src):
    _assert_matches(_ps_regexes(win_src, "cogstress:rate"), _RATE_NO_NONCE, "Windows rate (no nonce)")


def test_windows_compact_regex_accepts_signal_with_nonce(win_src):
    _assert_matches(_ps_regexes(win_src, "cogstress:compact"), _COMPACT_WITH_NONCE, "Windows compact")


def test_windows_compact_regex_accepts_signal_without_nonce(win_src):
    _assert_matches(_ps_regexes(win_src, "cogstress:compact"), _COMPACT_NO_NONCE, "Windows compact (no nonce)")


def test_windows_view_regex_accepts_all_valid_keys(win_src):
    pats = _ps_regexes(win_src, "cogstress:view")
    for key in ("today", "week", "month", "year"):
        _assert_matches(pats, f"cogstress:view:{key}", f"Windows view ({key})")
        _assert_matches(pats, f"cogstress:view:{key}:5", f"Windows view ({key} + nonce)")


def test_windows_height_regex_accepts_signal(win_src):
    _assert_matches(_ps_regexes(win_src, "cogstress:h:"), _HEIGHT, "Windows height")


def test_windows_rate_regex_rejects_invalid_grade(win_src):
    _assert_no_match(_ps_regexes(win_src, "cogstress:rate"), _RATE_INVALID_GRADE, "Windows rate")


def test_windows_rate_regex_rejects_invalid_date(win_src):
    _assert_no_match(_ps_regexes(win_src, "cogstress:rate"), _RATE_INVALID_DATE, "Windows rate")


def test_windows_view_regex_rejects_unknown_key(win_src):
    _assert_no_match(_ps_regexes(win_src, "cogstress:view"), _VIEW_INVALID_KEY, "Windows view")


def test_windows_compact_regex_rejects_invalid_bit(win_src):
    _assert_no_match(_ps_regexes(win_src, "cogstress:compact"), _COMPACT_INVALID_BIT, "Windows compact")


# ---------------------------------------------------------------------------
# KDE QML host
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def kde_src() -> str:
    return _KDE.read_text(encoding="utf-8")


def test_kde_rate_regex_accepts_signal_with_nonce(kde_src):
    _assert_matches(_qml_regexes(kde_src, "cogstress:rate"), _RATE_WITH_NONCE, "KDE rate")


def test_kde_rate_regex_accepts_signal_without_nonce(kde_src):
    _assert_matches(_qml_regexes(kde_src, "cogstress:rate"), _RATE_NO_NONCE, "KDE rate (no nonce)")


def test_kde_compact_regex_accepts_signal_with_nonce(kde_src):
    _assert_matches(_qml_regexes(kde_src, "cogstress:compact"), _COMPACT_WITH_NONCE, "KDE compact")


def test_kde_compact_regex_accepts_signal_without_nonce(kde_src):
    _assert_matches(_qml_regexes(kde_src, "cogstress:compact"), _COMPACT_NO_NONCE, "KDE compact (no nonce)")


def test_kde_view_regex_accepts_all_valid_keys(kde_src):
    pats = _qml_regexes(kde_src, "cogstress:view")
    for key in ("today", "week", "month", "year"):
        _assert_matches(pats, f"cogstress:view:{key}", f"KDE view ({key})")
        _assert_matches(pats, f"cogstress:view:{key}:5", f"KDE view ({key} + nonce)")


def test_kde_height_regex_accepts_signal(kde_src):
    _assert_matches(_qml_regexes(kde_src, "cogstress:h"), _HEIGHT, "KDE height")


def test_kde_rate_regex_rejects_invalid_grade(kde_src):
    _assert_no_match(_qml_regexes(kde_src, "cogstress:rate"), _RATE_INVALID_GRADE, "KDE rate")


def test_kde_rate_regex_rejects_invalid_date(kde_src):
    _assert_no_match(_qml_regexes(kde_src, "cogstress:rate"), _RATE_INVALID_DATE, "KDE rate")


def test_kde_view_regex_rejects_unknown_key(kde_src):
    _assert_no_match(_qml_regexes(kde_src, "cogstress:view"), _VIEW_INVALID_KEY, "KDE view")


def test_kde_compact_regex_rejects_invalid_bit(kde_src):
    _assert_no_match(_qml_regexes(kde_src, "cogstress:compact"), _COMPACT_INVALID_BIT, "KDE compact")


# ---------------------------------------------------------------------------
# Protocol parity: all three title-bridge hosts must handle the same signals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signal,fragment", [
    (_RATE_WITH_NONCE,    "cogstress:rate"),
    (_RATE_NO_NONCE,      "cogstress:rate"),
    (_COMPACT_WITH_NONCE, "cogstress:compact"),
    (_COMPACT_NO_NONCE,   "cogstress:compact"),
    (_VIEW_WITH_NONCE,    "cogstress:view"),
    (_VIEW_NO_NONCE,      "cogstress:view"),
    (_HEIGHT,             "cogstress:h"),
])
def test_all_title_bridge_hosts_accept_signal(signal, fragment, gtk_src, win_src, kde_src):
    """Every title-bridge host must accept every valid card signal. A signal
    accepted by one host but rejected by another is a latent cross-platform bug."""
    _assert_matches(_py_regexes(gtk_src, fragment), signal, f"GTK ({signal})")
    _assert_matches(_ps_regexes(win_src, fragment), signal, f"Windows ({signal})")
    _assert_matches(_qml_regexes(kde_src, fragment), signal, f"KDE ({signal})")
