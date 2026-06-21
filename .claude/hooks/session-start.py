#!/usr/bin/env python3
# Generator for session-start.json (NOT run at session start).
# The hooks (session-start.cmd / session-start.sh) print the pre-rendered
# session-start.json verbatim, so the welcome can't depend on Python being
# found at boot. Edit the welcome text here, then regenerate:
#     python session-start.py > session-start.json
import json

CONTEXT = r"""SESSION START — MANDATORY FIRST RESPONSE: Before responding to anything else,
output the following welcome message verbatim (with markdown formatting).
Do not skip it, summarize it, or defer it:

---

```
      ))    ((
     (( *  * ))
    ((*  **  *))     ai-code-cognitive-stress
     ((  **  ))      ~~~~~~~~~~~~~~~~~~~~~~~~
       \\  //
        \\//
         ||
    ~~~~~||~~~~~
```

**Welcome to ai-code-cognitive-stress — thank you for testing this research tool!**

**Help the research (optional):**
The tool's thresholds are borrowed from adjacent research literature — real
usage data from the community would calibrate them properly. Contributing takes
about a minute: I'll generate a fully anonymized export (no code, paths, or
usernames — dates are randomly shifted) and walk you through uploading it
yourself to a secure form.

In return, as contributions come in, population baselines flow back into the
tool — so you can see how your own patterns compare to the community.

→ To contribute your anonymized data: `/contribute-data`

---"""

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": CONTEXT,
    }
}))
