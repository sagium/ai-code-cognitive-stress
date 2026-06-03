#!/usr/bin/env python3
"""One-command installer for ai-code-cognitive-stress.

    python install.py

sets up everything on Linux, macOS, and Windows:

  1. the chat skill — a symlink from ~/.claude/skills/ai-code-cognitive-stress
     to this repo, so your agent picks it up on next start;
  2. the `aicogstress` CLI on your PATH — an editable install via `uv` or
     `pipx` when one is available, otherwise a small stdlib launcher script
     in ~/.local/bin (the desktop widget shells out to this command);
  3. the live desktop widget for this OS — KDE Plasma 6 on Linux,
     Übersicht on macOS (no desktop widget exists for Windows);
  4. a first metrics computation, so the widget and the report open with
     your data already in place.

Missing prerequisites never abort the run: each step reports what it needs —
with the exact install command for your distro/OS — and the rest continues.
Fix the prerequisite and re-run `python install.py` (every step is
idempotent).

Granular control: `--skill-only` registers just the chat skill; `--plasmoid`
/ `--ubersicht` (re)install just that widget; `--uninstall` removes
everything the steps above created (combine with the flags to remove only
one component).

On Windows, symbolic links require either administrator privileges or
Developer Mode enabled. When neither is available we fall back to a
junction point (NTFS-only, dir-only symlink with no admin requirement).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET = Path.home() / ".claude" / "skills" / "ai-code-cognitive-stress"

# `aicogstress` launcher script — the no-uv/no-pipx fallback for step 2.
LAUNCHER_DEST = Path.home() / ".local" / "bin" / "aicogstress"
LAUNCHER_MARK = "ai-code-cognitive-stress launcher"

# KDE Plasma 6 desktop widget. Lives as plain QML/JSON data files in the
# repo; installs into the user's plasmoids directory. The id is the
# reverse-DNS of ai-code-cognitive-stress.org.
PLASMOID_ID = "org.ai-code-cognitive-stress.plasmoid"
LEGACY_PLASMOID_IDS = ("org.cognitivestress.plasmoid",)  # pre-rename installs
PLASMOID_SRC = REPO_DIR / "desktop" / "plasmoid" / PLASMOID_ID
PLASMOIDS_DIR = Path.home() / ".local" / "share" / "plasma" / "plasmoids"
PLASMOID_DEST = PLASMOIDS_DIR / PLASMOID_ID

# macOS Übersicht desktop widget. A single JSX file; installs into
# Übersicht's widgets directory.
UBERSICHT_SRC = REPO_DIR / "desktop" / "ubersicht" / "cognitive-stress.jsx"
UBERSICHT_DEST = (
    Path.home() / "Library" / "Application Support" / "Übersicht" / "widgets"
    / "cognitive-stress.jsx"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Set up ai-code-cognitive-stress: chat skill + aicogstress CLI + "
            "desktop widget + first computation. Run with no flags for the "
            "full install."
        ),
    )
    parser.add_argument(
        "--target", type=Path, default=DEFAULT_TARGET,
        help=f"Where to install the chat skill (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help=(
            "Copy the repo instead of symlinking the skill. Useful on Windows "
            "without admin/Developer Mode, or when the install target lives "
            "on a filesystem that doesn't support symlinks."
        ),
    )
    parser.add_argument(
        "--skill-only", action="store_true",
        help="Only register the chat skill (skip CLI, widget, computation).",
    )
    parser.add_argument(
        "--plasmoid", action="store_true",
        help=(
            "Only (re)install the KDE Plasma 6 desktop widget (with "
            "--uninstall, remove it). Linux/KDE only."
        ),
    )
    parser.add_argument(
        "--ubersicht", action="store_true",
        help=(
            "Only (re)install the macOS Übersicht desktop widget (with "
            "--uninstall, remove it). macOS only; needs Übersicht "
            "(tracesof.net/uebersicht)."
        ),
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help=(
            "Remove everything install.py created (skill, CLI, widget). "
            "Combine with --skill-only / --plasmoid / --ubersicht to remove "
            "a single component."
        ),
    )
    return parser.parse_args()


def _step(title: str) -> None:
    print(f"\n— {title} —")


# ---------------------------------------------------------------------------
# Step 1: chat skill

def already_installed(target: Path) -> bool:
    """True when the target points back at the repo (whether by symlink,
    junction, or copy)."""
    if not target.exists() and not target.is_symlink():
        return False
    if target.is_symlink():
        try:
            return target.resolve(strict=False) == REPO_DIR
        except OSError:
            return False
    # A non-symlink dir — assume it's a junction or a copy; check the
    # SKILL.md identity rather than path equality.
    here_skill = REPO_DIR / "SKILL.md"
    there_skill = target / "SKILL.md"
    return there_skill.is_file() and (
        # Junctions return the original path on Windows; cheap check is
        # whether SKILL.md exists in both locations with the same size.
        here_skill.stat().st_size == there_skill.stat().st_size
    )


def uninstall(target: Path) -> int:
    if not target.exists() and not target.is_symlink():
        print(f"Chat skill: nothing to remove at {target}")
        return 0
    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)
    print(f"Chat skill: removed {target}")
    return 0


def install(target: Path, force_copy: bool) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)

    if already_installed(target):
        print(f"Chat skill: already installed ({target} -> {REPO_DIR})")
        return 0
    if target.exists() or target.is_symlink():
        print(
            f"ERROR: {target} already exists and points elsewhere.\n"
            f"  Remove it first or rerun with --uninstall.",
            file=sys.stderr,
        )
        return 1

    if force_copy:
        return _do_copy(target)
    if sys.platform == "win32":
        return _install_windows(target)
    return _install_posix(target)


def _install_posix(target: Path) -> int:
    target.symlink_to(REPO_DIR, target_is_directory=True)
    print(f"Chat skill: installed (symlink) {target} -> {REPO_DIR}")
    print("  Restart your agent to pick up the new skill.")
    return 0


def _install_windows(target: Path) -> int:  # pragma: no cover — Windows-only
    # Try a directory symlink first (requires admin or Developer Mode).
    try:
        target.symlink_to(REPO_DIR, target_is_directory=True)
        print(f"Chat skill: installed (symlink) {target} -> {REPO_DIR}")
        print("  Restart your agent to pick up the new skill.")
        return 0
    except OSError as exc:
        print(
            f"Symlink creation failed ({exc}). Falling back to junction...",
            file=sys.stderr,
        )
    # Junction fallback — NTFS-only, dir-only, but no admin needed.
    try:
        import _winapi  # type: ignore[import-not-found]
        _winapi.CreateJunction(str(REPO_DIR), str(target))
        print(f"Chat skill: installed (junction) {target} -> {REPO_DIR}")
        print("  Restart your agent to pick up the new skill.")
        return 0
    except (ImportError, OSError) as exc:
        print(
            f"Junction creation failed ({exc}). Falling back to copy...",
            file=sys.stderr,
        )
    return _do_copy(target)


def _do_copy(target: Path) -> int:
    shutil.copytree(
        REPO_DIR, target,
        ignore=shutil.ignore_patterns(
            ".git", ".github", "__pycache__", ".pytest_cache",
            "out", "*.pyc", "*.report.html", "stress-profile*.html",
        ),
    )
    print(f"Chat skill: installed (copy) {target} <- {REPO_DIR}")
    print("  Note: this is a COPY. Re-run install.py after pulling updates.")
    print("  Restart your agent to pick up the new skill.")
    return 0


# ---------------------------------------------------------------------------
# Step 2: the `aicogstress` CLI
#
# The desktop widget shells out to `aicogstress --emit-html-card`, so the
# command has to exist on PATH for the widget to show anything. Preference
# order: editable install via uv or pipx (tracks `git pull` automatically),
# then a stdlib launcher script — same effect, zero extra tools.

def install_cli() -> int:
    existing = shutil.which("aicogstress")
    if existing:
        print(f"CLI: `aicogstress` already on PATH ({existing}).")
        return 0

    for tool, cmd in (
        ("uv", ["uv", "tool", "install", "--editable", str(REPO_DIR)]),
        ("pipx", ["pipx", "install", "--editable", str(REPO_DIR)]),
    ):
        if not shutil.which(tool):
            continue
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and shutil.which("aicogstress"):
            print(f"CLI: installed `aicogstress` via {tool} (editable — "
                  "tracks `git pull`).")
            return 0
        err = (res.stderr or res.stdout).strip().splitlines()
        print(f"CLI: {tool} install failed; falling back. "
              f"({err[-1] if err else 'no output'})", file=sys.stderr)

    if sys.platform == "win32":  # pragma: no cover — Windows-only
        print(
            "CLI: no `uv` or `pipx` found — install one and re-run:\n"
            "    https://docs.astral.sh/uv/getting-started/installation/\n"
            "  (or run the tool as `python -m stress_levels` from the repo).",
        )
        return 1
    return _write_launcher()


def _write_launcher() -> int:
    LAUNCHER_DEST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_DEST.write_text(
        "#!/bin/sh\n"
        f"# {LAUNCHER_MARK} (generated by install.py)\n"
        f'PYTHONPATH="{REPO_DIR}" exec "{sys.executable}" -m stress_levels "$@"\n',
        encoding="utf-8",
    )
    LAUNCHER_DEST.chmod(0o755)
    print(f"CLI: wrote launcher {LAUNCHER_DEST} (runs this checkout).")
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(LAUNCHER_DEST.parent) not in path_dirs:
        print(
            f"  NOTE: {LAUNCHER_DEST.parent} is not on your PATH. Add it, "
            "e.g.:\n"
            f'    echo \'export PATH="$HOME/.local/bin:$PATH"\' >> '
            "~/.bashrc && exec $SHELL",
        )
    return 0


def uninstall_cli() -> int:
    removed = False
    if shutil.which("uv"):
        res = subprocess.run(
            ["uv", "tool", "uninstall", "ai-code-cognitive-stress"],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            print("CLI: removed the uv tool install.")
            removed = True
    if shutil.which("pipx") and not removed:
        res = subprocess.run(
            ["pipx", "uninstall", "ai-code-cognitive-stress"],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            print("CLI: removed the pipx install.")
            removed = True
    if LAUNCHER_DEST.is_file():
        try:
            ours = LAUNCHER_MARK in LAUNCHER_DEST.read_text(encoding="utf-8")
        except OSError:
            ours = False
        if ours:
            LAUNCHER_DEST.unlink()
            print(f"CLI: removed launcher {LAUNCHER_DEST}.")
            removed = True
    if not removed:
        print("CLI: nothing to remove.")
    return 0


# ---------------------------------------------------------------------------
# Step 3a: KDE Plasma 6 widget (Linux)

def _kde_available() -> bool:
    return bool(
        shutil.which("kpackagetool6")
        or shutil.which("plasmashell")
        or "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    )


def _qtwebengine_qml_present() -> bool:
    """Best-effort check for the QtWebEngine QML module the widget needs."""
    import glob
    patterns = (
        "/usr/lib/*/qt6/qml/QtWebEngine",   # Debian/Ubuntu multiarch
        "/usr/lib/qt6/qml/QtWebEngine",     # Arch
        "/usr/lib64/qt6/qml/QtWebEngine",   # Fedora / openSUSE
        "/usr/share/qt6/qml/QtWebEngine",
    )
    if any(glob.glob(p) for p in patterns):
        return True
    for p in os.environ.get("QML2_IMPORT_PATH", "").split(os.pathsep):
        if p and (Path(p) / "QtWebEngine").is_dir():
            return True
    return False


def _qtwebengine_hint() -> str:
    distro = ""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith(("ID=", "ID_LIKE=")):
                distro += line.split("=", 1)[1].strip().strip('"').lower() + " "
    except OSError:
        pass
    if any(d in distro for d in ("debian", "ubuntu", "mint", "pop")):
        return "sudo apt install qml6-module-qtwebengine"
    if any(d in distro for d in ("arch", "manjaro", "endeavouros")):
        return "sudo pacman -S qt6-webengine"
    if any(d in distro for d in ("fedora", "rhel", "centos")):
        return "sudo dnf install qt6-qtwebengine"
    if "suse" in distro:
        return "sudo zypper install qt6-webengine-imports"
    return (
        "Debian/Ubuntu: sudo apt install qml6-module-qtwebengine · "
        "Arch: sudo pacman -S qt6-webengine · "
        "Fedora: sudo dnf install qt6-qtwebengine"
    )


def _plasmoid_postinstall_hint() -> None:
    print(
        "  Next:\n"
        "    1. Restart Plasma so it picks up the widget:\n"
        "         kquitapp6 plasmashell && kstart plasmashell\n"
        '    2. Right-click the desktop or a panel -> "Add Widgets..." ->\n'
        '       search "Cognitive Stress" and drop it in.\n'
        "  The widget runs `aicogstress --emit-html-card` on a timer; make sure that command\n"
        "  is on PATH, or set its full path in the widget's settings (Plasma may\n"
        "  not inherit your shell PATH)."
    )


def _remove_legacy_plasmoids() -> None:
    """Drop installs under the pre-rename package id, so the renamed widget
    doesn't end up installed twice."""
    tool = shutil.which("kpackagetool6")
    for legacy in LEGACY_PLASMOID_IDS:
        removed = False
        if tool:
            res = subprocess.run(
                [tool, "--type", "Plasma/Applet", "--remove", legacy],
                capture_output=True, text=True,
            )
            removed = res.returncode == 0
        legacy_dest = PLASMOIDS_DIR / legacy
        if legacy_dest.is_symlink() or legacy_dest.is_file():
            legacy_dest.unlink()
            removed = True
        elif legacy_dest.exists():
            shutil.rmtree(legacy_dest)
            removed = True
        if removed:
            print(
                f"Plasma widget: removed the old package id ({legacy}).\n"
                "  The widget id changed — if it was already on your "
                "desktop/panel, remove the\n"
                "  dead instance and re-add \"Cognitive Stress\" after "
                "restarting Plasma."
            )


