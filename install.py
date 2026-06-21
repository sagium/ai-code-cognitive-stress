#!/usr/bin/env python3
"""One-command installer for ai-code-cognitive-stress.

    python install.py

sets up everything on Linux, macOS, and Windows:

  1. the skills — a symlink from ~/.claude/skills/ai-code-cognitive-stress to
     this repo so Claude Code picks them up, plus a mirror of the same skills
     into ~/.agents/skills/ so Codex CLI discovers them too (one canonical
     SKILL.md per skill, two consumers);
  2. the `aicogstress` CLI on your PATH — an editable install via `uv` or
     `pipx` when one is available, otherwise a small stdlib launcher script
     in ~/.local/bin (the desktop widget shells out to this command);
  3. the live desktop widget for this OS — KDE Plasma 6 on Linux (or the
     GTK3 + WebKit2GTK widget on non-KDE Linux desktops), Übersicht on
     macOS, WebView2 on Windows;
  4. a first metrics computation, so the widget and the report open with
     your data already in place.

Missing prerequisites never abort the run: each step reports what it needs —
with the exact install command for your distro/OS — and the rest continues.
Fix the prerequisite and re-run `python install.py` (every step is
idempotent).

Granular control: `--skill-only` registers just the skills (Claude + Codex);
`--codex` (re)mirrors only the Codex skills; `--plasmoid` / `--ubersicht` /
`--windows` / `--gtk` (re)install just that widget; `--uninstall` removes
everything the steps above created (combine with the flags to remove only
one component).

On Windows, symbolic links require either administrator privileges or
Developer Mode enabled. When neither is available we fall back to a
junction point (NTFS-only, dir-only symlink with no admin requirement).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET = Path.home() / ".claude" / "skills" / "ai-code-cognitive-stress"

# Codex CLI discovers Agent Skills from ~/.agents/skills/<name>/SKILL.md (and a
# few other locations) using the SAME SKILL.md format as Claude Code. Claude
# reads .claude/skills/, Codex reads .agents/skills/, and neither reads the
# other's directory — so we keep ONE canonical definition per skill (the
# repo-root chat skill + each .claude/skills/<name>) and mirror it into the
# Codex location here. The mirror is a per-skill symlink (POSIX) or copy
# (Windows fallback); no skill prose is ever duplicated in git.
CODEX_SKILLS_DIR = Path.home() / ".agents" / "skills"
# The chat skill is the whole repo (its SKILL.md is at the root), so it mirrors
# as a single entry pointing back at the repo, exactly like the Claude global.
CODEX_CHAT_TARGET = CODEX_SKILLS_DIR / "ai-code-cognitive-stress"

# `aicogstress` launcher script — the no-uv/no-pipx fallback for step 2.
if sys.platform == "win32":
    _win_local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    LAUNCHER_DEST = _win_local / "Programs" / "aicogstress" / "aicogstress.bat"
else:
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

# Git hooks that re-run the widget install after `git pull`/checkout, so the
# installed copy keeps tracking the repo. Marked so we only ever touch our own.
HOOK_MARK = "ai-code-cognitive-stress widget refresh"

# macOS Übersicht desktop widget. A single JSX file; installs into
# Übersicht's widgets directory.
UBERSICHT_SRC = REPO_DIR / "desktop" / "ubersicht" / "ai-code-cognitive-stress.jsx"
UBERSICHT_DEST = (
    Path.home() / "Library" / "Application Support" / "Übersicht" / "widgets"
    / "ai-code-cognitive-stress.jsx"
)

# GTK3 + WebKit2GTK desktop widget. A single Python script; installs as a copy
# into ~/.local/share/aicogstress/gtk-widget/ and registers an XDG autostart
# entry so it opens on login. Works on GNOME, XFCE, Cinnamon, MATE, Budgie,
# and KDE (as a non-KDE-Plasma fallback).
GTK_SRC      = REPO_DIR / "desktop" / "gtk"
GTK_DEST     = Path.home() / ".local" / "share" / "aicogstress" / "gtk-widget"
GTK_AUTOSTART = (
    Path.home() / ".config" / "autostart" / "cognitive-stress-aicogstress.desktop"
)

# Windows WebView2 desktop widget. A directory of PowerShell + vendored DLLs;
# installs into %LOCALAPPDATA%\Programs\aicogstress\widget and registers a
# VBScript launcher in the user Startup folder so it opens on login.
WINDOWS_WIDGET_SRC = REPO_DIR / "desktop" / "windows"
if sys.platform == "win32":  # pragma: no cover — Windows-only
    _win_local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    _win_roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
else:
    _win_local = Path.home() / "AppData" / "Local"    # placeholder (never used on POSIX)
    _win_roaming = Path.home() / "AppData" / "Roaming"
WINDOWS_WIDGET_DEST = _win_local / "Programs" / "aicogstress" / "widget"
WINDOWS_LAUNCHER_DEST = (
    _win_roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    / "Startup" / "aicogstress-widget.vbs"
)
WINDOWS_LAUNCHER_MARK = "ai-code-cognitive-stress windows widget"


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
        help=(
            "Only register the skills — for both Claude Code "
            "(~/.claude/skills) and Codex (~/.agents/skills); skip CLI, "
            "widget, computation."
        ),
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
        "--windows", action="store_true",
        help=(
            "Only (re)install the Windows WebView2 desktop widget (with "
            "--uninstall, remove it). Windows only; needs the WebView2 "
            "Evergreen Runtime (pre-installed on Windows 11, auto-deployed "
            "on Windows 10)."
        ),
    )
    parser.add_argument(
        "--gtk", action="store_true",
        help=(
            "Only (re)install the GTK3 + WebKit2GTK desktop widget (with "
            "--uninstall, remove it). Linux only; works on GNOME, XFCE, "
            "Cinnamon, MATE, Budgie, and KDE. Needs python3-gi and "
            "gir1.2-webkit2-4.1 (or equivalent for your distro)."
        ),
    )
    parser.add_argument(
        "--codex", action="store_true",
        help=(
            "Only (re)mirror the skills into the Codex location "
            "(~/.agents/skills); with --uninstall, remove those mirrors. "
            "Useful after adding a skill."
        ),
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help=(
            "Remove everything install.py created (skills, CLI, widget). "
            "Combine with --skill-only / --codex / --plasmoid / --ubersicht "
            "to remove a single component."
        ),
    )
    parser.add_argument(
        "--restart-shell", action="store_true",
        help=(
            "After installing the Plasma widget, restart plasmashell so the "
            "updated widget loads immediately — but only inside a live Plasma "
            "6 session. The git hook passes this; harmless elsewhere."
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
# Step 1b: mirror the skills into the Codex location
#
# Same skills, a second consumer. Claude reads .claude/skills/, Codex reads
# ~/.agents/skills/, and neither reads the other's dir — so we mirror the one
# canonical SKILL.md per skill into the Codex location. See CODEX_SKILLS_DIR.

def _points_at(link: Path, src: Path) -> bool:
    """True when *link* already resolves to the skill at *src* (symlink,
    junction, or copy)."""
    if not link.exists() and not link.is_symlink():
        return False
    if link.is_symlink():
        try:
            return link.resolve(strict=False) == src.resolve(strict=False)
        except OSError:
            return False
    # A non-symlink dir — a junction or a copy. Compare SKILL.md identity.
    here, there = src / "SKILL.md", link / "SKILL.md"
    return there.is_file() and here.is_file() and (
        here.stat().st_size == there.stat().st_size
    )


def _mirror_skill(src: Path, dest: Path, force_copy: bool) -> str:
    """Make *dest* resolve to the skill at *src*. Returns a status word
    ('current' / 'linked' / 'copied' / 'skipped'). Never raises — a failure is
    reported and downgraded so one bad skill can't abort the rest."""
    if _points_at(dest, src):
        return "current"
    if dest.exists() or dest.is_symlink():
        # Something foreign already lives there — never clobber it.
        print(f"  Codex: {dest.name} exists and points elsewhere — leaving it.",
              file=sys.stderr)
        return "skipped"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not force_copy:
        try:
            dest.symlink_to(src, target_is_directory=True)
            return "linked"
        except OSError:
            pass  # fall through (e.g. Windows without symlink privileges)
        if sys.platform == "win32":  # pragma: no cover — Windows-only
            try:
                import _winapi  # type: ignore[import-not-found]
                _winapi.CreateJunction(str(src), str(dest))
                return "linked"
            except (ImportError, OSError):
                pass
    try:
        shutil.copytree(
            src, dest,
            ignore=shutil.ignore_patterns(
                ".git", ".github", "__pycache__", ".pytest_cache",
                "out", "*.pyc", "*.report.html", "stress-profile*.html",
            ),
        )
        return "copied"
    except OSError as e:
        print(f"  Codex: couldn't mirror {dest.name} ({e}).", file=sys.stderr)
        return "skipped"


