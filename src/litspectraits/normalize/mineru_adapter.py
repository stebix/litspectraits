"""MinerU → :class:`~litspectraits.normalize.Document` adapter (spec §4).

Sibling of :mod:`litspectraits.normalize.docling_adapter` and
:mod:`litspectraits.normalize.xml_adapter`: consumes the verbatim MinerU
``middle.json`` payload that :func:`litspectraits.extract.mineru.extract_mineru`
writes to ``documents/sha256/<aa>/<sha>/document.json`` and produces a
:class:`Document` with ``route='mineru'``.

The walk
--------
``pdf_info`` is a list of pages (each a dict with ``page_idx`` and
``para_blocks``); ``para_blocks`` is already in reading order. We walk
pages in order, then blocks in order, mapping MinerU block types to
:class:`~litspectraits.normalize.models.Block` variants:

==================================  ==============================================
MinerU ``middle.json`` block type   → ``Block`` / action
==================================  ==============================================
``text`` / ``list`` / ``index`` /   ``TextBlock``; ``inline_equation`` spans →
``abstract`` / ``ref_text``         ``InlineMath`` with offsets into ``text``
``title``                           section-path push (``level`` → depth),
                                    mirroring docling's ``SectionHeaderItem``
``interline_equation``              ``EquationBlock(latex=…, mathml=None)``
``image`` / ``chart``               ``FigureBlock`` (caption from the nested
                                    ``*_caption`` sub-blocks)
``table``                           ``TableBlock``; the nested ``table_body``
                                    span's ``html`` is parsed to ``cells`` via
                                    ``lxml.html`` (spans preserved, not expanded)
everything else (incl. discarded)   dropped — same posture as the docling
                                    adapter, which handles the core block
                                    kinds and treats the rest as a follow-on
==================================  ==============================================

Provenance + geometry fidelity
------------------------------
Every emitted block carries ``Provenance(route='mineru', page=page_idx,
bbox=…, geometry_fidelity=…)``. ``middle.json`` bboxes are in **PDF points**
(forwarded verbatim into :class:`~litspectraits.normalize.models.BBox`; the
``content_list.json`` 0-1000 form is never read here). ``geometry_fidelity``
is ``'exact'`` for MinerU's ``pipeline`` engine (text-layer) and
``'approximate'`` for any other engine (``vlm``), read from the
``middle.json`` top-level ``_backend`` field. A block whose bbox is missing
or malformed is emitted with ``bbox=None, geometry_fidelity='absent'`` (the
honest relaxation in ``docs/mineru-backend-spec.md`` §2.2) — ``page`` is
always known because it comes from the owning page. The fail-loud case (no
page *and* no bbox) is therefore unreachable for a well-formed payload, but
guarded anyway.

Non-goals (first cut), aligned with the docling adapter
-------------------------------------------------------
- No abstract detection into :attr:`Document.abstract`; an ``abstract``
  block is emitted as a :class:`TextBlock` so its text is never lost.
- No structured reference parsing: ``ref_text`` blocks become
  :class:`TextBlock`\\s; :attr:`Document.references` is empty and
  :attr:`Completeness.has_structured_refs` is ``False``.
- :class:`InlineRef`\\s are anchored with the same bracketed-number regex
  the docling adapter uses (so the two PDF routes stay comparable), but
  ``ref_id`` resolution is a downstream pass.
"""

import re
from typing import Any, Final, cast

from lxml import html as lxml_html

from litspectraits.normalize.models import (
    BBox,
    Block,
    CharRange,
    Completeness,
    Document,
    EquationBlock,
    FigureBlock,
    GeometryFidelity,
    InlineMath,
    InlineRef,
    Provenance,
    TableBlock,
    TableCell,
    TextBlock,
)

# Prose-bearing block types → TextBlock. Kept in sync with the extractor's
# ``_TEXT_BLOCK_TYPES`` so the char-count floor and the block walk agree on
# what counts as text.
_TEXT_BLOCK_TYPES: Final = frozenset({'text', 'list', 'index', 'abstract', 'ref_text'})
_TITLE_BLOCK_TYPE: Final = 'title'
_INTERLINE_EQUATION_TYPE: Final = 'interline_equation'
_TABLE_BLOCK_TYPE: Final = 'table'
_FIGURE_BLOCK_TYPES: Final = frozenset({'image', 'chart'})