def install_plasmoid(required: bool = True) -> int:
    """Install the Plasma 6 widget. Prefers kpackagetool6; otherwise symlinks
    the package into ~/.local/share/plasma/plasmoids (so `git pull` updates it
    live). Best-effort and additive: a failure here never blocks the skill.

    `required=False` is the full-install path: a non-KDE desktop downgrades
    to an informational skip instead of an error."""
    if not sys.platform.startswith("linux"):
        print("Plasma widget: skipped (KDE Plasma is Linux-only).")
        return 0
    if not _kde_available():
        print(
            "Plasma widget: no KDE Plasma detected — skipped.\n"
            "  (The live widget currently exists for KDE Plasma 6 and macOS "
            "Übersicht only;\n"
            "  the report and CLI work everywhere.)"
        )
        return 1 if required else 0
    if not PLASMOID_SRC.is_dir():
        print(f"Plasma widget: source not found at {PLASMOID_SRC}", file=sys.stderr)
        return 1

    if not _qtwebengine_qml_present():
        print(
            "Plasma widget: the QtWebEngine QML module looks missing — the "
            "widget will load\n"
            "  but render an empty frame without it. Install it, then "
            "restart Plasma:\n"
            f"    {_qtwebengine_hint()}"
        )

    _remove_legacy_plasmoids()

    tool = shutil.which("kpackagetool6")
    if tool:
        res = None
        for op in ("--install", "--upgrade"):  # --upgrade if already present
            res = subprocess.run(
                [tool, "--type", "Plasma/Applet", op, str(PLASMOID_SRC)],
                capture_output=True, text=True,
            )
            if res.returncode == 0:
                print(f"Plasma widget: installed via kpackagetool6 ({PLASMOID_ID}).")
                _plasmoid_postinstall_hint()
                return 0
        stderr = (res.stderr or "").strip() if res else ""
        print(
            f"Plasma widget: kpackagetool6 failed ({stderr}); "
            "falling back to a symlink.",
            file=sys.stderr,
        )
    return _symlink_plasmoid()


