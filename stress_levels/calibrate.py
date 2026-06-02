"""Population calibration from pooled research exports.

Reads many anonymized exports (``research_export.py``, schema v1/v2/v3) that the
maintainer has collected, pools them, and crunches the population to recalibrate
the index so it covers the real range of work patterns. Produces a report plus a
suggested ``scoring`` config block; it changes nothing on its own — the operator
reviews and opts in (`config.json` `scoring`).

Local-only and stdlib-only: reads files the maintainer already has on disk,
writes a local report, and uses only ``statistics`` for the math (no numpy).

**Unsupervised by necessity.** The exports carry no felt-load labels, so we can
fit the normalization *scales* to the population and *suggest* weights from axis
redundancy, but we cannot validate/fit weights against felt load — that remains
future work needing a subjective criterion (NASA-TLX / EMA). The report says so.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA_PREFIX = "ai-code-cognitive-stress.research-export"
_WEEKEND = {"Sat", "Sun"}

CAVEAT = (
    "Unsupervised calibration: the exports carry no felt-load labels, so the "
    "suggested weights are a redundancy/variance heuristic, NOT validated "
    "against subjective load. Fitting weights to felt load needs a subjective "
    "criterion (NASA-TLX / EMA) and remains future work. Review before adopting."
)


@dataclass
class DayRecord:
    participant: str
    weekday: str
    codl_avg: float
    interruption_rate: float
    closure_deficit: float
    composite: float
    off_hours_minutes: float
    work_start: int | None = None
    work_end: int | None = None
    peak_headcount: int | None = None          # v2 debug only
    session_durations: list[int] = field(default_factory=list)  # v2 debug only


# ---------------------------------------------------------------------------
# Loading

def load_exports(paths: list[str]) -> tuple[list[dict], list[str]]:
    """Load export dicts from a mix of files and directories (dirs are globbed
    for ``*.json``). Foreign/malformed files are skipped with a warning rather
    than aborting the run. Returns ``(exports, warnings)``."""
    warnings: list[str] = []
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        elif p.is_file():
            files.append(p)
        else:
            warnings.append(f"not found, skipped: {p}")

    exports: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(f"unreadable JSON, skipped: {f} ({exc})")
            continue
        schema = data.get("schema", "") if isinstance(data, dict) else ""
        if not str(schema).startswith(_SCHEMA_PREFIX):
            warnings.append(f"not a research export, skipped: {f}")
            continue
        exports.append(data)
    return exports, warnings


def _hour(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    try:
        return int(str(hhmm).split(":")[0])
    except (ValueError, IndexError):
        return None


def pool_day_records(exports: list[dict]) -> list[DayRecord]:
    """Flatten every export's active days into per-day records (v2 debug fields
    captured when present)."""
    records: list[DayRecord] = []
    for i, exp in enumerate(exports):
        participant = exp.get("participant") or f"anon-{i}"
        days = (exp.get("profile") or {}).get("days") or []
        for d in days:
            ww = d.get("work_window") or [None, None]
            debug = d.get("debug") or {}
            records.append(DayRecord(
                participant=participant,
                weekday=d.get("weekday", ""),
                codl_avg=float(d.get("codl_avg", 0.0)),
                interruption_rate=float(d.get("interruption_rate", 0.0)),
                closure_deficit=float(d.get("closure_deficit", 0.0)),
                composite=float(d.get("composite", 0.0)),
                off_hours_minutes=float(d.get("off_hours_minutes", 0.0)),
                work_start=_hour(ww[0]),
                work_end=_hour(ww[1]),
                peak_headcount=debug.get("peak_headcount"),
                session_durations=[
                    s.get("duration_min", 0) for s in debug.get("sessions", [])
                ],
            ))
    return records


# ---------------------------------------------------------------------------
# Stats helpers (stdlib only)

def _percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in [0,1]) of an unsorted list."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(s):
        return float(s[lo] + (s[lo + 1] - s[lo]) * frac)
    return float(s[lo])


def _dist(values: list[float], pcts=(0.5, 0.75, 0.9, 0.95, 0.99)) -> dict:
    out: dict[str, Any] = {"n": len(values)}
    for q in pcts:
        out[f"p{int(q * 100)}"] = _round(_percentile(values, q))
    out["max"] = _round(max(values)) if values else None
    return out


def _round(x: float | None, ndigits: int = 3) -> float | None:
    return round(x, ndigits) if isinstance(x, (int, float)) else None


def _stdev(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def _corr(xs: list[float], ys: list[float]) -> float | None:
    try:
        return round(statistics.correlation(xs, ys), 3)
    except (statistics.StatisticsError, ValueError):
        return None  # < 2 points or a constant series


# ---------------------------------------------------------------------------
# Calibration

def calibrate(
    records: list[DayRecord],
    *,
    current_codl_ceiling: float = 5.0,
    current_interruption_ceiling: float = 10.0,
    ceiling_pct: float = 0.95,
) -> dict:
    """Crunch pooled day records into calibration suggestions + descriptives."""
    codl = [r.codl_avg for r in records]
    interr = [r.interruption_rate for r in records]
    closure = [r.closure_deficit for r in records]
    composite = [r.composite for r in records]
    off_hours = [r.off_hours_minutes for r in records]

    # 1. Ceilings — set each axis scale to a high population quantile so the
    #    [0,1] map covers the real spread without saturating most users.
    sugg_codl_ceiling = _percentile(codl, ceiling_pct) or current_codl_ceiling
    sugg_int_ceiling = _percentile(interr, ceiling_pct) or current_interruption_ceiling
    sugg_codl_ceiling = round(max(sugg_codl_ceiling, 0.1), 3)
    sugg_int_ceiling = round(max(sugg_int_ceiling, 0.1), 3)

    # 2. Redundancy-informed weights — normalize each axis with the suggested
    #    ceilings, then weight for equal variance contribution (∝ 1/stdev). A
    #    no-variance axis carries no discriminative info → weight 0. Correlations
    #    are reported so redundancy is visible. Heuristic, not felt-load-fitted.
    codl_n = [min(1.0, v / sugg_codl_ceiling) for v in codl]
    int_n = [min(1.0, v / sugg_int_ceiling) for v in interr]
    clo_n = [max(0.0, min(1.0, v)) for v in closure]
    stdevs = {
        "codl": round(_stdev(codl_n), 4),
        "interruption": round(_stdev(int_n), 4),
        "closure": round(_stdev(clo_n), 4),
    }
    inv = [1.0 / s if s > 0 else 0.0 for s in (stdevs["codl"], stdevs["interruption"], stdevs["closure"])]
    total_inv = sum(inv)
    if total_inv > 0:
        sugg_weights = [round(v / total_inv, 4) for v in inv]
    else:
        sugg_weights = [round(1 / 3, 4)] * 3
    correlations = {
        "codl_interruption": _corr(codl_n, int_n),
        "codl_closure": _corr(codl_n, clo_n),
        "interruption_closure": _corr(int_n, clo_n),
    }

    # 3. Work-pattern coverage.
    starts = [r.work_start for r in records if r.work_start is not None]
    ends = [r.work_end for r in records if r.work_end is not None]
    weekend_days = sum(1 for r in records if r.weekday in _WEEKEND)
    peaks = [r.peak_headcount for r in records if r.peak_headcount is not None]
    durations = [d for r in records for d in r.session_durations]
    per_participant: dict[str, int] = {}
    for r in records:
        per_participant[r.participant] = per_participant.get(r.participant, 0) + 1
    dpp = list(per_participant.values())

    coverage = {
        "work_start_hour": _dist([float(h) for h in starts], (0.1, 0.5, 0.9)),
        "work_end_hour": _dist([float(h) for h in ends], (0.1, 0.5, 0.9)),
        "weekend_share": _round(weekend_days / len(records)) if records else None,
        "peak_headcount": _dist([float(p) for p in peaks]) if peaks else None,
        "session_duration_min": _dist([float(d) for d in durations]) if durations else None,
        "days_per_participant": {
            "min": min(dpp) if dpp else 0,
            "p50": _round(_percentile([float(x) for x in dpp], 0.5)),
            "max": max(dpp) if dpp else 0,
        },
        "archetypes": _archetypes(records),
    }

    return {
        "caveat": CAVEAT,
        "n_participants": len(per_participant),
        "n_day_records": len(records),
        "ceilings": {
            "percentile": ceiling_pct,
            "current": {
                "codl": current_codl_ceiling,
                "interruption": current_interruption_ceiling,
            },
            "suggested": {"codl": sugg_codl_ceiling, "interruption": sugg_int_ceiling},
            "distribution": {
                "codl_avg": _dist(codl),
                "interruption_rate": _dist(interr),
                "off_hours_minutes": _dist(off_hours),
            },
        },
        "weights": {
            "method": "equal-variance-contribution (unsupervised heuristic)",
            "current": [round(1 / 3, 4)] * 3,
            "suggested": sugg_weights,
            "order": ["codl", "interruption", "closure"],
            "axis_stdev": stdevs,
            "correlations": correlations,
        },
        "coverage": coverage,
        "reference_percentiles": {
            "codl_avg": _dist(codl, (0.5, 0.75, 0.9)),
            "interruption_rate": _dist(interr, (0.5, 0.75, 0.9)),
            "closure_deficit": _dist(closure, (0.5, 0.75, 0.9)),
            "composite": _dist(composite, (0.5, 0.75, 0.9)),
        },
        "suggested_scoring_config": {
            "codl_ceiling": sugg_codl_ceiling,
            "interruption_ceiling": sugg_int_ceiling,
            "weights": sugg_weights,
        },
    }


def _archetypes(records: list[DayRecord]) -> dict[str, int]:
    """Coarse work-pattern buckets: start-of-day band × concurrency level.
    Surfaces population gaps (empty/thin cells)."""
    buckets: dict[str, int] = {}
    for r in records:
        if r.work_start is None:
            start_band = "unknown-start"
        elif r.work_start < 9:
            start_band = "early"
        elif r.work_start < 12:
            start_band = "standard"
        else:
            start_band = "late"
        peak = r.peak_headcount
        conc = "unknown-conc" if peak is None else ("high-conc" if peak >= 3 else "low-conc")
        key = f"{start_band}/{conc}"
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items()))


# ---------------------------------------------------------------------------
# Reporting

def render_report(result: dict) -> tuple[dict, str]:
    """Return ``(machine_readable_dict, human_text)``. The text ends with a
    pasteable ``scoring`` config block."""
    scoring_block = {"scoring": result["suggested_scoring_config"]}
    w = result["weights"]
    c = result["ceilings"]
    lines = [
        "Population calibration report",
        "=" * 32,
        f"participants: {result['n_participants']}   day-records: {result['n_day_records']}",
        "",
        f"NOTE: {result['caveat']}",
        "",
        "Normalization ceilings "
        f"(at p{int(c['percentile'] * 100)} of the population):",
        f"  CODL          current {c['current']['codl']}  -> suggested {c['suggested']['codl']}",
        f"  Interruption  current {c['current']['interruption']}  -> suggested {c['suggested']['interruption']}",
        "",
        f"Composite weights [codl, interruption, closure] — {w['method']}:",
        f"  current   {w['current']}",
        f"  suggested {w['suggested']}",
        f"  axis stdev {w['axis_stdev']}",
        f"  correlations {w['correlations']}",
        "",
        "Work-pattern coverage:",
        f"  weekend share: {result['coverage']['weekend_share']}",
        f"  work start hour: {result['coverage']['work_start_hour']}",
        f"  archetypes: {result['coverage']['archetypes']}",
        "",
        "Suggested config.json block (review before adopting):",
        json.dumps(scoring_block, indent=2),
    ]
    return result, "\n".join(lines)
