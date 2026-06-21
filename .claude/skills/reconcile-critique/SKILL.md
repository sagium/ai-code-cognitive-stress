---
name: reconcile-critique
description: Work through one self-critique from the paper's "Threats to Validity and Self-Critique" section (paper/main.tex §7), verify whether the critique is actually true against the current code and method using a team of independent verifier agents plus an adversary, then — only after the maintainer approves a written findings-and-plan report — improve the method/code and/or correct the paper so the critique is reconciled with reality. Operates on a fresh branch off main across as many commits as needed, builds the PDF locally, and stops for human review + merge. Never pushes, never merges, never adds a network/data-sharing path.
user-invocable: true
---

# reconcile-critique

Take a single self-critique that the paper levels at its own hypothesis, find out
whether it still holds, and close the gap between paper and reality — by fixing the
experiment, by correcting the critique text, or both. The verdict is decided by a
**team of independent verifier agents plus an adversary**, synthesized by the Opus
lead; the maintainer reviews and merges. This skill only prepares the branch.

## Context

- Repo: `ai-code-cognitive-stress` — a tool (`ai_code_cognitive_stress/`) plus a
  conference-style paper (`paper/main.tex`) that argues a load-index hypothesis and
  then attacks it.
- Source of truth for rules + layout: `AGENTS.md`. Read it before acting. The hard
  invariants that constrain every fix:
  - **No network calls.** Never add telemetry, network calls, or any remote data
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
  "Implementation" (`\label{sec:impl}`). Code lives in `ai_code_cognitive_stress/`; the
  hermetic test suite is `tests/` (pytest, synthetic fixtures only).
- `$ARGUMENTS` — which critique to target: a keyword or phrase matching one §7
  `\paragraph{}` (e.g. `engagement`, `beta`, `closure`, `cross-tool`, `composite
  weights`), or its 1-based position in the section. **One critique per run.**

## The verification team

The accuracy-critical step is the **verdict** (`CONFIRMED` / `STALE` / `INACCURATE` /
`MIS-SCOPED`) on each atomic claim — every downstream decision (the plan, the code
change, the paper edit) inherits it. A single reader reaching that verdict alone fails
two ways: a **false CONFIRMED** (the critique is actually stale/inaccurate but the
reader never found the code that refutes it — the closure axis is the standing
precedent for this drift) and a **false STALE/INACCURATE** (the reader thinks the code
differs but the critique was right). So Phase 2 is run as a team:

- **Verifiers** (1–3, count chosen by complexity — see Phase 2) each *independently*
  trace every atomic claim to the real code and return a full verdict table with
  `file:line` evidence. They do **not** see each other's work — independent traces are
  what catch a missed rework.
- **One adversary** then tries to **overturn** each consensus verdict — find the code
  path or recent change that flips it; for any claim the verifiers split on, it is the
  tie-breaker.
- The **Opus lead** (this session) synthesizes the final verdict table and resolves any
  residual disagreement with its own targeted trace.

**Architecture: synchronous parallel `Agent` spawns**, not the warm named-team
`SendMessage` model. Each wave is a self-contained task — verifiers need no warm
context across waves, and the adversary is *better* with fresh, un-anchored context —
so spawn each wave's agents in a single message (parallel), let them return their
findings directly as their final message, and self-terminate. There is no team to keep
warm and **no shutdown step to manage** (no idle/orphan-agent risk). All team agents in
Phase 2 are **read-only** (no worktree, no file writes).

## Model roles

- **Opus leads and synthesizes; verifier/adversary/fidelity agents are Opus; Sonnet
  codes.** Verifying the critique is *critique work* — run the verifiers, the
  adversary, and the Phase-4 paper-fidelity reviewer on Opus (`model: "opus"`).
  Designing the fix, synthesizing verdicts, writing the paper, and reviewing the
  coder's diff stay with the Opus lead in the main session. Routine code
  implementation is delegated to **Sonnet**.
