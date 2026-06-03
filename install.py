#!/usr/bin/env python3
"""Cross-platform installer for the ai-code-cognitive-stress skill.

Works on Linux, macOS, and Windows. Creates a directory symlink from
~/.claude/skills/ai-code-cognitive-stress to this repo so Claude Code picks it up
on next start.

On Windows, symbolic links require either administrator privileges or
Developer Mode enabled. When neither is available we fall back to a
junction point (NTFS-only, dir-only symlink with no admin requirement).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET = Path.home() / ".claude" / "skills" / "ai-code-cognitive-stress"

# KDE Plasma 6 desktop widget (optional, --plasmoid). Lives as plain QML/JSON
# data files in the repo; installs into the user's plasmoids directory.
PLASMOID_ID = "org.cognitivestress.plasmoid"
PLASMOID_SRC = REPO_DIR / "desktop" / "plasmoid" / PLASMOID_ID
PLASMOID_DEST = (
    Path.home() / ".local" / "share" / "plasma" / "plasmoids" / PLASMOID_ID
)

# macOS Übersicht desktop widget (optional, --ubersicht). A single JSX file;
# installs into Übersicht's widgets directory.
UBERSICHT_SRC = REPO_DIR / "desktop" / "ubersicht" / "cognitive-stress.jsx"
UBERSICHT_DEST = (
    Path.home() / "Library" / "Application Support" / "Übersicht" / "widgets"
    / "cognitive-stress.jsx"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install ai-code-cognitive-stress as a Claude Code skill on any OS.",
    )
    parser.add_argument(
        "--target", type=Path, default=DEFAULT_TARGET,
        help=f"Where to install (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help=(
            "Copy the repo instead of symlinking. Useful on Windows without "
            "admin/Developer Mode, or when the install target lives on a "
            "filesystem that doesn't support symlinks."
        ),
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Remove an existing install.",
    )
    parser.add_argument(
        "--plasmoid", action="store_true",
        help=(
            "Also install the KDE Plasma 6 desktop widget (with --uninstall, "
            "remove it). Linux/KDE only — silently skipped on other platforms."
        ),
    )
    parser.add_argument(
        "--ubersicht", action="store_true",
        help=(
            "Also install the macOS Übersicht desktop widget (with "
            "--uninstall, remove it). macOS only — silently skipped on other "
            "platforms; needs Übersicht (tracesof.net/uebersicht)."
        ),
    )
    return parser.parse_args()


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
        print(f"Nothing to remove at {target}")
        return 0
    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)
    print(f"Removed: {target}")
    return 0


def install(target: Path, force_copy: bool) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)

    if already_installed(target):
        print(f"Already installed: {target} -> {REPO_DIR}")
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
    print(f"Installed (symlink): {target} -> {REPO_DIR}")
    print("Restart Claude Code to pick up the new skill.")
    return 0


def _install_windows(target: Path) -> int:  # pragma: no cover — Windows-only
    # Try a directory symlink first (requires admin or Developer Mode).
    try:
        target.symlink_to(REPO_DIR, target_is_directory=True)
        print(f"Installed (symlink): {target} -> {REPO_DIR}")
        print("Restart Claude Code to pick up the new skill.")
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
        print(f"Installed (junction): {target} -> {REPO_DIR}")
        print("Restart Claude Code to pick up the new skill.")
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
    print(f"Installed (copy): {target} <- {REPO_DIR}")
    print("Note: this is a COPY. Re-run install.py after pulling updates.")
    print("Restart Claude Code to pick up the new skill.")
    return 0


# ---------------------------------------------------------------------------
# KDE Plasma 6 widget (optional)

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


def install_plasmoid() -> int:
    """Install the Plasma 6 widget. Prefers kpackagetool6; otherwise symlinks
    the package into ~/.local/share/plasma/plasmoids (so `git pull` updates it
    live). Best-effort and additive: a failure here never blocks the skill."""
    if not sys.platform.startswith("linux"):
        print("Plasma widget: skipped (KDE Plasma is Linux-only).")
        return 0
    if not PLASMOID_SRC.is_dir():
        print(f"Plasma widget: source not found at {PLASMOID_SRC}", file=sys.stderr)
        return 1

    tool = shutil.which("kpackagetool6")
    if tool:
        import subprocess
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
    tool = shutil.which("kpackagetool6")
    if tool:
        import subprocess
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
# macOS Übersicht widget (optional)

def install_ubersicht() -> int:
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
            "Übersicht widget: Übersicht doesn't look installed "
            f"(no {UBERSICHT_DEST.parent.parent}). Get it from "
            "https://tracesof.net/uebersicht/ first.",
            file=sys.stderr,
        )
        return 1
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


def main() -> int:
    args = parse_args()
    if args.uninstall:
        rc = uninstall(args.target)
        if args.plasmoid:
            rc = uninstall_plasmoid() or rc
        if args.ubersicht:
            rc = uninstall_ubersicht() or rc
        return rc
    rc = install(args.target, force_copy=args.copy)
    if args.plasmoid:
        rc = install_plasmoid() or rc
    if args.ubersicht:
        rc = install_ubersicht() or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
