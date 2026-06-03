---
name: ai-code-cognitive-stress
description: Generate a cognitive stress profile from the user's agent-coding-tool activity — Claude Code, OpenAI Codex CLI, Aider, or any combination — measures parallel-session load, interruption rate, and closure deficit, and produces an HTML report with daily, monthly, and yearly views against a personal optimum. Triggers when the user asks about their context switching load, cognitive stress, parallel-agent usage, burnout proximity, or wants to see how loaded their week/month has been. Reads local data only (session logs of supported tools); writes nothing remote.
---

# Stress Levels

Generates a self-contained HTML report visualising the user's cognitive load
over time, derived from local agent-coding session transcripts. Supports
Claude Code (`~/.claude/projects/`), OpenAI Codex CLI (`~/.codex/sessions/`),
Aider (`.aider.chat.history.md` per project), and any other tool via the
`SessionSource` plugin protocol.

## When to invoke

Use this skill when the user asks any of:

- "How loaded was my week / month?"
- "Show me my stress profile."
- "Am I context-switching too much?"
- "How close am I to burnout from running too many parallel agents?"
- Anything about their own cognitive load, parallel-agent usage, or
  multi-session supervisory load over a time window — regardless of
  which agent-coding tool they use.

Do **not** invoke for: questions about other people's load (the tool only
reads the local user's data); medical/clinical burnout assessment (the tool's
disclaimer points to the Maslach Burnout Inventory for that).

## How to invoke

The skill is a thin orchestrator around a Python CLI:

```bash
python -m stress_levels --month YYYY-MM   --output <path>
python -m stress_levels --year  YYYY      --output <path>
python -m stress_levels --day   YYYY-MM-DD --output <path>
```

Default time window is the current month. Default output path is
`~/stress-profile.html`.

The CLI emits **two files** on every run: the human-facing HTML report at
`<path>` and a machine-readable JSON sibling at the same name with a
`.json` suffix.

## The skill flow: generate → analyse → inject → open

Do **not** dump analysis text into the chat. The analysis goes into the
report alongside the data it describes. The user sees one thing: a
browser tab opening with everything in it. Follow this five-step flow:

### 1. Generate the report + JSON (no analysis yet)

```bash
python -m stress_levels --year 2026 --output ~/stress-2026.html
```

This writes `~/stress-2026.html` (without an analysis panel) and
`~/stress-2026.json`.

### 2. Read the JSON sibling

`~/stress-2026.json` carries the full StressProfile: per-day metrics,
work windows, percentiles, personal optimum, and ingest coverage stats.

### 3. Write a TIGHT advice-only markdown to a temp file

This is the most important constraint of the skill: **the embedded panel
is for top-priority advice, NOT a comprehensive analysis.** The
descriptive data (active days, peak day, percentiles, charts) lives in
the rest of the report. The panel should answer one question:

> *"What should I act on?"*

Rules for the markdown you write:

- **Maximum 3 bullets.** Often 1–2. If there's nothing actionable, omit
  the analysis step entirely and skip to step 4 without `--analysis`.
