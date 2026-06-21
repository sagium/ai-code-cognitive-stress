"""CLI entry point. Invoke with `python -m source`."""

from __future__ import annotations

import argparse
import re
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path


_YEAR_RE = re.compile(r'\d{4}')
_MONTH_RE = re.compile(r'\d{4}-\d{2}')

# Where the operator manually uploads a --export-research file. This is a plain
# link printed to stderr; the tool itself performs no upload.
RESEARCH_UPLOAD_URL = "https://tally.so/r/EkMM4q"


def _parse_range(
    args: argparse.Namespace,
    today: date | None = None,
) -> tuple[date, date, str]:
    """Resolve the --year / --month / --day flags into a (since, until, label) tuple.

    `today` is injectable so the default-current-month branch is testable
    without mocking `date.today()`.
    """
    if args.day:
        d = date.fromisoformat(args.day)
        return d, d, args.day
    if args.month:
        if not _MONTH_RE.fullmatch(args.month):
            raise ValueError(f"--month must be YYYY-MM, got {args.month!r}")
        y, m = map(int, args.month.split("-"))
        first = date(y, m, 1)
        last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
        return first, last, args.month
    if args.year:
        if not _YEAR_RE.fullmatch(args.year):
            raise ValueError(f"--year must be YYYY, got {args.year!r}")
        y = int(args.year)
        return date(y, 1, 1), date(y, 12, 31), args.year
    today = today or date.today()
    first = date(today.year, today.month, 1)
    return first, today, today.strftime("%Y-%m")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stress-levels",
        description=(
            "Generate a cognitive stress profile HTML report from "
            "agent-coding-tool session activity (Claude Code, Codex CLI, "
            "or any combination)."
        ),
    )
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--year", help="YYYY — full-year report")
    span.add_argument(
        "--month",
        help="YYYY-MM — month report (default: current month)",
    )
    span.add_argument("--day", help="YYYY-MM-DD — single-day report")
    parser.add_argument(
        "--output", "-o",
        default=str(Path.home() / "stress-profile.html"),
        help="Output path for the generated HTML (default: ~/stress-profile.html)",
    )
    parser.add_argument(
        "--baseline-days", type=int, default=90,
        help="Personal-baseline window in days (default: 90)",
    )
    parser.add_argument(
        "--source", action="append", metavar="NAME",
        help=(
            "Which agent-coding session source to ingest. Repeatable. "
            "Built-in names: claude-code, codex, auto. "
            "Default: auto, enabling every source whose data directory "
            "exists on disk."
        ),
    )
    parser.add_argument(
        "--analysis", metavar="PATH",
        help=(
            "Optional path to a markdown (.md) or HTML (.html) file containing "
            "agent-generated analysis. When set, the file is embedded into the "
            "rendered report as an 'Analysis' panel directly under the header."
        ),
    )
    parser.add_argument(
        "--open", dest="open_browser", action="store_true",
        help="After writing the report, open it in the default browser.",
    )
    parser.add_argument(
        "--emit-json", action="store_true",
        help="Print TODAY's full daily view (the same data the HTML day "
             "drill-down shows) as JSON to stdout and exit. Usable by any "
             "external display. Ignores the date-span flags; reuses "
             "--baseline-days and --source.",
    )
    parser.add_argument(
        "--emit-html-card", action="store_true",
        help="Print TODAY's daily view as one self-contained HTML card "
             "fragment (inline CSS + SVG, no scripts) to stdout and exit. "
             "This is the single renderer behind BOTH desktop widgets "
             "(KDE Plasma, macOS Übersicht) — they inject this output "
             "verbatim. Same span/source semantics as --emit-json.",
    )
    parser.add_argument(
        "--rebuild-cache", action="store_true",
        help="Nuke the on-disk aggregate cache and recompute from raw session "
             "logs, then run as usual. Use after the ingest/aggregate layer "
             "changes, or to force a clean full rebuild. (Metric/algorithm "
             "changes alone don't need this — metrics are always recomputed "
             "from the cached aggregates.) Leaves the durable archive intact.",
    )
    parser.add_argument(
        "--reset-archive", action="store_true",
        help="DESTRUCTIVE: delete the durable per-day stats archive (the store "
             "that preserves history after agent tools recycle their session "
             "logs), then run as usual. Use only to start the archive over from "
             "scratch — any history whose source logs have already been recycled "
             "is lost. Normal runs never touch the archive.",
    )
    parser.add_argument(
        "--export-research", nargs="?", const="", default=None, metavar="PATH",
        help="Write an ANONYMIZED full-year JSON snapshot to PATH (default: "
             "./stress-levels-research-<year>.json) for voluntary upload to "
             "the research-calibration form, then exit. Covers --year if given, "
             "else the current calendar year. "
             "Requires consent (interactive prompt, or --i-consent).",
    )
    parser.add_argument(
        "--i-consent", action="store_true",
        help="Acknowledge the research-export consent statement "
             "non-interactively, for use with --export-research in scripts.",
    )
    parser.add_argument(
        "--calibrate", nargs="+", default=None, metavar="PATH",
        help="MAINTAINER tool: pool anonymized research exports (files and/or "
             "directories of *.json) and crunch the population to suggest "
             "calibrated normalization ceilings and composite weights, plus a "
             "work-pattern coverage map. Writes a report and prints a suggested "
             "config 'scoring' block; changes nothing on its own.",
    )
    parser.add_argument(
        "--calibrate-out", default="calibration-report.json", metavar="PATH",
        help="Where --calibrate writes its JSON report "
             "(default: ./calibration-report.json).",
    )
    parser.add_argument(
        "--locale", metavar="CODE",
        help="Language for all rendered text (report and widget card), e.g. "
             "'en'. Overrides the 'locale' set in config.json. Catalogs live "
             "in source/core/locales/<CODE>.json; missing keys fall back "
             "to English.",
    )
    parser.add_argument(
        "--set-compact", choices=["true", "false", "toggle"], default=None,
        metavar="VALUE",
        help="Set the widget card's compact (small) mode and persist it to "
             "config.json, then exit. 'true' = small card (composite headline "
             "+ concurrency chart only), 'false' = full card (with the three "
             "axis tiles), 'toggle' = flip the current value. The desktop "
             "widget's expand/collapse button invokes this; it can also be run "
             "directly.",
    )
    parser.add_argument(
        "--set-view", choices=["today", "week", "month", "year"], default=None,
        metavar="VIEW",
        help="Set the widget card's active timeframe tab and persist it to "
             "config.json, then exit. One of: today, week, month, year. The "
             "desktop widget's tab buttons invoke this; it can also be run "
             "directly.",
    )
    parser.add_argument(
        "--rate", metavar="DAY:GRADE",
        help="Record a subjective day grade (0=chill, 1=heated, 2=cooked). "
             "DAY is an ISO date (YYYY-MM-DD) or the keyword 'today'. "
             "Example: --rate today:1 or --rate 2026-06-17:2. "
             "Writes to the sidecar archive and exits.",
    )
    return parser