# Span types within a text line.
_TEXT_SPAN_TYPE: Final = 'text'
_INLINE_EQUATION_SPAN_TYPE: Final = 'inline_equation'
# Span types carrying display-equation LaTeX (interline_equation block).
_EQUATION_SPAN_TYPES: Final = frozenset({'interline_equation', 'equation'})
# Span types carrying a table's HTML grid (table_body sub-block).
_TABLE_SPAN_TYPE: Final = 'table'

# Caption sub-block types inside an image / chart / table container.
_CAPTION_BLOCK_TYPES: Final = frozenset({'image_caption', 'chart_caption', 'table_caption'})
# The table grid lives in the table_body sub-block.
_TABLE_BODY_TYPE: Final = 'table_body'

# Same bracketed-number citation marker the docling adapter anchors
# (``docling_adapter._INLINE_REF_PATTERN``); duplicated locally so the two
# PDF routes stay comparable without a cross-adapter private import.
_INLINE_REF_PATTERN: Final = re.compile(r'\[\d+(?:\s*[,\-–]\s*\d+)*\]')  # noqa: RUF001


def normalize_mineru_document(document: dict[str, Any]) -> Document:
    """Convert a MinerU ``middle.json`` payload to a :class:`Document`.

    Parameters
    ----------
    document : dict
        The verbatim MinerU ``*_middle.json`` payload — the on-disk shape
        of ``documents/sha256/<aa>/<sha>/document.json`` produced by
        :func:`litspectraits.extract.mineru.extract_mineru`.

    Returns
    -------
    Document
        Frozen :class:`Document` with ``route='mineru'``. Block order is
        page order then ``para_blocks`` order; ``section_path`` is rebuilt
        from ``title`` blocks' ``level``;
        :attr:`Document.completeness.has_equations` reflects whether any
        :class:`EquationBlock` landed in ``blocks``.

    Raises
    ------
    ValueError
        A content block carries neither a page index nor a usable bbox
        (no placeable anchor — ``docs/mineru-backend-spec.md`` §4
        faithfulness discipline), or the payload is structurally
        unusable (``pdf_info`` not a list).
    """
    pdf_info = document.get('pdf_info')
    if not isinstance(pdf_info, list):
        raise ValueError(
            f'MinerU middle.json has no pdf_info list (got {type(pdf_info).__name__}); '
            'cannot normalise'
        )
    fidelity = _engine_fidelity(document.get('_backend'))

    blocks: list[Block] = []
    section_stack: list[tuple[int, str]] = []
    title: str | None = None

    for page in pdf_info:
        page_idx = page.get('page_idx')
        for block in page.get('para_blocks', []):
            block_type = block.get('type')

            if block_type == _TITLE_BLOCK_TYPE:
                heading = _merge_block_text(block)
                if title is None and heading:
                    title = heading
                _push_section_header(section_stack, level=_title_level(block), text=heading)
                continue

            section_path = tuple(text for _, text in section_stack)
            emitted = _block_to_block(
                block,
                block_type=block_type,
                page_idx=page_idx,
                fidelity=fidelity,
                section_path=section_path,
            )
            if emitted is not None:
                blocks.append(emitted)

    completeness = Completeness(
        has_structured_refs=False,
        has_inline_ref_ids=False,
        has_equations=any(isinstance(b, EquationBlock) for b in blocks),
        table_source='mineru',
    )
    return Document(
        route='mineru',
        blocks=tuple(blocks),
        references=(),
        completeness=completeness,
        title=title,
        abstract=None,
    )


# ---------------------------------------------------------------------------
# Engine → geometry fidelity
# ---------------------------------------------------------------------------


def _engine_fidelity(backend: Any) -> GeometryFidelity:
    """Map MinerU's ``_backend`` tag to a :class:`GeometryFidelity`.

    ``'pipeline'`` reads the PDF text layer → ``'exact'``; any other engine
    (``'vlm'``) predicts geometry → ``'approximate'``. This is the grade a
    *present* bbox gets; a missing bbox is always ``'absent'`` regardless
    (handled in :func:`_provenance`).
    """
    return 'exact' if backend == 'pipeline' else 'approximate'


# ---------------------------------------------------------------------------
# Section header stack (mirrors docling_adapter._push_section_header)
# ---------------------------------------------------------------------------


def _title_level(block: dict[str, Any]) -> int:
    """Heading depth from a ``title`` block's ``level`` (MinerU's own default 1)."""
    level = block.get('level', 1)
    try:
        level = int(level)
    except TypeError, ValueError:
        return 1
    return max(level, 1)


