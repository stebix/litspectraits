"""docling → :class:`~litspectraits.normalize.Document` adapter (E0.5b).

Consumes the verbatim ``DoclingDocument.export_to_dict()`` payload that
:func:`litspectraits.extract.pdf.extract_pdf` writes to
``documents/sha256/<aa>/<sha>/document.json``, re-hydrates it via
:meth:`docling_core.types.doc.DoclingDocument.model_validate`, and walks
its body tree (in docling's own reading order, via
:meth:`DoclingDocument.iterate_items`) to produce a :class:`Document`
with ``route='docling'``.

Why ``iterate_items`` over a hand-rolled body walk
--------------------------------------------------
Discussion-doc §2.3 names **table cell transposition** as the #1
dangerous bug class — but that bug lives in :class:`TableData` span
resolution, not in body-tree traversal. Body-tree reading-order
anomalies are a docling-side concern (Q2 in §2.1), not a normaliser-side
concern (Q1). Re-implementing :meth:`DoclingDocument.iterate_items` so
we could detect anomalies that docling itself produced would mean
solving a problem in the wrong place — and trading less code for more.
Discussion-doc §1.5 commits us to "body-tree order → block order";
deferring traversal to docling's own iterator preserves that
commitment faithfully.

What this adapter does NOT do (E0.5b explicit non-goals)
--------------------------------------------------------
Aligned with the scope chosen for the first E0.5b cut:

- **No abstract extraction.** docling tags Title items but has no
  reliable abstract marker for the corpus's PDFs; the
  abstract-detection heuristic (look for a ``section_header`` whose
  text matches ``r'^abstract'i``, take the subsequent paragraph) is a
  follow-on, not a structural transform.
- **No inline-ref char ranges on docling text.** Marker regex on
  ``TextBlock.text`` is slice 4, kept separate so the structural
  transform can be reviewed and tested independently.
- **No table caption / footnote ``inline_refs``.** Same reasoning as
  the XML adapter (E0.5a).

The honesty discipline from §1.5
--------------------------------
- :class:`Provenance` carries ``route='docling'`` and a populated
  ``page`` for every block; the route-conditional invariant in
  :meth:`Provenance.__attrs_post_init__` enforces this at construction.
- :class:`Reference` instances carry only ``raw_text`` (no
  :class:`ParsedReference`); structured reference fields require a
  separate citation-parsing pass (GROBID / anystyle / refextract /
  LLM), and emitting a partial / heuristic :class:`ParsedReference`
  here would make :attr:`Completeness.has_structured_refs` lie.
- :class:`InlineRef.ref_id` and ``.ref_id_source`` are both ``None`` on
  the docling route until the marker-match pass (slice 4) populates
  them — and even after slice 4, ``ref_id_source`` stays ``None`` until
  a follow-on marker→reference binding pass runs.
- :class:`Completeness` reports the docling-route baseline
  (``has_structured_refs=False``, ``has_inline_ref_ids=False``,
  ``has_equations`` derived from emitted blocks,
  ``table_source='tableformer'``).
"""

import re
from dataclasses import dataclass
from typing import Any, Final, cast

from litspectraits.normalize.models import (
    BBox,
    Block,
    CharRange,
    Completeness,
    Document,
    EquationBlock,
    FigureBlock,
    Provenance,
    Reference,
    TableBlock,
    TableCell,
    TextBlock,
)

# Reference markers in docling text we want to anchor as InlineRef.
#
# Bracketed-number form covers ``[12]``, ``[12, 14]``, ``[12, 14, 16]``,
# and ``[12-14]`` ranges including the en-dash variant that journal
# typesetters often substitute. This is the dominant marker style in
# radiology / MR-physics journals (Wiley among them), so it is the only
# form slice 4 anchors today. Author-year markers
# (``Smith et al., 2019``) are a regex-arms-race away from clean and
# get their own follow-on if the marker-match pass ever needs them.
_INLINE_REF_PATTERN: Final = re.compile(r'\[\d+(?:\s*[,\-–]\s*\d+)*\]')  # noqa: RUF001


@dataclass(frozen=True)
class _DoclingSdk:
    """Bundle of lazily-imported docling-core types.

    Mirrors the ``_DoclingAdapter`` pattern in :mod:`litspectraits.extract.pdf`:
    keep every import inside a single helper so test code can substitute
    or skip the load. ``DocItemLabel`` is the only non-class member, kept
    here for one-stop access to dispatch logic in the walk.
    """

    DoclingDocument: Any
    NodeItem: Any
    TextItem: Any
    TableItem: Any
    PictureItem: Any
    SectionHeaderItem: Any
    TitleItem: Any
    FormulaItem: Any
    DocItemLabel: Any


