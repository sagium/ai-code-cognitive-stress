"""Runtime configuration, loaded from ``config.json`` (shipped with defaults).

Keeps tunable values — currently the fixed working day — out of the code so
they can be changed by editing a data file rather than editing logic. Pure
stdlib (``json``), works on Python 3.10+.

The shipped ``config.json`` is the source of truth for defaults. A custom file
can be supplied via ``load_config(path=...)`` (used by tests, and available for
callers that want a per-user config).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Final

_DEFAULT_CONFIG_PATH: Final[Path] = Path(__file__).resolve().parent / "config.json"

# Cache by resolved path so build_profile doesn't re-read the file on every
# call. Cleared in tests via _CONFIG_CACHE.clear().
_CONFIG_CACHE: dict[str, "Config"] = {}


@dataclass(frozen=True, slots=True)
class WorkWindow:
    """The configured working day, in local time."""
    start: time
    end: time


@dataclass(frozen=True, slots=True)
class Config:
    work_window: WorkWindow


def _parse_hhmm(value: str) -> time:
    """Parse a "HH:MM" (or "HH:MM:SS") local-time string."""
    parts = [int(p) for p in value.split(":")]
    if not 2 <= len(parts) <= 3 or not (0 <= parts[0] <= 23) or not (0 <= parts[1] <= 59):
        raise ValueError(f"invalid time {value!r}; expected HH:MM")
    return time(*parts)


def load_config(path: Path | None = None) -> Config:
    """Load and validate the configuration. Cached per path."""
    p = (path or _DEFAULT_CONFIG_PATH).resolve()
    key = str(p)
    cached = _CONFIG_CACHE.get(key)
    if cached is not None:
        return cached

    data = json.loads(p.read_text(encoding="utf-8"))
    ww = data.get("work_window") or {}
    start = _parse_hhmm(ww["start"])
    end = _parse_hhmm(ww["end"])
    if end <= start:
        raise ValueError(
            f"work_window end ({end}) must be after start ({start})"
        )
    config = Config(work_window=WorkWindow(start=start, end=end))
    _CONFIG_CACHE[key] = config
    return config
