# Cognitive Stress — GTK3 + WebKit2GTK desktop widget

A desktop-environment-agnostic Linux widget that hosts the same card rendered
by `aicogstress --emit-html-card`. Works on GNOME, XFCE, Cinnamon, MATE,
Budgie, and KDE (as a fallback when the native Plasma plasmoid is not desired).

## Dependencies

Install the Python GObject introspection bindings and WebKit2GTK:

**Debian / Ubuntu:**
```bash
sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi
```
(On older releases substitute `webkit2-4.0` for `webkit2-4.1` if the 4.1
package is not available — the host falls back to 4.0 automatically.)

**Fedora / RHEL:**
```bash
sudo dnf install webkit2gtk4.1 python3-gobject gtk3
```

**Arch Linux:**
```bash
sudo pacman -S webkit2gtk python-gobject gtk3
```

## Manual run

```bash
python3 cognitive-stress.py
```

Or set environment variables to customise behaviour:

```bash
AICOGSTRESS_CMD="aicogstress --emit-html-card --source auto" \
AICOGSTRESS_REFRESH=60 \
AICOGSTRESS_CORNER=top-right \
python3 cognitive-stress.py
```

Available environment variables:

| Variable | Default | Description |
|---|---|---|
| `AICOGSTRESS_CMD` | `aicogstress --emit-html-card --source auto` | CLI command that prints the card HTML |
| `AICOGSTRESS_REFRESH` | `60` | Refresh interval in seconds (minimum 10) |
| `AICOGSTRESS_CORNER` | `top-right` | One of `top-right`, `top-left`, `bottom-right`, `bottom-left` |
| `AICOGSTRESS_X` | — | Explicit X offset from monitor top-left (overrides `AICOGSTRESS_CORNER`) |
| `AICOGSTRESS_Y` | — | Explicit Y offset from monitor top-left (overrides `AICOGSTRESS_CORNER`) |

## Automatic install via install.py

From the repo root:

```bash
python install.py --gtk
```

This copies the widget to `~/.local/share/aicogstress/gtk-widget/` and writes
an XDG autostart entry so it launches on login. To remove it:

```bash
python install.py --gtk --uninstall
```

On a full install (`python install.py`) without KDE Plasma detected, the GTK
widget is installed automatically instead of the Plasma plasmoid.

## Disable autostart

Remove or edit the autostart file:

```bash
rm ~/.config/autostart/cognitive-stress-aicogstress.desktop
```

Or set `X-GNOME-Autostart-enabled=false` in that file to disable without
removing.

## X11 vs Wayland

On **X11** the widget is frameless, transparent (glass corners via the RGBA
visual), and always-on-top. Window positioning via `move()` works as expected.

On **Wayland** (GNOME, KDE, etc.) the compositor controls window placement, so:
- `move()` calls are ignored — the window appears wherever the compositor
  places it.
- Transparency still works if your compositor supports it.
- The window is still always-on-top (`set_keep_above(True)`) on compositors
  that honour the hint (KDE Wayland does; GNOME Wayland ignores it by default).

For precise desktop-anchoring on Wayland you would need `gtk-layer-shell`
(wlroots-based compositors only). This host does not depend on it — the
always-on-top floating window is acceptable for the common case.
