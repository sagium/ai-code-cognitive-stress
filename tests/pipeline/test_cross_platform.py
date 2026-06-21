"""Tests for OS-portable behaviour.

We can't actually execute on every OS from CI here, but we can mock
sys.platform + env vars to verify the path-resolution logic and the
browser-open fallback chain take the right code path on each.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_code_cognitive_stress.pipeline.aggregate import (
    _default_cache_dir,
    _default_data_dir,
)


class TestDefaultCacheDir:
    def test_linux_uses_home_dot_cache(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        result = _default_cache_dir()
        assert result == Path.home() / ".cache" / "ai-code-cognitive-stress"

    def test_linux_respects_xdg_cache_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert _default_cache_dir() == tmp_path / "xdg" / "ai-code-cognitive-stress"

    def test_macos_uses_library_caches(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert _default_cache_dir() == (
            Path.home() / "Library" / "Caches" / "ai-code-cognitive-stress"
        )

    def test_macos_xdg_override_still_wins(self, monkeypatch, tmp_path):
        """Users who deliberately set XDG_CACHE_HOME on macOS (e.g. dotfile
        users following XDG everywhere) get their override honoured."""
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert _default_cache_dir() == tmp_path / "xdg" / "ai-code-cognitive-stress"

    def test_windows_uses_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert _default_cache_dir() == (
            tmp_path / "Local" / "ai-code-cognitive-stress" / "Cache"
        )

    def test_windows_fallback_when_localappdata_missing(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        assert _default_cache_dir() == (
            Path.home() / "AppData" / "Local" / "ai-code-cognitive-stress" / "Cache"
        )

    def test_freebsd_falls_back_to_dot_cache(self, monkeypatch):
        """Anything that isn't darwin or win32 routes through the XDG / dot-cache
        path (Linux, FreeBSD, OpenBSD all hit this branch)."""
        monkeypatch.setattr("sys.platform", "freebsd14")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert _default_cache_dir() == Path.home() / ".cache" / "ai-code-cognitive-stress"


class TestDefaultDataDir:
    """The durable archive lives in the *data* dir, not the cache dir — so the
    archive survives a `~/.cache` clean. Mirrors TestDefaultCacheDir but for the
    XDG data convention (~/.local/share, ~/Library/Application Support, etc.)."""

    def test_linux_uses_home_local_share(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert _default_data_dir() == (
            Path.home() / ".local" / "share" / "ai-code-cognitive-stress"
        )

    def test_linux_respects_xdg_data_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert _default_data_dir() == tmp_path / "xdg" / "ai-code-cognitive-stress"

    def test_macos_uses_application_support(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert _default_data_dir() == (
            Path.home() / "Library" / "Application Support" / "ai-code-cognitive-stress"
        )

    def test_macos_xdg_override_still_wins(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert _default_data_dir() == tmp_path / "xdg" / "ai-code-cognitive-stress"

    def test_windows_uses_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert _default_data_dir() == (
            tmp_path / "Local" / "ai-code-cognitive-stress" / "Data"
        )

    def test_windows_fallback_when_localappdata_missing(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        assert _default_data_dir() == (
            Path.home() / "AppData" / "Local" / "ai-code-cognitive-stress" / "Data"
        )

    def test_freebsd_falls_back_to_local_share(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "freebsd14")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert _default_data_dir() == (
            Path.home() / ".local" / "share" / "ai-code-cognitive-stress"
        )

    def test_data_dir_is_distinct_from_cache_dir(self, monkeypatch):
        """The whole point: the durable archive must not sit under the cache dir
        (which the OS is free to wipe)."""
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert _default_data_dir() != _default_cache_dir()


class TestOpenInBrowser:
    """The browser-open helper tries webbrowser.open() first; if that
    returns False, it falls back to platform-native openers. Verify the
    right fallback runs on each OS."""

    def test_uses_webbrowser_when_it_succeeds(self, monkeypatch):
        from ai_code_cognitive_stress.__main__ import _open_in_browser
        opened: list[str] = []
        monkeypatch.setattr(
            "webbrowser.open", lambda url: opened.append(url) or True,
        )
        _open_in_browser(Path("/tmp/dummy.html"))
        assert len(opened) == 1

    def test_macos_fallback_uses_open(self, monkeypatch):
        from ai_code_cognitive_stress.__main__ import _open_in_browser
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("webbrowser.open", lambda url: False)
        commands: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda cmd, **kw: commands.append(cmd) or object(),
        )
        _open_in_browser(Path("/tmp/dummy.html"))
        assert commands == [["open", "/tmp/dummy.html"]]

    def test_linux_fallback_uses_xdg_open(self, monkeypatch):
        from ai_code_cognitive_stress.__main__ import _open_in_browser
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("webbrowser.open", lambda url: False)
        commands: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda cmd, **kw: commands.append(cmd) or object(),
        )
        _open_in_browser(Path("/tmp/dummy.html"))
        assert commands[0] == ["xdg-open", "/tmp/dummy.html"]

    def test_linux_fallback_chains_through_alternatives(self, monkeypatch):
        """If `xdg-open` is missing (FileNotFoundError), the helper tries
        `x-www-browser` next, then `gnome-open`."""
        from ai_code_cognitive_stress.__main__ import _open_in_browser
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("webbrowser.open", lambda url: False)
        attempts: list[list[str]] = []

        def fake_popen(cmd, **kwargs):
            attempts.append(cmd)
            if cmd[0] in ("xdg-open", "x-www-browser"):
                raise FileNotFoundError(cmd[0])
            return object()

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        _open_in_browser(Path("/tmp/dummy.html"))
        assert [c[0] for c in attempts] == [
            "xdg-open", "x-www-browser", "gnome-open",
        ]

    def test_truly_no_opener_does_not_crash(self, monkeypatch, capsys):
        from ai_code_cognitive_stress.__main__ import _open_in_browser
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("webbrowser.open", lambda url: False)

        def always_missing(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr("subprocess.Popen", always_missing)
        _open_in_browser(Path("/tmp/dummy.html"))
        err = capsys.readouterr().err
        assert "could not auto-open browser" in err
        assert "/tmp/dummy.html" in err
