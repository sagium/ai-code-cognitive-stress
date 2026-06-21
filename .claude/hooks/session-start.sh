#!/usr/bin/env bash
# SessionStart hook (Unix/WSL): emit the pre-rendered welcome JSON verbatim.
# Mirrors session-start.cmd — print a constant file, no Python at runtime.
# Regenerate session-start.json via: python session-start.py > session-start.json
cat "$(dirname "$0")/session-start.json" 2>/dev/null || true