def _load_docling_sdk() -> _DoclingSdk:
    """Lazy-load the docling-core types the adapter dispatches on.

    Kept lazy so a caller that only ever touches the XML route does not
    need ``[extract]`` installed. An :class:`ImportError` here surfaces
    untouched — the discussion doc's ``[extract]``-optional posture
    treats "you called the docling adapter without docling installed"
    as a configuration error, and Python's own ``ImportError`` is the
    canonical loud signal for that.
    """
    from docling_core.types.doc.document import (  # pyright: ignore[reportMissingImports]
        DoclingDocument,
        FormulaItem,
        NodeItem,
        PictureItem,
        SectionHeaderItem,
        TableItem,
        TextItem,
        TitleItem,
    )
    from docling_core.types.doc.labels import DocItemLabel  # pyright: ignore[reportMissingImports]

    return _DoclingSdk(
        DoclingDocument=DoclingDocument,
        NodeItem=NodeItem,
        TextItem=TextItem,
        TableItem=TableItem,
        PictureItem=PictureItem,
        SectionHeaderItem=SectionHeaderItem,
        TitleItem=TitleItem,
        FormulaItem=FormulaItem,
        DocItemLabel=DocItemLabel,
    )


def normalize_docling_document(document: dict[str, Any]) -> Document:
    """Convert a docling ``export_to_dict()`` payload to a :class:`Document`.

    Parameters
    ----------
    document : dict
        The verbatim ``DoclingDocument.export_to_dict()`` payload — the
        on-disk shape of ``documents/sha256/<aa>/<sha>/document.json`` produced by
        :func:`litspectraits.extract.pdf.extract_pdf`. Validated via
        :meth:`DoclingDocument.model_validate`; a malformed payload
        surfaces as :class:`pydantic.ValidationError`, which is the
        loud signal we want.

    Returns
    -------
    Document
        Frozen :class:`Document` with ``route='docling'``. Block order
        matches docling's :meth:`DoclingDocument.iterate_items` reading
        order; section-path stack is rebuilt from the body tree's
        :class:`SectionHeaderItem` levels;
        :attr:`Document.completeness.has_equations` is computed from
        whether any :class:`EquationBlock` instances ended up in
        ``blocks``.

    Raises
    ------
    ImportError
        ``docling-core`` (the ``[extract]`` extra) is not installed.
        Surfaces unmodified from :func:`_load_docling_sdk`.
    pydantic.ValidationError
        ``document`` is not a valid ``DoclingDocument`` shape.
    ValueError
        A content-carrying item (``TextItem`` / ``TableItem`` /
        ``PictureItem`` / ``FormulaItem``) was emitted by docling with
        no :class:`ProvenanceItem` entries. The discussion doc's
        no-text-loss invariant assumes every block has a placeable
        anchor; an item with no provenance has no anchor and we refuse
        to silently invent one.
    """
    sdk = _load_docling_sdk()
    doc = sdk.DoclingDocument.model_validate(document)

    blocks: list[Block] = []
    section_stack: list[tuple[int, str]] = []
    title: str | None = None

    for item, _depth in doc.iterate_items(traverse_pictures=True):
        if isinstance(item, sdk.SectionHeaderItem):
            _push_section_header(section_stack, level=item.level, text=item.text)
            continue

        if isinstance(item, sdk.TitleItem):
            if title is None:
                title = item.text
            # A title also doesn't belong to the block stream; section_path
            # only tracks ``section_header`` levels.
            continue

        section_path = tuple(text for _, text in section_stack)

        block = _item_to_block(item, sdk=sdk, doc=doc, section_path=section_path)
        if block is not None:
            blocks.append(block)

    references = _extract_references(doc, sdk=sdk)
    completeness = Completeness(
        has_structured_refs=False,
        has_inline_ref_ids=False,
        # Derived from what actually landed: ``do_formula_enrichment=True``
        # is on by default in extract/pdf.py, but a paper with no display
        # equations honestly reports False here.
        has_equations=any(isinstance(b, EquationBlock) for b in blocks),
        table_source='tableformer',
    )

    return Document(
        route='docling',
        blocks=tuple(blocks),
        references=references,
        completeness=completeness,
        title=title,
        abstract=None,
    )


# ---------------------------------------------------------------------------
# Section header stack
# ---------------------------------------------------------------------------


