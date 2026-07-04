# Build the paper as a download-friendly PDF: ai-code-cognitive-stress-paper.pdf
# Requires a TeX distribution (pdflatex + bibtex). Source stays main.tex;
# -jobname only renames the output. Run from this dir: `make pdf`.
#
# `make slides` builds the companion presentation deck
# (ai-code-cognitive-stress-slides.pdf) from presentation.tex. The deck embeds
# the README screenshots; the `assets` target derives them from
# docs/screenshots/ (inkscape + ImageMagick `convert`, only needed when the
# screenshots change). The vector-simple ones become PDFs; the report
# screenshots are rasterized to high-res PNGs (their SVG filters do not
# survive a PDF conversion) and the tall report page is cropped into its
# year and month sections.
JOB = ai-code-cognitive-stress-paper
SLIDESJOB = ai-code-cognitive-stress-slides

SCREENSHOTS = ../docs/screenshots
ASSETS = assets/pipeline.pdf assets/day-modal.png \
         assets/overview-top.png assets/overview-month.png

.PHONY: pdf slides assets clean cleanall

pdf:
	pdflatex -interaction=nonstopmode -jobname=$(JOB) main.tex
	bibtex $(JOB)
	pdflatex -interaction=nonstopmode -jobname=$(JOB) main.tex
	pdflatex -interaction=nonstopmode -jobname=$(JOB) main.tex
	@rm -f $(JOB).aux $(JOB).bbl $(JOB).blg $(JOB).log $(JOB).out
	@echo "built $(JOB).pdf"

slides: $(ASSETS)
	pdflatex -interaction=nonstopmode -jobname=$(SLIDESJOB) presentation.tex
	pdflatex -interaction=nonstopmode -jobname=$(SLIDESJOB) presentation.tex
	@rm -f $(SLIDESJOB).aux $(SLIDESJOB).log $(SLIDESJOB).out \
	       $(SLIDESJOB).nav $(SLIDESJOB).snm $(SLIDESJOB).toc
	@echo "built $(SLIDESJOB).pdf"

assets: $(ASSETS)

assets/%.pdf: $(SCREENSHOTS)/%.svg
	@mkdir -p assets
	inkscape $< --export-type=pdf --export-filename=$@

assets/day-modal.png: $(SCREENSHOTS)/day-modal.svg
	@mkdir -p assets
	inkscape $< --export-type=png --export-width=1920 --export-filename=$@

assets/overview-top.png assets/overview-month.png &: $(SCREENSHOTS)/report-overview.svg
	@mkdir -p assets
	inkscape $< --export-type=png --export-width=1920 \
	  --export-filename=assets/report-overview-full.png
	convert assets/report-overview-full.png -crop 1920x1270+0+0 +repage \
	  assets/overview-top.png
	convert assets/report-overview-full.png -crop 1920x1200+0+1280 +repage \
	  assets/overview-month.png
	@rm -f assets/report-overview-full.png

clean:
	rm -f $(JOB).aux $(JOB).bbl $(JOB).blg $(JOB).log $(JOB).out
	rm -f $(SLIDESJOB).aux $(SLIDESJOB).log $(SLIDESJOB).out \
	      $(SLIDESJOB).nav $(SLIDESJOB).snm $(SLIDESJOB).toc

cleanall: clean
	rm -f $(JOB).pdf $(SLIDESJOB).pdf
