#!/usr/bin/env python3
"""
Cognitive Stress — GTK3 + WebKit2GTK desktop widget. Thin host only.

The card itself — TODAY's full daily view (composite, sparkline, off-hours
nag, per-hour concurrency chart, axis tiles) — is rendered by the CLI:
`aicogstress --emit-html-card` prints one self-contained HTML fragment
(inline CSS + SVG, no scripts), built by ai_code_cognitive_stress/output/widget_card.py.
That module is the SINGLE renderer shared with the KDE Plasma widget and
the macOS Übersicht widget, so the surfaces can't drift. This file just
runs the CLI on a timer and shows its output in a WebKit2GTK WebView.

Private: shells out to a local CLI and renders stdout from memory
(load_html, no base URI). The card contains no scripts and no external
references; the only JavaScript run in the view is the same click-handler
and height-measure code from the KDE Plasma host.

DE-agnostic: works on GNOME, XFCE, Cinnamon, MATE, Budgie, and KDE.
Frameless always-on-top window; transparent corners on X11. Under Wayland
(GNOME, KDE) it falls back to a normal always-on-top floating window —
_NET_WM docking and gtk-layer-shell are not used here.

Configuration via environment variables:
  AICOGSTRESS_CMD      full CLI command (default: aicogstress --emit-html-card --source auto)
  AICOGSTRESS_REFRESH  refresh interval in seconds (default: 60)

Optional position hint (X11 only — Wayland compositors ignore move()):
  AICOGSTRESS_X / AICOGSTRESS_Y  pixel offsets from top-left of the primary monitor
  AICOGSTRESS_CORNER             one of: top-right (default), top-left, bottom-right, bottom-left
                                 (used when X/Y are not set; overridden by explicit X/Y)
"""

import os
import re
import sys