def _clear_dir(target: Path, label: str, noun: str) -> bool:
    """Delete an on-disk store directory. Returns True if anything was removed.
    Best-effort — a failure is reported to stderr but never aborts the run.
    `label` prefixes the log line; `noun` names the store in the message."""
    import shutil

    if not target.exists():
        print(f"{label}: no {noun} at {target}", file=sys.stderr)
        return False
    try:
        shutil.rmtree(target)
        print(f"{label}: cleared {target}", file=sys.stderr)
        return True
    except OSError as exc:
        print(
            f"{label}: could not clear {target}: {exc}", file=sys.stderr,
        )
        return False


def _force_utf8_stdio() -> None:
    """Emit UTF-8 on stdout/stderr regardless of the platform default.

    On Windows the console defaults to a legacy code page (e.g. cp1252) that
    can't encode characters like '→' present in the rendered card, which
    would raise UnicodeEncodeError. Reconfiguring the streams is a no-op where
    UTF-8 is already in effect (most Linux/macOS shells).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = _build_parser().parse_args(argv)
    try:
        if args.locale:
            from .core.i18n import set_locale
            set_locale(args.locale)
        since, until, label = _parse_range(args)
    except ValueError as exc:
        print(f"stress-levels: error: {exc}", file=sys.stderr)
        return 1
    output_path = Path(args.output).expanduser().resolve()

    # Import here so the CLI's `--help` path doesn't pay the cost of loading
    # the whole pipeline when the user only wants usage.
    import json

    from . import __version__
    from .pipeline.aggregate import (
        DEFAULT_CACHE_DIR, DEFAULT_DATA_DIR, get_day_aggregates,
    )
    from .core.markdown_min import to_html as md_to_html
    from .pipeline.metrics import build_profile
    from .output.render import report
    from .output.serialize import profile_to_dict

    # The durable per-day stats archive — survives session-log recycling and is
    # independent of the cache (a --rebuild-cache never touches it). Every
    # pipeline call site passes this so the report, the JSON sibling, and both
    # desktop widgets all archive + recover the same way.
    archive_dir = DEFAULT_DATA_DIR / "archive"

    # Nuke-and-rebuild: clear the cache before anything reads it, so every mode
    # (report, widget, emit-json) recomputes from raw logs this run. The archive
    # is deliberately left intact — wiping it would lose history whose source
    # logs are already recycled. --reset-archive is the explicit opt-in for that.
    if args.rebuild_cache:
        _clear_dir(DEFAULT_CACHE_DIR, "rebuild-cache", "cache")
    if args.reset_archive:
        _clear_dir(archive_dir, "reset-archive", "archive")

    # Subjective grade: --rate DAY:GRADE (e.g. --rate today:2 or --rate 2026-06-17:1).
    # Validates input, writes the sidecar, and exits — same pattern as the other
    # state-mutating flags (--rebuild-cache, --reset-archive, --export-research).
    if args.rate is not None:
        from datetime import date as _date
        from .pipeline.subjective import VALID_GRADES, write_grade

        raw = args.rate
        if ":" not in raw:
            print(
                f"stress-levels: error: --rate requires DAY:GRADE "
                f"(e.g. today:1 or 2026-06-17:2), got {raw!r}",
                file=sys.stderr,
            )
            return 1
        day_str, grade_str = raw.split(":", 1)
        # Resolve 'today' keyword
        if day_str == "today":
            rate_day = _date.today()
        else:
            try:
                rate_day = _date.fromisoformat(day_str)
            except ValueError:
                print(
                    f"stress-levels: error: --rate DAY must be an ISO date "
                    f"(YYYY-MM-DD) or 'today', got {day_str!r}",
                    file=sys.stderr,
                )
                return 1
        try:
            rate_grade = int(grade_str)
        except ValueError:
            print(
                f"stress-levels: error: --rate GRADE must be 0, 1, or 2 "
                f"(chill/heated/cooked), got {grade_str!r}",
                file=sys.stderr,
            )
            return 1
        if rate_grade not in VALID_GRADES:
            print(
                f"stress-levels: error: --rate GRADE must be 0, 1, or 2 "
                f"(chill/heated/cooked), got {rate_grade}",
                file=sys.stderr,
            )
            return 1
        write_grade(archive_dir, rate_day, rate_grade)
        print(
            f"rate: recorded grade {rate_grade} for {rate_day.isoformat()}",
            file=sys.stderr,
        )
        return 0

    # Widget compact toggle: --set-compact true|false|toggle. Persists the
    # widget card's compact mode to config.json and exits — the desktop
    # widget's expand/collapse button shells out to this, same exit-early
    # pattern as --rate. Local disk only; no network.
    if args.set_compact is not None:
        from .core.config import load_config, set_compact_widget

        if args.set_compact == "toggle":
            new_compact = not load_config().compact_widget
        else:
            new_compact = args.set_compact == "true"
        set_compact_widget(new_compact)
        print(
            f"set-compact: compact_widget = {str(new_compact).lower()}",
            file=sys.stderr,
        )
        return 0

    # Widget view tab: --set-view today|week|month|year. Persists the widget
    # card's active timeframe tab to config.json and exits — the desktop
    # widget's tab buttons shell out to this, same exit-early pattern as
    # --set-compact. Local disk only; no network.
    if args.set_view is not None:
        from .core.config import set_widget_view

        set_widget_view(args.set_view)
        print(
            f"set-view: widget_view = {args.set_view}",
            file=sys.stderr,
        )
        return 0

    # Resolve agent-analysis input early so a bad path errors before we burn
    # the whole pipeline.
    agent_analysis_html: str | None = None
    if args.analysis:
        analysis_path = Path(args.analysis).expanduser().resolve()
        if not analysis_path.is_file():
            print(
                f"stress-levels: error: --analysis file not found: "
                f"{analysis_path}",
                file=sys.stderr,
            )
            return 1
        text = analysis_path.read_text(encoding="utf-8")
        if analysis_path.suffix.lower() in (".html", ".htm"):
            agent_analysis_html = text
        else:
            agent_analysis_html = md_to_html(text)

    # Resolve the session sources.
    from .adapters import (
        ClaudeCodeSessionSource, CodexSessionSource,
        default_sources,
    )
    source_names = args.source or ["auto"]
    sources = []
    for name in source_names:
        if name == "auto":
            sources.extend(default_sources())
            continue
        if name == "claude-code":
            sources.append(ClaudeCodeSessionSource())
        elif name == "codex":
            sources.append(CodexSessionSource())
        else:
            print(
                f"stress-levels: error: unknown --source {name!r}. "
                f"Built-ins: claude-code, codex, auto.",
                file=sys.stderr,
            )
            return 1
    # De-dupe by class identity in case --source auto + --source claude-code.
    seen_types: set = set()
    dedup = []
    for s in sources:
        if type(s) in seen_types:
            continue
        seen_types.add(type(s))
        dedup.append(s)
    sources = dedup

    # Emit modes: print today's full daily view to stdout for an external
    # display — JSON for arbitrary consumers, or the rendered HTML card that
    # both desktop widgets inject verbatim (widget_card.py). Ignores the date
    # span and the report pipeline. Only the payload goes to stdout;
    # diagnostics to stderr.
    if args.emit_json or args.emit_html_card:
        if args.emit_html_card:
            from .core.config import load_config
            from .output.dayview import compute_timeframe_views
            from .output.widget_card import render_card_tabbed
            views = compute_timeframe_views(
                baseline_days=args.baseline_days, sources=sources,
                archive_dir=archive_dir,
            )
            cfg = load_config()
            print(render_card_tabbed(
                views,
                compact=cfg.compact_widget,
                active_view=cfg.widget_view,
            ))
        else:
            from .output.dayview import compute_today_dayview, dayview_to_dict
            view = compute_today_dayview(
                baseline_days=args.baseline_days, sources=sources,
                archive_dir=archive_dir,
            )
            print(json.dumps(dayview_to_dict(view), default=str))
        return 0

    # Calibration mode (maintainer): pool anonymized exports the maintainer has
    # collected and crunch the population into suggested scaling.
    if args.calibrate is not None:
        from .research.calibrate import (
            calibrate, load_exports, pool_day_records, render_report,
        )

        exports, warns = load_exports(args.calibrate)
        for w in warns:
            print(f"calibrate: {w}", file=sys.stderr)
        if not exports:
            print(
                "stress-levels: no research exports found to calibrate.",
                file=sys.stderr,
            )
            return 1
        records = pool_day_records(exports)
        if not records:
            print(
                "stress-levels: exports contained no day records.",
                file=sys.stderr,
            )
            return 1
        machine, text = render_report(calibrate(records))
        out_path = Path(args.calibrate_out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(machine, indent=2, default=str), encoding="utf-8",
        )
        print(text, file=sys.stderr)
        print(f"\ncalibration report: {out_path}", file=sys.stderr)
        return 0

    # Research-export mode: write an ANONYMIZED full-year snapshot to disk for
    # voluntary manual upload. Overrides the date span with a full calendar year
    # (--year if given, else the current year). Gated on explicit consent.
    if args.export_research is not None:
        from .research.research_export import (
            CONSENT_TEXT, build_research_export, consent_satisfied,
        )

        year = int(args.year) if args.year else date.today().year
        rs_since, rs_until = date(year, 1, 1), date(year, 12, 31)

        print(CONSENT_TEXT, file=sys.stderr)
        if not consent_satisfied(
            flag=args.i_consent, isatty=sys.stdin.isatty(),
        ):
            print(
                "stress-levels: research export cancelled — consent not given. "
                "Re-run with --i-consent to acknowledge non-interactively.",
                file=sys.stderr,
            )
            return 1

        rs_aggs, rs_stats = get_day_aggregates(
            rs_since, rs_until, sources=sources, archive_dir=archive_dir,
        )
        rs_profile = build_profile(
            rs_aggs, baseline_days=args.baseline_days,
            as_of=datetime.now().astimezone(),
        )
        payload = build_research_export(
            rs_profile, since=rs_since, until=rs_until,
            package_version=__version__, ingest_stats=rs_stats,
            aggregates=rs_aggs, archive_dir=archive_dir,
        )
        rs_path = Path(
            args.export_research
            or f"stress-levels-research-{year}.json"
        ).expanduser().resolve()
        rs_path.parent.mkdir(parents=True, exist_ok=True)
        rs_path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8",
        )
        print(f"research export: {rs_path}", file=sys.stderr)
        print(f"upload it at:    {RESEARCH_UPLOAD_URL}", file=sys.stderr)
        return 0

    print(
        f"window: {since} → {until}  sources: "
        f"{', '.join(s.name for s in sources)}",
        file=sys.stderr,
    )
    aggregates, stats = get_day_aggregates(
        since, until, sources=sources, archive_dir=archive_dir,
    )
    print(
        f"ingested {stats.ingest.events_emitted:,} events from "
        f"{stats.ingest.files_kept} sessions; "
        f"{stats.cache_hits} cache hits / {stats.cache_misses} misses",
        file=sys.stderr,
    )
    if stats.archive_recovered_days:
        print(
            f"recovered {stats.archive_recovered_days} day(s) from the durable "
            f"archive (source logs recycled — see {archive_dir})",
            file=sys.stderr,
        )
    profile = build_profile(
        aggregates, baseline_days=args.baseline_days,
        as_of=datetime.now().astimezone(),
    )
    html = report(
        profile, aggregates, label=label,
        ingest_stats=stats,
        agent_analysis_html=agent_analysis_html,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"report: {output_path}", file=sys.stderr)

    # Emit a JSON sibling so the Claude Code skill (or any other agent /
    # analysis layer) can read the structured profile without parsing the HTML.
    json_path = output_path.with_suffix(".json")
    json_payload = profile_to_dict(
        profile,
        label=label,
        since=since,
        until=until,
        ingest_stats=stats,
        package_version=__version__,
    )
    json_path.write_text(
        json.dumps(json_payload, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"data:   {json_path}", file=sys.stderr)

    if args.open_browser:
        _open_in_browser(output_path)
        print(f"opened: {output_path.as_uri()}", file=sys.stderr)

    return 0


def _open_in_browser(path: Path) -> None:
    """Cross-platform open-in-default-browser.

    `webbrowser.open()` is the canonical Python entry point, but on Linux
    it can claim success and silently no-op when no browser is already
    running. Fall back to platform-native openers (macOS `open`, Windows
    `os.startfile`, Linux `xdg-open` then `x-www-browser`) so the report
    actually surfaces regardless of OS or current browser state.
    """
    import subprocess
    import webbrowser

    url = path.as_uri()
    try:
        if webbrowser.open(url):
            return
    except webbrowser.Error:
        pass
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", str(path)])
            return
        except FileNotFoundError:
            pass
    elif sys.platform == "win32":  # pragma: no cover — exercised on Windows only
        import os
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        except OSError:
            pass
    else:
        for cmd in (
            ["xdg-open", str(path)],
            ["x-www-browser", str(path)],
            ["gnome-open", str(path)],
        ):
            try:
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return
            except FileNotFoundError:
                continue
    # Truly no opener found — print a hint but don't crash the run.
    print(
        f"stress-levels: could not auto-open browser; open this file "
        f"manually: {path}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
