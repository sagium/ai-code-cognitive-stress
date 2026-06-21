# Paper

A short conference-style write-up of the method behind `ai-code-cognitive-stress`:
what it measures, how, the literature it rests on, and a deliberately adversarial
treatment of its own validity plus a validation roadmap.

- `main.tex` — the paper (two-column, self-contained `article` class).
- `references.bib` — bibliography (mirrors `ai_code_cognitive_stress/core/citations.yml`, plus a
  software citation for this repo as the proof of concept).

## Build

Needs a TeX distribution (`pdflatex` + `bibtex`):

```bash
cd paper
make pdf          # → ai-code-cognitive-stress-paper.pdf
```

Manual fallback (no `make`):

```bash
cd paper
pdflatex -interaction=nonstopmode -jobname=ai-code-cognitive-stress-paper main.tex
bibtex ai-code-cognitive-stress-paper
pdflatex -interaction=nonstopmode -jobname=ai-code-cognitive-stress-paper main.tex
pdflatex -interaction=nonstopmode -jobname=ai-code-cognitive-stress-paper main.tex
# → ai-code-cognitive-stress-paper.pdf
```

Or, if you have `latexmk`:

```bash
latexmk -pdf -jobname=ai-code-cognitive-stress-paper main.tex
```

## Targeting a specific venue

The default uses the stock `article` class so it compiles anywhere. To submit to
a venue, swap the first line of `main.tex`:

- IEEE conference: `\documentclass[conference]{IEEEtran}`
- ACM: `\documentclass[sigconf]{acmart}`

and adjust the title/author block to that class's conventions. The body,
equations, and bibliography are class-agnostic.
