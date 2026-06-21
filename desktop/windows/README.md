# Cognitive Stress — Windows desktop widget

A frameless, always-on-top desktop widget that shows your cognitive stress
card on Windows. It is a thin WebView2 host that runs `aicogstress
--emit-html-card` on a timer and injects the output into a WinForms window
— the same card rendered by the KDE Plasma and macOS Übersicht widgets, from
the same single renderer (`ai_code_cognitive_stress/output/widget_card.py`).

**Private — no network calls at runtime.** The host only shells out to your
local `aicogstress` CLI and injects its stdout. The card is self-contained
(inline CSS + SVG). Network use is limited to the one-time DLL fetch at
install time (`fetch-webview2.ps1`).

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows 10 or 11 | 64-bit recommended; ARM64 also supported |
| WebView2 Evergreen Runtime | Pre-installed on Windows 11; auto-deployed on Windows 10 via Windows Update. If missing, download from https://developer.microsoft.com/en-us/microsoft-edge/webview2/ |
| Windows PowerShell 5.1 | Ships with every Windows 10/11 installation — `powershell.exe`, not `pwsh` |
| `aicogstress` CLI on PATH | Installed by `python install.py` (step 2) |

## Quick install

Run from the repo root on any OS (the `--windows` path is skipped on
Linux/macOS, so it is safe to include in a cross-platform install):

```powershell
python install.py --windows
```

This:
1. Calls `fetch-webview2.ps1` to vendor the WebView2 managed DLLs into
   `desktop/windows/lib/` (a one-time network fetch from NuGet).
2. Copies the widget host to
   `%LOCALAPPDATA%\Programs\aicogstress\widget\`.
3. Writes a VBScript launcher into your Startup folder so the widget opens
   silently on login.

To install just the widget (without the full install):

```powershell
python install.py --windows
```

To uninstall:

```powershell
python install.py --windows --uninstall
```

## Fetching the WebView2 DLLs manually

If `install.py` can't run `fetch-webview2.ps1` automatically, run it yourself:

```powershell
cd desktop\windows
powershell -ExecutionPolicy Bypass -File fetch-webview2.ps1
```

This downloads the NuGet package `Microsoft.Web.WebView2` and extracts the
`net45` managed assemblies into `desktop/windows/lib/net45/`. The DLLs are
not committed to git; the fetch script is the source of truth for which
version is pinned.

## Running the widget

After install, the widget starts on login via the Startup launcher. To start
it immediately without logging out:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden `
  -File "%LOCALAPPDATA%\Programs\aicogstress\widget\cognitive-stress.ps1"
```

To quit, right-click the tray icon and choose **Quit**. Double-click the
tray icon to bring the widget to the front.

## PATH and the aicogstress command

A GUI process on Windows may not inherit your shell's PATH, so the widget
might fail to find `aicogstress` even if it works in your terminal. If the
widget shows an error about the command not being found:

1. Find the full path: in PowerShell, run `(Get-Command aicogstress).Source`
2. Pass it at launch: add `-Command "C:\path\to\aicogstress.exe"` to the
   PowerShell call in the Startup VBScript, or set the environment variable
   `AICOGSTRESS_CMD` to the full path.

## Refresh interval

Default: 60 seconds. Override via the environment variable
`AICOGSTRESS_REFRESH_SECONDS` (minimum 10), or pass `-RefreshSeconds <n>`
to the PowerShell script.

## Single renderer

The card is identical to what the KDE Plasma and macOS Übersicht widgets
show — all three hosts inject the output of
`ai_code_cognitive_stress/output/widget_card.py` verbatim. If you change the
card's appearance, you change it everywhere at once.

## Position

The widget remembers its position across restarts. Drag it anywhere on the
desktop. Position is saved to `%LOCALAPPDATA%\aicogstress\widget-pos.json`.
On first launch it appears in the top-right corner of your primary display.

## Files in this directory

| File | Purpose |
|---|---|
| `cognitive-stress.ps1` | The widget host (run this) |
| `fetch-webview2.ps1` | One-time DLL fetch from NuGet (called by install.py) |
| `lib/` | Vendored WebView2 DLLs (populated by `fetch-webview2.ps1`) |
| `README.md` | This file |
