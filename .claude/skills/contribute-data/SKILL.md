---
name: contribute-data
description: Help the user export their anonymized usage data and manually submit it to the calibration study. Use when the user asks to contribute data, help the research, share anonymized stats, or donate their usage patterns to the community dataset.
---

# contribute-data

Walks the user through generating a privacy-safe anonymized export and
submitting it manually to the calibration study. Every step requires the
user's explicit confirmation — never automate the upload.

## What this does (and doesn't do)

This skill generates an anonymized file and then opens a browser form where
the user submits it themselves. The user is in control of every step.

What the export contains:
- Derived daily metrics (CODL, Interruption Index, Closure Deficit, composite)
- Per-session activity counts (message/tool-call tallies and durations)
- Hourly activity-load shape and typical working-hour ranges

What it **never** contains:
- Code, file paths, repo names, branch names
- Usernames, hostnames, email addresses, or timezone
- Calendar dates (randomly shifted by a secret per-export offset)
- Any content from session transcripts

Why contribute: the tool's current thresholds are borrowed from related
research. Real usage data from the community will calibrate them properly —
and contributors get population baselines back in the tool, showing how their
own patterns compare to the community.

## Detecting the platform

Check whether `$env:OS` equals `Windows_NT` (PowerShell) or run `uname` (Bash).
Use **PowerShell** for Windows, **Bash** for macOS/Linux.

On Windows, `aicogstress` is not on PATH. Use its known install location:
`$env:LOCALAPPDATA\Programs\aicogstress\aicogstress.bat`

## Step 1 — surface the consent statement

Before generating anything, show the user the consent text.

**Windows (PowerShell):**
```powershell
& "$env:LOCALAPPDATA\Programs\aicogstress\aicogstress.bat" --export-research --year YYYY 2>&1 | Select-Object -First 30
```

**macOS/Linux (Bash):**
```bash
aicogstress --export-research --year YYYY 2>&1 | head -30
```

(This prints the consent statement then exits without `--i-consent`.)

Read the consent text aloud to the user and ask: **"Do you consent to
submitting this anonymized data?"** Do not proceed unless they say yes.

## Step 2 — generate the anonymized export

Once the user has consented, resolve the Desktop path and run the export.

**Windows (PowerShell):**
```powershell
$exportDir = if (Test-Path "$env:USERPROFILE\Desktop") { "$env:USERPROFILE\Desktop" } else { "$env:USERPROFILE" }
& "$env:LOCALAPPDATA\Programs\aicogstress\aicogstress.bat" --export-research --year YYYY --i-consent --output "$exportDir\aicogstress-research-YYYY.json"
```

**macOS/Linux (Bash):**
```bash
EXPORT_DIR=$([ -d "$HOME/Desktop" ] && echo "$HOME/Desktop" || echo "$HOME")
aicogstress --export-research --year YYYY --i-consent \
    --output "$EXPORT_DIR/aicogstress-research-YYYY.json"
```

Replace `YYYY` with the current year, or the year the user specifies.

Note: the tool may ignore `--output` and print the actual saved path in its
output (e.g. `research export: C:\...\stress-levels-research-YYYY.json`).
Always use the path the tool reports for subsequent steps.

Saving outside the repo avoids leaving an uncommitted file behind.

## Step 3 — offer to show the file

Before submitting, offer:

> "Would you like to inspect the file before uploading it?"

If yes:

**Windows (PowerShell):**
```powershell
Get-Content "<path from tool output>" | ConvertFrom-Json | ConvertTo-Json -Depth 10 | Select-Object -First 60
```

**macOS/Linux (Bash):**
```bash
cat "$EXPORT_DIR/aicogstress-research-YYYY.json" | python3 -m json.tool | head -60
```

Walk through what each field means if asked. The user should feel confident
about what they're submitting.

## Step 4 — open the submission form

Tell the user:

> "The file is ready. Please upload it at the form below — this is a manual
> step that only you can do. An anonymous submission cannot be withdrawn
> afterwards."

Open the form in their browser:

**Windows (PowerShell):**
```powershell
Start-Process "https://tally.so/r/EkMM4q"
```

**macOS/Linux (Bash):**
```bash
python3 -c "import webbrowser; webbrowser.open('https://tally.so/r/EkMM4q')"
```

Tell them to attach the export file (path reported by the tool) to the file
field in the form and submit.

## Step 5 — confirm and close

After the user says they've submitted, reply with a single short confirmation,
e.g.:

> "Thank you — your data is in. As more contributions come in, population
> baselines will flow back into the tool."

Do not ask follow-up questions or offer further analysis. The skill is done.

## Constraints

- **Never** automate the upload or add any network call to the tool itself.
- **Never** pass `--i-consent` without the user having explicitly agreed in
  this conversation.
- If the user declines consent, acknowledge that and close gracefully — do not
  push further.
- If the user asks for a different year, run the export for that year (or
  multiple years, one file per year).
