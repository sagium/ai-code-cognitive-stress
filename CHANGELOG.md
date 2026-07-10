# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version number is defined in `ai_code_cognitive_stress/__init__.py` and read
dynamically by the build.

## [Unreleased]

### Fixed
- Codex subagent threads no longer inflate concurrency. A Codex session that
  spawns subagents (a reviewer team, parallel workers) writes one rollout file
  per thread, and each was counted as a separate concurrent session — so one
  orchestrated run read as many parallel sessions. Threads are now keyed on the
  root `session_id` from their `session_meta` header and fold into the single
  session actually driven.

## [0.2.1] - 2026-06-22

### Fixed
- Interruption Index no longer spikes at the start of a live day. The rate is
  events per work-hour, and early in the day the elapsed-hours denominator
  tends to zero, so a single event exploded the rate and then decayed as
  ~1/elapsed — a small-sample artifact, not real context switching.

### Added
- `scoring.interruption_warmup_hours` (default `1.0`): floors the
  interruption-rate denominator while elapsed work is below it, so an early
  event reads as its weight-per-hour instead of a 10x+ spike. Binds only below
  the floor, so completed-day scores are unchanged.

## [0.2.0] - 2026-06-21

### Changed
- CODL is now scored as a graded capacity-dose instead of a windowed
  time-average. Per minute `phi(t) = min(1, C(t) / codl_capacity)` (with
  `codl_capacity = 4`, Cowan's working-memory limit used as an instantaneous
  saturation anchor), summed and normalised by a dose horizon to `[0, 1]`. Idle
  minutes no longer dilute the score, and duration is preserved — a day peaking
  at 4 concurrent sessions now contributes meaningfully to the composite.
- `codl_avg` / `codl_peak_active` are retained as descriptive fields; the
  personal optimum still buckets by `codl_avg`.

### Added
- `scoring.codl_capacity` and `scoring.codl_dose_horizon_minutes` config keys
  (replacing the former `codl_ceiling`). `--calibrate` fits the horizon from the
  p95 of observed raw dose; `codl_capacity` is the theory anchor and is never
  calibrated. Exports and the JSON sibling carry `codl_raw_dose` / `codl_dose`.

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

[Unreleased]: https://github.com/sagium/ai-code-cognitive-stress/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/sagium/ai-code-cognitive-stress/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/sagium/ai-code-cognitive-stress/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/sagium/ai-code-cognitive-stress/releases/tag/v0.1.0
