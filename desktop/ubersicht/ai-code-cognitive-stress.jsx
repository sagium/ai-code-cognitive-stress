/*
 * Cognitive Stress — macOS desktop widget (Übersicht). Thin host only.
 *
 * The card itself — TODAY's full daily view (composite, sparkline, off-hours
 * nag, per-hour concurrency chart, axis tiles) — is rendered by the CLI:
 * `aicogstress --emit-html-card` prints one self-contained HTML fragment
 * (inline CSS + SVG, no scripts), built by ai_code_cognitive_stress/output/widget_card.py.
 * That module is the SINGLE renderer shared with the KDE Plasma widget and
 * the browser preview (preview.html), so the surfaces can't drift. This file
 * just runs the CLI on a timer and injects its output verbatim.
 *
 * Private: shells out to a local CLI and shows its stdout. No network —
 * the card uses system font stacks, nothing is fetched.
 *
 * Install: copy or symlink this file into
 *   ~/Library/Application Support/Übersicht/widgets/
 * (or run `python install.py --ubersicht` from the repo on a Mac).
 *
 * Preview without a Mac: see preview.html next to this file — it injects the
 * same CLI output in a browser, so what you preview IS the widget.
 */

// `run` must be imported — it is not a global in Übersicht JSX widgets.
// Without this import every run() call throws ReferenceError silently.
import { run } from "uebersicht";

// Übersicht runs commands via /bin/sh, which doesn't source ~/.zshenv or
// ~/.zprofile, so tools installed to ~/.local/bin (uv, pipx) aren't on PATH.
// Wrapping every shell-out in `zsh -lc '...'` gives a login shell that does
// source those files — the standard fix for macOS Übersicht widgets.
const _sh = (cmd) => `/bin/zsh -lc '${cmd}'`;

export const command = _sh("aicogstress --emit-html-card --source auto");

export const refreshFrequency = 60 * 1000; // ms

// Position on the desktop — edit to taste.
export const className = `
  top: 36px;
  right: 36px;
`;

// ---------------------------------------------------------------------------
// localStorage-backed UI state. Persists view + compact across Übersicht
// restarts without relying on config.json being up-to-date.
// The CLI is also updated via run() as a best-effort sync (for CLI tools),
// but localStorage is the authoritative source for the widget's own UI.
// ---------------------------------------------------------------------------
const _KEY_VIEW    = "aicogstress:view";
const _KEY_COMPACT = "aicogstress:compact";

const _loadView    = () => localStorage.getItem(_KEY_VIEW);
const _loadCompact = () => localStorage.getItem(_KEY_COMPACT);

// Apply persisted state to the card DOM after every render. Called via a
// callback ref so it runs after dangerouslySetInnerHTML replaces the inner
// HTML on each 60-second refresh.
const _applyState = (container) => {
  if (!container) return;
  const root = container.querySelector(".cogstress");
  if (!root) return;
  const view    = _loadView();
  const compact = _loadCompact();
  if (view !== null && /^(today|week|month|year)$/.test(view)) {
    root.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t.getAttribute("data-view") === view)
    );
    root.querySelectorAll(".view").forEach((v) =>
      v.classList.toggle("hidden", v.getAttribute("data-view") !== view)
    );
  }
  if (compact !== null) {
    root.setAttribute("data-compact", compact);
  }
};

// ---------------------------------------------------------------------------
// Minimal self-contained error chip — shown when the CLI itself failed, so
// the card's own stylesheet never arrived.
// ---------------------------------------------------------------------------
const errorStyle = {
  width: 384, padding: "14px 18px", borderRadius: 16, boxSizing: "border-box",
  font: '600 10.5px/1.5 -apple-system, "Helvetica Neue", sans-serif',
  color: "#d98c80", background: "rgb(24, 26, 23)",
  border: "1px solid rgba(176, 74, 58, .26)",
};

// ---------------------------------------------------------------------------
// Click handler. The card ships an in-page <script> that wires tabs, grade
// chips, and the resize toggle, but React's dangerouslySetInnerHTML never
// executes injected <script> tags — so under Übersicht that script is inert.
// We reproduce everything here with a delegated click handler on the outer div.
// ---------------------------------------------------------------------------
const onCardClick = (e) => {
  // Expand/collapse branch.
  const rt = e.target.closest(".resize-toggle");
  if (rt) {
    const root = rt.closest(".cogstress");
    if (root) {
      const willCompact = root.getAttribute("data-compact") !== "true";
      const val = willCompact ? "true" : "false";
      root.setAttribute("data-compact", val);
      localStorage.setItem(_KEY_COMPACT, val);
      run(_sh("aicogstress --set-compact " + val));
    }
    return;
  }

  // Grade chip branch.
  const chip = e.target.closest(".grade-chip");
  if (chip) {
    const grade = chip.getAttribute("data-grade");
    const day   = chip.getAttribute("data-day");
    if (grade !== null && day &&
        /^\d{4}-\d{2}-\d{2}$/.test(day) && /^[0-2]$/.test(grade)) {
      // Immediate visual feedback (mirrors the in-page <script> which is
      // inert here): light the chosen chip and remove the ungraded nag.
      const root = chip.closest(".cogstress");
      if (root) root.removeAttribute("data-ungraded");
      const chips = chip.closest(".grader-chips");
      if (chips) chips.querySelectorAll(".grade-chip").forEach(
        (c) => c.classList.toggle("sel", c === chip)
      );
      run(_sh("aicogstress --rate " + day + ":" + grade));
    }
    return;
  }

  // Tab branch.
  const tab = e.target.closest(".tab");
  if (!tab) return;
  const root = tab.closest(".cogstress");
  if (!root) return;
  const key = tab.getAttribute("data-view");
  root.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  root.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("hidden", v.getAttribute("data-view") !== key)
  );
  if (/^(today|week|month|year)$/.test(key)) {
    localStorage.setItem(_KEY_VIEW, key);
    run(_sh("aicogstress --set-view " + key));
  }
};

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
export const render = ({ output, error }) => {
  if (error)
    return (
      <div style={errorStyle}>
        {`${error} — is aicogstress installed? Run: uv tool install ai-code-cognitive-stress`}
      </div>
    );
  if (!output || output.indexOf('class="cogstress"') === -1)
    return (
      <div style={errorStyle}>
        Unexpected output. Run `aicogstress --emit-html-card` in a terminal to check.
      </div>
    );
  // The inline ref function is intentionally recreated each render so React
  // calls _applyState after every dangerouslySetInnerHTML update (not just mount).
  return (
    <div
      ref={(el) => _applyState(el)}
      onClick={onCardClick}
      dangerouslySetInnerHTML={{ __html: output }}
    />
  );
};