def _push_section_header(
    stack: list[tuple[int, str]],
    *,
    level: int,
    text: str,
) -> None:
    """Reconcile a new section header into the open-section stack.

    Pops every entry at ``stack_level >= level`` (entering a new sibling
    or shallower header closes any deeper ones), then pushes the new
    entry. Skipped levels — a ``h1`` followed directly by an ``h3``
    with no ``h2`` between them — are preserved as-is: we do not invent
    a synthetic parent because that would inject text into
    ``section_path`` that does not appear anywhere in the source PDF,
    and the verbatim-anchor gate cannot tolerate that. Downstream
    consumers that need a fully-populated hierarchy can recover one
    from the original ``section_path`` length.
    """
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, text))


# ---------------------------------------------------------------------------
# Per-item dispatch
# ---------------------------------------------------------------------------


def _item_to_block(
    item: Any,
    *,
    sdk: _DoclingSdk,
    doc: Any,
    section_path: tuple[str, ...],
) -> Block | None:
    """Dispatch one docling :class:`NodeItem` to its :class:`Block` shape.

    Returns ``None`` for items that are deliberately not emitted as
    Blocks: caption / footnote text picked up via their parent table /
    figure (today this means dropped, since caption association is a
    deferred follow-on); reference list entries collected separately by
    :func:`_extract_references`; any item kind the adapter does not yet
    handle (lists, code, form items). The first cut prioritises
    "TextItem / TableItem / PictureItem / FormulaItem land correctly"
    over "every node type is mapped"; growing the dispatch is a
    forward-compatible follow-on.

    The :class:`FormulaItem` branch must come before the
    :class:`TextItem` branch because :class:`FormulaItem` *is* a
    :class:`TextItem` subclass; flipping the order would silently
    route formulas to :class:`TextBlock`.
    """
    if isinstance(item, sdk.FormulaItem):
        return _formula_to_equation_block(item, section_path=section_path)

    if isinstance(item, sdk.TextItem):
        # References and captions are handled out-of-band.
        if item.label == sdk.DocItemLabel.REFERENCE:
            return None
        if item.label == sdk.DocItemLabel.CAPTION:
            return None
        return _text_to_text_block(item, section_path=section_path)

    if isinstance(item, sdk.TableItem):
        return _table_to_table_block(item, doc=doc, section_path=section_path)

    if isinstance(item, sdk.PictureItem):
        return _picture_to_figure_block(item, doc=doc, section_path=section_path)

    return None


# ---------------------------------------------------------------------------
# TextBlock
# ---------------------------------------------------------------------------


def _text_to_text_block(item: Any, *, section_path: tuple[str, ...]) -> TextBlock:
    text = item.text or ''
    return TextBlock(
        text=text,
        provenance=_provenance_from(item),
        section_path=cast(tuple[str | None, ...], section_path),
        inline_refs=_find_inline_refs(text),
    )


def _find_inline_refs(text: str) -> tuple:
    """Anchor bracketed-number citation markers as :class:`InlineRef`.

    Slice 4 of E0.5b. ``ref_id`` and ``ref_id_source`` are both
    ``None`` — anchoring the marker at a char range is its own pass;
    binding to a :class:`Reference` is a downstream marker-match pass
    that runs against the back-matter once :func:`_extract_references`
    has produced ids worth matching against.
    """
    from litspectraits.normalize.models import InlineRef

    refs: list[InlineRef] = []
    for match in _INLINE_REF_PATTERN.finditer(text):
        refs.append(
            InlineRef(
                char_range=CharRange(start=match.start(), end=match.end()),
                surface_form=match.group(0),
                ref_id=None,
                ref_id_source=None,
            )
        )
    return tuple(refs)


# ---------------------------------------------------------------------------
# TableBlock — the §2.3 #1 risk surface
# ---------------------------------------------------------------------------


