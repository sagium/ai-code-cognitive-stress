"""Minimal Markdown → HTML converter, stdlib-only.

Supports the subset the Claude skill is likely to emit when writing its
day-of analysis: ATX headings (`#`..`######`), unordered lists (`-` or `*`),
paragraphs, blank-line separators, inline `**bold**`, `*italic*`,
`` `code` ``, and `[text](url)` links.

Does NOT support: ordered lists, blockquotes, code fences, tables, images,
nested lists, HTML pass-through. If the agent needs richer formatting it
can pre-render to HTML and pass that in directly (the CLI accepts either).

Output is HTML-safe: source text is escaped first via `html.escape`, then
markdown patterns are applied to the escaped string. This is robust against
accidental injection from agent output but is not a security guarantee —
treat agent output as semi-trusted, never as adversarial input.
"""

from __future__ import annotations

import html
import re

_HEADER_RE = re.compile(r'^(#{1,6})\s+(.+)$')
_LIST_RE = re.compile(r'^[-*]\s+(.+)$')
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
_ITALIC_RE = re.compile(r'(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)')
_CODE_RE = re.compile(r'`([^`\n]+)`')
_LINK_RE = re.compile(r'\[([^\]\n]+)\]\(([^)\n]+)\)')


def to_html(md: str) -> str:
    """Convert a markdown string to an HTML fragment."""
    lines = md.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + " ".join(paragraph) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        if not line:
            flush_paragraph()
            close_list()
            continue

        m = _HEADER_RE.match(line)
        if m:
            flush_paragraph()
            close_list()
            # Bump h1 → h2 so it doesn't fight with the page-level h1.
            level = max(2, min(len(m.group(1)) + 1, 6))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue

        m = _LIST_RE.match(line)
        if m:
            flush_paragraph()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        close_list()
        paragraph.append(_inline(line))

    flush_paragraph()
    close_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Apply HTML escaping then inline markdown patterns."""
    s = html.escape(text)
    s = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", s)
    s = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", s)
    s = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", s)
    s = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s
    )
    return s
