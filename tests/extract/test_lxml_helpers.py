"""Unit tests for :func:`litspectraits.extract._lxml_helpers.walk_paragraph_with_offsets`.

The walker is small but load-bearing — the normaliser's verbatim-anchor
gate is built on the offsets it emits (``docs/normalized-documents-discussion.md``
§1.4). These tests pin the byte-equality-to-:func:`full_text` invariant
and exercise the edge cases the existing XML extractor fixtures don't
naturally hit (nested formatting, multiple xrefs in one paragraph, tails
after xrefs, no-xref paragraphs).
"""

from lxml import etree

from litspectraits.extract._lxml_helpers import (
    full_text,
    walk_paragraph_with_offsets,
)


def _parse(xml: str) -> etree._Element:
    """Parse a fragment into a root element. ``recover=False`` so a typo
    in a fixture fails loud rather than silently dropping content."""
    parser = etree.XMLParser(recover=False)
    return etree.fromstring(xml.encode('utf-8'), parser=parser)


# ---------------------------------------------------------------------------
# Byte-equality invariant: assembled text == full_text(paragraph)
# ---------------------------------------------------------------------------


def test_no_xrefs_text_matches_full_text() -> None:
    p = _parse('<p>just prose, no markers.</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text == full_text(p)
    assert spans == []


def test_single_xref_offset_slices_to_surface_form() -> None:
    p = _parse('<p>We cite <xref rid="R1">[1]</xref> here.</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text == full_text(p)
    assert text == 'We cite [1] here.'
    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == '[1]'


def test_multiple_xrefs_offsets_are_distinct_and_ordered() -> None:
    p = _parse('<p>See <xref rid="R1">[1]</xref> and <xref rid="R2">[2]</xref>.</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text == full_text(p)
    assert [(text[s.start : s.end]) for s in spans] == ['[1]', '[2]']
    assert spans[0].end <= spans[1].start


def test_xref_with_leading_paragraph_text() -> None:
    p = _parse('<p>prelude <xref rid="R1">[12]</xref> coda</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text == 'prelude [12] coda'
    assert text[spans[0].start : spans[0].end] == '[12]'
    assert spans[0].start == len('prelude ')


def test_xref_at_start_of_paragraph() -> None:
    p = _parse('<p><xref rid="R1">[1]</xref> opens.</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert spans[0].start == 0
    assert text[spans[0].start : spans[0].end] == '[1]'


def test_xref_with_no_label_text_has_zero_width_span() -> None:
    p = _parse('<p>before<xref rid="R1"/>after</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text == 'beforeafter'
    assert len(spans) == 1
    # Empty xref still emitted so downstream sees the citation marker
    # exists; surface_form will be empty.
    assert spans[0].start == spans[0].end == len('before')


# ---------------------------------------------------------------------------
# Nested formatting: xref inside bold / italic should still get correct offsets
# ---------------------------------------------------------------------------


def test_xref_nested_in_inline_formatting() -> None:
    # The xref sits inside <bold>; full_text descends into it and the
    # walker must mirror that descent while still keying the offset on
    # the xref element.
    p = _parse('<p>start <bold>warmup <xref rid="R1">[1]</xref> wind-down</bold> end</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text == full_text(p)
    assert text == 'start warmup [1] wind-down end'
    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == '[1]'


def test_text_with_tail_after_xref() -> None:
    p = _parse('<p>head <xref rid="R1">[1]</xref> tail with more stuff.</p>')
    text, _spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text.endswith('tail with more stuff.')
    assert text == full_text(p)


# ---------------------------------------------------------------------------
# Elsevier localname dispatch (cross-ref)
# ---------------------------------------------------------------------------


def test_elsevier_cross_ref_localname() -> None:
    # CEP's element is ``<ce:cross-ref>``; with local-name dispatch the
    # caller drops the prefix. A bare ``<xref>`` in the same paragraph
    # must be ignored when xref_localnames=('cross-ref',).
    p = _parse(
        '<p xmlns:ce="http://example.com/ce">'
        'See <ce:cross-ref refid="b1">[1]</ce:cross-ref> '
        'but not <xref rid="R0">[0]</xref>.'
        '</p>'
    )
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('cross-ref',))
    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == '[1]'


# ---------------------------------------------------------------------------
# Whitespace preservation around xrefs (the project's invariant)
# ---------------------------------------------------------------------------


def test_whitespace_inside_marker_is_preserved() -> None:
    # ``[ 1 ]`` must survive byte-for-byte — full_text() docstring calls
    # this out explicitly; the offset walker must inherit the property.
    p = _parse('<p>foo <xref rid="R1">[ 1 ]</xref> bar</p>')
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    assert text[spans[0].start : spans[0].end] == '[ 1 ]'
