"""Coverage for the docling → Document adapter (E0.5b).

Two layers:

- **Empty-document contract** (slices 1-2 still relevant) — the
  adapter on a minimal-shape :class:`DoclingDocument` payload pins the
  route discriminator, the docling-route baseline ``completeness``, and
  cattrs round-trip on the empty case.

- **Structural transform** (slice 5, the real coverage) — small
  hand-built :class:`DoclingDocument` instances are built using
  docling's own public API, dumped to dict, and pushed through
  :func:`normalize_docling_document`. Each test exercises one slice-3
  invariant (section_path stack, prov-to-Provenance, span-preserved
  cells, FormulaItem→EquationBlock, REFERENCE-tagged items become
  :class:`Reference`s, title extraction), plus the slice-4 inline-ref
  regex on a TextBlock.

Adversarial fixtures from discussion §2.2 / §2.3 (cell transposition,
out-of-order body, duplicate self_refs, skipped heading levels beyond
"don't fabricate parents") are explicitly out of scope per the slice
answer; the cross-format differential harness (``normalize/diff.py``)
is the quality net for that bucket once the corpus has dual-format
papers to feed it.
"""

from typing import Any

import pytest

from litspectraits.normalize import (
    Document,
    EquationBlock,
    FigureBlock,
    Reference,
    TableBlock,
    TextBlock,
    converter,
    normalize_docling_document,
)

# ---------------------------------------------------------------------------
# Empty-document fixtures and tests (slices 1-2)
# ---------------------------------------------------------------------------


def _docling_dict_minimal() -> dict[str, Any]:
    """A trivially-shaped docling export payload.

    Slice-3's real implementation re-hydrates this via
    ``DoclingDocument.model_validate``; an empty body/texts/tables/
    pictures payload yields an empty Document. The contents are the
    minimum the pydantic validator accepts.
    """
    return {
        'schema_name': 'DoclingDocument',
        'version': '1.0.0',
        'name': 'stub',
        'origin': None,
        'furniture': {'self_ref': '#/furniture', 'children': [], 'content_layer': 'furniture'},
        'body': {'self_ref': '#/body', 'children': [], 'content_layer': 'body'},
        'groups': [],
        'texts': [],
        'pictures': [],
        'tables': [],
        'key_value_items': [],
        'form_items': [],
        'pages': {},
    }


def test_empty_payload_yields_empty_route_tagged_document() -> None:
    """Empty :class:`DoclingDocument` → empty but route-tagged Document.

    The route discriminator is the most load-bearing field on the
    docling Document (the route-purity invariant in
    ``Document.__attrs_post_init__`` keys on it); pinning it separately
    from the empty-container assertions means a regression that drops
    ``route`` to its default surfaces here rather than silently miswiring
    downstream consumers.
    """
    doc = normalize_docling_document(_docling_dict_minimal())

    assert doc.route == 'docling'
    assert doc.blocks == ()
    assert doc.references == ()
    assert doc.title is None
    assert doc.abstract is None


def test_empty_completeness_is_docling_baseline() -> None:
    """The baseline values from discussion §1.5 — pinned so slice 3
    cannot regress them to the XML-route shape.

    ``table_source='tableformer'`` is the load-bearing one: a downstream
    consumer using this flag to decide "trust the publisher markup or
    not" must not be lied to.
    """
    doc = normalize_docling_document(_docling_dict_minimal())

    assert doc.completeness.has_structured_refs is False
    assert doc.completeness.has_inline_ref_ids is False
    assert doc.completeness.has_equations is False
    assert doc.completeness.table_source == 'tableformer'


def test_empty_document_round_trips_through_converter() -> None:
    """Empty Document survives cattrs round-trip.

    The non-empty case is covered by ``test_round_trip_on_built_document``
    below; this exists so a slice that breaks ``Completeness`` un/structure
    fails here too (the Block-union hook is not exercised on empty blocks,
    so an unstructure regression on ``Completeness`` would otherwise hide).
    """
    doc = normalize_docling_document(_docling_dict_minimal())

    payload = converter.unstructure(doc)
    rebuilt = converter.structure(payload, Document)

    assert rebuilt == doc