- **Phase 2 team** (read-only): spawn each verifier and the adversary via the `Agent`
  tool with `subagent_type: "claude"`, `model: "opus"`, a stable `name`
  (`verifier-1`, `verifier-2`, …, `adversary`). Do **not** pass `isolation` — they
  only read.
- **Phase 4 coder**: spawn the implementation as a Sonnet coding agent —
  `subagent_type: "claude"`, `model: "sonnet"`, `isolation: "worktree"`. Give it the
  approved plan, the no-network constraint, and the exact files/tests to change. The
  Opus lead then reviews the returned diff, runs the tests, and writes the paper
  updates itself. If a change is too small to be worth delegating, the lead may make it
  directly — but anything beyond a few lines goes to Sonnet.
- **Phase 4 paper-fidelity reviewer** (read-only, Opus): after the paper edit, a
  `paper-fidelity` agent checks `paper-current-state` compliance and that the
  reconciled §7/§8 actually match the new code.
- Everything else (Phases 0–1, 3, the Phase-2 synthesis, and the paper edits in Phase
  4) stays on the Opus lead.

If the background `Agent` model is somehow unavailable, fall back to doing the
verification inline on the lead (single-reader Phase 2 as a degraded mode) and say so
in the report — never silently skip the adversary without flagging it.

## Operating rules

- **One critique, one run.** Do not batch the whole section. Pick exactly the
  targeted paragraph.
- **Hard approval gate before any code or paper edit.** Phases 0–2 are read-only
  investigation. After Phase 2 you STOP and present findings + plan, then wait for
  the maintainer's explicit approval. Do not touch any file under `ai_code_cognitive_stress/`,
  `tests/`, or `paper/` until approved.
- **Honesty over tidiness.** Reconciling a critique does *not* mean deleting the
  caveat. Recent project history deliberately tightened claims and kept honest
  caveats. If a residual weakness remains after a fix, state it plainly; only remove
  a caveat when the code genuinely makes it false.
- **The team informs; it does not decide.** The lead owns the final verdict table. If
  the verifiers and adversary disagree and the lead cannot resolve it with evidence,
  the report must record the verdict as **contested** rather than guessing.
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

## Phase 1 — Read the critique precisely + size it (read-only)

1. Extract the exact `\paragraph{...}` text from §7. Identify the *specific,
   checkable claims* it makes — separate each "the code does X / the method assumes
   Y / this is uncalibrated" into an atomic claim.
2. Pull the matching Method/Implementation definitions (§3/§4) it refers to, by
   `\label`/`\ref`.
3. Note any §8 prediction or roadmap item that references the same weakness.
4. **Size the critique** to pick the verifier count for Phase 2. Tally: number of
   atomic claims; whether it touches a drift-prone / previously-reworked axis (closure,
   $\beta$, engagement); whether the claims span multiple method/impl sections; whether
   they turn on subtle code behaviour vs. plain text-vs-text comparison. Then:
   - **Low** (1 atomic claim, narrow single-function code surface, no known drift)
     → **1 verifier + 1 adversary**.
   - **Medium** (2–4 claims, single subsystem) → **2 verifiers + 1 adversary**.
   - **High** (≥5 claims, *or* touches a previously-reworked axis, *or* spans multiple
     method/impl sections) → **3 verifiers + 1 adversary**.

   The adversary always runs. Record the chosen count and the one-line rationale; you
   will report it in Phase 3.

## Phase 2 — Verify the critique against reality, as a team (read-only)

The verdict taxonomy each claim is assigned:

- **CONFIRMED** — the weakness is real, accurately described, and still present in
  the current code/method.
- **STALE** — was true once but the code/method has since changed (the paper text
  describes a version that no longer exists). The closure axis is a known precedent:
  it has been reworked more than once (most recently into the resumption-load metric),
  and the paragraph/roadmap had to be updated to match — watch for the same drift
  elsewhere.
- **INACCURATE** — mischaracterizes what the code actually does (wrong mechanism,
  wrong default, wrong scope).
- **MIS-SCOPED** — real but the severity/scope is over- or under-stated.

### 2a — Wave 1: independent verifiers (parallel)