def _skill_sources() -> list[tuple[Path, Path]]:
    """(src, dest) pairs to mirror into the Codex skills dir: the whole repo as
    the chat skill, plus every .claude/skills/<name> management skill."""
    pairs = [(REPO_DIR, CODEX_CHAT_TARGET)]
    skills_dir = REPO_DIR / ".claude" / "skills"
    if skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            if (d / "SKILL.md").is_file():
                pairs.append((d, CODEX_SKILLS_DIR / d.name))
    return pairs


def install_codex_skills(force_copy: bool) -> int:
    """Mirror every skill into ~/.agents/skills/ so Codex discovers the same
    skills as Claude Code, from the one canonical SKILL.md per skill.
    Idempotent; never fatal."""
    counts: dict[str, int] = {}
    for src, dest in _skill_sources():
        status = _mirror_skill(src, dest, force_copy)
        counts[status] = counts.get(status, 0) + 1
    done = counts.get("linked", 0) + counts.get("copied", 0)
    parts = []
    if done:
        parts.append(f"{done} mirrored")
    if counts.get("current"):
        parts.append(f"{counts['current']} already current")
    if counts.get("skipped"):
        parts.append(f"{counts['skipped']} skipped")
    print(f"Codex skills: {', '.join(parts) or 'nothing to do'} "
          f"in {CODEX_SKILLS_DIR}.")
    if done:
        print("  Restart Codex to pick up the skills.")
    return 0


