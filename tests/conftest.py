"""Shared test safety nets.

These autouse fixtures guarantee the suite can never touch the host: no test
may open a real browser via the report's open-in-browser path. Tests that
specifically exercise that path (tests/test_cross_platform.py) override these
with their own monkeypatch, which is applied after the autouse fixture and
therefore wins.

The Popen guard is deliberately narrow: it no-ops only known browser-opener
commands (xdg-open/open/x-www-browser/gnome-open) and delegates everything else
to the real subprocess.Popen — so subprocess.run keeps working.
"""

from __future__ import annotations

import subprocess
import webbrowser

import pytest

_BROWSER_OPENERS = {"xdg-open", "x-www-browser", "gnome-open", "open"}
_REAL_POPEN = subprocess.Popen


class _DummyPopen:
    """Stand-in returned for browser-opener commands — starts nothing."""
    def __init__(self, *args, **kwargs):
        self.args = args


def _guarded_popen(cmd, *args, **kwargs):
    prog = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
    if isinstance(prog, str) and prog in _BROWSER_OPENERS:
        return _DummyPopen(cmd, *args, **kwargs)
    return _REAL_POPEN(cmd, *args, **kwargs)


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    # webbrowser.open returns True → callers treat it as success and never fall
    # through to a native opener in the first place.
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True, raising=False)
    # Belt-and-suspenders: if a path does reach a native opener, neutralize only
    # the browser commands; real subprocess.run stays intact.
    monkeypatch.setattr(subprocess, "Popen", _guarded_popen, raising=False)
    yield