def _push_section_header(stack: list[tuple[int, str]], *, level: int, text: str) -> None:
    """Reconcile a new section header into the open-section stack.

    Same discipline as the docling adapter: pop every entry at
    ``stack_level >= level`` (a sibling or shallower header closes deeper
    ones), then push. Skipped levels are preserved as-is — we never invent a
    synthetic parent, since that would inject text into ``section_path`` that
    appears nowhere in the source.
    """
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, text))


# ---------------------------------------------------------------------------
# Per-block dispatch
# ---------------------------------------------------------------------------


def _block_to_block(
    block: dict[str, Any],
    *,
    block_type: Any,
    page_idx: Any,
    fidelity: GeometryFidelity,
    section_path: tuple[str, ...],
) -> Block | None:
    """Dispatch one MinerU ``para_block`` to its :class:`Block` shape.

    Returns ``None`` for block types the first cut does not emit (discarded
    furniture, aside text, code, …) — the same forward-compatible posture as
    the docling adapter's ``_item_to_block``.
    """
    if block_type in _TEXT_BLOCK_TYPES:
        return _text_block(block, page_idx=page_idx, fidelity=fidelity, section_path=section_path)
    if block_type == _INTERLINE_EQUATION_TYPE:
        return _equation_block(
            block, page_idx=page_idx, fidelity=fidelity, section_path=section_path
        )
    if block_type == _TABLE_BLOCK_TYPE:
        return _table_block(block, page_idx=page_idx, fidelity=fidelity, section_path=section_path)
    if block_type in _FIGURE_BLOCK_TYPES:
        return _figure_block(
            block, page_idx=page_idx, fidelity=fidelity, section_path=section_path
        )
    return None


# ---------------------------------------------------------------------------
# TextBlock + inline math
# ---------------------------------------------------------------------------


def _text_block(
    block: dict[str, Any],
    *,
    page_idx: Any,
    fidelity: GeometryFidelity,
    section_path: tuple[str, ...],
) -> TextBlock:
    text, inline_math = _render_text_and_math(block)
    return TextBlock(
        text=text,
        provenance=_provenance(block, page_idx=page_idx, fidelity=fidelity),
        section_path=section_path,
        inline_refs=_find_inline_refs(text),
        inline_math=inline_math,
    )


def _render_text_and_math(block: dict[str, Any]) -> tuple[str, tuple[InlineMath, ...]]:
    """Concatenate a prose block's spans, recording inline-equation offsets.

    The returned ``text`` contains each ``inline_equation`` span's LaTeX
    source verbatim *in place*; the matching :class:`InlineMath` records the
    half-open ``[start, end)`` offsets into ``text`` and the same LaTeX. This
    preserves where the math sat in the prose without a lossy markdown
    round-trip (``CLAUDE.md``). Lines are joined with a single space; the
    offsets are computed against the exact string built here.
    """
    parts: list[str] = []
    inline: list[InlineMath] = []
    pos = 0
    for line in block.get('lines', []):
        spans = line.get('spans', [])
        if parts and spans:
            parts.append(' ')
            pos += 1
        for span in spans:
            span_type = span.get('type')
            content = span.get('content') or ''
            if not content:
                continue
            if span_type == _TEXT_SPAN_TYPE:
                parts.append(content)
                pos += len(content)
            elif span_type == _INLINE_EQUATION_SPAN_TYPE:
                start = pos
                parts.append(content)
                pos += len(content)
                inline.append(
                    InlineMath(char_range=CharRange(start=start, end=pos), latex=content)
                )
            # other span types in a prose block are not text-bearing; skip.
    text = ''.join(parts).rstrip()
    # rstrip only trims trailing whitespace, which is never inside an
    # InlineMath range (math content is non-whitespace), so offsets stay valid.
    return text, tuple(inline)


def _find_inline_refs(text: str) -> tuple[InlineRef, ...]:
    """Anchor bracketed-number citation markers as :class:`InlineRef`.

    ``ref_id`` / ``ref_id_source`` are both ``None`` — anchoring the marker
    is one pass; binding it to a :class:`Reference` is a downstream
    marker-match pass (same staging as the docling adapter).
    """
    return tuple(
        InlineRef(
            char_range=CharRange(start=match.start(), end=match.end()),
            surface_form=match.group(0),
            ref_id=None,
            ref_id_source=None,
        )
        for match in _INLINE_REF_PATTERN.finditer(text)
    )


