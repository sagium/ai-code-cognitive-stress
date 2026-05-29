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
class CodlConfig:
    """Engagement weighting for the CODL axis.

    A session counts at full weight only while actively driven — within
    ``foreground_grace_minutes`` of one of your messages; otherwise it is
    "cooking" in the background and counts at ``background_weight`` (0..1).
    Research basis lives in config.json (Smith 2003; Masicampo & Baumeister
    2011): a monitored/pending task costs ~15-20% of active capacity, not
    zero and not full."""
    foreground_grace_minutes: int = 5
    background_weight: float = 0.25


@dataclass(frozen=True, slots=True)
class ClosureConfig:
    """Real closure-event ingestion for the Closure Deficit axis.

    ``repos`` is an explicit, opt-in list of local git repo paths to scan for
    commit/merge closure events. Empty by default: the tool never auto-walks
    the disk looking for repos (mirrors ``GitRepoClosureSource``'s "opt in
    with explicit repos" contract). When empty, the Closure Deficit falls
    back to the legacy concurrency-presence proxy."""
    repos: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    work_window: WorkWindow
    codl: CodlConfig = CodlConfig()
    closure: ClosureConfig = ClosureConfig()


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
    config = Config(
        work_window=WorkWindow(start=start, end=end),
        codl=_parse_codl(data.get("codl") or {}),
        closure=_parse_closure(data.get("closure") or {}),
    )
    _CONFIG_CACHE[key] = config
    return config


def _parse_closure(raw: dict) -> ClosureConfig:
    """Parse the closure block. ``repos`` must be a list of strings (paths);
    anything else is rejected. Missing/empty → no closure source (the
    default, falls back to the legacy proxy)."""
    repos = raw.get("repos", [])
    if repos in (None, ""):
        repos = []
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise ValueError(
            f"closure.repos must be a list of path strings, got {repos!r}"
        )
    return ClosureConfig(repos=tuple(repos))


def _parse_codl(raw: dict) -> CodlConfig:
    """Parse + validate the CODL engagement-weighting block, falling back to
    defaults for any missing key."""
    defaults = CodlConfig()
    grace = raw.get("foreground_grace_minutes", defaults.foreground_grace_minutes)
    weight = raw.get("background_weight", defaults.background_weight)
    if not isinstance(grace, (int, float)) or grace < 0:
        raise ValueError(
            f"codl.foreground_grace_minutes must be >= 0, got {grace!r}"
        )
    if not isinstance(weight, (int, float)) or not 0.0 <= weight <= 1.0:
        raise ValueError(
            f"codl.background_weight must be in [0, 1], got {weight!r}"
        )
    return CodlConfig(
        foreground_grace_minutes=int(grace),
        background_weight=float(weight),
    )