# ---------------------------------------------------------------------------
# Real-document fixtures — built using docling's public API, dumped to
# dict, fed to the adapter. Same shape the adapter sees from disk.
# ---------------------------------------------------------------------------


def _docling_module():
    """Pull the docling-core types in one place, mirroring the adapter's lazy import."""
    from docling_core.types.doc.base import BoundingBox  # pyright: ignore[reportMissingImports]
    from docling_core.types.doc.document import (  # pyright: ignore[reportMissingImports]
        DoclingDocument,
        ProvenanceItem,
        TableCell,
        TableData,
    )
    from docling_core.types.doc.labels import DocItemLabel  # pyright: ignore[reportMissingImports]

    return {
        'DoclingDocument': DoclingDocument,
        'ProvenanceItem': ProvenanceItem,
        'TableCell': TableCell,
        'TableData': TableData,
        'BoundingBox': BoundingBox,
        'DocItemLabel': DocItemLabel,
    }


def _prov(*, page: int = 1, charspan: tuple[int, int] = (0, 10)) -> Any:
    """One-line :class:`ProvenanceItem` builder.

    Every content item the adapter accepts needs a provenance — the
    ``no-provenance → ValueError`` case has its own test below; every
    other test calls into here so the bbox / page numbers stay legible
    against the assertions.
    """
    mod = _docling_module()
    bbox = mod['BoundingBox'](l=10.0, t=20.0, r=110.0, b=40.0)
    return mod['ProvenanceItem'](page_no=page, bbox=bbox, charspan=charspan)


# ---------------------------------------------------------------------------
# Section path
# ---------------------------------------------------------------------------


def test_section_header_populates_section_path_on_subsequent_block() -> None:
    """A ``SectionHeaderItem`` enters the section stack; subsequent blocks see it.

    The heading does *not* itself become a Block — it only influences
    the ``section_path`` of the items that follow. This matches the
    extractor's ``n_section_headers`` / ``n_text_blocks`` discipline
    (counts are kept disjoint in ``extract/pdf.py:_count_structure``).
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_heading(text='Methods', level=1, prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='We did things.', prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    assert len(normalised.blocks) == 1
    block = normalised.blocks[0]
    assert isinstance(block, TextBlock)
    assert block.text == 'We did things.'
    assert block.section_path == ('Methods',)


def test_skipped_heading_levels_dont_fabricate_a_parent() -> None:
    """An h1 followed by an h3 leaves ``section_path`` exactly two long.

    The discussion-doc commitment is verbatim-anchor compatibility:
    inserting a synthetic h2 would put a string in ``section_path``
    that does not appear anywhere in the source PDF, breaking that
    invariant. A two-element ``section_path`` reading
    ``('Big', 'Small')`` is the honest shape.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_heading(text='Big', level=1, prov=_prov())
    doc.add_heading(text='Small', level=3, prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='inside small.', prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    block = normalised.blocks[0]
    assert block.section_path == ('Big', 'Small')


def test_sibling_heading_closes_the_previous_section() -> None:
    """Two h1s in a row → blocks under each see only their own parent.

    Failure mode this catches: a stack that only pushes without popping
    on equal-level entry would leave ``section_path`` accumulating all
    previous siblings.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_heading(text='Methods', level=1, prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='m.', prov=_prov())
    doc.add_heading(text='Results', level=1, prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='r.', prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    assert normalised.blocks[0].section_path == ('Methods',)
    assert normalised.blocks[1].section_path == ('Results',)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_forwards_page_bbox_and_charspan() -> None:
    """``ProvenanceItem`` → :class:`Provenance` is verbatim, not derived."""
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_text(
        label=mod['DocItemLabel'].TEXT,
        text='See [1] for context.',
        prov=mod['ProvenanceItem'](
            page_no=3,
            bbox=mod['BoundingBox'](l=1.0, t=2.0, r=3.0, b=4.0),
            charspan=(100, 120),
        ),
    )
    normalised = normalize_docling_document(doc.export_to_dict())

    prov = normalised.blocks[0].provenance
    assert prov.route == 'docling'
    assert prov.xpath is None
    assert prov.page == 3
    assert prov.bbox is not None
    assert (prov.bbox.x0, prov.bbox.y0, prov.bbox.x1, prov.bbox.y1) == (1.0, 2.0, 3.0, 4.0)
    assert prov.page_char_range is not None
    assert (prov.page_char_range.start, prov.page_char_range.end) == (100, 120)


def test_no_provenance_raises() -> None:
    """A content item without a ``prov`` entry surfaces loudly, not silently.

    Discussion-doc / CLAUDE.md "fail loudly" — refusing to invent a
    page anchor is the honest behaviour; silently dropping the item
    (or using ``page=0``) would mask a real docling regression.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    # No prov= passed; docling emits prov=[].
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='floating text')

    with pytest.raises(ValueError, match='no provenance'):
        normalize_docling_document(doc.export_to_dict())


