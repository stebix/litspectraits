"""Unit coverage for the cross-format differential primitives (E0.5b slice 2).

The harness's headline use is "is the docling route still as good as it
was last week?" — i.e. a regression tripwire whose signal is *changes*
in the proxy numbers, not absolute correctness. Each test below pins
one such change-detector by constructing an XML reference Document and
a docling Document that differs in exactly one way; the assertion is
that the proxy moves in the right direction (and only that proxy moves).

The fixtures are built straight against the attrs models rather than
via the adapters — slice 2 is independent of slice 3, and going
through ``normalize_docling_document`` would just push the
documents-under-test through the slice-1 stub and produce uniformly
empty inputs.
"""

import pytest

from litspectraits.normalize import (
    Completeness,
    Document,
    Provenance,
    Reference,
    TableBlock,
    TableCell,
    TextBlock,
    compare_documents,
    format_comparison_report,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _xml_table(
    *,
    id_: str,
    section_path: tuple[str, ...] = (),
    cells: tuple[tuple[str, ...], ...] = (('a', 'b'), ('1', '2')),
) -> TableBlock:
    return TableBlock(
        id=id_,
        label=None,
        caption=None,
        n_rows=len(cells),
        n_cols=len(cells[0]) if cells else 0,
        cells=tuple(tuple(TableCell(text=t, kind='td') for t in row) for row in cells),
        provenance=Provenance(route='jats'),
        section_path=section_path,
    )


def _docling_table(
    *,
    id_: str,
    section_path: tuple[str, ...] = (),
    cells: tuple[tuple[str, ...], ...] = (('a', 'b'), ('1', '2')),
) -> TableBlock:
    return TableBlock(
        id=id_,
        label=None,
        caption=None,
        n_rows=len(cells),
        n_cols=len(cells[0]) if cells else 0,
        cells=tuple(tuple(TableCell(text=t, kind='td') for t in row) for row in cells),
        # docling provenance must carry a page; the diff doesn't read
        # it, but the route-conditional invariant in Provenance is real.
        provenance=Provenance(route='docling', page=1),
        section_path=section_path,
    )


def _xml_text(*, text: str, section_path: tuple[str, ...] = ()) -> TextBlock:
    return TextBlock(text=text, provenance=Provenance(route='jats'), section_path=section_path)


def _docling_text(*, text: str, section_path: tuple[str, ...] = ()) -> TextBlock:
    return TextBlock(
        text=text,
        provenance=Provenance(route='docling', page=1),
        section_path=section_path,
    )


def _xml_completeness() -> Completeness:
    return Completeness(
        has_structured_refs=True,
        has_inline_ref_ids=True,
        has_equations=False,
        table_source='publisher',
    )


def _docling_completeness(*, has_equations: bool = False) -> Completeness:
    return Completeness(
        has_structured_refs=False,
        has_inline_ref_ids=False,
        has_equations=has_equations,
        table_source='tableformer',
    )


def _xml_doc(
    *,
    blocks: tuple = (),
    references: tuple[Reference, ...] = (),
) -> Document:
    return Document(
        route='jats',
        blocks=blocks,
        references=references,
        completeness=_xml_completeness(),
    )


def _docling_doc(
    *,
    blocks: tuple = (),
    references: tuple[Reference, ...] = (),
    has_equations: bool = False,
) -> Document:
    return Document(
        route='docling',
        blocks=blocks,
        references=references,
        completeness=_docling_completeness(has_equations=has_equations),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_identical_documents_full_recall() -> None:
    """Same blocks both sides → 1:1 table match, every XML token shared.

    This is the calibration case: every proxy at its ceiling. Any drift
    in the table-token recall ratio for a downstream test is meaningful
    only because this test pins the ceiling.
    """
    cells = (('Tissue', 'T1 (ms)'), ('Liver', '589'))
    xml = _xml_doc(blocks=(_xml_table(id_='T1', cells=cells),))
    docling = _docling_doc(blocks=(_docling_table(id_='T1', cells=cells),))

    comp = compare_documents(xml, docling)

    assert comp.n_tables_xml == comp.n_tables_docling == 1
    assert len(comp.table_comparisons) == 1
    tc = comp.table_comparisons[0]
    assert tc.xml_cell_tokens == tc.docling_cell_tokens == tc.shared_cell_tokens
    assert tc.shared_cell_tokens > 0


# ---------------------------------------------------------------------------
# Per-proxy change detection
# ---------------------------------------------------------------------------


def test_docling_dropped_a_table_truncates_pairing() -> None:
    """XML has 2 tables, docling has 1 → matched pair count = 1.

    The pairing is by document order; whether docling "dropped table 1
    or table 2" is unknowable from numbers alone. The harness's
    contract is just that ``len(table_comparisons) == min(xml, docling)``
    so the gap is visible.
    """
    xml = _xml_doc(blocks=(_xml_table(id_='T1'), _xml_table(id_='T2')))
    docling = _docling_doc(blocks=(_docling_table(id_='T1'),))

    comp = compare_documents(xml, docling)

    assert comp.n_tables_xml == 2
    assert comp.n_tables_docling == 1
    assert len(comp.table_comparisons) == 1


def test_docling_misread_cells_reduces_recall() -> None:
    """Same tokens missing on docling side → ``shared < xml_cell_tokens``.

    The XML row ``('Liver', '589')`` lands in both routes; the second
    XML row ``('Kidney', '742')`` is missing from docling. Recall
    should therefore be roughly half (modulo header-row tokens that
    survive on both sides).
    """
    xml_cells = (('Tissue', 'T1'), ('Liver', '589'), ('Kidney', '742'))
    docling_cells = (('Tissue', 'T1'), ('Liver', '589'))
    xml = _xml_doc(blocks=(_xml_table(id_='T1', cells=xml_cells),))
    docling = _docling_doc(blocks=(_docling_table(id_='T1', cells=docling_cells),))

    comp = compare_documents(xml, docling)

    tc = comp.table_comparisons[0]
    assert tc.shared_cell_tokens < tc.xml_cell_tokens
    # ``Kidney`` + ``742`` lost = exactly 2 tokens missing.
    assert tc.xml_cell_tokens - tc.shared_cell_tokens == 2


def test_docling_extra_tokens_dont_inflate_shared() -> None:
    """``shared`` is bounded by the multiset *intersection*, not the union.

    If docling reads phantom rows that XML doesn't have, those extra
    tokens count toward ``docling_cell_tokens`` but never toward
    ``shared_cell_tokens``. Precision (``shared / docling_tokens``)
    therefore drops while recall (``shared / xml_tokens``) holds steady.
    """
    xml_cells = (('Liver', '589'),)
    # docling hallucinated an extra row from a layout misread.
    docling_cells = (('Liver', '589'), ('Phantom', '999'))
    xml = _xml_doc(blocks=(_xml_table(id_='T1', cells=xml_cells),))
    docling = _docling_doc(blocks=(_docling_table(id_='T1', cells=docling_cells),))

    comp = compare_documents(xml, docling)

    tc = comp.table_comparisons[0]
    assert tc.shared_cell_tokens == tc.xml_cell_tokens
    assert tc.docling_cell_tokens > tc.xml_cell_tokens


def test_token_multiset_counts_repeats() -> None:
    """Repeated tokens contribute with multiplicity to the multiset.

    This pins that we use ``Counter`` semantics, not set semantics —
    a tabular column of repeated values (``'1.5 T'`` ten times) reports
    a token count of ~20, not 2. Without this, a docling output that
    dropped one of those rows would still show 100% recall.
    """
    # Repeats the same row 3x to make the multiset effect visible.
    xml_cells = (('1.5', 'T'), ('1.5', 'T'), ('1.5', 'T'))
    docling_cells = (('1.5', 'T'),)
    xml = _xml_doc(blocks=(_xml_table(id_='T1', cells=xml_cells),))
    docling = _docling_doc(blocks=(_docling_table(id_='T1', cells=docling_cells),))

    comp = compare_documents(xml, docling)

    tc = comp.table_comparisons[0]
    assert tc.xml_cell_tokens == 6  # 3 rows * 2 tokens
    assert tc.docling_cell_tokens == 2
    assert tc.shared_cell_tokens == 2


def test_section_paths_in_first_encounter_order() -> None:
    """``section_paths_xml`` / ``_docling`` preserve first-encounter order.

    Order is itself a regression signal — if docling moves Methods
    after Results, the set-overlap count is unchanged but the per-side
    sequences differ.
    """
    xml = _xml_doc(
        blocks=(
            _xml_text(text='intro', section_path=('Introduction',)),
            _xml_text(text='m1', section_path=('Methods',)),
            _xml_text(text='m2', section_path=('Methods',)),  # dup of preceding
            _xml_text(text='r1', section_path=('Results',)),
        )
    )
    docling = _docling_doc(
        blocks=(
            _docling_text(text='intro', section_path=('Introduction',)),
            _docling_text(text='r1', section_path=('Results',)),
            _docling_text(text='m1', section_path=('Methods',)),  # swapped order
        )
    )

    comp = compare_documents(xml, docling)

    assert comp.section_paths_xml == (
        ('Introduction',),
        ('Methods',),
        ('Results',),
    )
    assert comp.section_paths_docling == (
        ('Introduction',),
        ('Results',),
        ('Methods',),
    )
    # Set overlap is unchanged at 3; the *order* drift is visible to a
    # caller that compares the per-side tuples element-wise.
    assert comp.shared_section_paths == 3


def test_reference_count_diff() -> None:
    """Reference counts surface verbatim — coarse but useful pre-citation-pass."""
    xml = _xml_doc(
        references=(
            Reference(id='R1', raw_text='ref 1'),
            Reference(id='R2', raw_text='ref 2'),
            Reference(id='R3', raw_text='ref 3'),
        )
    )
    docling = _docling_doc(
        references=(
            Reference(id='auto-1', raw_text='ref 1'),
            Reference(id='auto-2', raw_text='ref 2'),
        )
    )

    comp = compare_documents(xml, docling)

    assert comp.n_references_xml == 3
    assert comp.n_references_docling == 2


# ---------------------------------------------------------------------------
# Edge cases on the stub
# ---------------------------------------------------------------------------


def test_empty_docling_against_real_xml() -> None:
    """The slice-1 stub case: real XML reference vs empty docling output.

    Pins what the harness reports today, so when slice 3 lands and the
    docling adapter starts producing real blocks, every number here
    should move *up* — a regression that drops one back to zero is
    obvious.
    """
    xml = _xml_doc(
        blocks=(_xml_table(id_='T1'),),
        references=(Reference(id='R1', raw_text='r'),),
    )
    docling = _docling_doc()  # empty, as the stub returns

    comp = compare_documents(xml, docling)

    assert comp.n_tables_xml == 1
    assert comp.n_tables_docling == 0
    assert comp.table_comparisons == ()
    assert comp.n_references_xml == 1
    assert comp.n_references_docling == 0
    assert comp.shared_section_paths == 0  # docling has no blocks → no paths


# ---------------------------------------------------------------------------
# Directionality
# ---------------------------------------------------------------------------


def test_rejects_swapped_arguments_xml_side_is_docling() -> None:
    """Passing a docling document as the XML reference is loud, not silent.

    The diff's directionality is load-bearing — XML is the *trusted
    reference* and the numbers (especially recall) only mean what the
    name says if the inputs are on the right side.
    """
    xml = _xml_doc()
    docling = _docling_doc()

    with pytest.raises(ValueError, match=r'xml_doc\.route'):
        compare_documents(docling, docling)
    with pytest.raises(ValueError, match=r'docling_doc\.route'):
        compare_documents(xml, xml)


# ---------------------------------------------------------------------------
# Report formatter
# ---------------------------------------------------------------------------


def test_report_includes_recall_and_handles_empty_xml_cells() -> None:
    """``format_comparison_report`` survives degenerate inputs and shows recall.

    A zero-token XML cell multiset would otherwise inject a
    ZeroDivisionError into the report — pinning the ``n/a`` fallback.
    A non-degenerate row appears with its computed recall.
    """
    cells_full = (('Liver', '589'),)
    cells_empty = (('', ''),)  # whitespace-only cells → zero tokens
    xml = _xml_doc(
        blocks=(
            _xml_table(id_='T1', cells=cells_full),
            _xml_table(id_='T2', cells=cells_empty),
        )
    )
    docling = _docling_doc(
        blocks=(
            _docling_table(id_='T1', cells=cells_full),
            _docling_table(id_='T2', cells=cells_empty),
        )
    )

    comp = compare_documents(xml, docling)
    report = format_comparison_report([('10.example/abc', comp)])

    assert '10.example/abc' in report
    assert 'recall=1.00' in report
    assert 'recall=n/a' in report