def uninstall_codex_skills() -> int:
    """Remove only the skill mirrors we created under ~/.agents/skills/."""
    removed = 0
    for src, dest in _skill_sources():
        if not _points_at(dest, src):
            continue
        try:
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            else:
                shutil.rmtree(dest)
            removed += 1
        except OSError as e:
            print(f"  Codex: couldn't remove {dest.name} ({e}).",
                  file=sys.stderr)
    print(f"Codex skills: removed {removed} mirror(s) from {CODEX_SKILLS_DIR}.")
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
            "CLI: no `uv` or `pipx` found; writing a launcher script instead.\n"
            "  Install uv for an editable install that tracks `git pull`:\n"
            "    https://docs.astral.sh/uv/getting-started/installation/",
        )
    return _write_launcher()


def _add_to_user_path_windows(directory: Path) -> None:  # pragma: no cover — Windows-only
    """Add *directory* to the current user's persistent PATH via the registry.
    Broadcasts WM_SETTINGCHANGE so new shells pick it up without a reboot.
    Falls back to printing the manual instruction on any error."""
    import winreg
    launcher_dir = str(directory)
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS,
        )
        try:
            current, reg_type = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            current, reg_type = "", winreg.REG_EXPAND_SZ
        dirs_lower = [p.lower() for p in current.split(os.pathsep) if p]
        if launcher_dir.lower() not in dirs_lower:
            new_path = (current.rstrip(os.pathsep) + os.pathsep + launcher_dir
                        if current else launcher_dir)
            winreg.SetValueEx(key, "PATH", 0, reg_type, new_path)
            print(f"CLI: added {launcher_dir} to user PATH "
                  "(new terminals will find `aicogstress`).")
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 2, 5000, None,
            )
        else:
            print(f"CLI: {launcher_dir} already on user PATH.")
        winreg.CloseKey(key)
    except Exception as exc:
        print(
            f"  NOTE: could not update PATH automatically ({exc}).\n"
            f"  Add manually in PowerShell (user-level, permanent):\n"
            f'    [Environment]::SetEnvironmentVariable("PATH",'
            f' $Env:PATH + ";{launcher_dir}", "User")',
        )


def _write_launcher() -> int:
    LAUNCHER_DEST.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        LAUNCHER_DEST.write_text(
            f"@echo off\n"
            f"REM {LAUNCHER_MARK} (generated by install.py)\n"
            f"set PYTHONPATH={REPO_DIR}\n"
            f'"{sys.executable}" -m ai_code_cognitive_stress %*\n',
            encoding="utf-8",
        )
        print(f"CLI: wrote launcher {LAUNCHER_DEST} (runs this checkout).")
        _add_to_user_path_windows(LAUNCHER_DEST.parent)
    else:
        LAUNCHER_DEST.write_text(
            "#!/bin/sh\n"
            f"# {LAUNCHER_MARK} (generated by install.py)\n"
            f'PYTHONPATH="{REPO_DIR}" exec "{sys.executable}" -m ai_code_cognitive_stress "$@"\n',
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
        "  not inherit your shell PATH).\n"
        "  The installed widget is a copy — re-run `python install.py --plasmoid`\n"
        "  after `git pull` to update it (or wire that into a git post-merge hook)."
    )