# ---------------------------------------------------------------------------
# Tables — §2.3 #1 risk surface
# ---------------------------------------------------------------------------


def _table_data_simple() -> Any:
    """Plain 2-row 2-col grid with a header row and no spans.

    Concrete data: ``Tissue | T1`` over ``Liver | 589``. The headers
    pin the ``th`` / ``td`` mapping; the values pin verbatim cell text
    propagation.
    """
    mod = _docling_module()
    cells = [
        mod['TableCell'](
            text='Tissue',
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
            column_header=True,
        ),
        mod['TableCell'](
            text='T1',
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=1,
            end_col_offset_idx=2,
            column_header=True,
        ),
        mod['TableCell'](
            text='Liver',
            start_row_offset_idx=1,
            end_row_offset_idx=2,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
        ),
        mod['TableCell'](
            text='589',
            start_row_offset_idx=1,
            end_row_offset_idx=2,
            start_col_offset_idx=1,
            end_col_offset_idx=2,
        ),
    ]
    return mod['TableData'](table_cells=cells, num_rows=2, num_cols=2)


def test_table_block_preserves_cell_text_and_header_kind() -> None:
    """2x2 simple table -> 2 rows of 2 cells, headers tagged ``th``.

    The §2.3 cell-transposition smoke check: every cell's text appears
    at its claimed (row, col) coordinate, and no cell text leaks into a
    neighbour. With four distinct strings, any swap is immediately
    visible.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_table(data=_table_data_simple(), prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    tables = [b for b in normalised.blocks if isinstance(b, TableBlock)]
    assert len(tables) == 1
    table = tables[0]
    assert table.n_rows == 2
    assert table.n_cols == 2
    assert len(table.cells) == 2
    assert [c.text for c in table.cells[0]] == ['Tissue', 'T1']
    assert [c.text for c in table.cells[1]] == ['Liver', '589']
    assert [c.kind for c in table.cells[0]] == ['th', 'th']
    assert [c.kind for c in table.cells[1]] == ['td', 'td']


def test_table_block_preserves_spans_without_expansion() -> None:
    """A spanned cell records ``rowspan`` / ``colspan`` and is not duplicated.

    Anchor row carries the spanned cell; the row it spans into does
    *not* re-emit it. Same semantics as the XML adapter; pinned here
    because expansion would silently double-count the cell's value
    tokens against the cross-format diff harness.
    """
    mod = _docling_module()
    cells = [
        # A 2-row spanning header cell at (0, 0)
        mod['TableCell'](
            text='Tissue',
            start_row_offset_idx=0,
            end_row_offset_idx=2,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
            column_header=True,
            row_span=2,
            col_span=1,
        ),
        mod['TableCell'](
            text='T1 1.5T',
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=1,
            end_col_offset_idx=2,
            column_header=True,
        ),
        mod['TableCell'](
            text='T1 3T',
            start_row_offset_idx=1,
            end_row_offset_idx=2,
            start_col_offset_idx=1,
            end_col_offset_idx=2,
            column_header=True,
        ),
    ]
    data = mod['TableData'](table_cells=cells, num_rows=2, num_cols=2)

    doc = mod['DoclingDocument'](name='t')
    doc.add_table(data=data, prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    table = next(b for b in normalised.blocks if isinstance(b, TableBlock))
    # Row 0 carries 'Tissue' (rowspan=2) plus 'T1 1.5T'; row 1 carries only 'T1 3T'.
    assert [c.text for c in table.cells[0]] == ['Tissue', 'T1 1.5T']
    assert table.cells[0][0].rowspan == 2
    assert [c.text for c in table.cells[1]] == ['T1 3T']


def test_table_without_captions_yields_caption_none() -> None:
    """No caption attached → :attr:`TableBlock.caption` stays ``None``.

    Honest "no caption present" signal — mirrors the figure variant.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_table(data=_table_data_simple(), prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    table = next(b for b in normalised.blocks if isinstance(b, TableBlock))
    assert table.caption is None


def test_table_with_caption_populates_table_block_caption() -> None:
    """Caption text attached via ``TableItem.captions[0]`` lands on the TableBlock.

    Same wiring as figures: a CAPTION-labelled :class:`TextItem` plus a
    :class:`RefItem` on the table. Resolving the ref and forwarding its
    text is the whole transform.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    caption_item = doc.add_text(
        label=mod['DocItemLabel'].CAPTION,
        text='Table 1. Relaxometric values for the liver phantom.',
        prov=_prov(),
    )
    doc.add_table(data=_table_data_simple(), prov=_prov(), caption=caption_item)

    normalised = normalize_docling_document(doc.export_to_dict())

    table = next(b for b in normalised.blocks if isinstance(b, TableBlock))
    assert table.caption == 'Table 1. Relaxometric values for the liver phantom.'
    # Caption text must not double-emit as a TextBlock.
    text_blocks = [b for b in normalised.blocks if isinstance(b, TextBlock)]
    assert not any(
        'Relaxometric values' in tb.text for tb in text_blocks
    ), 'CAPTION-labelled item should be out-of-band, not a TextBlock'


def test_multi_caption_picture_takes_the_first() -> None:
    """When ``captions`` carries multiple entries, the adapter forwards [0] only.

    Primary-caption convention. The alternative (concatenate) was
    considered and rejected: it loses the natural primary /
    supplementary boundary, and the multi-caption case is rare enough
    that a downstream consumer who needs both can read raw docling.
    """
    from docling_core.types.doc.document import RefItem  # pyright: ignore[reportMissingImports]

    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    primary = doc.add_text(
        label=mod['DocItemLabel'].CAPTION,
        text='Figure 2. Primary caption.',
        prov=_prov(),
    )
    secondary = doc.add_text(
        label=mod['DocItemLabel'].CAPTION,
        text='Figure 2 (supp). Secondary caption.',
        prov=_prov(),
    )
    pic = doc.add_picture(prov=_prov(), caption=primary)
    # Public API attaches one caption; the multi-caption shape is real
    # in some Elsevier-converted papers — exercise it by appending a
    # second RefItem directly. ``cref`` is the Python-side field name
    # (per ``RefItem.model_fields``); pydantic also exposes ``$ref`` as
    # the JSON alias which is what pyright sees.
    pic.captions.append(RefItem(cref=secondary.self_ref))  # pyright: ignore[reportCallIssue]

    normalised = normalize_docling_document(doc.export_to_dict())

    figure = next(b for b in normalised.blocks if isinstance(b, FigureBlock))
    assert figure.caption == 'Figure 2. Primary caption.'


# ---------------------------------------------------------------------------
# FormulaItem → EquationBlock
# ---------------------------------------------------------------------------


def test_formula_item_becomes_equation_block_with_latex_payload() -> None:
    """A :class:`FormulaItem` lands as :class:`EquationBlock(latex=…)` on the docling route.

    The :class:`EquationBlock.__attrs_post_init__` invariant requires
    exactly one of ``mathml`` / ``latex`` to be set, and route='docling'
    forbids ``mathml``. This test trips both invariants if the dispatch
    is wrong: a TextBlock would lose the FormulaItem entirely, and a
    misconstructed EquationBlock would raise at construction.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_text(label=mod['DocItemLabel'].FORMULA, text='S = S_0 e^{-TE/T_2}', prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    equations = [b for b in normalised.blocks if isinstance(b, EquationBlock)]
    assert len(equations) == 1
    eq = equations[0]
    assert eq.latex == 'S = S_0 e^{-TE/T_2}'
    assert eq.mathml is None
    assert eq.text == eq.latex  # what the anchor gate keys on
    assert normalised.completeness.has_equations is True


def test_no_formula_items_means_has_equations_false() -> None:
    """``has_equations`` is *derived* from emitted blocks, not the input shape.

    A paper with no display equations honestly reports False — the flag
    is conservative, not optimistic.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='prose only', prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    assert normalised.completeness.has_equations is False


# ---------------------------------------------------------------------------
# Pictures → FigureBlock
# ---------------------------------------------------------------------------


def test_picture_item_becomes_figure_block_without_caption() -> None:
    """:class:`PictureItem` with no captions → :class:`FigureBlock` with ``caption=None``.

    Honest "no caption present" signal — the source PDF had no caption
    attached to this picture, so the normalised shape says so. A
    regression that silently invents a caption from nearby text would
    fail this.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_picture(prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    figures = [b for b in normalised.blocks if isinstance(b, FigureBlock)]
    assert len(figures) == 1
    assert figures[0].caption is None
    assert figures[0].provenance.route == 'docling'


def test_picture_item_with_caption_populates_figure_block_caption() -> None:
    """Caption text attached via ``PictureItem.captions[0]`` lands on the FigureBlock.

    Wiring: docling stores the caption as a separate CAPTION-labelled
    :class:`TextItem`; the picture's ``captions`` list holds a
    :class:`RefItem` whose ``cref`` resolves back to that text. The
    adapter calls :meth:`RefItem.resolve` and forwards the resolved
    text verbatim. Also verifies that the caption-labelled text item
    does *not* additionally land in ``Document.blocks`` (it's
    out-of-band, like REFERENCE-labelled items).
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    caption_item = doc.add_text(
        label=mod['DocItemLabel'].CAPTION,
        text='Figure 1. T1 maps of white matter.',
        prov=_prov(),
    )
    doc.add_picture(prov=_prov(), caption=caption_item)

    normalised = normalize_docling_document(doc.export_to_dict())

    figures = [b for b in normalised.blocks if isinstance(b, FigureBlock)]
    assert len(figures) == 1
    assert figures[0].caption == 'Figure 1. T1 maps of white matter.'
    # Caption text item must not have leaked into the body block stream.
    text_blocks = [b for b in normalised.blocks if isinstance(b, TextBlock)]
    assert not any(
        'T1 maps of white matter' in tb.text for tb in text_blocks
    ), 'CAPTION-labelled item should be out-of-band, not a TextBlock'


# ---------------------------------------------------------------------------
# References — REFERENCE-labelled text items
# ---------------------------------------------------------------------------


def test_reference_labelled_text_items_become_references_not_blocks() -> None:
    """REFERENCE-labelled items skip the block stream and land in ``references``.

    Two parallel assertions: (a) ``Document.blocks`` does not contain a
    TextBlock with the reference text — a regression here would inflate
    the no-text-loss invariant *and* the diff harness's section-token
    overlap; (b) ``Document.references`` carries the entry as
    ``raw_text``-only.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='Body sentence.', prov=_prov())
    doc.add_text(
        label=mod['DocItemLabel'].REFERENCE,
        text='Smith J. 2019. A study. JMRI.',
        prov=_prov(),
    )
    normalised = normalize_docling_document(doc.export_to_dict())

    body_texts = [b.text for b in normalised.blocks if isinstance(b, TextBlock)]
    assert body_texts == ['Body sentence.']
    assert len(normalised.references) == 1
    ref = normalised.references[0]
    assert ref.raw_text == 'Smith J. 2019. A study. JMRI.'
    assert ref.parsed is None  # discussion §1.5: docling route never auto-parses
    assert ref.resolved is None


# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------


def test_title_item_populates_document_title() -> None:
    """First :class:`TitleItem` text wins for ``Document.title``.

    Most papers have exactly one TitleItem; a paper with two would
    deserve a hand-inspection anyway, and "first wins" is the
    simplest tie-breaker the discussion doc is silent on.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_title(text='A study of liver T1', prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='Body.', prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    assert normalised.title == 'A study of liver T1'


# ---------------------------------------------------------------------------
# Inline-ref marker regex (slice 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('text', 'expected_markers'),
    [
        ('Liver T1 of 589 ms [3].', ['[3]']),
        ('Reported in [3, 5, 7] consistently.', ['[3, 5, 7]']),
        ('Earlier work [3-5] showed the same.', ['[3-5]']),
        # En-dash (U+2013) is the typesetter variant for ranges; the
        # adapter's regex matches it, so we test that here.
        ('En-dash variant [3–5] is common in proofs.', ['[3–5]']),  # noqa: RUF001
        ('Two markers [3] and [7] in one sentence.', ['[3]', '[7]']),
        ('No markers here.', []),
    ],
    ids=[
        'single',
        'comma-separated',
        'hyphen-range',
        'en-dash-range',
        'two-markers',
        'no-markers',
    ],
)
def test_inline_ref_regex_anchors_bracket_number_markers(
    text: str, expected_markers: list[str]
) -> None:
    """The slice-4 regex anchors bracket-number markers verbatim.

    The ``surface_form`` assertion is what the anchor gate later keys
    on — it must equal ``block.text[start:end]`` byte-for-byte. The
    ``ref_id=None`` / ``ref_id_source=None`` pair is the honesty
    invariant: anchoring is not binding.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_text(label=mod['DocItemLabel'].TEXT, text=text, prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    block = normalised.blocks[0]
    assert isinstance(block, TextBlock)
    markers = [ref.surface_form for ref in block.inline_refs]
    assert markers == expected_markers
    for ref in block.inline_refs:
        assert ref.ref_id is None
        assert ref.ref_id_source is None
        # The byte-for-byte equivalence the anchor gate depends on.
        assert block.text[ref.char_range.start : ref.char_range.end] == ref.surface_form


def test_inline_ref_does_not_flag_has_inline_ref_ids() -> None:
    """``has_inline_ref_ids`` stays False until a marker-match pass binds them.

    Anchored-but-unbound markers (this slice's state) are not the same
    as ref-id-resolved markers (the citation-parsing follow-on's
    state). The flag must track the latter, not the former; otherwise
    downstream consumers using ``has_inline_ref_ids`` to gate
    citation-aware logic would be misled.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='See [3].', prov=_prov())
    normalised = normalize_docling_document(doc.export_to_dict())

    assert normalised.completeness.has_inline_ref_ids is False


# ---------------------------------------------------------------------------
# End-to-end shape: round-trip through the cattrs converter
# ---------------------------------------------------------------------------


def test_round_trip_on_built_document() -> None:
    """A multi-block Document survives cattrs un/structure.

    Exercises the Block tagged-union hook on every concrete variant
    that the adapter emits today: TextBlock, TableBlock, FigureBlock,
    EquationBlock. The dict-level diff is the strongest equality check
    we have — ``Document.__eq__`` already cascades into ``Block`` via
    attrs.
    """
    mod = _docling_module()
    doc = mod['DoclingDocument'](name='t')
    doc.add_title(text='A title', prov=_prov())
    doc.add_heading(text='Methods', level=1, prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].TEXT, text='Prose with [3].', prov=_prov())
    doc.add_table(data=_table_data_simple(), prov=_prov())
    doc.add_picture(prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].FORMULA, text='E=mc^2', prov=_prov())
    doc.add_text(label=mod['DocItemLabel'].REFERENCE, text='Smith et al.', prov=_prov())

    normalised = normalize_docling_document(doc.export_to_dict())
    payload = converter.unstructure(normalised)
    rebuilt = converter.structure(payload, Document)

    assert rebuilt == normalised
    # And the document is non-trivially populated — the equality check is
    # only meaningful if there's actually something there to compare.
    assert any(isinstance(b, TextBlock) for b in normalised.blocks)
    assert any(isinstance(b, TableBlock) for b in normalised.blocks)
    assert any(isinstance(b, FigureBlock) for b in normalised.blocks)
    assert any(isinstance(b, EquationBlock) for b in normalised.blocks)
    assert len(normalised.references) == 1
    assert isinstance(normalised.references[0], Reference)
