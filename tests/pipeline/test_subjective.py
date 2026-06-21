"""Tests for the subjective sidecar storage (pipeline/subjective.py) and
the --rate CLI flag, including the main() dispatch."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ai_code_cognitive_stress.pipeline.subjective import (
    GRADE_SCALE,
    VALID_GRADES,
    read_grade,
    subjective_path,
    write_grade,
)


# ---------------------------------------------------------------------------
# Low-level read/write helpers

def test_write_grade_creates_sidecar(tmp_path):
    d = date(2026, 6, 17)
    write_grade(tmp_path, d, 2)
    path = subjective_path(tmp_path, d)
    assert path.is_file()


def test_write_grade_sidecar_content(tmp_path):
    import json
    d = date(2026, 6, 17)
    write_grade(tmp_path, d, 1)
    raw = json.loads(subjective_path(tmp_path, d).read_text(encoding="utf-8"))
    assert raw["day"] == "2026-06-17"
    assert raw["grade"] == 1
    assert raw["scale"] == GRADE_SCALE
    assert "recorded_on" in raw


def test_write_grade_then_read_round_trips(tmp_path):
    d = date(2026, 6, 17)
    for grade in (0, 1, 2):
        write_grade(tmp_path, d, grade)
        assert read_grade(tmp_path, d) == grade


def test_write_grade_overwrites_previous(tmp_path):
    d = date(2026, 6, 17)
    write_grade(tmp_path, d, 0)
    write_grade(tmp_path, d, 2)
    assert read_grade(tmp_path, d) == 2


def test_read_grade_absent_returns_none(tmp_path):
    assert read_grade(tmp_path, date(2026, 6, 17)) is None


def test_read_grade_tolerates_corrupt_file(tmp_path):
    d = date(2026, 6, 17)
    path = subjective_path(tmp_path, d)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert read_grade(tmp_path, d) is None


def test_read_grade_tolerates_wrong_scale(tmp_path):
    import json
    d = date(2026, 6, 17)
    path = subjective_path(tmp_path, d)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"day": d.isoformat(), "grade": 1, "scale": "band-v0", "recorded_on": "2026-06-17"}),
        encoding="utf-8",
    )
    assert read_grade(tmp_path, d) is None


def test_write_grade_rejects_invalid_grade(tmp_path):
    with pytest.raises(ValueError):
        write_grade(tmp_path, date(2026, 6, 17), 3)


def test_write_grade_is_atomic(tmp_path):
    """No .json.tmp file left behind after a successful write."""
    d = date(2026, 6, 17)
    write_grade(tmp_path, d, 0)
    path = subjective_path(tmp_path, d)
    assert not path.with_suffix(".json.tmp").exists()


def test_write_grade_creates_parent_dirs(tmp_path):
    d = date(2026, 6, 17)
    archive_dir = tmp_path / "archive"
    assert not archive_dir.exists()
    write_grade(archive_dir, d, 1)
    assert subjective_path(archive_dir, d).is_file()


# ---------------------------------------------------------------------------
# --rate CLI flag (via main())

def _import_main():
    from ai_code_cognitive_stress.__main__ import main
    return main


def _isolate_rate(monkeypatch, tmp_path):
    """Route archive_dir to tmp_path so the CLI flag writes there."""
    from pathlib import Path
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    import ai_code_cognitive_stress.pipeline.aggregate as agg
    real = agg.get_day_aggregates

    def fake(since, until, **kw):
        kw["cache_dir"] = tmp_path / "cache"
        kw["archive_dir"] = tmp_path / "archive"
        return real(since, until, **kw)

    monkeypatch.setattr(agg, "get_day_aggregates", fake)
    # Also patch DEFAULT_DATA_DIR used by main() directly for archive_dir
    import ai_code_cognitive_stress.__main__ as cli
    import ai_code_cognitive_stress.pipeline.aggregate as _agg
    monkeypatch.setattr(_agg, "DEFAULT_DATA_DIR", tmp_path)
    return tmp_path / "archive"


def test_rate_flag_writes_sidecar(monkeypatch, tmp_path, capsys):
    archive_dir = _isolate_rate(monkeypatch, tmp_path)
    main = _import_main()
    rc = main(["--rate", "2026-06-17:2"])
    assert rc == 0
    assert read_grade(archive_dir, date(2026, 6, 17)) == 2
    err = capsys.readouterr().err
    assert "2026-06-17" in err


def test_rate_flag_overwrites_on_rerate(monkeypatch, tmp_path):
    archive_dir = _isolate_rate(monkeypatch, tmp_path)
    main = _import_main()
    main(["--rate", "2026-06-17:0"])
    main(["--rate", "2026-06-17:1"])
    assert read_grade(archive_dir, date(2026, 6, 17)) == 1


def test_rate_flag_today_keyword(monkeypatch, tmp_path):
    archive_dir = _isolate_rate(monkeypatch, tmp_path)
    main = _import_main()
    rc = main(["--rate", "today:1"])
    assert rc == 0
    from datetime import date as _date
    assert read_grade(archive_dir, _date.today()) == 1


def test_rate_flag_malformed_day_errors(capsys):
    main = _import_main()
    rc = main(["--rate", "not-a-date:1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err.lower()
    assert "Traceback" not in err


def test_rate_flag_out_of_range_grade_errors(capsys):
    main = _import_main()
    rc = main(["--rate", "2026-06-17:5"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err.lower()
    assert "Traceback" not in err


def test_rate_flag_missing_colon_errors(capsys):
    main = _import_main()
    rc = main(["--rate", "2026-06-17-2"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err.lower()


def test_rate_flag_non_integer_grade_errors(capsys):
    main = _import_main()
    rc = main(["--rate", "2026-06-17:high"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err.lower()
