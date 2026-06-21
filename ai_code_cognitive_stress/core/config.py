"""Runtime configuration, loaded from ``config.json`` (shipped with defaults).

Keeps tunable values — currently the fixed working day — out of the code so
they can be changed by editing a data file rather than editing logic. Pure
stdlib (``json``), works on Python 3.10+.

``config.default.json`` (tracked) is the source of truth for defaults — it
documents every knob and is what ships in the package. The live, user-editable
config is ``config.json`` (gitignored): edit it freely without dirtying the
repo. ``load_config`` reads ``config.json`` when present and falls back to
``config.default.json`` otherwise, so a fresh clone (or an installed wheel with
no user config) still works. A custom file can be supplied via
``load_config(path=...)`` (used by tests).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Final

_CONFIG_DIR: Final[Path] = Path(__file__).resolve().parent
# Tracked, documented defaults — the source of truth shipped in the package.
_DEFAULTS_CONFIG_PATH: Final[Path] = _CONFIG_DIR / "config.default.json"
# Live, user-editable config (gitignored). Falls back to the defaults above
# when absent (fresh clone / installed wheel with no user override).
_RUNTIME_CONFIG_PATH: Final[Path] = _CONFIG_DIR / "config.json"


def _resolve_config_path(path: Path | None) -> Path:
    if path is not None:
        return path.resolve()
    if _RUNTIME_CONFIG_PATH.exists():
        return _RUNTIME_CONFIG_PATH.resolve()
    return _DEFAULTS_CONFIG_PATH.resolve()

# Default for the shared idle-close cutoff (minutes). Mirrors the code-level
# fallback in metrics.py (IDLE_CLOSE_MINUTES_DEFAULT); the two layers carry their
# own defaults exactly as the CODL grace/weight do.
DEFAULT_IDLE_CLOSE_MINUTES: Final[int] = 180

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
    background_weight: float = 0.20


@dataclass(frozen=True, slots=True)
class ResumptionConfig:
    """Scoring parameters for the Closure Deficit (resumption load).

    A resume is a true-idle gap in a session of at least ``threshold_minutes``
    (or a cross-day pickup); its severity is ``min(1, gap / full_decay_minutes)``;
    the day's axis is ``min(1, Σ severity / daily_ceiling)``. All three are
    citation-anchored modeling priors (Monk et al. 2008 duration→cost; loose
    Cowan 2001 anchor for the ceiling), calibratable but NOT fitted to felt load.
    Lowering ``threshold_minutes`` below the aggregate store floor (2 min) has no
    effect without a cache rebuild."""
    threshold_minutes: int = 30
    full_decay_minutes: int = 120
    daily_ceiling: float = 4.0


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """Composite-scoring scales and weights.

    Defaults are the v1 literature / calibration-prior values (mirroring the
    constants in ``metrics.py``). The CODL axis uses a graded capacity-dose:
    phi(t) = min(1, C(t) / codl_capacity) per minute, summed into a raw_dose
    (capacity-equivalent minutes), then normalised by codl_dose_horizon_minutes
    to [0,1]. ``codl_capacity`` is the Cowan (2001) working-memory limit (an
    instantaneous saturation anchor, not a time-average ceiling). The horizon is
    a calibration target fitted to observed data. Population calibration
    (``calibrate.py``) can suggest a data-fitted horizon override that the
    operator opts into here. ``weights`` is (codl, interruption, closure) and
    need not sum to 1 — the composite normalizes by their sum."""
    codl_capacity: float = 4.0
    codl_dose_horizon_minutes: float = 240.0
    interruption_ceiling: float = 10.0
    weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)


@dataclass(frozen=True, slots=True)
class Config:
    work_window: WorkWindow | None = None
    codl: CodlConfig = CodlConfig()
    resumption: ResumptionConfig = ResumptionConfig()
    scoring: ScoringConfig = ScoringConfig()
    # Silence longer than this inside a session is taken as the coding app
    # having been closed (no live process) and reopened later: the gap is dead
    # time, not a live session. One knob shared by both axes that read session
    # liveness — the CODL concurrency graph (a cut gap stops counting as an open
    # session) and the Closure Deficit (a gap this long means you got closure, so
    # it is no longer an unfinished-loop resume). Shorter idle gaps stay inside a
    # single live run (the app was open, just idle — a process would still be
    # alive). Should comfortably exceed ``resumption.threshold_minutes`` so the
    # resume band [threshold, idle_close) is non-empty.
    idle_close_minutes: int = DEFAULT_IDLE_CLOSE_MINUTES
    # Locale for all rendered text (report + widget card); a catalog file
    # source/core/locales/<locale>.json. Missing keys fall back to English.
    locale: str = "en"
    # Compact widget card: when true, the desktop widget card drops the three
    # per-axis tiles and shows only the composite headline and the concurrency
    # chart — a smaller card for a tight panel. Affects only the rendered widget
    # card (--emit-html-card); the HTML report is unchanged.
    compact_widget: bool = False
    # Active timeframe tab in the widget card: one of "today", "week", "month",
    # "year". Persisted by the desktop widget's tab buttons via --set-view; the
    # next --emit-html-card render opens to that tab. Affects only the rendered
    # widget card; the HTML report is unchanged.
    widget_view: str = "today"


def _parse_hhmm(value: str) -> time:
    """Parse a "HH:MM" (or "HH:MM:SS") local-time string."""
    parts = [int(p) for p in value.split(":")]
    if not 2 <= len(parts) <= 3 or not (0 <= parts[0] <= 23) or not (0 <= parts[1] <= 59):
        raise ValueError(f"invalid time {value!r}; expected HH:MM")
    return time(*parts)


def load_config(path: Path | None = None) -> Config:
    """Load and validate the configuration. Cached per path."""
    p = _resolve_config_path(path)
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
    locale = data.get("locale", "en")
    if not isinstance(locale, str) or not locale:
        raise ValueError(f"locale must be a non-empty string, got {locale!r}")
    idle_close = data.get("idle_close_minutes", DEFAULT_IDLE_CLOSE_MINUTES)
    if not isinstance(idle_close, (int, float)) or idle_close <= 0:
        raise ValueError(
            f"idle_close_minutes must be > 0, got {idle_close!r}"
        )
    compact_widget = data.get("compact_widget", False)
    if not isinstance(compact_widget, bool):
        raise ValueError(
            f"compact_widget must be a boolean, got {compact_widget!r}"
        )
    _VALID_VIEWS: frozenset[str] = frozenset({"today", "week", "month", "year"})
    widget_view = data.get("widget_view", "today")
    if widget_view not in _VALID_VIEWS:
        raise ValueError(
            f"widget_view must be one of {sorted(_VALID_VIEWS)}, got {widget_view!r}"
        )
    config = Config(
        work_window=work_window,
        codl=_parse_codl(data.get("codl") or {}),
        resumption=_parse_resumption(data.get("resumption") or {}),
        scoring=_parse_scoring(data.get("scoring") or {}),
        idle_close_minutes=int(idle_close),
        locale=locale,
        compact_widget=compact_widget,
        widget_view=widget_view,
    )
    _CONFIG_CACHE[key] = config
    return config


def set_compact_widget(value: bool) -> bool:
    """Persist ``compact_widget`` to the live, user-editable ``config.json``,
    then invalidate the cache so the next ``load_config()`` reflects it.

    This is what the desktop widget's expand/collapse button writes through
    (via ``--set-compact``). It seeds ``config.json`` from the tracked defaults
    when the file doesn't exist yet, and only touches the one key — every other
    setting the user has is preserved. Writing here (never to
    ``config.default.json``) keeps the tracked defaults clean and the user's
    override out of the repo (``config.json`` is gitignored).

    Pure local disk I/O — no network (the no-network core constraint). Returns
    the value written.
    """
    source = (
        _RUNTIME_CONFIG_PATH if _RUNTIME_CONFIG_PATH.exists()
        else _DEFAULTS_CONFIG_PATH
    )
    data = json.loads(source.read_text(encoding="utf-8"))
    data["compact_widget"] = bool(value)
    _RUNTIME_CONFIG_PATH.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8",
    )
    _CONFIG_CACHE.clear()
    return bool(value)


_VALID_WIDGET_VIEWS: frozenset[str] = frozenset({"today", "week", "month", "year"})


def set_widget_view(value: str) -> str:
    """Persist ``widget_view`` to the live, user-editable ``config.json``,
    then invalidate the cache so the next ``load_config()`` reflects it.

    This is what the desktop widget's tab buttons write through (via
    ``--set-view``). It seeds ``config.json`` from the tracked defaults when
    the file doesn't exist yet, and only touches the one key — every other
    setting the user has is preserved. Writing here (never to
    ``config.default.json``) keeps the tracked defaults clean and the user's
    override out of the repo (``config.json`` is gitignored).

    Pure local disk I/O — no network (the no-network core constraint). Returns
    the value written.
    """
    if value not in _VALID_WIDGET_VIEWS:
        raise ValueError(
            f"widget_view must be one of {sorted(_VALID_WIDGET_VIEWS)}, got {value!r}"
        )
    source = (
        _RUNTIME_CONFIG_PATH if _RUNTIME_CONFIG_PATH.exists()
        else _DEFAULTS_CONFIG_PATH
    )
    data = json.loads(source.read_text(encoding="utf-8"))
    data["widget_view"] = value
    _RUNTIME_CONFIG_PATH.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8",
    )
    _CONFIG_CACHE.clear()
    return value


def _parse_scoring(raw: dict) -> ScoringConfig:
    """Parse + validate the composite-scoring block, falling back to the
    calibration-prior defaults for any missing key. All numeric parameters must
    be > 0; weights must be three non-negative numbers that don't all sum to zero."""
    d = ScoringConfig()
    codl_cap = raw.get("codl_capacity", d.codl_capacity)
    codl_horizon = raw.get("codl_dose_horizon_minutes", d.codl_dose_horizon_minutes)
    int_c = raw.get("interruption_ceiling", d.interruption_ceiling)
    weights = raw.get("weights", list(d.weights))
    for name, c in (
        ("codl_capacity", codl_cap),
        ("codl_dose_horizon_minutes", codl_horizon),
        ("interruption_ceiling", int_c),
    ):
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
        codl_capacity=float(codl_cap),
        codl_dose_horizon_minutes=float(codl_horizon),
        interruption_ceiling=float(int_c),
        weights=(float(weights[0]), float(weights[1]), float(weights[2])),
    )


def _parse_resumption(raw: dict) -> ResumptionConfig:
    """Parse + validate the resumption-scoring block, falling back to defaults
    for any missing key. ``threshold_minutes`` and ``full_decay_minutes`` must be
    > 0; ``daily_ceiling`` must be > 0."""
    d = ResumptionConfig()
    threshold = raw.get("threshold_minutes", d.threshold_minutes)
    full_decay = raw.get("full_decay_minutes", d.full_decay_minutes)
    ceiling = raw.get("daily_ceiling", d.daily_ceiling)
    for name, v in (
        ("threshold_minutes", threshold),
        ("full_decay_minutes", full_decay),
        ("daily_ceiling", ceiling),
    ):
        if not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"resumption.{name} must be > 0, got {v!r}")
    return ResumptionConfig(
        threshold_minutes=int(threshold),
        full_decay_minutes=int(full_decay),
        daily_ceiling=float(ceiling),
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
