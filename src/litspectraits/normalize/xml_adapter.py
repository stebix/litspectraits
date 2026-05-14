"""XML route → :class:`~litspectraits.normalize.Document` adapter (E0.5a).

Consumes the publisher-agnostic dict shape emitted by
:mod:`litspectraits.extract.jats` and :mod:`litspectraits.extract.elsevier`
— both deliberately aligned, per ``docs/overview-v3.md`` §11 — and
produces a :class:`Document` ready for the verbatim-anchor gate.

Pure rename: the cumulative-length offset re-walk that makes
``InlineRef.char_range`` possible lives in the extractors (under
:func:`litspectraits.extract._lxml_helpers.walk_paragraph_with_offsets`),
not here. The adapter just structures the dict.

Limitations (E0.5a explicit non-goals — tracked as follow-ons in
``docs/normalized-documents-discussion.md`` open-follow-ons list)
-----------------------------------------------------------------
- **Block order is by kind, not by source reading order.** Section
  paragraphs come first (in their natural section traversal order),
  then all tables, then all figures. Recovering true reading order
  requires tracking source-position in the extractor walk; deferred
  because measurement-extraction binds context via ``section_path`` +
  explicit cross-reference resolution, not block adjacency.
- **``Block.provenance.xpath`` is ``None``.** The dict has no xpath
  field today, and the adapter does not re-parse the artifact.
  Resurfacing xpath for highlight UI is its own follow-on (extend the
  extractor to emit ``getpath(node)`` alongside the structural fields).
- **Inline refs only on text blocks.** Caption-level xrefs in JATS /
  Elsevier (rare but present) are not yet anchored; this matches the
  schema, where ``TableBlock.inline_refs`` / ``FigureBlock.inline_refs``
  default to ``()``.
- **Inline equations stay inside paragraph text.** ``<inline-formula>`` /
  ``<ce:formula>`` content embedded mid-sentence is preserved verbatim
  in the surrounding ``TextBlock.text`` via the extractor's ``full_text``
  walk; it never becomes its own block. Only display-mode equations
  (``<disp-formula>`` / ``<ce:formula>`` as a direct child of a section)
  surface as :class:`EquationBlock`, matching the docling route's
  display-only equation policy.
"""

from typing import Any, Final, Literal

from litspectraits.normalize.models import (
    CharRange,
    Completeness,
    Document,
    EquationBlock,
    FigureBlock,
    InlineRef,
    ParsedReference,
    Provenance,
    Reference,
    RefIdSource,
    TableBlock,
    TableCell,
    TextBlock,
)

XmlRoute = Literal['jats', 'elsevier']
"""The two XML routes this adapter handles; narrower than :class:`Route`
because the docling route runs through its own adapter (E0.5b)."""

_REF_ID_SOURCE_BY_ROUTE: Final[dict[XmlRoute, RefIdSource]] = {
    'jats': 'jats',
    'elsevier': 'elsevier',
}


def normalize_xml_document(
    document: dict[str, Any],
    *,
    route: XmlRoute,
) -> Document:
    """Convert a publisher dict to a normalised :class:`Document`.

    Parameters
    ----------
    document : dict
        The on-disk ``documents/sha256/<aa>/<sha>/document.json`` payload — exactly
        the dict shape :func:`litspectraits.extract.jats.extract_jats`
        and :func:`litspectraits.extract.elsevier.extract_elsevier`
        emit. Required top-level keys are ``front``, ``sections``,
        ``tables``, ``figures``, ``references``. The ``schema_name`` /
        ``schema_version`` companion fields are tolerated but ignored;
        the normaliser owns its own schema version on
        :attr:`Document.schema_version`.
    route : XmlRoute
        ``'jats'`` or ``'elsevier'``. Drives the route discriminator on
        every :class:`Provenance` and the ``ref_id_source`` on inline
        refs.

    Returns
    -------
    Document
        Frozen, ready to serialise via
        :data:`litspectraits.normalize.converter`. Route-purity is
        enforced by :meth:`Document.__attrs_post_init__`.
    """
    blocks: list[TextBlock | TableBlock | FigureBlock | EquationBlock] = []

    for section in document.get('sections', []) or []:
        section_path = tuple(section.get('path') or ())
        for raw_block in section.get('blocks', []) or []:
            section_block = _section_block_to_normalized(
                raw_block, section_path=section_path, route=route
            )
            if section_block is not None:
                blocks.append(section_block)

    for raw_table in document.get('tables', []) or []:
        blocks.append(_table_to_block(raw_table, route=route))

    for raw_figure in document.get('figures', []) or []:
        blocks.append(_figure_to_block(raw_figure, route=route))

    references = tuple(
        _reference_to_attrs(raw_ref) for raw_ref in document.get('references', []) or []
    )

    completeness = _derive_completeness(blocks=blocks, references=references)

    front = document.get('front') or {}
    return Document(
        route=route,
        blocks=tuple(blocks),
        references=references,
        completeness=completeness,
        title=front.get('title'),
        abstract=front.get('abstract'),
    )


