---
name: install-aicogstress
description: Install the ai-code-cognitive-stress tool on Linux or macOS — checks Python version, installs uv/pipx if missing, runs install.py, and optionally sets up the desktop widget (KDE Plasma 6 or GTK3+WebKit2GTK on Linux, Übersicht on macOS). Use when the user asks to install the tool, set up the CLI, or get the desktop widget running.
---

# install-aicogstress

One-command installer for the `aicogstress` CLI and optional desktop widget.
Covers Linux (KDE Plasma 6 on KDE; GTK3 + WebKit2GTK on GNOME/XFCE/Cinnamon/MATE/Budgie)
and macOS (Übersicht). Run every step in the user's terminal; do not skip
dependency checks.

## Step 1 — detect OS and Python

```bash
uname -s          # Darwin = macOS, Linux = Linux
python3 --version # need 3.10 or higher
```

If Python < 3.10, tell the user and stop — they need to upgrade first:
- macOS: `brew install python@3.13`
- Ubuntu/Debian: `sudo apt install python3.13`
- Fedora/RHEL: `sudo dnf install python3.13`

## Step 2 — ensure uv is available (preferred) or pipx

```bash
which uv || which pipx
```

If neither is found:

**macOS:**
```bash
brew install uv
```

**Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# or: pip install --user uv
```

If `curl` / `brew` / `pip` are also missing, tell the user which package
manager to use for their distro and pause — do not guess.

## Step 3 — run the installer

From the repo root:

```bash
python3 install.py
```

`install.py` is idempotent — re-running it is always safe.

It will:
1. Symlink the chat skill into `~/.claude/skills/`
2. Install the `aicogstress` CLI via `uv tool install --editable .`
   (falls back to a small launcher script in `~/.local/bin/` if uv isn't found)
3. Install the desktop widget for the current OS (see below)
4. Run a first computation so the widget opens with real data

## Step 4 — verify

```bash
aicogstress --emit-html-card | head -5
```

Should output the opening lines of an HTML card. If it prints
`ModuleNotFoundError`, the entry-point script is stale — reinstall:

```bash
uv tool install --reinstall --editable .
```

## Step 5 — desktop widget (OS-specific)

### Linux — KDE Plasma 6

The plasmoid requires KDE Plasma 6 with `QtWebEngine`. Check:

```bash
plasmashell --version           # need 6.x
qmake --version 2>/dev/null || echo "no qmake"
```

If Plasma < 6, the widget is not available — tell the user.

`install.py --plasmoid` handles the widget install. To reload an existing
widget after an update, restart plasmashell:

```bash
plasmashell --replace &>/dev/null &
```

### Linux — GNOME, XFCE, Cinnamon, MATE, Budgie (non-KDE)

On non-KDE Linux desktops, `python install.py` automatically installs the
GTK3 + WebKit2GTK widget instead of the Plasma plasmoid.

The GTK widget requires `python3-gi` and `gir1.2-webkit2-4.1`. Check:

```bash
python3 -c "import gi; gi.require_version('WebKit2','4.1'); from gi.repository import WebKit2; print('ok')" \
  || python3 -c "import gi; gi.require_version('WebKit2','4.0'); from gi.repository import WebKit2; print('ok (4.0)')"
```

If missing, install the dependencies:

- Debian/Ubuntu: `sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi`
- Fedora: `sudo dnf install webkit2gtk4.1 python3-gobject gtk3`
- Arch: `sudo pacman -S webkit2gtk python-gobject gtk3`

Then (re)install the widget explicitly:

```bash
python install.py --gtk
```

The widget is installed to `~/.local/share/aicogstress/gtk-widget/` and an
XDG autostart entry is written to `~/.config/autostart/` so it launches on
login. To start it immediately:

```bash
python3 ~/.local/share/aicogstress/gtk-widget/cognitive-stress.py
```

### macOS — Übersicht

Requires [Übersicht](https://tracesof.net/uebersicht/). Check:

```bash
ls ~/Library/Application\ Support/Übersicht/widgets/ 2>/dev/null \
  || echo "Übersicht not found"
```

If the directory is missing, Übersicht is not installed. Direct the user to
<https://tracesof.net/uebersicht/> — it is not on Homebrew.

Once Übersicht is running, `install.py --ubersicht` copies the widget file.
Übersicht picks up changes automatically; no restart needed.

## What the user ends up with

| Component | Path / command |
|---|---|
| CLI | `aicogstress --help` |
| HTML report | `aicogstress --year YYYY --open` |
| Desktop widget | auto-refreshes every ~60 s |
| Chat skill | loaded automatically in Claude Code sessions |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found: aicogstress` | `~/.local/bin` not on PATH — add `export PATH="$HOME/.local/bin:$PATH"` to shell rc |
| Widget shows "Error" | Run `aicogstress --emit-html-card` in a terminal to see the Python traceback |
| `ModuleNotFoundError: No module named 'ai_code_cognitive_stress'` | `uv tool install --reinstall --editable .` from the repo root |
| macOS: widget not updating | Übersicht may need a refresh — click the Übersicht menu bar icon → Refresh All |
