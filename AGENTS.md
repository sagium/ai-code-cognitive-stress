# AGENTS.md — ai-code-cognitive-stress

Instructions for AI assistants and agent coding tools (Claude Code, Codex CLI,
Cursor, Copilot, …) working in this repo — and a reminder for humans.
This is the single source of truth for repo rules and layout; there is no
separate `CLAUDE.md`.

## Welcome — greet the user at session start

When a new session opens in this project, present the following message to
the user before anything else:

---

```
      ))    ((
     (( *  * ))
    ((*  **  *))     ai-code-cognitive-stress
     ((  **  ))      ~~~~~~~~~~~~~~~~~~~~~~~~
       \\  //
        \\//
         ||
    ~~~~~||~~~~~
```

**Welcome to ai-code-cognitive-stress — thank you for testing this research tool!**

**Help the research (optional):**
The tool's thresholds are borrowed from adjacent research literature — real
usage data from the community would calibrate them properly. Contributing takes
about a minute: I'll generate a fully anonymized export (no code, paths, or
usernames — dates are randomly shifted) and walk you through uploading it
yourself to a secure form.

In return, as contributions come in, population baselines flow back into the
tool — so you can see how your own patterns compare to the community.

→ To contribute your anonymized data: `/contribute-data`

---

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

## No network calls (core constraint)

The tool reads agent-coding session logs and writes an HTML report to disk.
It has **no network calls** — no telemetry, no remote rendering, no data
collection. Do not add any data-sharing/upload path without a separate,
explicit, maintainer-approved decision.

The `--export-research` command (`ai_code_cognitive_stress/research/research_export.py`) is the one
maintainer-approved sharing path: it **only writes an anonymized file to
disk** — the user uploads it manually via the `contribute-data` skill. Do not
"improve" it into an auto-upload or add any network call to it — the upload
step must always be a deliberate, manual action by the user.

`--calibrate` (`ai_code_cognitive_stress/research/calibrate.py`) is the maintainer-side companion: it
reads exports the maintainer has already collected on disk and writes a
calibration report. It only *suggests* a `scoring` config block; it never
mutates config or the index on its own.

## Skills

The following skills are available. When a user invokes one by name (e.g.
`/install-aicogstress`) or asks for the described behavior, read the
corresponding file and follow its instructions precisely.

| Skill | File | When to use |
|---|---|---|
| `onboard` | `.claude/skills/onboard/SKILL.md` | User is new to the tool, asks to get started / set things up, or runs `/onboard` |
| `install-aicogstress` | `.claude/skills/install-aicogstress/SKILL.md` | User asks to install the tool, set up the CLI, or get the desktop widget |
| `contribute-data` | `.claude/skills/contribute-data/SKILL.md` | User asks to contribute data, export anonymized stats, or help the research |
| `crisp-screenshots` | `.claude/skills/crisp-screenshots/SKILL.md` | Regenerating screenshots or visual artifacts in `docs/screenshots/` |
| `paper-current-state` | `.claude/skills/paper-current-state/SKILL.md` | Every paper edit — apply before committing any change under `paper/` |
| `reconcile-critique` | `.claude/skills/reconcile-critique/SKILL.md` | User asks to reconcile a self-critique from §7 of the paper |

### One definition, both tools

Each skill is **defined once** — the canonical `SKILL.md` under
`.claude/skills/<name>/` (plus the repo-root `SKILL.md`, which is the chat
skill). Skill prose is never duplicated.

Both Claude Code and Codex use the identical `SKILL.md` format (`name` +
`description` frontmatter), but they look in different directories and neither
reads the other's:

- **Claude Code** loads skills from `.claude/skills/` — slash commands work out
  of the box.
- **Codex CLI** loads skills from `~/.agents/skills/`. `install.py` mirrors the
  canonical skills into that location (a per-skill symlink, or a copy on
  Windows), so Codex discovers the same set with no extra files in git. Re-run
  `python install.py --codex` after adding a skill.

Other agent tools without skill auto-discovery: read the relevant `SKILL.md`
directly and follow it when its trigger condition is met.

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
├── ai_code_cognitive_stress/   # the package (stdlib-only at runtime)
│   ├── __main__.py             # CLI entry point (--year/--month/--day/--emit-json …)
│   ├── core/                   # shared config, i18n, citations, markdown utilities
│   │   ├── config.py           # config loader (reads config.json, falls back to config.default.json)
│   │   ├── config.default.json # tracked, documented defaults (source of truth; ships in the package)
│   │   ├── config.json         # live, user-editable config (gitignored; seeded from defaults on install)
│   │   ├── i18n.py             # translatable strings (t/tn + date names)
│   │   ├── locales/            # one <code>.json catalog per language; en.json is the reference
│   │   ├── citations.py        # research-registry loader
│   │   ├── citations.yml       # literature registry (single source of truth)
│   │   └── markdown_min.py     # minimal Markdown → HTML converter (stdlib-only)
│   ├── pipeline/               # data pipeline: ingest → aggregate → metrics
│   │   ├── ingest.py           # session logs → typed events
│   │   ├── aggregate.py        # per-day reduction + mtime-keyed cache + durable day archive (survives source-log recycling)
│   │   └── metrics.py          # the three axes, work-hours detection, optimum, composite
│   ├── adapters/               # pluggable input adapters (one per agent-coding tool)
│   │   ├── base.py             # the SessionSource protocol every adapter implements
│   │   └── …                   # one small adapter per supported LLM coding tool
│   ├── output/                 # rendering: scales, dayview, widgets, report, serialize
│   │   ├── scales.py           # shared zones / colours / status (report + widgets)
│   │   ├── dayview.py          # canonical daily-view model + compute_today_dayview (report + widgets)
│   │   ├── widget_card.py      # the ONE widget renderer: dayview → self-contained HTML card (--emit-html-card)
│   │   ├── render.py           # self-contained HTML report builder
│   │   └── serialize.py        # JSON sibling for the chat skill
│   └── research/               # research export and calibration (maintainer tools)
│       ├── research_export.py  # anonymized full-year export (local file; manual upload)
│       └── calibrate.py        # maintainer: pool exports → suggest scoring (unsupervised)
├── tests/                      # hermetic pytest suite (synthetic fixtures only)
├── scripts/generate_demo_report.py   # deterministic demo data for the screenshots
├── scripts/capture_screenshots.py    # README screenshots as crisp mostly-vector SVGs (crisp-screenshots skill)
├── docs/screenshots/           # images in the README (scalable SVGs + the widget photo)
├── templates/report.html       # original static design mockup (not used at runtime)
├── desktop/plasmoid/            # KDE Plasma 6 widget — thin WebEngineView host for `aicogstress --emit-html-card`
├── desktop/gtk/                 # DE-agnostic Linux widget — thin GTK3 + WebKit2GTK host (same card); works on GNOME, XFCE, Cinnamon, MATE, Budgie
├── desktop/ubersicht/           # macOS Übersicht widget — thin JSX host (same card) + browser preview harness
├── desktop/windows/             # Windows widget — thin PowerShell 5.1 + WebView2 host (same card); DLLs vendored by fetch-webview2.ps1
├── SKILL.md  ·  AGENTS.md       # chat-skill definition · this file (rules + layout)
├── install.py                   # one-command setup: skill + CLI + widget + first computation
├── pyproject.toml               # packaging (hatchling) + console scripts
└── LICENSE                      # MIT
```

Adding a tool is a single file: implement the `SessionSource` protocol in
`ai_code_cognitive_stress/adapters/base.py` (yield typed events from wherever that tool logs)
and the rest of the pipeline — aggregate, metrics, render, cache — is identical.