Spawn the chosen number of verifier agents **in a single message** (parallel
`Agent` calls), `model: "opus"`, names `verifier-1` … `verifier-N`. Each gets the
**same** prompt and works blind to the others. Verifier prompt template:

```
You are an independent verifier reconciling one self-critique in paper/main.tex §7
against the actual code in the ai-code-cognitive-stress repo. Work alone; another
verifier is checking the same claims independently — do not coordinate.

CRITIQUE (verbatim §7 paragraph):
<paragraph text>

METHOD/IMPL DEFINITIONS IT REFERS TO (§3/§4):
<the \label/\ref text the lead pulled in Phase 1>

ATOMIC CLAIMS TO VERIFY:
1. <claim>
2. <claim>
...

For EACH claim: locate the real implementation in ai_code_cognitive_stress/ using
mcp__claude-context__search_code and the LSP tool to find what to read, then Read the
actual code. Decide whether the claim accurately describes the CURRENT code/method.
Cite file:line for every finding — a verdict with no file:line evidence is invalid.
Do not trust the paper's or the code comments' self-description; check the behaviour.

Return ONLY this JSON block as your final message — nothing else:

```json
{
  "verifier": "<your name>",
  "claims": [{
    "id": <claim number>,
    "verdict": "CONFIRMED|STALE|INACCURATE|MIS-SCOPED",
    "confidence": <0-100>,
    "evidence": ["ai_code_cognitive_stress/path.py:line — what the code does there"],
    "reasoning": "Why this verdict, comparing the claim to the cited code."
  }]
}
```
```

**Barrier-wait** for all verifiers to return (synchronous spawns return their final
message directly). Parse each JSON block.

### 2b — Consensus + Wave 2: adversary

Tabulate the verifiers' verdicts per claim. For each claim compute the **consensus
verdict** (agreement = the shared verdict; split = note both). Then spawn ONE
`adversary` agent (`model: "opus"`), given the claims, the consensus verdicts, and the
verifiers' cited evidence, tasked to overturn them. Adversary prompt template:

```
You are an adversary. For each claim below a team of verifiers reached a preliminary
verdict against the current ai-code-cognitive-stress code. Your job is to OVERTURN each
verdict — find the code path, recent change, default, or scope detail that makes it
wrong. For any claim the verifiers SPLIT on, you are the tie-breaker: find decisive
file:line evidence. Be skeptical of CONFIRMED verdicts especially — the standing
failure mode is a critique that was true once but the code has since moved on.

Locate real code with mcp__claude-context__search_code + the LSP tool, then Read it.
Cite file:line for everything.

CLAIMS + PRELIMINARY VERDICTS + VERIFIER EVIDENCE:
<table>

Return ONLY this JSON block as your final message:

```json
{
  "claims": [{
    "id": <claim number>,
    "challenge": "upheld|overturned|uncertain",
    "proposed_verdict": "CONFIRMED|STALE|INACCURATE|MIS-SCOPED",
    "evidence": ["ai_code_cognitive_stress/path.py:line — decisive detail"],
    "reasoning": "What you found trying to break the preliminary verdict."
  }]
}
```
```

### 2c — Lead synthesis

The lead owns the final verdict per claim:

- Verifiers agree **and** adversary upholds → final verdict = consensus (solid).
- Adversary overturns, or verifiers split → the lead does its **own** targeted trace
  (claude-context + LSP + Read) and decides on the evidence. If the lead still cannot
  resolve it, mark the claim **contested** in the report — do not guess.

Then, for **CONFIRMED** (and the real part of **MIS-SCOPED**) claims, assess whether a
method/code improvement is feasible and in scope:

- What is the minimal, auditable change to `ai_code_cognitive_stress/` (+ `tests/`) that
  reduces or removes the weakness?
- Does it respect the project's preference for simple, auditable signals over
  fragile inference? If the only fix needs the network or un-loggable data, it
  stays a stated caveat — reconcile in prose, do not bolt on a remote path.
- What residual weakness remains after the change, and how should the §7 paragraph +
  §8 roadmap read afterward?