def _remove_legacy_plasmoids() -> None:
    """Drop installs under the pre-rename package id, so the renamed widget
    doesn't end up installed twice."""
    tool = shutil.which("kpackagetool6")
    for legacy in LEGACY_PLASMOID_IDS:
        legacy_dest = PLASMOIDS_DIR / legacy
        removed = False
        # Unlink a symlinked legacy install ourselves first — never let
        # kpackagetool6 dereference it (it would delete the symlink's target).
        # Only a genuine copy install is safe to unregister with the tool.
        contents = legacy_dest / "contents"
        is_live = legacy_dest.is_symlink() or contents.is_symlink()
        if not is_live and tool:
            res = subprocess.run(
                [tool, "--type", "Plasma/Applet", "--remove", legacy],
                capture_output=True, text=True,
            )
            removed = res.returncode == 0
        if legacy_dest.is_symlink() or legacy_dest.is_file():
            legacy_dest.unlink()
            removed = True
        elif legacy_dest.exists():
            shutil.rmtree(legacy_dest)  # unlinks inner contents/ symlink, safe
            removed = True
        if removed:
            print(
                f"Plasma widget: removed the old package id ({legacy}).\n"
                "  The widget id changed — if it was already on your "
                "desktop/panel, remove the\n"
                "  dead instance and re-add \"Cognitive Stress\" after "
                "restarting Plasma."
            )


def install_plasmoid(required: bool = True, restart_shell: bool = False) -> int:
    """Install the Plasma 6 widget as a real copy in the plasmoids dir. Best-
    effort and additive: a failure here never blocks the skill.

    The install is a copy we own, not a symlink into the repo. That's
    deliberate: a whole-package symlink would update live on `git pull`, but
    `kpackagetool6 --remove` and Plasma's GUI "Uninstall" dereference it and
    delete the target — the repo source. A copy is safe to remove by any of
    those paths. The trade-off is that updates flow only through install.py:
    re-run `python install.py --plasmoid` after a pull (a git post-merge hook
    automates it). See _copy_plasmoid / uninstall_plasmoid.

    `restart_shell=True` (what the git hook passes) reloads plasmashell after
    the copy so the new QML shows at once — but only inside a live Plasma 6
    session, so it's a no-op over SSH or on a non-Plasma desktop. A plain
    manual install prints the restart step instead of forcing it.

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

    rc = _copy_plasmoid()
    if rc == 0:
        _install_git_hook("--plasmoid")
        if restart_shell:
            if _plasma6_session_running():
                _restart_plasmashell()
        else:
            _plasmoid_postinstall_hint()
    return rc


def _copy_plasmoid() -> int:
    """Install a real copy via kpackagetool6 (a registered package), falling
    back to a plain directory copy. Either way the install is a copy we own, so
    removing it — by install.py, kpackagetool6, or Plasma's GUI — never touches
    the repo source."""
    PLASMOID_DEST.parent.mkdir(parents=True, exist_ok=True)

    # Clear any prior *symlinked* install before invoking kpackagetool6:
    # --install/--upgrade would dereference it and delete the repo source.
    # unlink() drops a whole-package symlink's pointer; rmtree unlinks an inner
    # contents/ symlink (the old broken layout) rather than following it. A
    # real copy is left in place — kpackagetool6 --upgrade replaces it safely.
    if PLASMOID_DEST.is_symlink() or PLASMOID_DEST.is_file():
        PLASMOID_DEST.unlink()
    elif PLASMOID_DEST.is_dir() and (PLASMOID_DEST / "contents").is_symlink():
        shutil.rmtree(PLASMOID_DEST)

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
                return 0
        stderr = (res.stderr or "").strip() if res else ""
        print(
            f"Plasma widget: kpackagetool6 failed ({stderr}); "
            "falling back to a copy.",
            file=sys.stderr,
        )

    # Manual copy fallback (no kpackagetool6): replace any existing real copy.
    if PLASMOID_DEST.exists():
        shutil.rmtree(PLASMOID_DEST)
    shutil.copytree(PLASMOID_SRC, PLASMOID_DEST)
    print(f"Plasma widget: installed (copy) {PLASMOID_DEST} <- {PLASMOID_SRC}.")
    return 0


def _git_hooks_dir() -> Path | None:
    """The repo's hooks dir, or None when REPO_DIR isn't a git checkout (e.g. a
    `--copy` skill install) or git isn't available."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=REPO_DIR, capture_output=True, text=True,
        )
    except OSError:
        return None
    if res.returncode != 0:
        return None
    hooks = Path(res.stdout.strip())
    return hooks if hooks.is_absolute() else REPO_DIR / hooks


