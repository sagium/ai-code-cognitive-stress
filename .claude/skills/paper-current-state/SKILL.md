---
name: paper-current-state
description: Enforce that the paper (paper/main.tex) describes only the current state of the method — its decisions and their rationale — never its own revision history, and enforce its house style (no em-dash `---`, no first-person voice). MUST be applied every time the paper is created, regenerated, edited, reviewed, or reconciled (including every reconcile-critique pass). Use before committing any change under paper/.
user-invocable: true
---

# paper-current-state — the paper is a snapshot, not a changelog

The paper accretes through many revision passes. Each pass must leave it reading
as if the method had always been what it is today. Git history is the changelog;
the PDF is not. A sentence that narrates a revision ("we dropped X", "the axis
is now Y") is a defect, even when factually true.

## The rule

Every sentence states the method **as it is** and justifies each decision **on
its own merits** — never by contrast with what the paper or the code used to
say or do.

### Banned framings (the pattern, not just these strings)

- "we dropped / removed / replaced / reworked / switched to / moved away from …"
- "the earlier/previous version …", "originally", "formerly", "used to"
- "… is now …", "no longer …", "went with it", "for this version"
- "this removed our dependence on …", "which removes the earlier …'s …"

### Allowed framings

- Present-tense design statements: *"A resume is a true-idle gap in a single
  session's event timeline."*
- Justifying by contrast with an **alternative design**, not with **our past**:
  *"the toll is additive, not multiplicative: a multiplicative form would
  wrongly leave a fully-off-hours day at zero"* — fine;
  *"which removes the earlier version's confounds"* — not fine.
- Statements about the world or the literature changing: *"developers no longer
  type most of the code"*, *"burnout now has its first peer-reviewed anchor"*.
- Outstanding/future work in §`sec:validation`, stated from the present:
  *"what remains is collecting the population."*

### No git, anywhere

The method does not use `git` data. The paper must not reference git at all —
not as a data source, not as a limitation, and **not even as a rejected
alternative design** (e.g. "avoiding the confounds a commit-based measure would
face" is still a git discussion — drop it). The only acceptable occurrence is
the repository URL. The same applies to the README, `SKILL.md`, code comments,
and docstrings.

### The one exception

The reflexive-loop subsection (`\label{sec:reflexive}`, "Method revision: the
paper as adversarial reviewer") documents the revision process **as
methodology** — there, naming what past passes produced is the content, not
residue. History references must stay confined to that subsection.

## Style invariants

Two house-style rules apply to **every** sentence of the paper: the abstract,
figure captions, TikZ node text, body, and the reflexive-loop subsection alike.
Both are mechanical to check (see the checklist greps) and have no content
exceptions.

### No em-dash (`---`)

The paper never uses the em-dash (`---`) to break a sentence or amplify a
clause. It splices independent clauses and inflates emphasis; restructure into
ordinary grammar instead, picking whichever fits:

- a period or semicolon for a clause that can stand alone:
  `A---it accumulates` becomes `A. It accumulates`;
- a colon to introduce a list or an explanation:
  `three axes---CODL, ...` becomes `three axes: CODL, ...`;
- a comma for a light appositive:
  `the score---never a norm---reinforces` becomes
  `the score, never a norm, reinforces`;
- parentheses for a true aside:
  `the rate---lines per hour---is` becomes `the rate (lines per hour) is`.

This targets the **em-dash (`---`) only**. The **en-dash (`--`)** is correct
typography and stays: numeric ranges (`09:00--19:00`, `15--20\%`), score ranges
(`0--100`), and hyphenated compound names (`Job Demands--Resources`,
`duration--cost`, `perception--reality`, `speed--accuracy`).

### No first person

The paper never uses first-person voice (`we`, `our`, `us`, `ours`). Recast into
`this work` / `this study` / `this paper`, into a concrete subject (`the
method`, `the index`, `the axis`), or into the passive voice:

- `We present X` becomes `This work presents X` (or `X is presented here`);
- `we set $\beta=0.20$` becomes `the value $\beta=0.20$ is adopted`;
- `our axes` becomes `the axes`; `our index` becomes `this index`;
- `we make no assumption` becomes `no assumption is made`.

`The authors` is the one acceptable third-person agent noun, used only where one
is genuinely needed (e.g. the reflexive-loop subsection: "the authors are
incidentally $n{=}1$ subjects").

## Rewrite recipe

When a decision replaced an old one:

1. **Delete the old design's narrative** — the reader never needs to know it
   existed.
2. **Keep the rationale** if it still earns its place, reframed as a comparison
   to a hypothetical alternative ("a commit-based measure would …").
3. **Re-check the cross-references**: §7 (threats) and §8 (validation) often
   restate §3 decisions — fix every restatement, not just the first hit.

## Checklist on every paper edit

1. Audit the **whole file**, not just the diff (residue survives from older
   passes):

   ```
   grep -n -iE "no longer|is now|now (provides|supports|defined|inferred)|earlier (version|draft)|previous version|originally|formerly|used to|we (dropped|removed|replaced|reworked|switched)|was reworked|went with it|for this version|this removed" paper/main.tex
   ```

   Review every hit. Exempt: hits inside `sec:reflexive`, and statements about
   the world/literature rather than the paper's own history. Then check for git
   references (only the repository URL may match):

   ```
   grep -n -iE "\bgit\b|commit|reflog|monorepo" paper/main.tex
   ```

   Then check the two style invariants (see **Style invariants**); both must
   return **nothing**:

   ```
   grep -n -- '---' paper/main.tex
   grep -n -iE '\b(we|our|us|ours)\b' paper/main.tex
   ```
2. Verify every present-tense claim against the running code in
   `ai_code_cognitive_stress/` — the paper must be true of the code on this branch, not of
   a past or planned version.
3. Rebuild the PDF (`cd paper && make pdf`) and re-read the changed sections
   rendered. The built `ai-code-cognitive-stress-paper.pdf` is force-tracked —
   commit it alongside the `.tex` change.