**"Make sure the critique is real first."** If the final verdict is STALE / INACCURATE
/ MIS-SCOPED, the paper is wrong about itself and that must be fixed regardless of
whether the method also improves — correcting the critique text is part of the plan,
and comes first.

## Phase 3 — STOP: findings + plan, get approval

Write a report to `/tmp/reconcile-critique-<slug>.md` and present a tight summary in
chat. It must contain:

- **Target critique** (verbatim paragraph).
- **Team composition** — verifier count chosen + the one-line complexity rationale
  from Phase 1.
- **Claim-by-claim verdict table** — claim → each verifier's verdict → adversary's
  challenge → **final verdict (lead)** → `file:line` evidence. Flag any **contested**
  claim explicitly.
- **Plan**, split into:
  1. *Critique-text corrections* (for STALE/INACCURATE/MIS-SCOPED) — what the §7
     paragraph and any §8 item should say to match current reality.
  2. *Method/code improvement* (for CONFIRMED, if feasible) — exact
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
   to a **Sonnet** coding agent (see *Model roles*) — editing `ai_code_cognitive_stress/` and
   adding/updating `tests/` with synthetic fixtures only, keeping the change minimal
   and auditable. The Opus lead reviews the returned diff and runs
   `python -m pytest` (or the repo's documented test command) until green. Commit.
3. **Update the paper to match the new experiment**: revise §3/§4 (Method/
   Implementation) to describe the code as it now is, then **reconcile the §7
   paragraph** — narrow it to the honest residual weakness (or remove it only if the
   fix makes it false) — and sync the §8 prediction/roadmap item.

   Every paper edit must follow the **`paper-current-state`** skill
   (`.claude/skills/paper-current-state/SKILL.md`): the paper states the current
   method and decisions only — never revision narrative ("was reworked", "no
   longer", "is now") outside the `sec:reflexive` subsection. Run its audit grep
   before committing.

   Then spawn a read-only **`paper-fidelity`** reviewer agent (`model: "opus"`) on the
   paper diff to catch what self-review misses:

   ```
   You are reviewing an edit to paper/main.tex that reconciles one §7 self-critique
   with the current ai-code-cognitive-stress code. Check ONLY these, citing line
   numbers:
   1. paper-current-state compliance — no revision narrative ("was reworked", "is
      now", "no longer", "the earlier version", "we dropped/reworked") anywhere
      outside the sec:reflexive subsection; no em-dash house-style violations.
   2. No git mentioned as a data source anywhere in the change.
   3. The reconciled §7 paragraph and §8 roadmap item match the verdict table and the
      code as it now is — no over-claiming the fix, no caveat deleted that the code
      did not actually make false.
   Diff to review:
   <paper diff>
   Return a JSON block: { "issues": [{ "line": N, "kind": "...", "detail": "..." }],
   "verdict": "clean|needs-fix" }.
   ```

   Address any `needs-fix` issue, then commit.

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
   - Team composition (verifier count + adversary) and per-claim outcome:
     fixed-in-code / corrected-in-paper / retained-as-caveat / contested.
   - The residual weakness the paper now states.
   - Path to the built PDF and the key files changed, so they can **review the
     rendered paper + the code and merge to main themselves**.
3. Do **not** push, merge, or open a PR/MR. Stop here.

## Failure / abort conditions

- Working tree dirty at start, or not branchable off a clean main → stop and report.
- The background `Agent` model is unavailable → fall back to single-reader inline
  verification and say so in the report; never silently drop the adversary.
- A claim's verdict is genuinely contested after verifiers + adversary + the lead's
  own trace → report it as contested; do not guess a verdict to keep the table tidy.
- The only viable fix requires a network call or off-machine data → do not implement
  it; reconcile in prose and flag as out of scope.
- Tests cannot be made green for the proposed code change → stop, report the failure
  and the diff so far; do not paper over it or weaken the test.
- Maintainer does not approve the Phase 3 plan → revise or abort; never edit ahead of
  approval.
