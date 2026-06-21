"""Sidecar storage for per-day subjective (felt-workload) grades.

A grade is a user's self-report of how their day felt, captured through the
desktop widget. It is stored in a sidecar file next to the durable day archive:
    <archive_dir>/<YYYY>/<YYYY-MM-DD>.subjective.json
= {"day": "...", "grade": 0|1|2, "scale": "band-v1", "recorded_on": "..."}

It is deliberately NOT embedded in the archive day file, because the archive
day file is rewritten from the merged aggregate on every run (to pick up newly
recycled streams) and would clobber an embedded grade.

Grades are write-once-per-day by convention; re-grading overwrites the sidecar
(the user corrected themselves). Reads are tolerant — any OS or JSON error
returns None rather than raising.

The three bands mirror the tool's composite-status vocabulary:
    0 = chill   (composite: "good")
    1 = heated  (composite: "caution")
    2 = cooked  (composite: "high")
These are stable integers, not the localized labels, so the sidecar stays
meaningful when the locale changes.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path


GRADE_SCALE = "band-v1"
VALID_GRADES = frozenset({0, 1, 2})


def subjective_path(archive_dir: Path, day: date) -> Path:
    """Absolute path of the sidecar for *day* under *archive_dir*."""
    return archive_dir / f"{day.year:04d}" / f"{day.isoformat()}.subjective.json"


def read_grade(archive_dir: Path, day: date) -> int | None:
    """Return the stored grade (0/1/2) for *day*, or None if absent/unreadable.

    Tolerant by design: any OS error, JSON decode error, or missing/invalid
    field yields None rather than raising. The `scale` field is checked so that
    a future instrument change doesn't silently return an incompatible int."""
    path = subjective_path(archive_dir, day)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("scale") != GRADE_SCALE:
        return None
    grade = raw.get("grade")
    if grade not in VALID_GRADES:
        return None
    return int(grade)


def write_grade(archive_dir: Path, day: date, grade: int) -> None:
    """Persist a subjective grade to the sidecar (atomic tmp → os.replace).

    ``grade`` must be in {0, 1, 2}; callers are expected to validate before
    calling. ``archive_dir`` is created including parents if absent. Mirrors the
    atomic-write pattern in ``pipeline/aggregate.py``'s ``_write_archive``."""
    if grade not in VALID_GRADES:
        raise ValueError(f"grade must be 0, 1, or 2 — got {grade!r}")
    path = subjective_path(archive_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "day": day.isoformat(),
        "grade": grade,
        "scale": GRADE_SCALE,
        "recorded_on": date.today().isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)
