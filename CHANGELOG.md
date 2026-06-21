# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version number is defined in `ai_code_cognitive_stress/__init__.py` and read
dynamically by the build.

## [Unreleased]

## [0.1.0] - 2026-06-21

First public release.

### Added
- Cognitive-stress profile computed from local agent-coding session logs across
  three behavioral axes: parallel-session load, attention interruptions, and
  closure deficit (resumption load).
- Pipeline: ingest → aggregate (mtime-keyed cache + durable day archive that
  survives source-log recycling) → per-day metrics, work-hours detection,
  personal optimum, and composite score.
- Pluggable input adapters via the `SessionSource` protocol — one small file per
  supported agent-coding tool.
- Self-contained HTML report with daily, monthly, and yearly views scored
  against a personal baseline, plus a machine-readable JSON sibling.
- Desktop widgets sharing a single renderer (`--emit-html-card`): KDE Plasma 6,
  DE-agnostic GTK3 + WebKit2GTK (GNOME/XFCE/Cinnamon/MATE/Budgie), macOS
  Übersicht, and Windows WebView2.
- Chat skills for Claude Code and Codex CLI (`onboard`, `install-aicogstress`,
  `contribute-data`, and the report skill).
- `--export-research`: writes an anonymized full-year export to disk for manual,
  user-initiated upload (no network calls).
- `--calibrate`: maintainer-side tool that pools collected exports and suggests a
  scoring config block.
- Internationalization (`t`/`tn` + localized date names) with per-language
  catalogs; English is the reference catalog.
- One-command setup via `install.py` (skill + CLI + widget + first computation),
  packaging via hatchling, and a hermetic pytest suite on synthetic fixtures.

### Notes
- Pure Python standard library at runtime — zero third-party dependencies.
- No network calls: the tool reads local logs and writes a report to disk.

[Unreleased]: https://github.com/sagium/ai-code-cognitive-stress/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sagium/ai-code-cognitive-stress/releases/tag/v0.1.0