# ---------------------------------------------------------------------------
# EquationBlock (display / interline)
# ---------------------------------------------------------------------------


def _equation_block(
    block: dict[str, Any],
    *,
    page_idx: Any,
    fidelity: GeometryFidelity,
    section_path: tuple[str, ...],
) -> EquationBlock:
    """An ``interline_equation`` block → :class:`EquationBlock` with LaTeX.

    The LaTeX is the first equation span's ``content``; when MinerU's formula
    head could not recover it (image-only span), ``latex`` is ``''`` — the
    block is still emitted so the display equation's page + bbox provenance
    is preserved (same posture as the docling formula branch). ``mathml``
    stays ``None`` (the PDF route would reject it anyway).
    """
    latex = _first_equation_latex(block)
    return EquationBlock(
        id=None,
        text=latex,
        mathml=None,
        latex=latex,
        provenance=_provenance(block, page_idx=page_idx, fidelity=fidelity),
        section_path=section_path,
    )


def _first_equation_latex(block: dict[str, Any]) -> str:
    for line in block.get('lines', []):
        for span in line.get('spans', []):
            if span.get('type') in _EQUATION_SPAN_TYPES:
                content = span.get('content')
                if content:
                    return content
    return ''


# ---------------------------------------------------------------------------
# TableBlock — the §4 "correct tabular values" surface
# ---------------------------------------------------------------------------


def _table_block(
    block: dict[str, Any],
    *,
    page_idx: Any,
    fidelity: GeometryFidelity,
    section_path: tuple[str, ...],
) -> TableBlock:
    """A ``table`` container → :class:`TableBlock`, cells parsed from HTML.

    MinerU emits the table as an HTML string in the nested ``table_body``
    span (not a cell grid). :func:`_parse_table_html` turns that into the
    anchored-cell grid the schema models — each cell placed once at its
    anchor row/col with ``rowspan`` / ``colspan`` preserved verbatim (no
    duplication into spanned slots), matching the docling adapter's table
    convention so the two PDF routes are diffable.
    """
    html = _first_table_html(block)
    n_rows, n_cols, cells = _parse_table_html(html)
    caption = _collect_caption(block)
    return TableBlock(
        id=None,
        label=None,
        caption=caption,
        n_rows=n_rows,
        n_cols=n_cols,
        cells=cells,
        provenance=_provenance(block, page_idx=page_idx, fidelity=fidelity),
        section_path=section_path,
    )


def _first_table_html(block: dict[str, Any]) -> str:
    """Pull the ``table_body`` span's HTML from a table container block."""
    for sub in block.get('blocks', []):
        if sub.get('type') != _TABLE_BODY_TYPE:
            continue
        for line in sub.get('lines', []):
            for span in line.get('spans', []):
                if span.get('type') == _TABLE_SPAN_TYPE and span.get('html'):
                    return span['html']
    return ''


def _parse_table_html(html: str) -> tuple[int, int, tuple[tuple[TableCell, ...], ...]]:
    """Parse a MinerU table HTML string into ``(n_rows, n_cols, cells)``.

    Standard HTML table grid walk with an occupancy map so cells land at the
    correct column even when earlier rows' ``rowspan`` or earlier cells'
    ``colspan`` consume slots. Each ``<td>`` / ``<th>`` is recorded once at
    its anchor (the row containing its markup), in column order;
    ``rowspan`` / ``colspan`` are kept on the :class:`TableCell` rather than
    expanded into duplicate cells. ``n_cols`` is the widest occupied column
    across all rows.

    An empty / unparseable string yields ``(0, 0, ())`` — an honest "no grid
    recovered", consistent with the relaxation that a table whose structure
    MinerU could not parse still exists as a block (with provenance) rather
    than failing the whole document.
    """
    if not html or not html.strip():
        return 0, 0, ()
    fragment = lxml_html.fromstring(html)
    # lxml's .xpath() is typed as a broad union; we only ever feed it element
    # path expressions that return element lists, so cast for the type checker.
    rows = cast(list[Any], fragment.xpath('.//tr'))
    if not rows:
        return 0, 0, ()

    # occupied[(row, col)] = True for slots covered by a rowspan/colspan.
    occupied: dict[tuple[int, int], bool] = {}
    grid: list[list[TableCell]] = []
    n_cols = 0
    for r, row in enumerate(rows):
        anchored: list[TableCell] = []
        col = 0
        for cell_el in cast(list[Any], row.xpath('./td | ./th')):
            while occupied.get((r, col)):
                col += 1
            rowspan = _span_attr(cell_el, 'rowspan')
            colspan = _span_attr(cell_el, 'colspan')
            text = cell_el.text_content().strip()
            kind = 'th' if cell_el.tag == 'th' else 'td'
            anchored.append(TableCell(text=text, kind=kind, rowspan=rowspan, colspan=colspan))
            for dr in range(rowspan):
                for dc in range(colspan):
                    occupied[(r + dr, col + dc)] = True
            col += colspan
            n_cols = max(n_cols, col)
        grid.append(anchored)
    return len(rows), n_cols, tuple(tuple(row) for row in grid)