# ---------------------------------------------------------------------------
# PyGObject import with fallback for WebKit2 4.1 → 4.0
# ---------------------------------------------------------------------------

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2
except (ImportError, ValueError) as _gi_err:
    print(
        f"ERROR: PyGObject / WebKit2GTK not found ({_gi_err}).\n"
        "Install the dependencies for your distro:\n"
        "  Debian/Ubuntu: sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi\n"
        "  Fedora:        sudo dnf install webkit2gtk4.1 python3-gobject gtk3\n"
        "  Arch:          sudo pacman -S webkit2gtk python-gobject gtk3\n"
        "Then re-run this script.",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CARD_CMD     = os.environ.get("AICOGSTRESS_CMD", "aicogstress --emit-html-card --source auto")
REFRESH_SECS = max(10, int(os.environ.get("AICOGSTRESS_REFRESH", "60")))
CARD_WIDTH   = 384  # matches cardWidth in main.qml

# ---------------------------------------------------------------------------
# Bridge: the card carries its OWN <script> (widget_card.py _TAB_SCRIPT) that
# wires tabs / grade chips / the resize toggle and pushes the cogstress: title
# protocol. WebKit2GTK runs that inline script (unlike QtWebEngine's loadHtml in
# the KDE plasmoid, which can't, and so injects the handlers itself). We must NOT
# re-inject them here: a second handler double-fires every click, and the
# non-idempotent resize toggle (data-compact flip) nets to a no-op. The host's
# only job is to watch document.title (notify::title) — see _on_title_changed.
# This read-only one-liner is just a belt-and-suspenders initial height measure;
# the card's script reports height through the title bridge as well.
# ---------------------------------------------------------------------------

_HEIGHT_JS = (
    "Math.ceil(document.querySelector('.cogstress')"
    ".getBoundingClientRect().height)"
)

# Instant grade-chip feedback. The card's own script sets the cogstress:rate
# title (which we persist via two CLI round-trips), but does NOT light the
# chosen chip — so without this the choice only appears after the re-render,
# a few seconds later. This light-up is purely cosmetic and idempotent
# (toggle('sel', c===chip)): it does NOT touch document.title, so it never
# double-fires the bridge the way a full injected handler would. Mirrors the
# instant .sel feedback the KDE plasmoid injects.
_FEEDBACK_JS = r"""
(function () {
  var root = document.querySelector('.cogstress');
  if (!root) return;
  root.addEventListener('click', function (e) {
    var chip = e.target.closest('.grade-chip');
    if (!chip) return;
    var group = chip.closest('.grader-chips');
    if (!group) return;
    group.querySelectorAll('.grade-chip').forEach(function (c) {
      c.classList.toggle('sel', c === chip);
    });
  });
})();
"""

# ---------------------------------------------------------------------------
# Host window
# ---------------------------------------------------------------------------

class CogStressWidget(Gtk.Window):

    def __init__(self) -> None:
        super().__init__(title="Cognitive Stress")

        # Frameless, always-on-top, skip taskbar/pager
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_default_size(CARD_WIDTH, 100)
        self.set_resizable(False)

        # Transparent RGBA visual for glass corners (X11 compositors)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        self.set_app_paintable(True)

        # WebKit2 webview
        wk_settings = WebKit2.Settings()
        wk_settings.set_enable_javascript(True)
        wk_settings.set_enable_write_console_messages_to_stdout(False)
        wk_settings.set_allow_file_access_from_file_urls(False)
        wk_settings.set_allow_universal_access_from_file_urls(False)
        # Disable scrollbars
        wk_settings.set_property("enable-smooth-scrolling", False)

        self._webview = WebKit2.WebView.new_with_settings(wk_settings)
        self._webview.set_background_color(Gdk.RGBA(0, 0, 0, 0))

        # Transparent background on the WebView widget itself
        self._webview.override_background_color(
            Gtk.StateFlags.NORMAL, Gdk.RGBA(0, 0, 0, 0)
        )

        self.add(self._webview)

        # Bridge: watch document.title changes
        self._webview.connect("notify::title", self._on_title_changed)
        # Inject JS after page loads
        self._webview.connect("load-changed", self._on_load_changed)

        self.connect("destroy", Gtk.main_quit)

        # Initial load + recurring refresh
        self._card_html: str = ""
        self._load_card()
        GLib.timeout_add_seconds(REFRESH_SECS, self._on_refresh)

        # Position the window after realize
        self.connect("realize", self._position_window)
        self.show_all()

    # ------------------------------------------------------------------
    # Data: run the CLI, validate, load HTML

    def _load_card(self) -> bool:
        """Launch CARD_CMD asynchronously; render the card when it finishes.
        Async (Gio.Subprocess) so the CLI never blocks the GTK main loop — a
        synchronous run froze the widget on every refresh and every click."""
        try:
            proc = Gio.Subprocess.new(
                CARD_CMD.split(),
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error as exc:
            self._show_error(
                f"`{CARD_CMD}` failed ({exc.message}). Is aicogstress on PATH?"
            )
            return True
        proc.communicate_utf8_async(None, None, self._on_card_done)
        return True  # keep GLib timer alive

    def _on_card_done(self, proc: "Gio.Subprocess", result) -> None:
        try:
            _ok, stdout, stderr = proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            self._show_error(
                f"`{CARD_CMD}` failed ({exc.message}). Is aicogstress on PATH?"
            )
            return
        stdout = (stdout or "").strip()
        stderr = (stderr or "").strip()
        status = proc.get_exit_status()

        if status == 0 and 'class="cogstress"' in stdout:
            self._card_html = stdout
            html = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<style>"
                "html,body{margin:0;padding:0;background:transparent;overflow:hidden}"
                ".cogstress{box-shadow:none!important}"
                "</style></head>"
                "<body style='margin:0;background:transparent'>"
                + stdout
                + "</body></html>"
            )
            self._webview.load_html(html, None)
        elif status == 0:
            self._show_error(
                f"Unexpected output — `{CARD_CMD}` did not print a widget card."
            )
        else:
            msg = stderr if stderr else f"`{CARD_CMD}` failed (exit {status}). Is aicogstress on PATH?"
            self._show_error(msg)

    def _show_error(self, text: str) -> None:
        error_html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>body{margin:0;padding:14px 18px;font:600 10.5px/1.5"
            " -apple-system,sans-serif;color:#d98c80;"
            "background:rgb(24,26,23);border-radius:16px;"
            "box-sizing:border-box;width:384px}</style></head>"
            f"<body>{_html_escape(text)}</body></html>"
        )
        self._webview.load_html(error_html, None)
        self.resize(CARD_WIDTH, 80)

    def _on_refresh(self) -> bool:
        self._load_card()
        return True  # keep timer alive

    def _run_side_cmd(self, extra_args: list[str], reload: bool = True) -> None:
        """Run a side command (--rate / --set-compact / --set-view) asynchronously.
        When ``reload`` is True (the default), the card is re-fetched after the
        command finishes. Pass ``reload=False`` for persist-only commands that
        don't need an immediate re-render (e.g. --set-view: the client has already
        switched the tab, so re-rendering would snap back to the current view and
        flicker). Async so a chip/toggle click never freezes the UI."""
        self._reload_after_side = reload
        binary = CARD_CMD.split()[0]
        try:
            proc = Gio.Subprocess.new(
                [binary] + extra_args,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error:
            if reload:
                self._load_card()
            return
        proc.communicate_utf8_async(None, None, self._on_side_done)

    def _on_side_done(self, proc: "Gio.Subprocess", result) -> None:
        try:
            proc.communicate_utf8_finish(result)
        except GLib.Error:
            pass
        if getattr(self, "_reload_after_side", True):
            self._load_card()

    # ------------------------------------------------------------------
    # WebKit2 callbacks

    def _on_load_changed(
        self, webview: WebKit2.WebView, load_event: WebKit2.LoadEvent
    ) -> None:
        if load_event != WebKit2.LoadEvent.FINISHED:
            return
        # Fallback initial height measure — the card's own <script> drives all
        # interaction and reports height via the title bridge.
        self._webview.run_javascript(
            _HEIGHT_JS,
            None,
            self._on_height_result,
            None,
        )
        # Cosmetic instant grade-chip feedback only (no title bridge — see
        # _FEEDBACK_JS). Not a substitute for the card's own handlers.
        self._webview.run_javascript(_FEEDBACK_JS, None, None, None)

    def _on_height_result(
        self,
        webview: WebKit2.WebView,
        result,
        user_data,
    ) -> None:
        try:
            js_result = webview.run_javascript_finish(result)
            val = js_result.get_js_value()
            h = int(val.to_int32())
            if h > 0:
                self.resize(CARD_WIDTH, h)
        except Exception:
            pass

    def _on_title_changed(self, webview: WebKit2.WebView, _param) -> None:
        title = webview.get_title() or ""

        # Grade bridge — cogstress:rate:<day>:<grade>[:<nonce>]
        rm = re.match(r"^cogstress:rate:(\d{4}-\d{2}-\d{2}):([0-2])(?::\d+)?$", title)
        if rm:
            self._run_side_cmd(["--rate", f"{rm.group(1)}:{rm.group(2)}"])
            return

        # Compact toggle — cogstress:compact:<0|1>[:<nonce>]
        cm = re.match(r"^cogstress:compact:([01])(?::\d+)?$", title)
        if cm:
            value = "true" if cm.group(1) == "1" else "false"
            self._run_side_cmd(["--set-compact", value])
            return

        # View tab persist — cogstress:view:<key>[:<nonce>]. Persist only —
        # the card's own script already switched the visible tab, so no
        # re-render is needed (reload=False avoids a flicker/snap-back).
        vm = re.match(r"^cogstress:view:(today|week|month|year)(?::\d+)?$", title)
        if vm:
            self._run_side_cmd(["--set-view", vm.group(1)], reload=False)
            return

        # Height bridge — cogstress:h:<px>
        hm = re.match(r"^cogstress:h:(\d+)$", title)
        if hm:
            h = int(hm.group(1))
            if h > 0:
                self.resize(CARD_WIDTH, h)

    # ------------------------------------------------------------------
    # Window positioning

    def _position_window(self, _widget) -> None:
        screen  = self.get_screen()
        monitor = screen.get_primary_monitor()
        geom    = screen.get_monitor_geometry(monitor)

        # Explicit X/Y override
        env_x = os.environ.get("AICOGSTRESS_X")
        env_y = os.environ.get("AICOGSTRESS_Y")
        if env_x is not None and env_y is not None:
            try:
                self.move(geom.x + int(env_x), geom.y + int(env_y))
                return
            except ValueError:
                pass

        # Corner hint (default: top-right with 36px margin)
        corner  = os.environ.get("AICOGSTRESS_CORNER", "top-right").lower()
        margin  = 36
        win_w, win_h = self.get_size()

        if corner == "top-left":
            x = geom.x + margin
            y = geom.y + margin
        elif corner == "bottom-right":
            x = geom.x + geom.width  - win_w - margin
            y = geom.y + geom.height - win_h - margin
        elif corner == "bottom-left":
            x = geom.x + margin
            y = geom.y + geom.height - win_h - margin
        else:  # top-right (default)
            x = geom.x + geom.width - win_w - margin
            y = geom.y + margin

        self.move(x, y)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    CogStressWidget()
    Gtk.main()


if __name__ == "__main__":
    main()
