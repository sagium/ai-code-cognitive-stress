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
    """Real closure + rework event ingestion (git log + reflog).

    ``autodiscover`` (default True) finds repos from the working directories
    the agent sessions recorded — walking each cwd up to its nearest ``.git``
    root. This is bounded to cwds the sessions already touched; it never walks
    the disk hunting for repos, and stays fully local/read-only.

    ``repos`` is an explicit list of local git repo paths, always scanned and
    unioned with any auto-discovered roots. When the union is empty (e.g.
    ``autodiscover`` off and no explicit repos), the Closure Deficit falls back
    to the legacy concurrency-presence proxy.

    ``identities`` is the operator's own commit-author identities (emails and/or
    names) used to scope closures: only commits/merges by these authors count as
    the operator closing a loop, so a shared monorepo's teammate and merge-bot
    commits don't spuriously close it. When empty (the default) the identities
    are auto-discovered from local git config (global + each scanned repo's
    ``user.email``/``user.name``), which already handles an operator who commits
    under multiple accounts. Set explicitly only to add identities no local git
    config records. Pushes are self-scoped by their reflog and ignore this."""
    repos: tuple[str, ...] = ()
    autodiscover: bool = True
    identities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """Composite-scoring scales and weights.

    Defaults are the v1 literature / null-hypothesis values (mirroring the
    constants in ``metrics.py``): each axis is mapped to [0,1] by dividing by a
    ceiling, then blended with equal weights. Population calibration
    (``calibrate.py``) can suggest data-fitted overrides that the operator opts
    into here. ``weights`` is (codl, interruption, closure) and need not sum to
    1 — the composite normalizes by their sum."""
    codl_ceiling: float = 5.0
    interruption_ceiling: float = 10.0
    weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)


@dataclass(frozen=True, slots=True)
class Config:
    work_window: WorkWindow | None = None
    codl: CodlConfig = CodlConfig()
    closure: ClosureConfig = ClosureConfig()
    scoring: ScoringConfig = ScoringConfig()


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
    raw_ww = data.get("work_window")
    if raw_ww:
        start = _parse_hhmm(raw_ww["start"])
        end = _parse_hhmm(raw_ww["end"])
        if end <= start:
            raise ValueError(
                f"work_window end ({end}) must be after start ({start})"
            )
        work_window: WorkWindow | None = WorkWindow(start=start, end=end)
    else:
        work_window = None
    config = Config(
        work_window=work_window,
        codl=_parse_codl(data.get("codl") or {}),
        closure=_parse_closure(data.get("closure") or {}),
        scoring=_parse_scoring(data.get("scoring") or {}),
    )
    _CONFIG_CACHE[key] = config
    return config


def _parse_scoring(raw: dict) -> ScoringConfig:
    """Parse + validate the composite-scoring block, falling back to the
    literature defaults for any missing key. Ceilings must be > 0; weights must
    be three non-negative numbers that don't all sum to zero."""
    d = ScoringConfig()
    codl_c = raw.get("codl_ceiling", d.codl_ceiling)
    int_c = raw.get("interruption_ceiling", d.interruption_ceiling)
    weights = raw.get("weights", list(d.weights))
    for name, c in (("codl_ceiling", codl_c), ("interruption_ceiling", int_c)):
        if not isinstance(c, (int, float)) or c <= 0:
            raise ValueError(f"scoring.{name} must be > 0, got {c!r}")
    if (not isinstance(weights, (list, tuple)) or len(weights) != 3
            or not all(isinstance(w, (int, float)) and w >= 0 for w in weights)):
        raise ValueError(
            f"scoring.weights must be three non-negative numbers, got {weights!r}"
        )
    if sum(weights) <= 0:
        raise ValueError("scoring.weights must not sum to zero")
    return ScoringConfig(
        codl_ceiling=float(codl_c),
        interruption_ceiling=float(int_c),
        weights=(float(weights[0]), float(weights[1]), float(weights[2])),
    )


def _parse_closure(raw: dict) -> ClosureConfig:
    """Parse the closure block. ``repos`` must be a list of strings (paths);
    ``autodiscover`` must be a bool (default True). Missing/empty repos with
    autodiscover off → no closure source (falls back to the legacy proxy)."""
    repos = raw.get("repos", [])
    if repos in (None, ""):
        repos = []
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise ValueError(
            f"closure.repos must be a list of path strings, got {repos!r}"
        )
    autodiscover = raw.get("autodiscover", True)
    if not isinstance(autodiscover, bool):
        raise ValueError(
            f"closure.autodiscover must be a boolean, got {autodiscover!r}"
        )
    identities = raw.get("identities", [])
    if identities in (None, ""):
        identities = []
    if not isinstance(identities, list) or not all(
        isinstance(i, str) for i in identities
    ):
        raise ValueError(
            f"closure.identities must be a list of strings, got {identities!r}"
        )
    return ClosureConfig(
        repos=tuple(repos),
        autodiscover=autodiscover,
        identities=tuple(identities),
    )


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