def _span_attr(cell_el: Any, name: str) -> int:
    """Read a ``rowspan`` / ``colspan`` attribute, defaulting to 1.

    A missing, non-integer, or non-positive value falls back to 1 — a
    malformed span attribute must not silently drop the cell's slot.
    """
    raw = cell_el.get(name)
    if raw is None:
        return 1
    try:
        value = int(raw)
    except TypeError, ValueError:
        return 1
    return value if value >= 1 else 1


# ---------------------------------------------------------------------------
# FigureBlock
# ---------------------------------------------------------------------------


def _figure_block(
    block: dict[str, Any],
    *,
    page_idx: Any,
    fidelity: GeometryFidelity,
    section_path: tuple[str, ...],
) -> FigureBlock:
    return FigureBlock(
        id=None,
        label=None,
        caption=_collect_caption(block),
        provenance=_provenance(block, page_idx=page_idx, fidelity=fidelity),
        section_path=section_path,
    )


def _collect_caption(block: dict[str, Any]) -> str | None:
    """Concatenate the caption text from a container block's caption sub-blocks.

    Returns ``None`` when the container carries no caption sub-block (the
    honest "no caption" signal); a caption sub-block that resolves to empty
    text is reported as ``''``.
    """
    captions: list[str] = []
    found = False
    for sub in block.get('blocks', []):
        if sub.get('type') in _CAPTION_BLOCK_TYPES:
            found = True
            captions.append(_merge_block_text(sub))
    if not found:
        return None
    return ' '.join(part for part in captions if part)


# ---------------------------------------------------------------------------
# Shared text + provenance helpers
# ---------------------------------------------------------------------------


def _merge_block_text(block: dict[str, Any]) -> str:
    """Concatenate the text/inline-equation span content of a block.

    Used for titles and captions where inline math offsets are not tracked;
    lines are joined with a single space.
    """
    lines_text: list[str] = []
    for line in block.get('lines', []):
        spans_text: list[str] = []
        for span in line.get('spans', []):
            if span.get('type') in (_TEXT_SPAN_TYPE, _INLINE_EQUATION_SPAN_TYPE):
                content = span.get('content')
                if content:
                    spans_text.append(content)
        if spans_text:
            lines_text.append(''.join(spans_text))
    return ' '.join(lines_text).strip()


def _provenance(block: dict[str, Any], *, page_idx: Any, fidelity: GeometryFidelity) -> Provenance:
    """Build a ``route='mineru'`` :class:`Provenance` for one block.

    ``page`` comes from the owning page's ``page_idx``; ``bbox`` from the
    block's ``bbox`` (PDF points, forwarded verbatim). A missing or
    malformed bbox yields ``bbox=None, geometry_fidelity='absent'`` (the
    relaxation). The only hard failure is a block with neither a page index
    nor a bbox — no placeable anchor at all.
    """
    bbox = _bbox(block.get('bbox'))
    page = int(page_idx) if isinstance(page_idx, int) else None
    if page is None and bbox is None:
        raise ValueError(
            'MinerU content block has neither page_idx nor a usable bbox; '
            'cannot place it on a page in the normalised document'
        )
    return Provenance(
        route='mineru',
        page=page,
        bbox=bbox,
        geometry_fidelity=fidelity if bbox is not None else 'absent',
    )


def _bbox(raw: Any) -> BBox | None:
    """Forward a MinerU ``[x0, y0, x1, y1]`` bbox to :class:`BBox`, or ``None``.

    A bbox that is not a length-4 sequence of numbers is treated as absent
    rather than fabricated — the geometry is then honestly graded
    ``'absent'`` (see :func:`_provenance`).
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except TypeError, ValueError:
        return None
    return BBox(x0=x0, y0=y0, x1=x1, y1=y1)


__all__ = ['normalize_mineru_document']
