/*
 * Cognitive Stress — macOS desktop widget (Übersicht). Thin host only.
 *
 * The card itself — TODAY's full daily view (composite, sparkline, off-hours
 * nag, per-hour concurrency chart, axis tiles) — is rendered by the CLI:
 * `aicogstress --emit-html-card` prints one self-contained HTML fragment
 * (inline CSS + SVG, no scripts), built by stress_levels/widget_card.py.
 * That module is the SINGLE renderer shared with the KDE Plasma widget and
 * the browser preview (preview.html), so the surfaces can't drift. This file
 * just runs the CLI on a timer and injects its output verbatim.
 *
 * Local-only: shells out to a local CLI and shows its stdout. No network —
 * the card uses system font stacks, nothing is fetched.
 *
 * Install: copy or symlink this file into
 *   ~/Library/Application Support/Übersicht/widgets/
 * (or run `python install.py --ubersicht` from the repo on a Mac).
 *
 * Preview without a Mac: see preview.html next to this file — it injects the
 * same CLI output in a browser, so what you preview IS the widget.
 */

// If the card stays blank, `aicogstress` isn't on Übersicht's PATH — replace
// with the absolute path (`command -v aicogstress` in your terminal).
export const command = "aicogstress --emit-html-card";

export const refreshFrequency = 60 * 1000; // ms

// Position on the desktop — edit to taste.
export const className = `
  top: 36px;
  right: 36px;
`;

// Minimal self-contained error chip — shown when the CLI itself failed, so
// the card's own stylesheet never arrived.
const errorStyle = {
  width: 384, padding: "14px 18px", borderRadius: 16, boxSizing: "border-box",
  font: '600 10.5px/1.5 -apple-system, "Helvetica Neue", sans-serif',
  color: "#d98c80", background: "rgba(24, 26, 23, .72)",
  border: "1px solid rgba(176, 74, 58, .26)",
};

export const render = ({ output, error }) => {
  if (error)
    return (
      <div style={errorStyle}>
        {`${error} — is aicogstress on PATH? Set an absolute path in the widget's "command".`}
      </div>
    );
  if (!output || output.indexOf('class="cogstress"') === -1)
    return (
      <div style={errorStyle}>
        Unexpected output. Run `aicogstress --emit-html-card` in a terminal to check.
      </div>
    );
  return <div dangerouslySetInnerHTML={{ __html: output }} />;
};