def _install_git_hook(flag: str) -> None:
    """Drop a post-merge hook that re-runs `install.py <flag> --restart-shell`,
    so the installed widget copy tracks the repo after a `git pull`/merge (the
    only events we care about). Idempotent and quiet: a hook already matching is
    left as-is with no output; a hook we didn't write is never clobbered, just
    flagged with the one line to add by hand."""
    hooks = _git_hooks_dir()
    if hooks is None:
        return
    # Clean up the post-checkout hook older versions installed — we only refresh
    # on merge/pull now, not on every branch switch.
    _remove_hook_file(hooks / "post-checkout")

    hook = hooks / "post-merge"
    install_py = (REPO_DIR / "install.py").as_posix()
    body = (
        f"#!/bin/sh\n# {HOOK_MARK} (managed by install.py)\n"
        f'exec "{sys.executable}" "{install_py}" {flag} --restart-shell\n'
    )
    if hook.exists():
        try:
            current = hook.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if HOOK_MARK not in current:
            print(
                f"  Git hook: {hook} already exists and isn't ours — leaving it.\n"
                "    To auto-refresh the widget on pull, add this line to it:\n"
                f'      "{sys.executable}" "{install_py}" {flag} --restart-shell'
            )
            return
        if current == body:
            return  # already correct — no churn, no noise on every pull
    hooks.mkdir(parents=True, exist_ok=True)
    hook.write_text(body, encoding="utf-8")
    hook.chmod(0o755)
    print("  Git hook: post-merge will refresh the widget after `git pull`/merge.")


def _remove_hook_file(hook: Path) -> None:
    """Delete a hook only if we wrote it (leave the user's own hooks alone)."""
    if not hook.is_file():
        return
    try:
        ours = HOOK_MARK in hook.read_text(encoding="utf-8")
    except OSError:
        ours = False
    if ours:
        hook.unlink()
        print(f"  Git hook: removed {hook}.")


def _remove_git_hook() -> None:
    """Remove the hooks we wrote (post-merge, plus a post-checkout from older
    versions); leave any others."""
    hooks = _git_hooks_dir()
    if hooks is None:
        return
    _remove_hook_file(hooks / "post-merge")
    _remove_hook_file(hooks / "post-checkout")


def _plasma6_session_running() -> bool:
    """True only inside a live Plasma 6 graphical session — so an auto-restart
    reloads the real shell and never spawns one over SSH or on a non-Plasma
    desktop."""
    if "kde" not in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return False
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    if not shutil.which("plasmashell"):
        return False
    try:
        if subprocess.run(["pgrep", "-x", "plasmashell"],
                          capture_output=True).returncode != 0:
            return False
        res = subprocess.run(["plasmashell", "--version"],
                             capture_output=True, text=True)
    except OSError:
        return False
    m = re.search(r"\b(\d+)\.", res.stdout)
    return bool(m and m.group(1) == "6")