- **Every bullet starts with an action** ("Close loops sooner.", "Watch
  Mondays.", "Cut the parallel sessions on heavy days."). Not an
  observation ("You had 19 active days." ← belongs in the data).
- **Rank by impact.** The first bullet is the single most important
  thing the user should change. Each subsequent bullet must matter
  less, or it goes above.
- **One short rationale line per bullet** — the *why* in 1 sentence, with
  the specific evidence (a day, a value, a streak). No multi-paragraph
  explainers.
- **At most one citation across the whole panel.** Only if it sharpens
  the action. ("Cowan 2001 — working memory caps at ~4.")
- **No headings** unless absolutely needed. Bullets only.
- **Skip the calibration / coverage caveats.** Those live in the
  methodology footer.
- **Skip "peak day was Thu 14 May".** That's in the KPI card.

If the user's data is in healthy bands — composite below their p75 across
the window, no off-hours pattern, fan-out below WM cap — write a single
bullet like "**Nothing urgent.** Your load this {window} sits within your
healthy band; the calendar shows where you exceeded p75 if you want
detail." Or skip the panel entirely.

Write the markdown to a temp file, e.g. `/tmp/stress-focus.md`.

Supported markdown subset (anything else gets stripped or escaped):
ATX headings (`##`, `###`), unordered lists (`-`), paragraphs, blank-line
separators, inline `**bold**`, `*italic*`, `` `code` ``, `[text](url)`.

### 4. Re-run with `--analysis` and `--open`

```bash
python -m stress_levels --year 2026 --output ~/stress-2026.html \
    --analysis /tmp/stress-analysis.md --open
```

This rebuilds the HTML with the analysis embedded as an "Analysis"
panel directly under the header, and opens it in the user's default
browser via `webbrowser.open()`. The second run hits the disk cache for
all past days so it's fast.

### 5. One-line confirmation in chat

After the browser opens, reply to the user with a single line — e.g.
"Opened your 2026 stress profile in the browser." That's it. The
analysis is in the report; the chat doesn't repeat it.

## Important constraints

- **Never embed the analysis directly in the chat reply.** The user wants
  one artifact, not two views of the same content.
- **Always run `--open`** as the final step. The goal is "browser tab
  opens with the report"; without `--open` the user has to copy a file
  path.
- **Idempotent across re-runs.** If the user asks again later, generate
  fresh analysis; the cache makes ingest cheap.
- The HTML and JSON paths can be anywhere convenient — `~/`, `/tmp/`, a
  per-window subdirectory, whatever the user prefers.

## Contributing anonymized data (optional)

If the user asks to **share / donate / contribute their data** for the
calibration study (or asks how to help fit the borrowed thresholds), use the
research-export path — never hand-roll a dump:

```bash
python -m stress_levels --export-research --year 2026 --i-consent \
    --output ~/stress-levels-research-2026.json
```

- This writes **one anonymized year** to a local file: derived daily metrics and
  the components behind them, per-session activity counts (message/tool-call
  tallies and durations), an hourly activity-load shape, and typical
  working-hour ranges; **no** code, paths, repo/branch names, usernames, or
  timezone; calendar dates are randomly shifted and a random per-export id is
  used.
- `--i-consent` records the consent acknowledgment (it is embedded in the file).
  Only pass it when the user has actually agreed to share — surface the consent
  statement to them first.
- **The tool uploads nothing.** Tell the user to upload the file themselves at
  **https://tally.so/r/EkMM4q**, and that an anonymous submission can't be
  withdrawn afterwards. Offer to open the file so they can inspect it first.

## What the report contains

- **Year overview**: 12 monthly cells with avg composite stress, trend arrows,
  and a sparkline against the personal optimum.
- **Month overview**: calendar heatmap, four colour-coded summary KPIs, list
  of triggered recommendations with research citations.
- **Day view** (drill-down): composite stress curve clipped to detected work
  hours, with event annotations (commits, tool-blocks, MR mentions), a
  horizontal optimum line, and three axis tiles (CODL / Interruption Index /
  Closure Deficit), each with technique + research basis + caveat.
- **Methodology footer**: full citations list, data-source description, and
  honest caveats about taskload-vs-workload and the supervisory-control
  analogy.

## Research grounding

Every threshold and weight in the output is sourced from `citations.yml`.
Key references:

- Cowan (2001) — working-memory capacity → CODL ≥ 4 threshold
- Cummings & Mitchell (2008) — supervisory-control fan-out → PSC canary
- Mark, Gudith & Klocke (2008); Leroy (2009) — interruption cost framework
- Monk, Trafton & Boehm-Davis (2008); Altmann & Trafton (2002); Parnin &
  Rugaber (2011); Sonnentag & Fritz (2007) → Closure Deficit (resumption load:
  parked sessions reloaded cold, scored by gap length)
- McEwen (1998) — allostatic load → sustained-overload recommendation
- Yerkes & Dodson (1908); Csíkszentmihályi (1990) — optimum-load target line

## Privacy

All processing is local. The tool never sends data off-machine — including the
optional `--export-research` path, which only **writes a file**; the user
uploads it manually if they choose. Session transcripts often contain
proprietary code and credentials; treat the generated HTML as similarly
sensitive.
