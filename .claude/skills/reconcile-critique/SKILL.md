---
name: reconcile-critique
description: Work through one self-critique from the paper's "Threats to Validity and Self-Critique" section (paper/main.tex §7), verify whether the critique is actually true against the current code and method, then — only after the maintainer approves a written findings-and-plan report — improve the method/code and/or correct the paper so the critique is reconciled with reality. Operates on a fresh branch off main across as many commits as needed, builds the PDF locally, and stops for human review + merge. Never pushes, never merges, never adds a network/data-sharing path.
user-invocable: true
---

# reconcile-critique

Take a single self-critique that the paper levels at its own hypothesis, find out
whether it still holds, and close the gap between paper and reality — by fixing the
experiment, by correcting the critique text, or both. The maintainer reviews and
merges; this skill only prepares the branch.

## Context

- Repo: `ai-code-cognitive-stress` — a local-only tool (`stress_levels/`) plus a
  conference-style paper (`paper/main.tex`) that argues a load-index hypothesis and
  then attacks it.
- Source of truth for rules + layout: `AGENTS.md`. Read it before acting. The hard
  invariants that constrain every fix:
  - **Local-only.** Never add telemetry, network calls, or any off-machine data
    path. If the only way to "fix" a critique is a remote call (e.g. platform PR/MR
    events), that is out of scope — say so and reconcile in the paper instead.
  - **Never push or merge.** No `git push`, no tags, no releases, no PR/MR creation.
    The maintainer merges by hand after reviewing the PDF and the diff.
- The critiques live in **§7 "Threats to Validity and Self-Critique"**
  (`\section{...}\label{sec:threats}`) as a sequence of `\paragraph{...}` items. The
  companion **§8 "Falsifiable Predictions and Validation Roadmap"**
  (`\label{sec:validation}`) often references the same weaknesses — keep the two in
  sync.
- Method definitions referenced by the critiques live in §3 "Method"
  (`\label{sec:method}` and sub-labels like `sec:codl`, `sec:closure`) and §4
  "Implementation" (`\label{sec:impl}`). Code lives in `stress_levels/`; the
  hermetic test suite is `tests/` (pytest, synthetic fixtures only).
- `$ARGUMENTS` — which critique to target: a keyword or phrase matching one §7
  `\paragraph{}` (e.g. `engagement`, `beta`, `closure`, `cross-tool`, `composite
  weights`), or its 1-based position in the section. **One critique per run.**

## Model roles

- **Opus plans; Sonnet codes.** Investigation, verifying the critique, designing the
  fix, writing the paper, and reviewing the coder's diff are Opus work — done in the
  main session (which the maintainer runs on Opus). Routine code implementation is
  delegated to **Sonnet**.
- In **Phase 4**, spawn the implementation as a Sonnet coding agent via the `Agent`
  tool: `subagent_type: "claude"`, `model: "sonnet"`, `isolation: "worktree"`. Give
  it the approved plan, the local-only invariant, and the exact files/tests to
  change. The Opus session then reviews the returned diff, runs the tests, and writes
  the paper updates itself. If a change is too small to be worth delegating, the Opus
  session may make it directly — but anything beyond a few lines goes to Sonnet.
- Phases 0–3 (setup, reading, verification, plan) and the paper edits in Phase 4 stay
  on Opus.

## Operating rules

- **One critique, one run.** Do not batch the whole section. Pick exactly the
  targeted paragraph.
- **Hard approval gate before any code or paper edit.** Phases 0–2 are read-only
  investigation. After Phase 2 you STOP and present findings + plan, then wait for
  the maintainer's explicit approval. Do not touch any file under `stress_levels/`,
  `tests/`, or `paper/` until approved.
- **Honesty over tidiness.** Reconciling a critique does *not* mean deleting the
  caveat. Recent project history deliberately tightened claims and kept honest
  caveats. If a residual weakness remains after a fix, state it plainly; only remove
  a caveat when the code genuinely makes it false.
- **Branch off main, commit freely, merge never.** Multiple commits within a run are
  fine. End on the branch, clean working tree, PDF built.

## Phase 0 — Setup (read-only)

1. Resolve the target critique from `$ARGUMENTS`. If empty or ambiguous, list the §7
   `\paragraph{}` titles and ask the maintainer to pick one. Do not guess.
2. Confirm a clean starting point: `git status` clean, on `main` (or already on a
   `reconcile-critique/*` branch for this critique — then continue on it).
3. Create/checkout the working branch off main:
   `reconcile-critique/<slug-of-paragraph-title>`. If `git status` is dirty, stop and
   report rather than stashing.

## Phase 1 — Read the critique precisely (read-only)

1. Extract the exact `\paragraph{...}` text from §7. Identify the *specific,
   checkable claims* it makes — separate each "the code does X / the method assumes
   Y / this is uncalibrated" into an atomic claim.
2. Pull the matching Method/Implementation definitions (§3/§4) it refers to, by
   `\label`/`\ref`.
3. Note any §8 prediction or roadmap item that references the same weakness.

## Phase 2 — Verify the critique against reality (read-only)

For **each atomic claim**, trace it to the actual code and decide a verdict. Use
semantic search (`claude-context`) and the LSP tools to locate the real
implementation in `stress_levels/`; cite `file:line` for every finding.

Verdicts:

- **CONFIRMED** — the weakness is real, accurately described, and still present in
  the current code/method.
