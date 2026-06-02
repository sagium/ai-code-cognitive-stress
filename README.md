# ai-code-cognitive-stress

[![tests](https://github.com/sagium/ai-code-cognitive-stress/actions/workflows/tests.yml/badge.svg)](https://github.com/sagium/ai-code-cognitive-stress/actions/workflows/tests.yml)

**See when AI-paced coding is wearing you down — before it burns you out.**

> Your day gets a 0–100 score and a one-word verdict —
> from `Chill` 🧊 `▁▂▃▄▅▆▇█` 🍳 `Cooked`. Playful label, real research underneath.

📄 **Read the paper:** [*Measuring the Cognitive Load of Supervising Parallel AI Coding Agents — A Local, Research-Grounded, Individualized Index*](paper/ai-code-cognitive-stress-paper.pdf) (PDF). The method, the literature behind every axis, and a section devoted to attacking our own construct.

Running several LLM coding tools at once — or many sessions of one — puts you in a role
humans rarely held before: one operator supervising multiple semi-autonomous
agents at machine pace, switching between them all day and judging their output
in parallel. That load is real, it accumulates, and it stays invisible until it
isn't. `ai-code-cognitive-stress` turns the session logs you already generate
into an honest, research-grounded picture of that load — scored against **your
own** baseline, computed **entirely on your machine**.

<p align="center">
  <img src="docs/screenshots/report-overview.svg" alt="Cognitive stress report — year overview, month heatmap, and prioritised guidance" width="100%">
</p>

### Why this matters now

This isn't speculative. AI assistance doesn't remove cognitive effort — it
**shifts it from writing to verifying and supervising**, and that load is hard to
feel from the inside. In one randomized trial, experienced developers were
**slowed ~19% by AI tooling yet believed it had sped them up** — exactly the
perception gap an honest, behavioural, after-the-fact picture is built to close.
Running many agents at once is a role humans rarely held before; its closest
studied analogue (supervising multiple drones) shows performance collapsing
non-linearly past a personal "fan-out" limit *before* the operator feels
overloaded. And burnout tracks load *without recovery*, not load alone.

Productivity dashboards count output; this counts *cognitive cost* — concurrency,
interruption, and lack of closure — against your own healthy range. The full
argument and every citation are in the [paper](paper/ai-code-cognitive-stress-paper.pdf).

### How it tackles the problem

<p align="center">
  <img src="docs/screenshots/pipeline.svg" alt="Pipeline: LLM coding-tool session logs + local git → ingest typed events → aggregate per-day (cached) → metrics (CODL · interruption · closure) → composite 0–100 vs your personal optimum → HTML report and live widgets (tkinter + KDE Plasma)" width="100%">
</p>

The whole pipeline runs locally — no network, no telemetry, nothing leaves the
machine. It reads logs you already have, reduces them to three behavioural axes
plus a composite score, and positions today against *your* history and an
individually-derived optimum (an inverted-U "flow channel", not a fixed ceiling).

---

## Installation

Pure-stdlib Python, **3.10 – 3.14** (including the latest), zero third-party
dependencies. **Not published to a package index** — you run it straight from a
clone.

```bash
git clone <repo-url> ai-code-cognitive-stress
cd ai-code-cognitive-stress
```

**The easy path — let your agent install it.** This repo ships as a chat *skill*
for the LLM coding tools you already use. Open the clone in your agent (Claude
Code, Codex CLI, Aider, Cursor, …) and ask it to install the project — it runs
`python install.py`, which registers the skill on your machine:

```bash
python install.py        # what the agent runs — registers the chat skill
```

**Using it is then just talking to your agent.** Ask *"show me my stress
profile"* or *"how loaded was my week?"* and the project skill takes over: it
generates the report, writes a focused read of your own data, and opens it in
your browser. No CLI flags to remember.

**Prefer to drive it yourself?** It's pure stdlib, so a system Python ≥ 3.10
just works:

```bash
python -m stress_levels --year 2026 --open
```

Or **with [uv](https://docs.astral.sh/uv/)** (auto-provisions a Python in range,
tkinter included, if you don't already have one):

```bash
uv run python -m stress_levels --year 2026 --open        # run from the working tree
uvx --from . ai-code-cognitive-stress --month 2026-05    # build + run the console app
uv tool install --from . ai-code-cognitive-stress        # put `aicogstress` on your PATH
aicogstress --year 2026 --open
```

Each run writes a self-contained `<output>.html` report plus a `<output>.json`
sibling (structured data for the chat skill or any other analysis layer).
Common flags: `--year YYYY` · `--month YYYY-MM` (default: current month) ·
`--day YYYY-MM-DD` · `--source <tool>` (repeatable, or `auto`; run `--help`
for the available keys) · `-o <path>` · `--open` · `--rebuild-cache` (nuke the
on-disk cache and recompute from raw logs) · `--export-research` (write an
anonymized year for the calibration study — see below).

The per-day aggregate cache lives at `${XDG_CACHE_HOME:-~/.cache}/ai-code-cognitive-stress/`.
Metrics are always recomputed from it, so algorithm changes apply on every run;
`--rebuild-cache` is only needed after the ingest/aggregate layer changes or to
force a clean rebuild.

### Live desktop widget

An always-on-top window that tracks **today** live. A compact header — the
composite **/ 100**, a one-word read on the level (*Chill* / *Heating up* /
*Cooked*), and a small intraday **score-progression sparkline** drawn as a
severity gradient — sits above the full daily view from the HTML drill-down: the
per-hour concurrency chart and the three axis tiles with zone range bars
(baseline / optimum / you markers, severity-coloured values) and collapsible
methodology. Only today is recomputed each tick (past days are cached); the
window sizes to fit everything — no scrolling:

```bash
aicogstress --widget                 # refresh every 60s
aicogstress --widget --refresh 15    # faster
```

Built on stdlib **tkinter**; on Linux you may need the system package
`python3-tk` (uv-provisioned Python already includes it). Run it from a desktop
session.

### KDE Plasma widget (Plasma 6)

A native Plasma 6 desktop/panel widget showing that **same daily view**, themed
with Kirigami so it matches your Plasma look. It reads its data by running
`aicogstress --emit-json` on a timer — local-only, no network:

```bash
python install.py --plasmoid                  # install the widget package
kquitapp6 plasmashell && kstart plasmashell    # restart Plasma to pick it up
# then: right-click the desktop or a panel → "Add Widgets…" → "Cognitive Stress"
```

On the desktop it shows the full daily view inline (sized to fit, no scrolling);
in a panel it shows the compact composite score that expands on click. Plasma 6
/ Qt 6 only (Plasma 5 is end-of-life). The package lives in `desktop/plasmoid/`
as plain QML/JSON — no Python dependencies. If the score stays blank, `aicogstress`
isn't on Plasma's `PATH`: set the absolute path to `aicogstress` in the widget's
settings. Remove it with `python install.py --uninstall --plasmoid`. After
updating the widget, restart plasmashell so it drops the cached version
(`kquitapp6 plasmashell && kstart plasmashell`).

All three surfaces — HTML report, tkinter widget, and Plasma widget — render
from one shared model (`stress_levels/dayview.py`), so they can't drift.

> `python install.py` (the agent-install path above) also registers the chat
> *skill* so you can just ask "show me my stress profile" — separate from, and
> in addition to, the CLI and widgets.

---

## Help calibrate the index (optional, anonymous)

**Why your data moves the research forward.** Today the index is honest but
*borrowed* and *single-subject*: its thresholds come from adjacent fields and have
never been fitted to agent-coding developers, and its three axes are weighted
*equally* as an explicit null hypothesis, not a measured fact. A modest
multi-developer sample is what lets those thresholds and weights be checked, refit,
and validated — turning a *principled* instrument into a *validated* one. The
paper's [validation roadmap](paper/ai-code-cognitive-stress-paper.pdf) (§7) details
exactly what pooled data unlocks.

So you can help by donating **one anonymized year** of your own metrics — the
project's single, deliberate, **opt-in** sharing path. The tool still never makes a
network call; it writes a local file and *you* choose to upload it. Two steps,
entirely under your control:

```bash
aicogstress --export-research --year 2026     # writes ./stress-levels-research-2026.json
# then upload that file at: https://tally.so/r/EkMM4q
```

The tool **never sends anything** — it writes a local file, you choose to upload it.
The export shows you a consent statement and (in a terminal) asks you to type `yes`;
pass `--i-consent` to acknowledge non-interactively. What's in the file:

- ✅ **derived daily metrics** (the three axes + composite) and the **components
  behind them**, **per-session activity counts** (message/tool-call tallies and
  durations), an **hourly activity-load shape**, and your typical **working-hour
  ranges** — enough to debug the metrics and ingestion;
- 🔀 calendar **dates randomly shifted** and a **random per-export id** — so the data
  isn't tied to you or a real calendar;
- 🚫 **no** source code, file paths, repo or branch names, commit messages, session
  text, usernames, or timezone.

Open the JSON first if you'd like to see exactly what you'd send. Because the upload
is anonymous it can't be traced back and withdrawn afterwards, so it's entirely your
call. (Tally logs submitter IPs at the platform level; the *file contents* carry no
identity.)

### Calibrating from collected exports (maintainer)

Once a batch of exports has been collected, pool them and crunch the population to
recalibrate the index so it covers the real range of work patterns:

```bash
aicogstress --calibrate ./exports --calibrate-out ./calibration-report.json
```

This reads the export files (a directory or a list of files — local only, no
network), and suggests population-fitted **normalization ceilings** and
redundancy-informed **composite weights**, plus a **work-pattern coverage map** and
population reference percentiles. It prints a ready-to-paste `scoring` block but
changes nothing on its own — review it, then opt in by setting the `scoring` block
in `config.json` (`codl_ceiling`, `interruption_ceiling`, `weights`; defaults are the
current literature values). Because the exports carry no felt-load labels, the
calibration is **unsupervised**: it fits scales and suggests weights from axis
redundancy, but does **not** validate weights against felt load — that needs a
subjective criterion (NASA-TLX / EMA) and stays future work.

---

## Project structure

The project layout and the rules for working in this repo live in
[`AGENTS.md`](AGENTS.md) — the tool-agnostic instructions file read by Claude
Code, Codex CLI, Aider, Cursor, Copilot, and others.

In short: adding a new coding tool is a single file — implement the
`SessionSource` protocol in `stress_levels/sources/base.py` (yield typed events
from wherever that tool logs) and the rest of the pipeline (aggregate, metrics,
render, cache) is identical.

---

## The metrics, why they matter, and the literature

Three behavioural axes are computed inside a **work window inferred per operator** —
the band between the 10th and 90th percentile of the hours *you* actually message
your agents, floored/ceiled outward to whole hours and applied as one stable band to
every date. There's no privileged weekend: work is whatever falls inside *your* own
hours on *any* date, and activity outside them surfaces only as an off-hours
*recovery* signal. Before ~5 days of data accrue (or if you pin one in
`stress_levels/config.json`) a conventional 09:00–19:00 band serves as a cold-start
default.

<p align="center">
  <img src="docs/screenshots/day-modal.svg" alt="Day drill-down — composite score, hourly concurrency, and the three axis tiles with range bars" width="100%">
</p>

| Axis | What it measures | How | Grounded in |
|---|---|---|---|
| **CODL** (Concurrent Operational Demand Load) | How many agent sessions you supervise at once | 1-min samples over the work window; `codl_avg` time-weighted, `codl_peak` the max. Status threshold at the working-memory cap (~4) | Working memory ≈ 4 chunks (**Cowan 2001**); non-linear degradation past fan-out limits (**Cummings & Mitchell 2008**; **Sheridan 1992**) |
| **Interruption Index** | Weighted attention-pulls per work hour | `(tool_error × 1.5 + cross-session-start × 3.0) / work_hours`. Tool calls *within* a session don't count — that's a Waiting state, not an interruption | Interrupted work is faster but more stressful (**Mark, Gudith & Klocke 2008**); external switches cost ~25% more (**Mark, Gonzalez & Harris 2005**); attention residue (**Leroy 2009**); cross-tool switches cost more (**Wickens 2008**) |
| **Closure Deficit** | Share of the git-visible loops you opened but never closed (0 = everything landed, 1 = nothing did) | `1 − closed / correlatable`: a loop (stream started in the work window) is *closed* by a git commit/merge in **its own repo** whose timestamp falls within the session's active span + 30 min; each commit closes one loop. Loops in a repo git didn't touch that day are **excluded** (scored only where git can see them). Repos auto-discovered from session cwds (or set `closure.repos` in `config.json`). Per-session correlation, not concurrency — independent of the CODL shape. Falls back to the `CODL > 1` proxy when no repos are configured | Open loops keep consuming working memory until closed (**Masicampo & Baumeister 2011**); closure removes attention residue (**Leroy 2009**) and is a recovery resource (**Sonnentag & Fritz 2007**); burnout = demands exceeding recoverable resources (**Demerouti et al. 2001**) |

**Composite (0–100)** is the equal-weighted blend of the three normalised axes —
the explicit v1 null hypothesis (no evidence yet favours one axis), stated as such
in the report's methodology footer.

**Personal optimum** is the CODL band where you historically closed the most work
with the least off-hours follow-up — your individual *flow channel* (performance
follows an inverted-U with load), marked on the charts as a target, not a ceiling.
It needs ~14 active workdays to stabilise; below that the report shows `calibrating`.

**Recovery & off-hours.** Days that never reach low load, and work that spills
outside your inferred hours, are flagged — because chronic load *without recovery*,
not peaks, is what damages you over time.

### Honest about what it can't tell you

- It measures **taskload** (objective demand from session events), not
  **workload** (felt experience). The validated subjective instrument is NASA-TLX
  (**Hart & Staveland 1988**); objective↔subjective correlation is moderate
  (r ≈ 0.4–0.6). A calm composite doesn't prove you feel calm.
- It is **not a clinical assessment.** For diagnosed burnout the validated
  instrument is the Maslach Burnout Inventory (**Maslach & Jackson 1981**). This
  is a self-run triage signal, not a diagnosis.
- The supervisory-control analogy is borrowed from UAV operators
  (**Crandall & Cummings 2007**) and is plausible but **unvalidated** for LLM
  oversight. The Closure Deficit now folds in real git commits/merges,
  correlating each to a session by repo and time overlap — a heuristic, not a
  shared identifier; loops in repos git didn't touch are excluded by design, so
  it scores closure only where git can see it, and with no repos configured it
  falls back to a concurrency-presence proxy.

Every threshold, weight, and recommendation traces to an entry in
[`stress_levels/citations.yml`](stress_levels/citations.yml) — the report renders
each citation at the point the number appears, never as a bare figure.

The full bibliography (working memory, supervisory-control fan-out, the
interruption/attention-residue literature, the Job Demands–Resources model, and the
recent AI-assisted-coding studies) lives in the
[paper](paper/ai-code-cognitive-stress-paper.pdf) and in machine-readable form in
[`stress_levels/citations.yml`](stress_levels/citations.yml).

---

## License

[MIT](LICENSE) © 2026 Marinos Prevenios. Local-only by design.
