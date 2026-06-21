---
name: onboard
description: First-run onboarding for ai-code-cognitive-stress — installs the tool if it isn't already, verifies the setup (CLI on PATH, config seeded, which agent-coding tools' logs were found, whether any session data exists yet, desktop widget), then explains what to do next, including the common no-data-yet case. Use when someone is new to the tool, asks to get started or set things up, or runs /onboard.
---

# onboard

The front door for a brand-new user. Takes someone from "cloned repo" to
"knows exactly what they have and what to do next" — and, crucially, names the
empty state instead of presenting a silent blank report (the usual first-run
cliff).

This skill **orchestrates** the existing pieces; it does not reimplement them:

- OS-specific install detail lives in `install-aicogstress`
  (`.claude/skills/install-aicogstress/SKILL.md`)
- the actual setup is `install.py` at the repo root
- once data exists, the chat skill (`SKILL.md` at the repo root) renders the
  report and analysis.

Run every command in the user's shell. Use **Bash** on macOS/Linux and
**PowerShell** on Windows — detect with `uname` vs `$env:OS -eq "Windows_NT"`.
On Windows the CLI is not on PATH; use its install location
`$env:LOCALAPPDATA\Programs\aicogstress\aicogstress.bat` in place of
`aicogstress` below.

## Step 1 — is it installed?

```bash
command -v aicogstress
```

- **Found** → skip to Step 2.
- **Not found** → install it. Follow `install-aicogstress` for the per-OS
  dependency checks, then run `python3 install.py` from the repo root.
  `install.py` is idempotent — re-running it is always safe.

## Step 2 — verify the setup (the diagnostic)

Generate a report and read the diagnostics the tool prints to **stderr** — this
is the most reliable health check because it exercises the real pipeline. Use
the current year:

```bash
aicogstress --year <YYYY> --source auto
```

The stderr lines tell you, in order:

- `window: … sources: …` — which agent-coding tools were discovered
  (`claude-code`, `codex`, …). If a tool the user relies on is missing, its
  logs aren't where that adapter looks — note it.
- `ingested N events from M sessions …` — **this is the data check.**
  `M > 0` means real session data exists; **`M = 0` means no data yet**
  (handle it in Step 3, do not treat the run as a failure).
- `recovered K day(s) from the durable archive …` — history preserved across
  source-log recycling (informational).
- `report: …` / `data: …` — where the HTML and its JSON sibling were written
  (defaults to `~/stress-profile.html`).

Then confirm the supporting pieces:

- **Config**: `ai_code_cognitive_stress/core/config.json` exists (seeded from
  `config.default.json` by `install.py`; the tool falls back to defaults when
  it's absent). Mention the user can edit it to tune thresholds.
- **Widget (optional)**: note which OS widget applies — KDE Plasma 6 /
  Übersicht / Windows WebView2 — and that it polls
  `aicogstress --emit-html-card` about every 60 s. Point at `install-aicogstress`
  Step 5 to (re)install or reload it.

## Step 3 — explain what they have, and what's next

Branch on the session count from Step 2:

**Data exists (`M > 0`)** — open the report and hand off:

```bash
aicogstress --year <YYYY> --open
```

Then let the chat skill take over for analysis (the user can ask "show me my
stress profile"). Briefly orient them: the composite score, the three axes
(CODL / Interruption Index / Closure Deficit), and the personal optimum.

**No data yet (`M = 0`)** — do **not** present the blank report as a result.
Say plainly, e.g.:

> "Setup is complete, but there's no session data to profile yet. The tool
> reads your agent-coding session logs as you work — come back after a coding
> session or two, then run `/onboard` again (or just ask me to show your stress
> profile) and you'll see real numbers."

Offer concrete next steps: confirm which agent-coding tools they use and that
the matching `--source` was discovered in Step 2, and mention the desktop
widget will light up on its own once data lands.

## Step 4 — point to the rest

- `/contribute-data` — optionally donate fully anonymized stats to calibrate
  the tool's thresholds. Entirely manual; nothing is ever uploaded
  automatically.
- Re-run this skill any time to re-check the setup.

## Constraints

- **No network calls.** This skill only reads local logs and runs the local
  CLI — consistent with the tool's core constraint. Never add an upload step.
- **Never fabricate a non-empty report.** If there's no data, say so (Step 3).
- **Don't duplicate** install or report logic here — defer to
  `install-aicogstress`, `install.py`, and the chat skill.