- **STALE** — was true once but the code/method has since changed (the paper text
  describes a version that no longer exists). The closure axis is a known precedent:
  it has been reworked more than once (most recently into the git-free
  resumption-load metric), and the paragraph/roadmap had to be updated to match —
  watch for the same drift elsewhere.
- **INACCURATE** — mischaracterizes what the code actually does (wrong mechanism,
  wrong default, wrong scope).
- **MIS-SCOPED** — real but the severity/scope is over- or under-stated.

**"Make sure the critique is real first."** If a claim is STALE / INACCURATE /
MIS-SCOPED, the paper is wrong about itself and that must be fixed regardless of
whether the method also improves — correcting the critique text is part of the plan,
and comes first.

Then, for CONFIRMED (and the real part of MIS-SCOPED) claims, assess whether a
**local-only** method/code improvement is feasible and in scope:

- What is the minimal, auditable change to `stress_levels/` (+ `tests/`) that
  reduces or removes the weakness?
- Does it respect the local-only invariant and the project's preference for simple,
  auditable signals over fragile inference? If the only fix needs the network or
  un-loggable data, it stays a stated caveat — reconcile in prose, do not bolt on a
  remote path.
- What residual weakness remains after the change, and how should the §7 paragraph +
  §8 roadmap read afterward?

## Phase 3 — STOP: findings + plan, get approval

Write a report to `/tmp/reconcile-critique-<slug>.md` and present a tight summary in
chat. It must contain:

- **Target critique** (verbatim paragraph).
- **Claim-by-claim verdict table** — claim → verdict → `file:line` evidence.
- **Plan**, split into:
  1. *Critique-text corrections* (for STALE/INACCURATE/MIS-SCOPED) — what the §7
     paragraph and any §8 item should say to match current reality.
  2. *Method/code improvement* (for CONFIRMED, if feasible & local-only) — exact
     files to change, the new behaviour, the tests to add, and the residual caveat
     the paper will retain afterward.
  3. *What stays a caveat* and why (e.g. out of scope because it needs the network).
- **Out-of-scope / no-fix** items stated explicitly.

Then **stop and ask for explicit approval** (use AskUserQuestion or a plain "approve
this plan?"). Make no edits to tracked files until the maintainer approves. If they
amend the plan, revise and re-confirm.

## Phase 4 — Implement (only after approval)

Apply the approved plan in focused commits. Order:

1. **Correct the critique/method text first** if the critique was not fully real, so
   the paper stops misdescribing itself. Commit.
2. **Improve the code/method** for confirmed weaknesses: delegate the implementation
   to a **Sonnet** coding agent (see *Model roles*) — editing `stress_levels/` and
   adding/updating `tests/` with synthetic fixtures only, keeping the change minimal
   and auditable. The Opus session reviews the returned diff and runs
   `python -m pytest` (or the repo's documented test command) until green. Commit.
3. **Update the paper to match the new experiment**: revise §3/§4 (Method/
   Implementation) to describe the code as it now is, then **reconcile the §7
   paragraph** — narrow it to the honest residual weakness (or remove it only if the
   fix makes it false) — and sync the §8 prediction/roadmap item. Commit.

   Every paper edit must follow the **`paper-current-state`** skill
   (`.claude/skills/paper-current-state/SKILL.md`): the paper states the current
   method and decisions only — never revision narrative ("was reworked", "no
   longer", "is now") outside the `sec:reflexive` subsection. Run its audit grep
   before committing.

Follow the repo's commit-message conventions (see `git-commit` skill / global
rules): tight subject, short why-focused body, **no AI-attribution trailer**, no
process narration.

Keep each commit logically distinct so the maintainer can read the diff as a story:
(text correction) → (code+tests) → (paper reconciliation).

## Phase 5 — Build the PDF + hand off

1. Build the paper locally: `cd paper && make pdf` (fallback: the manual `pdflatex →
   bibtex → pdflatex ×2` from `paper/README.md`). Confirm it compiles clean and that
   the reconciled paragraph + roadmap render as intended. `make pdf` deletes its
   `.aux/.bbl/.log/.out` on success, so read the build's stdout for
   `Output written ... (N pages)` rather than grepping the (now-removed) log; verify
   refs/citations only via the full `make pdf` (which runs bibtex), not a standalone
   `pdflatex` jobname (that skips bibtex and reports spurious "undefined citation"
   warnings). The PDF (`ai-code-cognitive-stress-paper.pdf`) is **tracked on
   purpose** — `.gitignore` ignores `paper/*.pdf` but force-includes this one with a
   `!` negation, so the canonical rendered paper ships in the repo. **Rebuild it and
   commit it** alongside the `main.tex` changes so the committed PDF stays in sync
   with source.
2. Final report to the maintainer:
   - Branch name and the commit list.
   - Per-claim outcome: fixed-in-code / corrected-in-paper / retained-as-caveat.
   - The residual weakness the paper now states.
   - Path to the built PDF and the key files changed, so they can **review the
     rendered paper + the code and merge to main themselves**.
3. Do **not** push, merge, or open a PR/MR. Stop here.

## Failure / abort conditions

- Working tree dirty at start, or not branchable off a clean main → stop and report.
- The only viable fix requires a network call or off-machine data → do not implement
  it; reconcile in prose and flag as out of scope.
- Tests cannot be made green for the proposed code change → stop, report the failure
  and the diff so far; do not paper over it or weaken the test.
- Maintainer does not approve the Phase 3 plan → revise or abort; never edit ahead of
  approval.
