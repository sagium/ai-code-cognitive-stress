"""Tests for the minimal markdown → HTML converter."""

from __future__ import annotations

from stress_levels.markdown_min import to_html


def test_empty_string_returns_empty():
    assert to_html("") == ""


def test_paragraph_wraps_single_line():
    assert to_html("hello world") == "<p>hello world</p>"


def test_multiple_paragraphs_separated_by_blank_line():
    out = to_html("one\n\ntwo")
    assert "<p>one</p>" in out
    assert "<p>two</p>" in out


def test_consecutive_non_blank_lines_join_in_one_paragraph():
    out = to_html("first\nsecond")
    assert out == "<p>first second</p>"


def test_atx_heading_levels():
    # h1 in markdown bumps to h2 to avoid clashing with the page-level h1.
    assert to_html("# Top") == "<h2>Top</h2>"
    assert to_html("## Sub") == "<h3>Sub</h3>"
    assert to_html("### Deep") == "<h4>Deep</h4>"


def test_unordered_list_collects_items():
    out = to_html("- one\n- two\n- three")
    assert "<ul>" in out
    assert out.count("<li>") == 3
    assert "<li>one</li>" in out
    assert "<li>three</li>" in out
    assert "</ul>" in out


def test_list_closed_by_blank_line():
    out = to_html("- one\n- two\n\nparagraph")
    assert "</ul>" in out
    assert "<p>paragraph</p>" in out


def test_list_closed_by_heading():
    out = to_html("- one\n## Next section")
    assert "</ul>" in out
    assert "<h3>Next section</h3>" in out


def test_inline_bold_italic_code():
    out = to_html("hello **bold** and *italic* and `code`")
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<code>code</code>" in out


def test_inline_link():
    out = to_html("see [the docs](https://example.com)")
    assert '<a href="https://example.com">the docs</a>' in out


def test_html_in_source_is_escaped():
    out = to_html("watch out: <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_italic_does_not_consume_bold_markers():
    out = to_html("**a** and *b*")
    assert "<strong>a</strong>" in out
    assert "<em>b</em>" in out
    # No malformed nested <strong> inside <em>
    assert "<em>a" not in out


def test_code_span_preserves_special_chars():
    out = to_html("`a < b`")
    # `<` becomes &lt; via html.escape applied before pattern matching;
    # the code span content carries the escaped form.
    assert "<code>a &lt; b</code>" in out


def test_paragraphs_around_headings_and_lists():
    md = (
        "Intro paragraph.\n"
        "\n"
        "## Section\n"
        "\n"
        "- bullet one\n"
        "- bullet two\n"
        "\n"
        "Closing paragraph."
    )
    out = to_html(md)
    assert "<p>Intro paragraph.</p>" in out
    assert "<h3>Section</h3>" in out
    assert "<ul>" in out and "</ul>" in out
    assert "<p>Closing paragraph.</p>" in out


def test_trailing_whitespace_does_not_create_extra_paragraph():
    out = to_html("hello\n  \n")
    # The blank-with-spaces line is treated as blank.
    assert out == "<p>hello</p>"
