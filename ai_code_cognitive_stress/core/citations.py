"""Load the citations registry and expose a lookup-by-key helper.

The registry is the single source of truth for every research citation
referenced in the rendered report. Per the project's design principle,
metric thresholds and weights must be traceable to entries in this file.

Parsing uses a small hand-rolled reader for the strict subset of YAML
in `citations.yml`:

    - sequence of mappings at the top level
    - each mapping has scalar fields (string / integer / null) and one
      nested list field (`supports`)
    - scalars may be bare or double-quoted; `null` and `~` decode to None
    - no anchors, aliases, multi-line scalars, or flow-style nesting

PyYAML is not pulled in because the schema is flat and validated on load;
the reader fails loudly on anything outside the supported subset.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class Citation:
    key: str
    authors: str
    year: int
    title: str
    venue: str
    doi: str | None
    supports: tuple[str, ...]


REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "key", "authors", "year", "title", "venue", "supports",
)

_DEFAULT_REGISTRY_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "citations.yml"
)

# Module-level cache keyed by resolved registry path. Cleared in tests via
# `_REGISTRY_CACHE.clear()`. Each cite() call hits the cache after first load;
# the alternative — re-parsing 17 entries on every reference in the report —
# is wasteful without adding complexity worth preserving.
#
# Cached values are MappingProxyType so callers cannot mutate the shared
# registry (a poisoned key would propagate across the whole process).
_REGISTRY_CACHE: dict[str, Mapping[str, Citation]] = {}


def load_registry(path: Path | None = None) -> Mapping[str, Citation]:
    """Parse citations.yml and return a read-only registry keyed by citation key.

    Raises ValueError on malformed YAML, missing required fields, type
    mismatches, empty `supports` lists, or duplicate keys.
    """
    resolved = (path or _DEFAULT_REGISTRY_PATH).resolve()
    cache_key = str(resolved)
    if cache_key not in _REGISTRY_CACHE:
        _REGISTRY_CACHE[cache_key] = _build_registry(resolved)
    return _REGISTRY_CACHE[cache_key]


def cite(key: str) -> Citation:
    """Look up a citation by key. Raises KeyError on miss so missing
    references fail the build rather than silently rendering bare numbers."""
    return load_registry()[key]


# ---------------------------------------------------------------------------

def _build_registry(path: Path) -> Mapping[str, Citation]:
    raw_entries = _parse_yaml(path)
    if not raw_entries:
        raise ValueError(f"{path.name}: empty registry")

    registry: dict[str, Citation] = {}
    for idx, entry in enumerate(raw_entries):
        _validate_entry(entry, idx, path)
        citation = Citation(
            key=entry["key"],
            authors=entry["authors"],
            year=entry["year"],
            title=entry["title"],
            venue=entry["venue"],
            doi=entry.get("doi"),
            supports=tuple(entry["supports"]),
        )
        if citation.key in registry:
            raise ValueError(
                f"{path.name}: duplicate citation key {citation.key!r}"
            )
        registry[citation.key] = citation
    return MappingProxyType(registry)


_FIELD_PATTERN = re.compile(r'^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$')


def _parse_yaml(path: Path) -> list[dict]:
    entries: list[dict] = []
    current: dict | None = None
    current_list_key: str | None = None

    text = path.read_text(encoding="utf-8")
    for line_num, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        stripped = line.lstrip()

        if not stripped or stripped.startswith("#"):
            continue

        # Top-level entry header: "- key: value"
        if line.startswith("- "):
            if current is not None:
                entries.append(current)
            current = {}
            current_list_key = None
            # Re-interpret "- key: value" as "  key: value" so the field
            # regex below catches it uniformly.
            line = "  " + line[2:]
            stripped = line.lstrip()

        # Nested list item: "    - <value>"
        if stripped.startswith("- "):
            if current is None or current_list_key is None:
                raise ValueError(
                    f"{path.name}:{line_num}: list item outside of a "
                    f"mapping's list field"
                )
            value = stripped[2:].strip()
            current[current_list_key].append(_parse_scalar(value, path, line_num))
            continue

        match = _FIELD_PATTERN.match(line)
        if not match:
            raise ValueError(
                f"{path.name}:{line_num}: cannot parse line: {raw_line!r}"
            )
        if current is None:
            raise ValueError(
                f"{path.name}:{line_num}: field outside of a list entry"
            )

        _, key, value = match.group(1), match.group(2), match.group(3)
        if key in current:
            raise ValueError(
                f"{path.name}:{line_num}: duplicate field {key!r} in entry"
            )
        if value == "":
            current[key] = []
            current_list_key = key
        else:
            current[key] = _parse_scalar(value, path, line_num)
            current_list_key = None

    if current is not None:
        entries.append(current)
    return entries


def _parse_scalar(s: str, path: Path, line_num: int) -> str | int | None:
    s = _strip_trailing_comment(s).strip()
    if s in ("null", "~"):
        return None
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1]
    # Catch the asymmetric case — a leading quote with no matching closer —
    # rather than silently treating it as a bare string.
    if s.startswith('"') or s.startswith("'"):
        raise ValueError(
            f"{path.name}:{line_num}: unterminated quoted string: {s!r}"
        )
    # Integer (signed). We don't decode floats — the schema has none.
    if re.fullmatch(r'-?\d+', s):
        return int(s)
    return s


def _strip_trailing_comment(s: str) -> str:
    """Strip a YAML-style ` # comment` tail when the `#` is unquoted.

    Follows YAML 1.2: a `#` only starts a comment if preceded by whitespace.
    Inline `#` inside quoted strings, and `#` with no preceding space (e.g.
    `10.1234/foo#anchor` in an unquoted scalar) are preserved.
    """
    in_double = False
    in_single = False
    for i, ch in enumerate(s):
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif (
            ch == '#'
            and not in_double
            and not in_single
            and i > 0
            and s[i - 1] in (' ', '\t')
        ):
            return s[:i].rstrip()
    return s


def _validate_entry(entry: dict, idx: int, path: Path) -> None:
    missing = [f for f in REQUIRED_FIELDS if f not in entry]
    if missing:
        key_label = entry.get("key", f"<entry #{idx}>")
        raise ValueError(
            f"{path.name}: entry {key_label!r} missing required fields: {missing}"
        )

    for field in ("key", "authors", "title", "venue"):
        value = entry[field]
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"{path.name}: entry {entry['key']!r}: {field!r} must be "
                f"a non-empty string"
            )

    if not isinstance(entry["year"], int):
        raise ValueError(
            f"{path.name}: entry {entry['key']!r}: year must be integer, "
            f"got {type(entry['year']).__name__}"
        )

    if not isinstance(entry["supports"], list) or not entry["supports"]:
        raise ValueError(
            f"{path.name}: entry {entry['key']!r}: supports must be a "
            f"non-empty list"
        )
    for item in entry["supports"]:
        if not isinstance(item, str) or not item:
            raise ValueError(
                f"{path.name}: entry {entry['key']!r}: every supports "
                f"item must be a non-empty string"
            )

    doi = entry.get("doi")
    if doi is not None and not isinstance(doi, str):
        raise ValueError(
            f"{path.name}: entry {entry['key']!r}: doi must be string or null"
        )