def _symlink_plasmoid() -> int:
    PLASMOID_DEST.parent.mkdir(parents=True, exist_ok=True)
    if PLASMOID_DEST.is_symlink():
        if PLASMOID_DEST.resolve(strict=False) == PLASMOID_SRC.resolve():
            print(f"Plasma widget: already linked ({PLASMOID_DEST}).")
            _plasmoid_postinstall_hint()
            return 0
    if PLASMOID_DEST.is_symlink() or PLASMOID_DEST.exists():
        print(
            f"Plasma widget: {PLASMOID_DEST} already exists and points "
            "elsewhere; remove it to reinstall.",
            file=sys.stderr,
        )
        return 1
    PLASMOID_DEST.symlink_to(PLASMOID_SRC, target_is_directory=True)
    print(f"Plasma widget: linked {PLASMOID_DEST} -> {PLASMOID_SRC}")
    _plasmoid_postinstall_hint()
    return 0


def uninstall_plasmoid() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    _remove_legacy_plasmoids()
    tool = shutil.which("kpackagetool6")
    if tool:
        res = subprocess.run(
            [tool, "--type", "Plasma/Applet", "--remove", PLASMOID_ID],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            print(f"Plasma widget: removed via kpackagetool6 ({PLASMOID_ID}).")
            return 0
    if PLASMOID_DEST.is_symlink() or PLASMOID_DEST.is_file():
        PLASMOID_DEST.unlink()
        print(f"Plasma widget: removed {PLASMOID_DEST}.")
        return 0
    if PLASMOID_DEST.exists():
        shutil.rmtree(PLASMOID_DEST)
        print(f"Plasma widget: removed {PLASMOID_DEST}.")
        return 0
    print("Plasma widget: nothing to remove.")
    return 0


# ---------------------------------------------------------------------------
# Step 3b: macOS Übersicht widget

def install_ubersicht(required: bool = True) -> int:
    """Install the Übersicht widget: symlink the JSX into Übersicht's widgets
    directory (so `git pull` updates it live), falling back to a copy. Best-
    effort and additive: a failure here never blocks the skill."""
    if sys.platform != "darwin":
        print("Übersicht widget: skipped (macOS only).")
        return 0
    if not UBERSICHT_SRC.is_file():
        print(f"Übersicht widget: source not found at {UBERSICHT_SRC}", file=sys.stderr)
        return 1
    if not UBERSICHT_DEST.parent.parent.is_dir():
        print(
            "Übersicht widget: Übersicht isn't installed "
            f"(no {UBERSICHT_DEST.parent.parent}).\n"
            "  Install it, then re-run `python install.py`:\n"
            "    brew install --cask ubersicht\n"
            "  (or download from https://tracesof.net/uebersicht/)",
            file=sys.stderr,
        )
        return 1 if required else 0
    UBERSICHT_DEST.parent.mkdir(parents=True, exist_ok=True)
    if UBERSICHT_DEST.is_symlink():
        if UBERSICHT_DEST.resolve(strict=False) == UBERSICHT_SRC.resolve():
            print(f"Übersicht widget: already linked ({UBERSICHT_DEST}).")
            return 0
        print(
            f"Übersicht widget: {UBERSICHT_DEST} already exists and points "
            "elsewhere; remove it to reinstall.",
            file=sys.stderr,
        )
        return 1
    if UBERSICHT_DEST.exists():
        print(
            f"Übersicht widget: {UBERSICHT_DEST} already exists; "
            "remove it to reinstall.",
            file=sys.stderr,
        )
        return 1
    try:
        UBERSICHT_DEST.symlink_to(UBERSICHT_SRC)
        print(f"Übersicht widget: linked {UBERSICHT_DEST} -> {UBERSICHT_SRC}")
    except OSError:
        shutil.copy2(UBERSICHT_SRC, UBERSICHT_DEST)
        print(f"Übersicht widget: copied to {UBERSICHT_DEST}")
    print(
        "  Übersicht picks it up automatically (refresh from its menu-bar "
        "icon if not).\n"
        "  The widget runs `aicogstress --emit-html-card` on a timer; if the score "
        "stays blank,\n"
        "  set the absolute path in the file's `command` line "
        "(`command -v aicogstress`)."
    )
    return 0


def uninstall_ubersicht() -> int:
    if sys.platform != "darwin":
        return 0
    if UBERSICHT_DEST.is_symlink() or UBERSICHT_DEST.is_file():
        UBERSICHT_DEST.unlink()
        print(f"Übersicht widget: removed {UBERSICHT_DEST}.")
        return 0
    print("Übersicht widget: nothing to remove.")
    return 0


# ---------------------------------------------------------------------------
# Step 4: first computation

def first_computation() -> int:
    """Run the same code path the widget polls (`--emit-html-card`): ingests
    the session logs, fills the per-day cache, and computes today's card —
    so the widget and the first report open instantly. Never fatal: a
    failure just means the first real use computes instead."""
    cmd = [sys.executable, "-m", "stress_levels", "--emit-html-card"]
    t0 = time.monotonic()
    try:
        res = subprocess.run(
            cmd, cwd=REPO_DIR, capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        print("First computation: timed out — it will finish on first use instead.")
        return 0
    dt = time.monotonic() - t0
    if res.returncode == 0:
        print(
            f"First computation: done in {dt:.1f}s — session logs ingested, "
            "day cache primed,\n"
            "  today's card rendered. The widget shows data as soon as you "
            "add it."
        )
    else:
        tail = (res.stderr or res.stdout).strip().splitlines()[-3:]
        print(
            "First computation: failed — the widget/report will compute on "
            "first use instead."
        )
        for line in tail:
            print(f"    {line}")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    if sys.version_info < (3, 10):
        print(
            "ERROR: Python >= 3.10 is required "
            f"(this is {sys.version.split()[0]}).\n"
            "  Debian/Ubuntu: sudo apt install python3 · "
            "macOS: brew install python · or https://docs.astral.sh/uv/ "
            "(`uv python install`)",
            file=sys.stderr,
        )
        return 1
    args = parse_args()
    widget_only = args.plasmoid or args.ubersicht
    full = not (args.skill_only or widget_only)

    if args.uninstall:
        rc = 0
        if args.skill_only or full:
            rc = uninstall(args.target) or rc
        if args.plasmoid or full:
            rc = uninstall_plasmoid() or rc
        if args.ubersicht or full:
            rc = uninstall_ubersicht() or rc
        if full:
            rc = uninstall_cli() or rc
        return rc

    rc = 0
    if not widget_only:
        _step("Chat skill")
        rc = install(args.target, force_copy=args.copy) or rc
    if full:
        _step("aicogstress CLI")
        rc = install_cli() or rc
    if args.plasmoid or (full and sys.platform.startswith("linux")):
        _step("Desktop widget (KDE Plasma 6)")
        rc = install_plasmoid(required=args.plasmoid) or rc
    if args.ubersicht or (full and sys.platform == "darwin"):
        _step("Desktop widget (Übersicht)")
        rc = install_ubersicht(required=args.ubersicht) or rc
    if full:
        _step("First computation")
        first_computation()
        print(
            "\nDone. Try it:\n"
            '  · ask your agent: "show me my stress profile"\n'
            "  · or run: aicogstress --open"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
