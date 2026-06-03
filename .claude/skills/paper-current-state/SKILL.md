---
name: paper-current-state
description: Enforce that the paper (paper/main.tex) describes only the current state of the method — its decisions and their rationale — never its own revision history. MUST be applied every time the paper is created, regenerated, edited, reviewed, or reconciled (including every reconcile-critique pass). Use before committing any change under paper/.
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
2. Verify every present-tense claim against the running code in
   `stress_levels/` — the paper must be true of the code on this branch, not of
   a past or planned version.
3. Rebuild the PDF (`cd paper && make pdf`) and re-read the changed sections
   rendered. The built `ai-code-cognitive-stress-paper.pdf` is force-tracked —
   commit it alongside the `.tex` change.