def _restart_plasmashell() -> None:
    """Reload the running shell so it picks up the updated widget QML. Prefer
    the systemd user unit when it's the one running (clean restart, no double
    shell); otherwise quit and relaunch by hand."""
    sysctl = shutil.which("systemctl")
    if sysctl and subprocess.run(
        [sysctl, "--user", "--quiet", "is-active", "plasma-plasmashell.service"],
        capture_output=True,
    ).returncode == 0:
        subprocess.run([sysctl, "--user", "restart", "plasma-plasmashell.service"],
                       capture_output=True)
        print("  Reloaded Plasma to show the updated widget.")
        return
    subprocess.run(["kquitapp6", "plasmashell"], capture_output=True)
    kstart = shutil.which("kstart") or shutil.which("kstart6")
    if kstart:
        subprocess.Popen(
            [kstart, "plasmashell"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    print("  Reloaded Plasma to show the updated widget.")


def uninstall_plasmoid() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    _remove_legacy_plasmoids()
    _remove_git_hook()

    if not PLASMOID_DEST.exists() and not PLASMOID_DEST.is_symlink():
        print("Plasma widget: nothing to remove.")
        return 0

    # NEVER hand a symlinked layout to `kpackagetool6 --remove`: it
    # dereferences the symlink and recursively deletes the target — for our
    # live install that target is the repo source. Only a genuine copy install
    # (an older install.py) is safe to unregister with the tool.
    contents = PLASMOID_DEST / "contents"
    is_live = PLASMOID_DEST.is_symlink() or contents.is_symlink()
    tool = shutil.which("kpackagetool6")
    if not is_live and tool:
        res = subprocess.run(
            [tool, "--type", "Plasma/Applet", "--remove", PLASMOID_ID],
            capture_output=True, text=True,
        )
        if res.returncode == 0 and not PLASMOID_DEST.exists():
            print(f"Plasma widget: removed via kpackagetool6 ({PLASMOID_ID}).")
            return 0

    # shutil.rmtree unlinks the contents/ symlink rather than following it, and
    # unlink() drops a whole-package symlink without touching its target — so
    # the repo source is safe.
    if PLASMOID_DEST.is_symlink() or PLASMOID_DEST.is_file():
        PLASMOID_DEST.unlink()
    else:
        shutil.rmtree(PLASMOID_DEST)
    print(f"Plasma widget: removed {PLASMOID_DEST}.")
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
# Step 3c: GTK3 + WebKit2GTK widget (Linux, DE-agnostic)

def _webkit2gtk_present() -> bool:
    """Quick probe: can Python import gi and WebKit2 4.1 (or 4.0)?"""
    for ver in ("4.1", "4.0"):
        try:
            res = subprocess.run(
                [
                    sys.executable, "-c",
                    f"import gi; gi.require_version('WebKit2', '{ver}'); "
                    "from gi.repository import WebKit2",
                ],
                capture_output=True,
            )
            if res.returncode == 0:
                return True
        except OSError:
            pass
    return False


def _webkit2gtk_hint() -> str:
    return (
        "  Debian/Ubuntu: sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi\n"
        "  Fedora:        sudo dnf install webkit2gtk4.1 python3-gobject gtk3\n"
        "  Arch:          sudo pacman -S webkit2gtk python-gobject gtk3"
    )


def install_gtk_widget(required: bool = True) -> int:
    """Install the GTK3 + WebKit2GTK widget. Best-effort and additive: a
    failure here never blocks the skill or CLI install.

    Copies desktop/gtk/ → GTK_DEST, writes the XDG autostart .desktop, and
    installs a git post-merge hook so the copy stays in sync after pulls.

    `required=False` is the full-install path: a non-Linux OS or a KDE desktop
    downgrades to an informational skip instead of an error."""
    if not sys.platform.startswith("linux"):
        print("GTK widget: skipped (Linux only).")
        return 0
    if not GTK_SRC.is_dir():
        print(f"GTK widget: source not found at {GTK_SRC}", file=sys.stderr)
        return 1

    if not _webkit2gtk_present():
        print(
            "GTK widget: WebKit2GTK Python bindings look missing — the widget\n"
            "  will not start without them. Install them, then re-run:\n"
            + _webkit2gtk_hint()
        )
        if required:
            return 1

    # Copy the widget directory (replace any existing install).
    GTK_DEST.parent.mkdir(parents=True, exist_ok=True)
    if GTK_DEST.exists():
        shutil.rmtree(GTK_DEST)
    shutil.copytree(
        GTK_SRC, GTK_DEST,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(f"GTK widget: installed {GTK_DEST} <- {GTK_SRC}")

    # Write the resolved autostart .desktop.
    host_script = GTK_DEST / "cognitive-stress.py"
    desktop_content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Cognitive Stress\n"
        "Comment=AI code cognitive stress monitor — live desktop widget\n"
        f"Exec={sys.executable} {host_script}\n"
        "Icon=utilities-system-monitor\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=5\n"
    )
    GTK_AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    GTK_AUTOSTART.write_text(desktop_content, encoding="utf-8")
    print(f"GTK widget: autostart entry written to {GTK_AUTOSTART}")

    _install_git_hook("--gtk")

    print(
        "  Next:\n"
        f"    • To start it now:  python3 {host_script}\n"
        "    • It will start automatically on next login.\n"
        "    • The widget runs `aicogstress --emit-html-card` on a timer; make sure that\n"
        "      command is on PATH, or set AICOGSTRESS_CMD to the full path.\n"
        "    • To disable autostart: rm ~/.config/autostart/cognitive-stress-aicogstress.desktop\n"
        "  The installed widget is a copy — re-run `python install.py --gtk`\n"
        "  after `git pull` to update it (or wire that into a git post-merge hook)."
    )
    return 0


def uninstall_gtk_widget() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    removed = False

    if GTK_AUTOSTART.is_file():
        GTK_AUTOSTART.unlink()
        print(f"GTK widget: removed autostart entry {GTK_AUTOSTART}.")
        removed = True

    if GTK_DEST.exists():
        shutil.rmtree(GTK_DEST)
        print(f"GTK widget: removed {GTK_DEST}.")
        removed = True

    if not removed:
        print("GTK widget: nothing to remove.")
    return 0


# ---------------------------------------------------------------------------
# Step 3d: Windows WebView2 widget

def _fetch_webview2_dlls(widget_dest: Path) -> None:  # pragma: no cover — Windows-only
    """Best-effort: run fetch-webview2.ps1 if powershell is available.
    Never fatal — if it fails, install continues and prints a clear hint."""
    fetch_script = widget_dest / "fetch-webview2.ps1"
    if not fetch_script.is_file():
        return
    ps = shutil.which("powershell")
    if not ps:
        print(
            "  NOTE: `powershell` not on PATH — skipping DLL fetch.\n"
            f"  Run manually after install:\n"
            f"    powershell -ExecutionPolicy Bypass -File \"{fetch_script}\"",
        )
        return
    print("  Fetching WebView2 DLLs from NuGet (one-time network download)...")
    try:
        res = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(fetch_script)],
            capture_output=False,  # show progress to user
        )
        if res.returncode != 0:
            print(
                f"  WARNING: fetch-webview2.ps1 exited {res.returncode}.\n"
                f"  Run it manually if the widget shows a missing-DLL error:\n"
                f"    powershell -ExecutionPolicy Bypass -File \"{fetch_script}\"",
            )
    except OSError as exc:
        print(
            f"  WARNING: could not run fetch-webview2.ps1 ({exc}).\n"
            f"  Run it manually:\n"
            f"    powershell -ExecutionPolicy Bypass -File \"{fetch_script}\"",
        )