def _table_to_table_block(
    item: Any,
    *,
    doc: Any,
    section_path: tuple[str, ...],
) -> TableBlock:
    """Resolve a docling :class:`TableItem` to a :class:`TableBlock`.

    The cell grid is rebuilt from the flat :attr:`TableData.table_cells`
    list using each cell's ``start_row_offset_idx`` /
    ``start_col_offset_idx`` — docling does not pre-emit a 2D grid, so
    this is the place where a §2.3 transposition bug would hide. Two
    disciplines:

    1. **Each cell lands in exactly one slot.** ``cells[r]`` contains
       the cells anchored at row ``r`` (i.e. ``start_row_offset_idx == r``),
       in ascending ``start_col_offset_idx`` order. Cells that span
       multiple rows do *not* re-appear in subsequent rows — same
       semantics as the XML adapter, where a ``<td rowspan=2>`` shows
       up only in the row that contains its markup.
    2. **Spans are preserved verbatim.** ``rowspan`` and ``colspan``
       come from ``TableCell.row_span`` / ``TableCell.col_span`` with
       no expansion. The discussion-doc commitment is that downstream
       consumers who need a dense matrix can build one themselves; the
       adapter never invents cells.

    Caption resolution: :attr:`TableItem.captions` is a list of
    :class:`RefItem` references; the first one is resolved against
    ``doc`` via :meth:`RefItem.resolve` to recover the referenced
    :class:`TextItem` (typically ``DocItemLabel.CAPTION``-labelled) and
    its text. Multi-caption tables take the *first* — primary-caption
    convention; the rare multi-caption case becomes a downstream concern
    if it surfaces. See :func:`_resolve_caption` for the helper.
    """
    table_data = item.data
    grid_rows = table_data.num_rows
    grid_cols = table_data.num_cols

    # Bucket cells by their anchor row.
    anchored: list[list[Any]] = [[] for _ in range(grid_rows)]
    for cell in table_data.table_cells:
        row = cell.start_row_offset_idx
        if 0 <= row < grid_rows:
            anchored[row].append(cell)
    for row_cells in anchored:
        row_cells.sort(key=lambda c: c.start_col_offset_idx)

    cells: tuple[tuple[TableCell, ...], ...] = tuple(
        tuple(_table_cell_to_attrs(c) for c in row_cells) for row_cells in anchored
    )

    return TableBlock(
        id=item.self_ref,
        label=None,  # docling doesn't carry the "Table N" label literal as a
        # field; the leading "Table 1" string typically appears in
        # the caption when one is present. Caption-driven label
        # recovery is a small follow-on and stays deferred.
        caption=_resolve_caption(item, doc=doc),
        n_rows=grid_rows,
        n_cols=grid_cols,
        cells=cells,
        provenance=_provenance_from(item),
        section_path=cast(tuple[str | None, ...], section_path),
    )


def _table_cell_to_attrs(cell: Any) -> TableCell:
    """One docling :class:`TableCell` → our frozen :class:`TableCell`.

    ``kind`` resolves to ``'th'`` when *either* ``column_header`` or
    ``row_header`` is true. The header/body distinction is informative
    for downstream consumers that bind units to column headers; the
    binary ``th`` / ``td`` tag matches the XML adapter and is what the
    schema commits to. ``row_section`` (a TableFormer-V2-ish stratifier
    label) is intentionally not surfaced — the schema does not model
    it, and inferring header semantics from it is the kind of
    publisher-cross-route guesswork the discussion-doc §1.5 warns
    against.
    """
    is_header = bool(cell.column_header) or bool(cell.row_header)
    return TableCell(
        text=cell.text or '',
        kind='th' if is_header else 'td',
        rowspan=int(cell.row_span),
        colspan=int(cell.col_span),
    )


# ---------------------------------------------------------------------------
# FigureBlock
# ---------------------------------------------------------------------------


def _picture_to_figure_block(item: Any, *, doc: Any, section_path: tuple[str, ...]) -> FigureBlock:
    return FigureBlock(
        id=item.self_ref,
        label=None,  # same reasoning as TableBlock.label.
        caption=_resolve_caption(item, doc=doc),
        provenance=_provenance_from(item),
        section_path=cast(tuple[str | None, ...], section_path),
    )


def _resolve_caption(item: Any, *, doc: Any) -> str | None:
    """Return the first caption text attached to a :class:`TableItem` or :class:`PictureItem`.

    docling stores captions out-of-line: ``item.captions`` is a list of
    :class:`RefItem` references whose ``cref`` (e.g. ``'#/texts/3'``)
    points at a :class:`TextItem` (typically labelled
    :class:`DocItemLabel.CAPTION`) elsewhere in ``doc``.
    :meth:`RefItem.resolve` returns the referenced item; we read
    ``.text`` from it.

    The "first wins" rule is the primary-caption convention — most
    floats have exactly one caption; the rare multi-caption case (some
    Elsevier-converted papers use a separate sub-caption block) is a
    downstream concern if it ever proves load-bearing. Concatenation
    would be the alternative but loses the natural boundary between
    primary and supplementary text.

    Returns ``None`` when ``item.captions`` is empty — the honest "no
    caption present" signal. A non-empty captions list whose first
    resolution lands on a text item with empty text is reported as
    ``''`` rather than ``None``: that case is a docling-side anomaly
    (the publisher attached an empty caption) and we surface it
    verbatim rather than hiding the upstream weirdness.

    Raises
    ------
    pydantic.ValidationError / AttributeError
        ``RefItem.resolve`` failed (broken cref, or the resolved item
        does not carry a ``.text`` attribute). Both are docling-side
        contract violations and propagate unmodified per CLAUDE.md
        "fail loudly".
    """
    captions = item.captions or []
    if not captions:
        return None
    resolved = captions[0].resolve(doc)
    return resolved.text or ''


