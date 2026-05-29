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

The tool reads local agent-coding session logs + local git and writes an HTML
report to disk. It must **never send data off the machine** — no telemetry, no
network calls at render time, no remote data collection. Do not reintroduce any
data-sharing/upload path without a separate, explicit, maintainer-approved
decision (a prior opt-in-sharing experiment was deliberately reverted).

## Project structure

```
ai-code-cognitive-stress/
├── stress_levels/              # the package (stdlib-only at runtime)
│   ├── __main__.py             # CLI entry point (--year/--month/--day/--widget/--emit-json …)
│   ├── ingest.py               # session logs → typed events
│   ├── aggregate.py            # per-day reduction + mtime-keyed disk cache
│   ├── metrics.py              # the three axes, work-hours detection, optimum, composite
│   ├── scales.py               # shared zones / colours / status (report + widgets)
│   ├── dayview.py              # canonical daily-view model (report + both widgets)
│   ├── render.py               # self-contained HTML report builder
│   ├── widget.py               # live tkinter daily-view widget + compute_today_dayview
│   ├── serialize.py            # JSON sibling for the chat skill
│   ├── citations.py            # research-registry loader
│   ├── citations.yml           # 20-entry literature registry (single source of truth)
│   └── sources/                # pluggable input adapters
│       ├── base.py             # the SessionSource protocol every adapter implements
│       ├── git_closure.py      # local git commits/merges (closure signal)
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