def install_windows_widget(required: bool = True) -> int:  # pragma: no cover — Windows-only
    """Install the Windows WebView2 widget. Best-effort and additive: a failure
    here never blocks the skill or CLI install.

    Copies the desktop/windows/ host to %LOCALAPPDATA%\\Programs\\aicogstress\\widget\\,
    attempts to fetch the WebView2 DLLs (calls fetch-webview2.ps1 via powershell),
    and writes a VBScript launcher into the user Startup folder so the widget
    opens silently on login.

    `required=False` is the full-install path: a non-Windows OS downgrades to
    an informational skip instead of an error."""
    if sys.platform != "win32":
        print("Windows widget: skipped (Windows only).")
        return 0
    if not WINDOWS_WIDGET_SRC.is_dir():
        print(
            f"Windows widget: source not found at {WINDOWS_WIDGET_SRC}",
            file=sys.stderr,
        )
        return 1

    # Copy the host directory (replace any existing install).
    WINDOWS_WIDGET_DEST.parent.mkdir(parents=True, exist_ok=True)
    if WINDOWS_WIDGET_DEST.exists():
        shutil.rmtree(WINDOWS_WIDGET_DEST)
    shutil.copytree(
        WINDOWS_WIDGET_SRC, WINDOWS_WIDGET_DEST,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(f"Windows widget: installed {WINDOWS_WIDGET_DEST} <- {WINDOWS_WIDGET_SRC}")

    # Attempt to fetch the WebView2 DLLs (best-effort, never fatal).
    lib_dir = WINDOWS_WIDGET_DEST / "lib" / "net45"
    core_dll = lib_dir / "Microsoft.Web.WebView2.Core.dll"
    if not core_dll.is_file():
        _fetch_webview2_dlls(WINDOWS_WIDGET_DEST)
    if not core_dll.is_file():
        print(
            "  NOTE: WebView2 DLLs not yet in lib/ — run this to fetch them:\n"
            f"    powershell -ExecutionPolicy Bypass "
            f"-File \"{WINDOWS_WIDGET_DEST / 'fetch-webview2.ps1'}\"",
        )

    # Write the VBScript launcher into the Startup folder. WScript.Shell.Run
    # with window mode 0 = hidden, so there's no console flash on login.
    host = WINDOWS_WIDGET_DEST / "cognitive-stress.ps1"
    # Pass the CLI path explicitly so the widget works even before the user's
    # PATH is refreshed in an existing terminal session.
    cmd_path = shutil.which("aicogstress") or str(LAUNCHER_DEST)
    ps_cmd = (
        "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden "
        f'-File "{host}" -Command "{cmd_path}"'
    )
    # In a VBScript string literal every embedded `"` is written as `""`, with
    # the whole value wrapped in outer quotes. (The naive `"""x"""` form parses
    # as a closed string followed by a bare token — a syntax error.)
    vbs_arg = '"' + ps_cmd.replace('"', '""') + '"'
    vbs = (
        f"' {WINDOWS_LAUNCHER_MARK} (generated by install.py)\n"
        f'CreateObject("WScript.Shell").Run {vbs_arg}, 0, False\n'
    )
    WINDOWS_LAUNCHER_DEST.parent.mkdir(parents=True, exist_ok=True)
    WINDOWS_LAUNCHER_DEST.write_text(vbs, encoding="utf-8")
    print(f"Windows widget: Startup launcher written to {WINDOWS_LAUNCHER_DEST}")
    print(
        "  Next:\n"
        "    • The widget will start automatically on next login.\n"
        "    • To start it now without logging out, run:\n"
        f"        wscript \"{WINDOWS_LAUNCHER_DEST}\"\n"
        "    • Right-click the tray icon to quit or refresh.\n"
        "    • If the card stays blank, aicogstress may not be on your PATH.\n"
        "      Find it: (Get-Command aicogstress).Source\n"
        "      Then set AICOGSTRESS_CMD to the full path, or edit the launcher.\n"
        "  The installed widget is a copy — re-run `python install.py --windows`\n"
        "  after a pull to update it."
    )
    return 0


def uninstall_windows_widget() -> int:  # pragma: no cover — Windows-only
    if sys.platform != "win32":
        return 0
    removed = False

    # Remove the Startup launcher only if we wrote it.
    if WINDOWS_LAUNCHER_DEST.is_file():
        try:
            ours = WINDOWS_LAUNCHER_MARK in WINDOWS_LAUNCHER_DEST.read_text(encoding="utf-8")
        except OSError:
            ours = False
        if ours:
            WINDOWS_LAUNCHER_DEST.unlink()
            print(f"Windows widget: removed Startup launcher {WINDOWS_LAUNCHER_DEST}.")
            removed = True
        else:
            print(
                f"Windows widget: {WINDOWS_LAUNCHER_DEST} exists but wasn't written by "
                "install.py — leaving it.",
            )

    if WINDOWS_WIDGET_DEST.exists():
        shutil.rmtree(WINDOWS_WIDGET_DEST)
        print(f"Windows widget: removed {WINDOWS_WIDGET_DEST}.")
        removed = True

    if not removed:
        print("Windows widget: nothing to remove.")
    return 0


# ---------------------------------------------------------------------------
# Step 4: seed the editable runtime config, then first computation

def seed_runtime_config() -> int:
    """Create a user-editable ``config.json`` from the tracked
    ``config.default.json`` if one doesn't exist yet. ``config.json`` is
    gitignored, so editing it never dirties the repo; the tool falls back to
    the defaults file when it's absent, so this is purely for discoverability.
    Never fatal."""
    core = REPO_DIR / "ai_code_cognitive_stress" / "core"
    runtime, defaults = core / "config.json", core / "config.default.json"
    if runtime.exists():
        print(f"Config: keeping your existing {runtime.name} (edit it to tune behaviour).")
        return 0
    try:
        runtime.write_text(defaults.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        print(f"Config: couldn't seed config.json ({e}); the tool uses built-in defaults.")
        return 0
    print(f"Config: wrote {runtime} from defaults — edit it to tune behaviour (it's gitignored).")
    return 0


def first_computation() -> int:
    """Run the same code path the widget polls (`--emit-html-card`): ingests
    the session logs, fills the per-day cache, and computes today's card —
    so the widget and the first report open instantly. Never fatal: a
    failure just means the first real use computes instead."""
    cmd = [sys.executable, "-m", "ai_code_cognitive_stress", "--emit-html-card", "--source", "auto"]
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
    widget_only = args.plasmoid or args.ubersicht or args.windows or args.gtk
    full = not (args.skill_only or widget_only or args.codex)
    register_skills = full or args.skill_only

    if args.uninstall:
        rc = 0
        if args.skill_only or full:
            rc = uninstall(args.target) or rc
        if args.skill_only or args.codex or full:
            rc = uninstall_codex_skills() or rc
        if args.plasmoid or full:
            rc = uninstall_plasmoid() or rc
        if args.gtk or full:
            rc = uninstall_gtk_widget() or rc
        if args.ubersicht or full:
            rc = uninstall_ubersicht() or rc
        if args.windows or full:
            rc = uninstall_windows_widget() or rc
        if full:
            rc = uninstall_cli() or rc
        return rc

    rc = 0
    if register_skills:
        _step("Chat skill (Claude Code)")
        rc = install(args.target, force_copy=args.copy) or rc
    if register_skills or args.codex:
        _step("Skills (Codex)")
        rc = install_codex_skills(force_copy=args.copy) or rc
    if full:
        _step("aicogstress CLI")
        rc = install_cli() or rc
    if args.plasmoid or (full and sys.platform.startswith("linux") and _kde_available()):
        _step("Desktop widget (KDE Plasma 6)")
        rc = install_plasmoid(
            required=args.plasmoid, restart_shell=args.restart_shell,
        ) or rc
    if args.gtk or (full and sys.platform.startswith("linux") and not _kde_available()):
        _step("Desktop widget (GTK3 + WebKit2GTK)")
        rc = install_gtk_widget(required=args.gtk) or rc
    if args.ubersicht or (full and sys.platform == "darwin"):
        _step("Desktop widget (Übersicht)")
        rc = install_ubersicht(required=args.ubersicht) or rc
    if args.windows or (full and sys.platform == "win32"):
        _step("Desktop widget (Windows / WebView2)")
        rc = install_windows_widget(required=args.windows) or rc
    if full:
        _step("Runtime config")
        seed_runtime_config()
        _step("First computation")
        first_computation()
        today = datetime.date.today()
        print(
            "\nDone. Try it:\n"
            '  · ask your agent: "show me my stress profile"\n'
            f"  · or run: aicogstress --year {today.year} --open   "
            "(full year)\n"
            f"  ·         aicogstress --day {today.isoformat()} --open  "
            "(just today)"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