# ---------------------------------------------------------------------------
# Block conversions
# ---------------------------------------------------------------------------


def _section_block_to_normalized(
    raw: dict[str, Any],
    *,
    section_path: tuple[str | None, ...],
    route: XmlRoute,
) -> TextBlock | EquationBlock | None:
    """Dispatch one in-section block dict to its normalised counterpart.

    The publisher dict currently carries two block kinds inside a
    section: ``'paragraph'`` and ``'equation'``. Unknown kinds return
    ``None`` so a future block type (e.g. a list-item shape) isn't
    silently mis-typed as text — the loud failure here is the same
    posture as everywhere else in the extract path.
    """
    block_kind = raw.get('type')
    if block_kind == 'paragraph':
        return _paragraph_to_text_block(raw, section_path=section_path, route=route)
    if block_kind == 'equation':
        return _equation_to_block(raw, section_path=section_path, route=route)
    return None


def _paragraph_to_text_block(
    raw: dict[str, Any],
    *,
    section_path: tuple[str | None, ...],
    route: XmlRoute,
) -> TextBlock:
    text = raw.get('text') or ''
    inline_refs = tuple(
        _xref_to_inline_ref(xref, block_text=text, route=route)
        for xref in raw.get('xrefs', []) or []
    )
    return TextBlock(
        text=text,
        provenance=Provenance(route=route),
        section_path=section_path,
        inline_refs=inline_refs,
    )


def _equation_to_block(
    raw: dict[str, Any],
    *,
    section_path: tuple[str | None, ...],
    route: XmlRoute,
) -> EquationBlock:
    """Adapt one equation dict.

    The extractor only emits equation entries that carry verbatim MathML
    (see :func:`litspectraits.extract.jats._disp_formula_to_block` /
    :func:`litspectraits.extract.elsevier._formula_to_block`), so
    ``raw['mathml']`` is always populated on the XML route — the schema
    requires it. ``latex`` stays ``None``; that field is the docling
    route's responsibility.
    """
    return EquationBlock(
        id=raw.get('id'),
        text=raw.get('text') or '',
        mathml=raw.get('mathml'),
        latex=None,
        provenance=Provenance(route=route),
        section_path=section_path,
    )


def _xref_to_inline_ref(
    xref: dict[str, Any],
    *,
    block_text: str,
    route: XmlRoute,
) -> InlineRef:
    """Adapt one xref descriptor to an :class:`InlineRef`.

    The surface form is taken verbatim from ``block_text[start:end]``
    rather than the extractor's stripped ``label`` field — the anchor
    gate keys on the un-stripped substring, and ``surface_form`` should
    reflect what the reader sees in the text byte-for-byte. ``label``
    remains useful in the on-disk dict for ``jq`` ergonomics but the
    normalised model derives its canonical form here.
    """
    start = int(xref['start'])
    end = int(xref['end'])
    surface_form = block_text[start:end]
    rid = xref.get('rid')
    ref_id_source: RefIdSource | None = _REF_ID_SOURCE_BY_ROUTE[route] if rid else None
    return InlineRef(
        char_range=CharRange(start=start, end=end),
        surface_form=surface_form,
        ref_id=rid,
        ref_id_source=ref_id_source,
    )