# ---------------------------------------------------------------------------
# EquationBlock
# ---------------------------------------------------------------------------


def _formula_to_equation_block(item: Any, *, section_path: tuple[str, ...]) -> EquationBlock:
    """A docling :class:`FormulaItem` → :class:`EquationBlock` with LaTeX payload.

    ``do_formula_enrichment=True`` is frozen in
    :func:`litspectraits.extract.pdf._load_docling`, so formula items
    carry a VLM-rendered LaTeX source in ``item.text``. We forward that
    verbatim into :attr:`EquationBlock.latex`; :attr:`EquationBlock.mathml`
    stays ``None`` (route='docling' would reject it anyway via
    :meth:`EquationBlock.__attrs_post_init__`). :attr:`EquationBlock.text`
    holds the LaTeX source itself — the anchor gate keys on
    :attr:`EquationBlock.text`, so we deliberately pick a value that
    appears verbatim somewhere downstream (rather than synthesising a
    rendered-equation string that nothing else in the pipeline would
    produce).
    """
    latex = item.text or ''
    return EquationBlock(
        id=item.self_ref,
        text=latex,
        mathml=None,
        latex=latex,
        provenance=_provenance_from(item),
        section_path=cast(tuple[str | None, ...], section_path),
    )


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def _extract_references(doc: Any, *, sdk: _DoclingSdk) -> tuple[Reference, ...]:
    """Collect :class:`Reference`-tagged docling text items.

    docling tags back-matter entries with ``DocItemLabel.REFERENCE`` as
    part of layout recognition. We forward each one as a
    :class:`Reference` with :attr:`Reference.raw_text` = the verbatim
    item text and :attr:`Reference.parsed` = ``None``. Structured
    citation fields require a citation-parsing pass (GROBID / anystyle
    / refextract / LLM) that is explicitly future work in §1.5; the
    discussion doc's §3.5 ``completeness`` flag is the place that fact
    is recorded.

    Iterates ``doc.texts`` directly rather than walking the body tree
    because reference list ordering matters and the flat collection
    preserves it; ``iterate_items`` may interleave with the body's
    structural shape in ways the back-matter doesn't expect.
    """
    refs: list[Reference] = []
    for text_item in doc.texts:
        if text_item.label != sdk.DocItemLabel.REFERENCE:
            continue
        refs.append(
            Reference(
                id=text_item.self_ref,
                raw_text=text_item.text or '',
                parsed=None,
                resolved=None,
            )
        )
    return tuple(refs)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _provenance_from(item: Any) -> Provenance:
    """Forward docling's first :class:`ProvenanceItem` to our :class:`Provenance`.

    docling's ``prov`` is a list — typically one entry, occasionally
    two when an item straddles a page break. We forward ``prov[0]``
    only; multi-page-anchor blocks would require schema work
    (:class:`Provenance` currently models a single ``page``), and that
    is its own follow-on. Raises if a content-carrying item has no
    provenance at all: a docling content item with no placeable anchor
    is something we want to know about, not paper over.
    """
    provs = item.prov or []
    if not provs:
        raise ValueError(
            f'docling content item {item.self_ref!r} has no provenance; '
            'cannot place it on a page in the normalised document'
        )
    primary = provs[0]
    docling_bbox = primary.bbox
    bbox = BBox(
        x0=float(docling_bbox.l),
        y0=float(docling_bbox.t),
        x1=float(docling_bbox.r),
        y1=float(docling_bbox.b),
    )
    charspan = primary.charspan
    page_char_range = CharRange(start=int(charspan[0]), end=int(charspan[1]))
    return Provenance(
        route='docling',
        xpath=None,
        page=int(primary.page_no),
        bbox=bbox,
        page_char_range=page_char_range,
        # docling reads the deterministic PDF text-layer geometry, so its
        # bbox is exact (docs/mineru-backend-spec.md §2.2). docling-vlm,
        # when wired, will stamp 'approximate' here instead.
        geometry_fidelity='exact',
    )


__all__ = ['normalize_docling_document']
