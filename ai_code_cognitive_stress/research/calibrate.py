"""Population calibration from pooled research exports.

Reads many anonymized exports (``research_export.py``, schema v1/v2/v3/v4) that
the maintainer has collected, pools them, and crunches the population to
recalibrate the index so it covers the real range of work patterns. Produces a
report plus a suggested ``scoring`` config block; it changes nothing on its own
— the operator reviews and opts in (`config.json` `scoring`).

Stdlib-only: reads files the maintainer already has on disk, writes a report,
and uses only ``statistics`` for the math (no numpy).

**Unsupervised by necessity (label-free path).** When exports carry no
felt-load labels, we can fit the normalization *scales* to the population and
*suggest* weights from axis redundancy, but we cannot validate/fit weights
against felt load — that remains future work needing a subjective criterion
(NASA-TLX / EMA). The report says so.

**Felt-load path (v4 exports).** When enough labeled day-records are present
(subjective_rating 0/1/2), the report additionally computes Pearson correlations
between each normalized axis and the composite against felt load. These are
descriptive only — no regression weight-fit is performed. Fitting and validating
weights against felt load still needs sufficient labeled data across participants.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA_PREFIX = "ai-code-cognitive-stress.research-export"
_WEEKEND = {"Sat", "Sun"}

# Sample-size floor below which a p95 ceiling and an inverse-variance weight are
# too noisy to recommend over the literature priors. A p95 needs enough mass in
# the tail to be stable, and the inverse-stdev weights need a population, not a
# handful of days. Below these the report keeps the current values and says so.
# Both are overridable so the maintainer can relax them on a known-good pool.
_MIN_PARTICIPANTS = 20
_MIN_DAY_RECORDS = 100
# Closure carries its own, smaller N (only active days have a value). Below this
# the closure axis stdev/weight is flagged as thin even when the pool is large.
_MIN_CLOSURE_RECORDS = 40
# Minimum labeled day-records (subjective_rating not None) needed before
# reporting felt-load correlations. Below this, coverage is reported but no
# correlation is computed (too noisy to be meaningful).
_MIN_LABELED_RECORDS = 30

CAVEAT = (
    "Unsupervised calibration: the exports carry no felt-load labels, so the "
    "suggested weights are a redundancy/variance heuristic, NOT validated "
    "against subjective load. Fitting weights to felt load needs a subjective "
    "criterion (NASA-TLX / EMA) and remains future work. Review before adopting."
)

_CAVEAT_WITH_LABELS = (
    "Felt-load labels are now collected (v4 exports). The correlations below "
    "between axes and self-reported felt stress are descriptive — no regression "
    "weight-fit has been performed. Fitting and validating weights against felt "
    "load still needs sufficient labeled data across participants. The "
    "unsupervised weight suggestion (axis-redundancy heuristic) is unchanged. "
    "Review before adopting."
)


@dataclass
class DayRecord:
    participant: str
    weekday: str
    codl_avg: float
    interruption_rate: float
    closure_deficit: float | None  # resumption load; None → day had no activity
    composite: float
    off_hours_minutes: float
    work_start: int | None = None
    work_end: int | None = None
    peak_headcount: int | None = None          # v2 debug only
    session_durations: list[int] = field(default_factory=list)  # v2 debug only
    subjective_rating: int | None = None       # v4: 0=chill, 1=heated, 2=cooked


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
            raw_rating = d.get("subjective_rating")
            subjective_rating = (
                int(raw_rating) if isinstance(raw_rating, int) else None
            )
            records.append(DayRecord(
                participant=participant,
                weekday=d.get("weekday", ""),
                codl_avg=float(d.get("codl_avg", 0.0)),
                interruption_rate=float(d.get("interruption_rate", 0.0)),
                closure_deficit=(
                    float(d["closure_deficit"])
                    if d.get("closure_deficit") is not None else None
                ),
                composite=float(d.get("composite", 0.0)),
                off_hours_minutes=float(d.get("off_hours_minutes", 0.0)),
                work_start=_hour(ww[0]),
                work_end=_hour(ww[1]),
                peak_headcount=debug.get("peak_headcount"),
                session_durations=[
                    s.get("duration_min", 0) for s in debug.get("sessions", [])
                ],
                subjective_rating=subjective_rating,
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
    current_weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3),
    ceiling_pct: float = 0.95,
    min_participants: int = _MIN_PARTICIPANTS,
    min_day_records: int = _MIN_DAY_RECORDS,
    min_closure_records: int = _MIN_CLOSURE_RECORDS,
) -> dict:
    """Crunch pooled day records into calibration suggestions + descriptives.

    The fitted ceilings/weights are always reported under ``ceilings.suggested``
    and ``weights.suggested`` for inspection, but ``suggested_scoring_config`` —
    the block the operator is invited to paste — only advises a change when the
    pool clears the sample-size floor (see ``reliability``). Below it the block
    keeps the current literature priors, because a p95/inverse-stdev fit from a
    thin sample can be worse than the prior it would replace.
    """
    codl = [r.codl_avg for r in records]
    interr = [r.interruption_rate for r in records]
    # Closure (resumption load) is None only on days with no activity. Closure-
    # specific stats run over only the days that HAVE a value.
    closure = [r.closure_deficit for r in records if r.closure_deficit is not None]
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
    # Correlations with closure need axis values PAIRED on the same day, so they
    # use only the days that have closure data (clo_n's length differs from the
    # full codl_n/int_n otherwise).
    paired = [
        (min(1.0, r.codl_avg / sugg_codl_ceiling),
         min(1.0, r.interruption_rate / sugg_int_ceiling),
         max(0.0, min(1.0, r.closure_deficit)))
        for r in records if r.closure_deficit is not None
    ]
    codl_cp = [p[0] for p in paired]
    int_cp = [p[1] for p in paired]
    clo_cp = [p[2] for p in paired]
    stdevs = {
        "codl": round(_stdev(codl_n), 4),
        "interruption": round(_stdev(int_n), 4),
        "closure": round(_stdev(clo_n), 4) if clo_n else 0.0,
    }
    inv = [1.0 / s if s > 0 else 0.0 for s in (stdevs["codl"], stdevs["interruption"], stdevs["closure"])]
    total_inv = sum(inv)
    if total_inv > 0:
        sugg_weights = [round(v / total_inv, 4) for v in inv]
    else:
        sugg_weights = [round(1 / 3, 4)] * 3
    correlations = {
        "codl_interruption": _corr(codl_n, int_n),
        "codl_closure": _corr(codl_cp, clo_cp),
        "interruption_closure": _corr(int_cp, clo_cp),
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

    # Sample-size gate. The fitted values above stay visible under
    # ceilings.suggested / weights.suggested, but only graduate into the
    # pasteable scoring block when the pool is big enough to trust them.
    n_participants = len(per_participant)
    n_records = len(records)
    n_closure = len(closure)
    warnings: list[str] = []
    if n_participants < min_participants:
        warnings.append(
            f"only {n_participants} participant(s); need {min_participants} for a "
            "stable population fit")
    if n_records < min_day_records:
        warnings.append(
            f"only {n_records} day-record(s); need {min_day_records} for a stable "
            f"p{int(ceiling_pct * 100)} ceiling")
    if n_closure < min_closure_records:
        warnings.append(
            f"only {n_closure} day(s) carry a closure value; need "
            f"{min_closure_records} before the closure weight is trustworthy")
    meets_minimums = n_participants >= min_participants and n_records >= min_day_records
    reliability = {
        "meets_minimums": meets_minimums,
        "n_participants": n_participants,
        "n_day_records": n_records,
        "n_closure_records": n_closure,
        "minimums": {
            "participants": min_participants,
            "day_records": min_day_records,
            "closure_records": min_closure_records,
        },
        "warnings": warnings,
    }

    current_weights_r = [round(w, 4) for w in current_weights]
    if meets_minimums:
        scoring_config = {
            "codl_ceiling": sugg_codl_ceiling,
            "interruption_ceiling": sugg_int_ceiling,
            "weights": sugg_weights,
        }
    else:
        # Too thin to recommend a change — keep the current priors.
        scoring_config = {
            "codl_ceiling": current_codl_ceiling,
            "interruption_ceiling": current_interruption_ceiling,
            "weights": current_weights_r,
        }

    # 4. Felt-load analysis (v4 exports with subjective_rating).
    labeled = [r for r in records if r.subjective_rating is not None]
    n_labeled = len(labeled)
    if n_labeled >= _MIN_LABELED_RECORDS:
        fl_ratings = [float(r.subjective_rating) for r in labeled]
        fl_codl_n = [min(1.0, r.codl_avg / sugg_codl_ceiling) for r in labeled]
        fl_int_n = [min(1.0, r.interruption_rate / sugg_int_ceiling) for r in labeled]
        fl_clo_n = [
            max(0.0, min(1.0, r.closure_deficit)) if r.closure_deficit is not None
            else 0.0
            for r in labeled
        ]
        fl_composite = [r.composite / 100.0 for r in labeled]
        felt_load = {
            "n_labeled": n_labeled,
            "label_coverage": _round(n_labeled / len(records)) if records else None,
            "correlations_with_felt_load": {
                "codl_norm": _corr(fl_codl_n, fl_ratings),
                "interruption_norm": _corr(fl_int_n, fl_ratings),
                "closure_norm": _corr(fl_clo_n, fl_ratings),
                "composite_norm": _corr(fl_composite, fl_ratings),
            },
            "note": (
                "Correlations are descriptive (Pearson r). No regression "
                "weight-fit performed — fitting weights against felt load "
                "needs sufficient labeled data across participants."
            ),
        }
        active_caveat = _CAVEAT_WITH_LABELS
    else:
        felt_load = {
            "n_labeled": n_labeled,
            "label_coverage": _round(n_labeled / len(records)) if records else None,
            "correlations_with_felt_load": None,
            "note": (
                f"Only {n_labeled} labeled day-record(s); need "
                f"{_MIN_LABELED_RECORDS} for felt-load correlations. "
                "Collect more v4 exports with user grades."
            ),
        }
        active_caveat = CAVEAT

    return {
        "caveat": active_caveat,
        "reliability": reliability,
        "n_participants": n_participants,
        "n_day_records": n_records,
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
            "current": current_weights_r,
            "suggested": sugg_weights,
            "order": ["codl", "interruption", "closure"],
            "axis_stdev": stdevs,
            "correlations": correlations,
        },
        "coverage": coverage,
        "felt_load": felt_load,
        "reference_percentiles": {
            "codl_avg": _dist(codl, (0.5, 0.75, 0.9)),
            "interruption_rate": _dist(interr, (0.5, 0.75, 0.9)),
            "closure_deficit": _dist(closure, (0.5, 0.75, 0.9)),
            "composite": _dist(composite, (0.5, 0.75, 0.9)),
        },
        "suggested_scoring_config": scoring_config,
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
    rel = result["reliability"]
    fl = result.get("felt_load", {})
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
        "Felt-load labels (subjective_rating from v4 exports):",
        f"  labeled records: {fl.get('n_labeled', 0)} "
        f"  coverage: {fl.get('label_coverage')}",
    ]
    fl_corr = fl.get("correlations_with_felt_load")
    if fl_corr is not None:
        lines += [
            "  correlations with felt load (Pearson r, descriptive only):",
            f"    codl_norm:         {fl_corr.get('codl_norm')}",
            f"    interruption_norm: {fl_corr.get('interruption_norm')}",
            f"    closure_norm:      {fl_corr.get('closure_norm')}",
            f"    composite_norm:    {fl_corr.get('composite_norm')}",
        ]
    else:
        lines.append(f"  {fl.get('note', 'no label data')}")
    lines.append("")
    if rel["meets_minimums"]:
        lines += [
            "Suggested config.json block (review before adopting):",
            json.dumps(scoring_block, indent=2),
        ]
    else:
        m = rel["minimums"]
        lines += [
            "INSUFFICIENT SAMPLE "
            f"(participants {rel['n_participants']}/{m['participants']}, "
            f"day-records {rel['n_day_records']}/{m['day_records']}) "
            "-- keeping current values; do not adopt the fitted suggestions yet.",
            *(f"  - {msg}" for msg in rel["warnings"]),
            "",
            "Block below keeps the current priors (no change advised at this N):",
            json.dumps(scoring_block, indent=2),
        ]
    return result, "\n".join(lines)