def _table_to_block(raw: dict[str, Any], *, route: XmlRoute) -> TableBlock:
    cells = tuple(
        tuple(_cell_to_attrs(cell) for cell in row) for row in raw.get('cells', []) or []
    )
    return TableBlock(
        id=raw.get('id'),
        label=raw.get('label'),
        caption=raw.get('caption'),
        n_rows=int(raw.get('n_rows', 0)),
        n_cols=int(raw.get('n_cols', 0)),
        cells=cells,
        provenance=Provenance(route=route),
        section_path=tuple(raw.get('section_path') or ()),
    )


def _cell_to_attrs(raw: dict[str, Any]) -> TableCell:
    """Normalise the cell-kind tag across publishers.

    JATS emits ``'th'`` / ``'td'``; Elsevier CALS emits ``'entry'`` for
    every cell (CALS has no semantic header/body cell distinction at
    the element level — header rows live under ``<thead>`` instead).
    Map CALS ``'entry'`` to ``'td'`` so the normalised :class:`TableCell`
    union stays binary. Header semantics on CALS tables are recoverable
    downstream by inspecting row position; we don't paper over the
    upstream limitation by guessing here.
    """
    raw_kind = raw.get('type', 'td')
    kind: Literal['th', 'td'] = 'th' if raw_kind == 'th' else 'td'
    return TableCell(
        text=raw.get('text') or '',
        kind=kind,
        rowspan=int(raw.get('rowspan', 1)),
        colspan=int(raw.get('colspan', 1)),
    )


def _figure_to_block(raw: dict[str, Any], *, route: XmlRoute) -> FigureBlock:
    return FigureBlock(
        id=raw.get('id'),
        label=raw.get('label'),
        caption=raw.get('caption'),
        provenance=Provenance(route=route),
        section_path=tuple(raw.get('section_path') or ()),
    )


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def _reference_to_attrs(raw: dict[str, Any]) -> Reference:
    """Adapt one reference dict.

    ``parsed`` is populated whenever any structured field is non-empty;
    if the extractor recovered nothing structured (a ``mixed-citation``
    with prose only), :attr:`Reference.parsed` stays ``None`` so the
    consumer's :attr:`Completeness.has_structured_refs` flag reflects
    reality rather than always reporting ``True``.

    ``year`` lands as an int when parseable; otherwise stored as-is via
    ``raw_text`` and dropped from the structured side. JATS / Elsevier
    sometimes emit ranged years (``'2019-2020'``) or letters
    (``'2020a'``) and we don't try to coerce those - surface as ``None``
    in :attr:`ParsedReference.year`.
    """
    ref_id = raw.get('id') or ''
    raw_text = raw.get('raw_text') or ''
    authors = tuple(raw.get('authors') or ())
    title = raw.get('title')
    source = raw.get('source')
    year_raw = raw.get('year')
    doi = raw.get('doi')
    year_int: int | None
    if isinstance(year_raw, int):
        year_int = year_raw
    elif isinstance(year_raw, str):
        try:
            year_int = int(year_raw)
        except ValueError:
            year_int = None
    else:
        year_int = None

    has_structured = bool(authors or title or source or year_int is not None or doi)
    parsed: ParsedReference | None = None
    if has_structured:
        parsed = ParsedReference(
            authors=authors,
            title=title,
            source=source,
            year=year_int,
            doi=doi,
        )
    return Reference(id=ref_id, raw_text=raw_text, parsed=parsed)


# ---------------------------------------------------------------------------
# Completeness summary (Part 1.5 of the discussion doc)
# ---------------------------------------------------------------------------


def _derive_completeness(
    *,
    blocks: list[TextBlock | TableBlock | FigureBlock | EquationBlock],
    references: tuple[Reference, ...],
) -> Completeness:
    has_structured_refs = any(ref.parsed is not None for ref in references)
    has_inline_ref_ids = any(
        inline.ref_id is not None
        for block in blocks
        if isinstance(block, TextBlock)
        for inline in block.inline_refs
    )
    has_equations = any(isinstance(block, EquationBlock) for block in blocks)
    return Completeness(
        has_structured_refs=has_structured_refs,
        has_inline_ref_ids=has_inline_ref_ids,
        has_equations=has_equations,
        # Both XML routes take tables straight from the publisher
        # markup; no TableFormer in the loop, so the table source is
        # unambiguous.
        table_source='publisher',
    )


__all__ = ['XmlRoute', 'normalize_xml_document']
