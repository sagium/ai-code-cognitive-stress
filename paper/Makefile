# Build the paper as a download-friendly PDF: ai-code-cognitive-stress-paper.pdf
# Requires a TeX distribution (pdflatex + bibtex). Source stays main.tex;
# -jobname only renames the output. Run from this dir: `make pdf`.
JOB = ai-code-cognitive-stress-paper

.PHONY: pdf clean cleanall

pdf:
	pdflatex -interaction=nonstopmode -jobname=$(JOB) main.tex
	bibtex $(JOB)
	pdflatex -interaction=nonstopmode -jobname=$(JOB) main.tex
	pdflatex -interaction=nonstopmode -jobname=$(JOB) main.tex
	@rm -f $(JOB).aux $(JOB).bbl $(JOB).blg $(JOB).log $(JOB).out
	@echo "built $(JOB).pdf"

clean:
	rm -f $(JOB).aux $(JOB).bbl $(JOB).blg $(JOB).log $(JOB).out

cleanall: clean
	rm -f $(JOB).pdf
