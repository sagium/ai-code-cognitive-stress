"""Tests for the citations registry loader."""

from __future__ import annotations

import pytest

from stress_levels.citations import (
    Citation,
    _REGISTRY_CACHE,
    cite,
    load_registry,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _REGISTRY_CACHE.clear()
    yield
    _REGISTRY_CACHE.clear()


def _write_yml(path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _good_entry(**overrides: str) -> str:
    """Render one well-formed entry, allowing per-test field overrides."""
    fields = {
        "key": "test-1900",
        "authors": '"Test, T."',
        "year": "1900",
        "title": '"A test title"',
        "venue": '"Test journal, 1(1), 1–2"',
        "doi": "null",
        "supports_block": '  supports:\n    - "A claim"',
    }
    fields.update(overrides)
    return (
        f"- key: {fields['key']}\n"
        f"  authors: {fields['authors']}\n"
        f"  year: {fields['year']}\n"
        f"  title: {fields['title']}\n"
        f"  venue: {fields['venue']}\n"
        f"  doi: {fields['doi']}\n"
        f"{fields['supports_block']}\n"
    )


# ---------------------------------------------------------------------------
# Default registry — exercises the real citations.yml shipped with the repo.

def test_default_registry_loads_cleanly():
    registry = load_registry()
    assert registry, "default registry should not be empty"


def test_default_registry_keys_are_unique_and_kebab_cased():
    registry = load_registry()
    for key in registry:
        assert key == key.lower(), f"key {key!r} is not lowercased"
        assert " " not in key, f"key {key!r} contains whitespace"


def test_every_entry_has_required_fields_with_correct_types():
    registry = load_registry()
    for citation in registry.values():
        assert isinstance(citation, Citation)
        assert citation.key and isinstance(citation.key, str)
        assert citation.authors and isinstance(citation.authors, str)
        assert isinstance(citation.year, int)
        assert citation.title and isinstance(citation.title, str)
        assert citation.venue and isinstance(citation.venue, str)
        assert citation.doi is None or isinstance(citation.doi, str)
        assert isinstance(citation.supports, tuple)
        assert len(citation.supports) > 0
        assert all(isinstance(s, str) and s for s in citation.supports)


def test_every_entry_year_is_plausible():
    for citation in load_registry().values():
        assert 1900 <= citation.year <= 2100, (
            f"{citation.key}: implausible year {citation.year}"
        )


def test_anchor_citations_referenced_in_readme_are_present():
    # README and SKILL.md call these out as load-bearing for the metric.
    # If any one disappears, a render referencing it will crash on cite().
    registry = load_registry()
    expected = {
        "cowan-2001",
        "cummings-mitchell-2008",
        "mark-gudith-klocke-2008",
        "leroy-2009",
        "demerouti-2001",
        "mcewen-1998",
        "yerkes-dodson-1908",
        "csikszentmihalyi-1990",
    }
    missing = expected - set(registry)
    assert not missing, f"anchor citations missing from registry: {sorted(missing)}"


# ---------------------------------------------------------------------------
# cite()

def test_cite_returns_citation_on_hit():
    citation = cite("cowan-2001")
    assert citation.year == 2001
    assert "Cowan" in citation.authors


def test_cite_raises_keyerror_on_miss():
    with pytest.raises(KeyError):
        cite("does-not-exist-1999")


# ---------------------------------------------------------------------------
# Caching

def test_load_registry_caches_results():
    first = load_registry()
    second = load_registry()
    assert first is second


def test_explicit_path_is_cached_separately(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(key="ad-hoc-2000"))
    from_explicit = load_registry(fake)
    from_default = load_registry()
    assert from_explicit is not from_default
    assert "ad-hoc-2000" in from_explicit
    assert "ad-hoc-2000" not in from_default


# ---------------------------------------------------------------------------
# Parser edge cases

def test_loader_handles_null_doi(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(doi="null"))
    registry = load_registry(fake)
    assert registry["test-1900"].doi is None


def test_loader_handles_quoted_doi(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(doi='"10.0000/example"'))
    registry = load_registry(fake)
    assert registry["test-1900"].doi == "10.0000/example"


def test_loader_handles_multiple_supports_items(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(
        fake,
        _good_entry(
            supports_block='  supports:\n    - "First claim"\n    - "Second claim"',
        ),
    )
    registry = load_registry(fake)
    assert registry["test-1900"].supports == ("First claim", "Second claim")


def test_loader_preserves_unicode_in_authors(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(authors='"Csíkszentmihályi, M."'))
    registry = load_registry(fake)
    assert registry["test-1900"].authors == "Csíkszentmihályi, M."


def test_loader_preserves_colons_inside_quoted_titles(tmp_path):
    fake = tmp_path / "fixture.yml"
    title_with_colon = '"The thing: a subtitle with: more colons"'
    _write_yml(fake, _good_entry(title=title_with_colon))
    registry = load_registry(fake)
    assert registry["test-1900"].title == (
        "The thing: a subtitle with: more colons"
    )


def test_loader_ignores_comments_and_blank_lines(tmp_path):
    fake = tmp_path / "fixture.yml"
    body = (
        "# top-of-file comment\n"
        "\n"
        f"{_good_entry()}"
        "\n"
        "# trailing comment\n"
    )
    _write_yml(fake, body)
    registry = load_registry(fake)
    assert "test-1900" in registry


def test_loader_strips_inline_comment_after_quoted_value(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(authors='"Cowan, N." # primary author'))
    registry = load_registry(fake)
    assert registry["test-1900"].authors == "Cowan, N."


def test_loader_strips_inline_comment_after_bare_value(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(year="1900 # publication year"))
    registry = load_registry(fake)
    assert registry["test-1900"].year == 1900


def test_loader_preserves_hash_inside_quoted_string(tmp_path):
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(authors='"#hashtag, A."'))
    registry = load_registry(fake)
    assert registry["test-1900"].authors == "#hashtag, A."


def test_loader_preserves_hash_in_unquoted_string_without_preceding_space(tmp_path):
    # `foo#bar` (no space) is part of the value, not a comment.
    fake = tmp_path / "fixture.yml"
    _write_yml(fake, _good_entry(doi="10.1234/foo#anchor"))
    registry = load_registry(fake)
    assert registry["test-1900"].doi == "10.1234/foo#anchor"


# ---------------------------------------------------------------------------
# Validation — malformed inputs

def test_empty_file_raises(tmp_path):
    fake = tmp_path / "empty.yml"
    _write_yml(fake, "# only a comment\n")
    with pytest.raises(ValueError, match="empty registry"):
        load_registry(fake)


def test_missing_required_field_raises(tmp_path):
    fake = tmp_path / "missing.yml"
    body = (
        "- key: missing-venue\n"
        '  authors: "Test, T."\n'
        "  year: 1900\n"
        '  title: "T"\n'
        "  doi: null\n"
        '  supports:\n    - "claim"\n'
    )
    _write_yml(fake, body)
    with pytest.raises(ValueError, match="venue"):
        load_registry(fake)


def test_year_must_be_integer(tmp_path):
    fake = tmp_path / "bad_year.yml"
    _write_yml(fake, _good_entry(year='"not a number"'))
    with pytest.raises(ValueError, match="year"):
        load_registry(fake)


def test_empty_supports_list_raises(tmp_path):
    fake = tmp_path / "empty_supports.yml"
    _write_yml(fake, _good_entry(supports_block="  supports:"))
    with pytest.raises(ValueError, match="supports"):
        load_registry(fake)


def test_empty_string_field_raises(tmp_path):
    fake = tmp_path / "empty_title.yml"
    _write_yml(fake, _good_entry(title='""'))
    with pytest.raises(ValueError, match="title"):
        load_registry(fake)


def test_duplicate_keys_raise(tmp_path):
    fake = tmp_path / "duplicate.yml"
    body = _good_entry(key="dup") + _good_entry(key="dup")
    _write_yml(fake, body)
    with pytest.raises(ValueError, match="duplicate"):
        load_registry(fake)


def test_malformed_line_raises(tmp_path):
    fake = tmp_path / "malformed.yml"
    body = _good_entry() + "  this line has no colon\n"
    _write_yml(fake, body)
    with pytest.raises(ValueError, match="cannot parse"):
        load_registry(fake)


def test_orphan_list_item_raises(tmp_path):
    # A "- value" line outside any mapping's list field.
    fake = tmp_path / "orphan.yml"
    _write_yml(fake, '    - "stray"\n')
    with pytest.raises(ValueError, match="list item outside"):
        load_registry(fake)


def test_unterminated_quoted_string_raises(tmp_path):
    fake = tmp_path / "unterminated.yml"
    body = (
        "- key: bad\n"
        '  authors: "Test, T.\n'
        "  year: 1900\n"
        '  title: "T"\n'
        '  venue: "V"\n'
        "  doi: null\n"
        '  supports:\n    - "x"\n'
    )
    _write_yml(fake, body)
    with pytest.raises(ValueError, match="unterminated"):
        load_registry(fake)


def test_duplicate_field_within_entry_raises(tmp_path):
    fake = tmp_path / "dup_field.yml"
    body = (
        "- key: dup-field\n"
        '  authors: "A"\n'
        '  authors: "B"\n'
        "  year: 1900\n"
        '  title: "T"\n'
        '  venue: "V"\n'
        "  doi: null\n"
        '  supports:\n    - "x"\n'
    )
    _write_yml(fake, body)
    with pytest.raises(ValueError, match="duplicate field"):
        load_registry(fake)


def test_load_registry_returns_read_only_mapping():
    registry = load_registry()
    with pytest.raises(TypeError):
        registry["new-key"] = None  # type: ignore[index]


def test_load_registry_disallows_clearing():
    registry = load_registry()
    with pytest.raises(AttributeError):
        registry.clear()  # type: ignore[attr-defined]
