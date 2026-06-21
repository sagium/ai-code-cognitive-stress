@echo off
rem SessionStart hook (Windows): emit the pre-rendered welcome JSON verbatim.
rem No Python/PowerShell/PATH games, just print a constant file so this cannot
rem degrade into a "Python was not found" stub or an interactive cmd prompt.
rem %~dp0 resolves to this file's directory (with trailing backslash) at runtime.
rem Regenerate session-start.json via: python session-start.py ^> session-start.json
type "%~dp0session-start.json" 2>nul
exit /b 0
