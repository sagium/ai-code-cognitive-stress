import json
from pathlib import Path


REPO = Path(__file__).parents[1]


def _between_rules(text: str) -> str:
    after_first_rule = text.split("\n---\n", 1)[1]
    return after_first_rule.split("\n---", 1)[0].strip()


def test_codex_and_claude_session_welcomes_match() -> None:
    agents_welcome = _between_rules((REPO / "AGENTS.md").read_text())
    hook = json.loads((REPO / ".claude/hooks/session-start.json").read_text())
    hook_context = hook["hookSpecificOutput"]["additionalContext"]

    assert agents_welcome == _between_rules(hook_context)
