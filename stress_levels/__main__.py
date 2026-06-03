"""CLI entry point. Invoke with `python -m stress_levels`."""

from __future__ import annotations

import argparse
import re
import sys
import webbrowser
from datetime import date, timedelta
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
            "Aider, or any combination)."
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
        "--baseline-days", type=int, default=30,
        help="Personal-baseline window in days (default: 30)",
    )
    parser.add_argument(
        "--source", action="append", metavar="NAME",
        help=(
            "Which agent-coding session source to ingest. Repeatable. "
            "Built-in names: claude-code, codex, aider, auto. "
            "Default: claude-code (preserves v0 behaviour). "
            "Use 'auto' to enable every source whose data directory "
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
             "drill-down shows) as JSON to stdout and exit. Used by the KDE "
             "Plasma widget and usable by any external display. Ignores the "
             "date-span flags; reuses --baseline-days and --source. "
             "Local-only — reads local data, writes nothing, no network.",
    )
    parser.add_argument(
        "--rebuild-cache", action="store_true",
        help="Nuke the on-disk aggregate cache and recompute from raw session "
             "logs, then run as usual. Use after the ingest/aggregate layer "
             "changes, or to force a clean full rebuild. (Metric/algorithm "
             "changes alone don't need this — metrics are always recomputed "
             "from the cached aggregates.)",
    )
    parser.add_argument(
        "--export-research", nargs="?", const="", default=None, metavar="PATH",
        help="Write an ANONYMIZED full-year JSON snapshot to PATH (default: "
             "./stress-levels-research-<year>.json) for voluntary upload to "
             "the research-calibration form, then exit. Covers --year if given, "
             "else the current calendar year. Local-only: writes a file and "
             "nothing else — you upload it manually; the tool never sends data. "
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
             "config 'scoring' block; changes nothing on its own. Local-only.",
    )
    parser.add_argument(
        "--calibrate-out", default="calibration-report.json", metavar="PATH",
        help="Where --calibrate writes its JSON report "
             "(default: ./calibration-report.json).",
    )
    return parser


def _clear_cache(cache_dir: Path) -> bool:
    """Delete the on-disk aggregate cache. Returns True if anything was removed.
    Best-effort — a failure is reported to stderr but never aborts the run."""
    import shutil

    if not cache_dir.exists():
        print(f"rebuild-cache: no cache at {cache_dir}", file=sys.stderr)
        return False
    try:
        shutil.rmtree(cache_dir)
        print(f"rebuild-cache: cleared {cache_dir}", file=sys.stderr)
        return True
    except OSError as exc:
        print(
            f"rebuild-cache: could not clear {cache_dir}: {exc}", file=sys.stderr,
        )
        return False


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        since, until, label = _parse_range(args)
    except ValueError as exc:
        print(f"stress-levels: error: {exc}", file=sys.stderr)
        return 1
    output_path = Path(args.output).expanduser().resolve()

    # Import here so the CLI's `--help` path doesn't pay the cost of loading
    # the whole pipeline when the user only wants usage.
    import json

    from . import __version__
    from .aggregate import DEFAULT_CACHE_DIR, get_day_aggregates
    from .markdown_min import to_html as md_to_html
    from .metrics import build_profile
    from .render import report
    from .serialize import profile_to_dict

    # Nuke-and-rebuild: clear the cache before anything reads it, so every mode
    # (report, widget, emit-json) recomputes from raw logs this run.
    if args.rebuild_cache:
        _clear_cache(DEFAULT_CACHE_DIR)

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
    from .sources import (
        AiderSessionSource, ClaudeCodeSessionSource, CodexSessionSource,
        default_sources,
    )
    source_names = args.source or ["claude-code"]
    sources = []
    for name in source_names:
        if name == "auto":
            sources.extend(default_sources())
            continue
        if name == "claude-code":
            sources.append(ClaudeCodeSessionSource())
        elif name == "codex":
            sources.append(CodexSessionSource())
        elif name == "aider":
            sources.append(AiderSessionSource())
        else:
            print(
                f"stress-levels: error: unknown --source {name!r}. "
                f"Built-ins: claude-code, codex, aider, auto.",
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

    # Emit-JSON mode: print today's full daily view to stdout for an external
    # display (the KDE Plasma widget). Ignores the date span and the report
    # pipeline. Only JSON goes to stdout; diagnostics to stderr.
    if args.emit_json:
        from .dayview import compute_today_dayview, dayview_to_dict
        view = compute_today_dayview(
            baseline_days=args.baseline_days, sources=sources,
        )
        print(json.dumps(dayview_to_dict(view), default=str))
        return 0

    # Calibration mode (maintainer): pool anonymized exports the maintainer has
    # collected and crunch the population into suggested scaling. Reads local
    # files, writes a local report — no network, changes nothing on its own.
    if args.calibrate is not None:
        from .calibrate import (
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
    # voluntary manual upload. Local-only — writes a file and nothing else.
    # Overrides the date span with a full calendar year (--year if given, else
    # the current year). Gated on explicit consent.
    if args.export_research is not None:
        from .research_export import (
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
            rs_since, rs_until, sources=sources,
        )
        rs_profile = build_profile(
            rs_aggs, baseline_days=args.baseline_days,
        )
        payload = build_research_export(
            rs_profile, since=rs_since, until=rs_until,
            package_version=__version__, ingest_stats=rs_stats,
            aggregates=rs_aggs,
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
        since, until, sources=sources,
    )
    print(
        f"ingested {stats.ingest.events_emitted:,} events from "
        f"{stats.ingest.files_kept} sessions; "
        f"{stats.cache_hits} cache hits / {stats.cache_misses} misses",
        file=sys.stderr,
    )
    profile = build_profile(
        aggregates, baseline_days=args.baseline_days,
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
