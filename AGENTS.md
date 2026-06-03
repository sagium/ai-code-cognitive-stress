# AGENTS.md — ai-code-cognitive-stress

Instructions for AI assistants and agent coding tools (Claude Code, Codex CLI,
Aider, Cursor, Copilot, …) working in this repo — and a reminder for humans.
This is the single source of truth for repo rules and layout; there is no
separate `CLAUDE.md`.

## Releasing & publishing — never without an explicit prompt

Do **not** publish or push anything unless the maintainer explicitly asks for it
in that request. This includes, but is not limited to:

- Publishing to PyPI (`twine upload`, `hatch publish`, `flit publish`, etc.)
- `git push` to any remote, and creating git tags or GitHub/GitLab releases
- Building and pushing Docker images or any other distributable artifact to a
  registry

Building artifacts **locally** for verification is fine (e.g. `python -m build`
into `dist/`). Stop there and report — the maintainer performs the actual
release. "Build it" or "package it" is **not** permission to publish.

## Local-only (core invariant)

The tool reads local agent-coding session logs and writes an HTML
report to disk. It must **never send data off the machine** — no telemetry, no
network calls at render time, no remote data collection. Do not reintroduce any
data-sharing/upload path without a separate, explicit, maintainer-approved
decision (a prior opt-in-sharing experiment was deliberately reverted).

The `--export-research` command (`stress_levels/research_export.py`) is the one
maintainer-approved sharing path, and it stays inside this invariant by design:
it **only writes an anonymized file to local disk** — the user uploads it
manually. It performs no network I/O. Do not "improve" it into an auto-upload or
add any network call to it.

`--calibrate` (`stress_levels/calibrate.py`) is the maintainer-side companion: it
reads exports the maintainer has already collected on disk and writes a local
calibration report. Same invariant — local files in, local report out, no
network. It only *suggests* a `scoring` config block; it never mutates config or
the index on its own.

## Paper (`paper/main.tex`) — current state, never a changelog

The paper always describes the method **as it is on this branch** and justifies
decisions on their own merits. It must not narrate its own revision history —
no "we dropped/reworked X", "is now", "no longer", "the earlier version" (the
git log is the changelog). The only place process history belongs is the
reflexive-loop subsection (`\label{sec:reflexive}`), where the revision loop is
itself the subject. Apply the `paper-current-state` skill
(`.claude/skills/paper-current-state/SKILL.md`) on every paper edit, and rebuild
the tracked PDF (`cd paper && make pdf`) alongside any `.tex` change.

The method does not use `git` data at all — the paper, the code, its comments,
and the docs must not reference git as a data source (not even as a rejected
alternative design).

## Project structure

```
ai-code-cognitive-stress/
├── stress_levels/              # the package (stdlib-only at runtime)
│   ├── __main__.py             # CLI entry point (--year/--month/--day/--emit-json …)
│   ├── ingest.py               # session logs → typed events
│   ├── aggregate.py            # per-day reduction + mtime-keyed disk cache
│   ├── metrics.py              # the three axes, work-hours detection, optimum, composite
│   ├── scales.py               # shared zones / colours / status (report + widget)
│   ├── dayview.py              # canonical daily-view model + compute_today_dayview (report + widget)
│   ├── render.py               # self-contained HTML report builder
│   ├── serialize.py            # JSON sibling for the chat skill
│   ├── research_export.py      # anonymized full-year export (local file; manual upload)
│   ├── calibrate.py            # maintainer: pool exports → suggest scoring (local-only, unsupervised)
│   ├── citations.py            # research-registry loader
│   ├── citations.yml           # literature registry (single source of truth)
│   └── sources/                # pluggable input adapters
│       ├── base.py             # the SessionSource protocol every adapter implements
│       └── …                   # one small adapter per supported LLM coding tool
├── tests/                      # hermetic pytest suite (synthetic fixtures only)
├── scripts/generate_demo_report.py   # deterministic demo data for the screenshots
├── docs/screenshots/           # images in the README
├── templates/report.html       # original static design mockup (not used at runtime)
├── desktop/plasmoid/            # KDE Plasma 6 widget (QML/JSON; fed by `aicogstress --emit-json`)
├── SKILL.md  ·  AGENTS.md       # chat-skill definition · this file (rules + layout)
├── pyproject.toml               # packaging (hatchling) + console scripts
└── LICENSE                      # MIT
```

Adding a tool is a single file: implement the `SessionSource` protocol in
`stress_levels/sources/base.py` (yield typed events from wherever that tool logs)
and the rest of the pipeline — aggregate, metrics, render, cache — is identical.
