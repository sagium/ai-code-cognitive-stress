"""Tests for the CLI entry point."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timezone
from pathlib import Path

import pytest

from stress_levels.__main__ import (
    _build_parser,
    _clear_cache,
    _open_in_browser,
    _parse_range,
    main,
)


def _ns(**overrides) -> argparse.Namespace:
    """Build a Namespace matching the CLI's argparse defaults."""
    defaults = {"year": None, "month": None, "day": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _parse_range

def test_default_resolves_to_current_month():
    today = date(2026, 5, 28)
    since, until, label = _parse_range(_ns(), today=today)
    assert since == date(2026, 5, 1)
    assert until == today
    assert label == "2026-05"


def test_default_label_pads_single_digit_month():
    today = date(2026, 3, 4)
    _, _, label = _parse_range(_ns(), today=today)
    assert label == "2026-03"


def test_month_flag_resolves_full_month():
    since, until, label = _parse_range(_ns(month="2026-03"))
    assert since == date(2026, 3, 1)
    assert until == date(2026, 3, 31)
    assert label == "2026-03"


def test_month_flag_handles_february_leap_year():
    _, until, _ = _parse_range(_ns(month="2024-02"))
    assert until == date(2024, 2, 29)


def test_month_flag_handles_february_non_leap_year():
    _, until, _ = _parse_range(_ns(month="2023-02"))
    assert until == date(2023, 2, 28)


def test_month_flag_handles_year_rollover():
    since, until, _ = _parse_range(_ns(month="2025-12"))
    assert since == date(2025, 12, 1)
    assert until == date(2025, 12, 31)


def test_month_flag_invalid_format_raises():
    with pytest.raises(ValueError, match="YYYY-MM"):
        _parse_range(_ns(month="not-a-month"))


def test_month_flag_rejects_yyyy_mm_dd():
    # A common confusion — passing a day-shaped value to --month.
    with pytest.raises(ValueError, match="YYYY-MM"):
        _parse_range(_ns(month="2026-05-15"))


def test_month_flag_rejects_single_digit_month():
    with pytest.raises(ValueError, match="YYYY-MM"):
        _parse_range(_ns(month="2026-5"))


def test_month_flag_rejects_two_digit_year():
    with pytest.raises(ValueError, match="YYYY-MM"):
        _parse_range(_ns(month="26-05"))


def test_year_flag_resolves_full_year():
    since, until, label = _parse_range(_ns(year="2025"))
    assert since == date(2025, 1, 1)
    assert until == date(2025, 12, 31)
    assert label == "2025"


def test_year_flag_rejects_two_digit_year():
    # `--year 26` would otherwise yield a report for year 26 CE — silently wrong.
    with pytest.raises(ValueError, match="YYYY"):
        _parse_range(_ns(year="26"))


def test_year_flag_rejects_five_digit_year():
    with pytest.raises(ValueError, match="YYYY"):
        _parse_range(_ns(year="20255"))


def test_year_flag_rejects_non_numeric():
    with pytest.raises(ValueError, match="YYYY"):
        _parse_range(_ns(year="abcd"))


def test_day_flag_resolves_single_day():
    since, until, label = _parse_range(_ns(day="2026-04-15"))
    assert since == date(2026, 4, 15)
    assert until == date(2026, 4, 15)
    assert label == "2026-04-15"


def test_day_flag_invalid_format_raises():
    with pytest.raises(ValueError):
        _parse_range(_ns(day="2026/04/15"))


def test_day_flag_takes_precedence_over_month_when_both_set_in_namespace():
    # Argparse enforces the mutex group at parse time; this guards against a
    # future regression where _parse_range stops respecting the precedence.
    since, until, label = _parse_range(
        _ns(day="2026-04-15", month="2026-03"),
    )
    assert since == until == date(2026, 4, 15)
    assert label == "2026-04-15"


# ---------------------------------------------------------------------------
# _build_parser — argparse-level behaviour

def test_span_flags_are_mutually_exclusive():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--year", "2026", "--month", "2026-05"])


def test_default_output_path_is_home_relative():
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.output == str(Path.home() / "stress-profile.html")


def test_baseline_days_defaults_to_30():
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.baseline_days == 30


def test_baseline_days_accepts_override():
    parser = _build_parser()
    args = parser.parse_args(["--baseline-days", "14"])
    assert args.baseline_days == 14


def test_baseline_days_rejects_non_integer():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--baseline-days", "fourteen"])


def test_output_short_flag_accepted():
    parser = _build_parser()
    args = parser.parse_args(["-o", "/tmp/x.html"])
    assert args.output == "/tmp/x.html"


# ---------------------------------------------------------------------------
# main — end-to-end integration.

def test_main_writes_report_against_empty_projects_dir(tmp_path, monkeypatch):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR",
                        tmp_path / "no-projects")
    out = tmp_path / "report.html"
    rc = main(["--month", "2026-05", "--output", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>")
    assert "Cognitive Stress Profile" in text


def test_main_reports_window_to_stderr(tmp_path, capsys, monkeypatch):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR",
                        tmp_path / "no-projects")
    out = tmp_path / "report.html"
    main(["--month", "2025-12", "--output", str(out)])
    err = capsys.readouterr().err
    assert "2025-12-01" in err
    assert "2025-12-31" in err
    assert str(out) in err


def test_main_invalid_year_returns_clean_error(capsys):
    rc = main(["--year", "26"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "YYYY" in err
    assert "Traceback" not in err


def test_main_invalid_month_returns_clean_error(capsys):
    rc = main(["--month", "2026-05-15"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "YYYY-MM" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# --analysis injection + --open

def test_main_injects_markdown_analysis_into_html(tmp_path, monkeypatch):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR",
                        tmp_path / "no-projects")

    analysis = tmp_path / "analysis.md"
    analysis.write_text(
        "## Headline\n\n"
        "- **Peak day** was unusually high.\n"
        "- Check `Mon 12 May` for the spike.\n",
        encoding="utf-8",
    )
    out = tmp_path / "report.html"
    rc = main([
        "--month", "2026-05",
        "--output", str(out),
        "--analysis", str(analysis),
    ])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert 'class="agent-analysis"' in text
    assert "<h3>Headline</h3>" in text
    assert "<strong>Peak day</strong>" in text
    assert "<code>Mon 12 May</code>" in text


def test_main_passes_through_html_analysis_verbatim(tmp_path, monkeypatch):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR",
                        tmp_path / "no-projects")

    analysis = tmp_path / "analysis.html"
    analysis.write_text(
        "<p>pre-rendered <strong>analysis</strong></p>",
        encoding="utf-8",
    )
    out = tmp_path / "report.html"
    rc = main([
        "--month", "2026-05",
        "--output", str(out),
        "--analysis", str(analysis),
    ])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "<p>pre-rendered <strong>analysis</strong></p>" in text


def test_main_reports_clean_error_when_analysis_file_missing(tmp_path, monkeypatch, capsys):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR",
                        tmp_path / "no-projects")
    out = tmp_path / "report.html"
    rc = main([
        "--month", "2026-05",
        "--output", str(out),
        "--analysis", str(tmp_path / "does-not-exist.md"),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--analysis file not found" in err
    # And the report was NOT generated.
    assert not out.exists()


def test_main_open_flag_invokes_webbrowser(tmp_path, monkeypatch):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR",
                        tmp_path / "no-projects")

    opened: list[str] = []
    import webbrowser
    # Return True so _open_in_browser treats the open as successful and does
    # NOT fall through to a real subprocess opener (xdg-open/open).
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    out = tmp_path / "report.html"
    rc = main([
        "--month", "2026-05",
        "--output", str(out),
        "--open",
    ])
    assert rc == 0
    assert len(opened) == 1
    assert opened[0].endswith("report.html")
    assert opened[0].startswith("file://")


def test_main_without_open_flag_does_not_invoke_webbrowser(tmp_path, monkeypatch):
    import stress_levels.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR",
                        tmp_path / "no-projects")
    opened: list[str] = []
    import webbrowser
    # Return True so _open_in_browser treats the open as successful and does
    # NOT fall through to a real subprocess opener (xdg-open/open).
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    out = tmp_path / "report.html"
    rc = main(["--month", "2026-05", "--output", str(out)])
    assert rc == 0
    assert opened == []


# --- --rebuild-cache -------------------------------------------------------

def test_rebuild_cache_flag_parses_default_false():
    args = _build_parser().parse_args([])
    assert args.rebuild_cache is False
    assert _build_parser().parse_args(["--rebuild-cache"]).rebuild_cache is True


def test_clear_cache_removes_existing_dir(tmp_path):
    cache = tmp_path / "cache"
    (cache / "v3" / "2026").mkdir(parents=True)
    (cache / "v3" / "2026" / "2026-05-29.json").write_text("{}", encoding="utf-8")
    assert _clear_cache(cache) is True
    assert not cache.exists()


def test_clear_cache_missing_dir_is_noop(tmp_path):
    assert _clear_cache(tmp_path / "nope") is False


def test_clear_cache_reports_oserror_without_aborting(tmp_path, monkeypatch, capsys):
    import shutil
    cache = tmp_path / "cache"
    cache.mkdir()

    def _boom(_p):
        raise OSError("permission denied")

    monkeypatch.setattr(shutil, "rmtree", _boom)
    assert _clear_cache(cache) is False
    assert "could not clear" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main — source resolution / widget / emit-json / rebuild-cache

def _isolate_pipeline(monkeypatch, tmp_path):
    """Point every default data dir at an empty home and route the aggregate
    cache to a tmp dir, so main() runs hermetically and writes nothing real."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    import stress_levels.aggregate as agg
    real = agg.get_day_aggregates

    def fake(since, until, **kw):
        kw["cache_dir"] = tmp_path / "cache"
        return real(since, until, **kw)

    monkeypatch.setattr(agg, "get_day_aggregates", fake)


def test_main_unknown_source_returns_clean_error(capsys):
    rc = main(["--source", "bogus", "--month", "2026-05", "-o", "/tmp/x.html"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown --source" in err
    assert "Traceback" not in err


def test_main_codex_source_named_in_stderr(tmp_path, monkeypatch, capsys):
    _isolate_pipeline(monkeypatch, tmp_path)
    rc = main(["--source", "codex", "--month", "2026-05",
               "-o", str(tmp_path / "r.html")])
    assert rc == 0
    assert "sources: codex" in capsys.readouterr().err


def test_main_aider_source_named_in_stderr(tmp_path, monkeypatch, capsys):
    _isolate_pipeline(monkeypatch, tmp_path)
    rc = main(["--source", "aider", "--month", "2026-05",
               "-o", str(tmp_path / "r.html")])
    assert rc == 0
    assert "sources: aider" in capsys.readouterr().err


def test_main_auto_source_falls_back_to_claude_code(tmp_path, monkeypatch, capsys):
    _isolate_pipeline(monkeypatch, tmp_path)  # empty home → nothing available
    rc = main(["--source", "auto", "--month", "2026-05",
               "-o", str(tmp_path / "r.html")])
    assert rc == 0
    assert "sources: claude-code" in capsys.readouterr().err


def test_main_dedupes_repeated_sources(tmp_path, monkeypatch, capsys):
    _isolate_pipeline(monkeypatch, tmp_path)
    rc = main(["--source", "auto", "--source", "claude-code",
               "--month", "2026-05", "-o", str(tmp_path / "r.html")])
    assert rc == 0
    line = next(l for l in capsys.readouterr().err.splitlines() if "sources:" in l)
    assert line.count("claude-code") == 1


def test_main_emit_json_prints_dayview(monkeypatch, capsys):
    import stress_levels.dayview as dayview_mod
    from stress_levels.dayview import build_dayview
    from stress_levels.metrics import DayMetrics, StressProfile

    prof = StressProfile(
        days={}, work_windows={}, local_tz_name="UTC", baseline_window_days=30,
        personal_optimum=2.0, composite_p50=20.0, composite_p75=30.0,
        composite_p90=50.0,
    )
    dv = build_dayview(
        DayMetrics(day=date(2026, 5, 29), composite=10.0), None, prof, timezone.utc,
    )
    monkeypatch.setattr(dayview_mod, "compute_today_dayview", lambda **kw: dv)
    rc = main(["--emit-json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)


def test_main_rebuild_cache_clears_before_running(tmp_path, monkeypatch):
    _isolate_pipeline(monkeypatch, tmp_path)
    import stress_levels.__main__ as cli
    cleared: list = []
    monkeypatch.setattr(cli, "_clear_cache", lambda d: cleared.append(d) or True)
    rc = main(["--rebuild-cache", "--month", "2026-05",
               "-o", str(tmp_path / "r.html")])
    assert rc == 0
    assert len(cleared) == 1


def test_open_in_browser_falls_back_when_webbrowser_errors(tmp_path, monkeypatch):
    import subprocess
    import webbrowser

    monkeypatch.setattr(sys, "platform", "linux")

    def _err(_url):
        raise webbrowser.Error("no browser running")

    monkeypatch.setattr(webbrowser, "open", _err)
    popened: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: popened.append(cmd))

    f = tmp_path / "x.html"
    f.write_text("<html></html>", encoding="utf-8")
    _open_in_browser(f)
    assert popened and popened[0][0] == "xdg-open"


def test_open_in_browser_darwin_uses_open_command(tmp_path, monkeypatch):
    import subprocess
    import webbrowser

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(webbrowser, "open", lambda _u: False)  # webbrowser no-op
    calls: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    f = tmp_path / "x.html"
    f.write_text("x", encoding="utf-8")
    _open_in_browser(f)
    assert calls and calls[0][0] == "open"


def test_open_in_browser_prints_hint_when_no_opener(tmp_path, monkeypatch, capsys):
    import subprocess
    import webbrowser

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(webbrowser, "open", lambda _u: False)

    def _missing(cmd, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "Popen", _missing)
    f = tmp_path / "x.html"
    f.write_text("x", encoding="utf-8")
    _open_in_browser(f)
    assert "could not auto-open" in capsys.readouterr().err
