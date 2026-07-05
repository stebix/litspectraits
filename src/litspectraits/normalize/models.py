"""Frozen :mod:`attrs` data classes for the normalised ``Document``.

See :mod:`litspectraits.normalize` for the package-level intent and the
schema commitments enumerated in ``docs/normalized-documents-discussion.md``
Part 1.5. cattrs (un)structure hooks live in
:mod:`litspectraits.normalize.hooks` so this module stays
deserialisation-agnostic.

Notes
-----
The :class:`Block` union uses ``type`` as its tagged-union discriminator —
the field name shadows the builtin, matching the choice already taken in
:class:`litspectraits.manifest.CrossRefMetadata` and
:class:`litspectraits.manifest.RetrievePayload` (see those docstrings for
the rationale). Each concrete block sets ``type`` to a unique
:class:`typing.Literal` constant so cattrs can dispatch on it.
"""

from typing import Final, Literal

from attrs import field, frozen

Route = Literal['jats', 'elsevier', 'docling', 'mineru']
"""The provenance route a :class:`Block` came from. Mandatory on every
:class:`Provenance`. JATS + Elsevier share the XML-route shape (xpath,
no page geometry); ``docling`` and ``mineru`` are the PDF routes and
share the page + bbox shape. The *tool* identity within a PDF route
(``docling-standard`` vs ``docling-vlm``, MinerU ``pipeline`` vs ``vlm``
engine) lives in the extract ``meta.json`` ``backend_id``, not here —
see ``docs/mineru-backend-spec.md`` §1, §2.1."""

_XML_ROUTES: Final[tuple[Route, ...]] = ('jats', 'elsevier')
"""Routes whose provenance is an XML element address (xpath), carrying no
post-typesetting page geometry."""

_PDF_ROUTES: Final[tuple[Route, ...]] = ('docling', 'mineru')
"""Routes whose provenance is page + (optional) bbox. A block on a PDF
route always knows its ``page``; ``bbox`` is graded by
:class:`GeometryFidelity` (``docs/mineru-backend-spec.md`` §2.2)."""

GeometryFidelity = Literal['exact', 'approximate', 'absent']
"""How trustworthy a :attr:`Provenance.bbox` is, on the PDF routes.

- ``'exact'`` — read from the deterministic PDF text-layer geometry
  (docling's text layer; MinerU's ``pipeline`` engine).
- ``'approximate'`` — model/VLM-predicted (docling-vlm; MinerU ``vlm``
  engine). Highlight-back-to-source must treat these as coarse — see the
  promotion gate in ``docs/mineru-backend-spec.md`` §11 Q-D.
- ``'absent'`` — no geometry recovered for this block; ``bbox`` is
  ``None``. The only value permitted on XML routes."""

RefIdSource = Literal['jats', 'elsevier', 'marker-match']
"""Which pass populated :attr:`InlineRef.ref_id`. ``'jats'`` /
``'elsevier'`` mean the extractor's xref descriptor resolved against the
back-matter reference list at parse time; ``'marker-match'`` is the
future docling-route pass that re-binds bracketed markers (``[12]``) to
:class:`Reference` ids. ``None`` on the source side means "not resolved
yet"."""


@frozen
class CharRange:
    """Half-open ``[start, end)`` byte offsets into some text.

    What ``text`` is depends on the consumer: :class:`InlineRef` offsets
    index into the containing block's ``text`` field;
    :attr:`Provenance.page_char_range` indexes into the source PDF
    page's text layer.
    """

    start: int
    end: int


@frozen
class BBox:
    """Axis-aligned bounding box on a single PDF page.

    Coordinates are the backend's native page-space floats (origin
    top-left). docling forwards points/pixels per its own model; the
    MinerU adapter forwards ``middle.json`` bboxes, which are in PDF
    points (the ``content_list.json`` 0-1000 form is rescaled before it
    reaches here — ``docs/mineru-backend-spec.md`` §4). Used only on the
    PDF routes (``docling``, ``mineru``); never re-projected.
    """

    x0: float
    y0: float
    x1: float
    y1: float


@frozen
class Provenance:
    """Where a block came from in its source artifact.

    The :attr:`route` field is the discriminator. Its value constrains
    which other fields may be populated:

    - ``route in {'jats', 'elsevier'}`` — XML routes. :attr:`xpath`
      *should* be populated (the unique element address);
      :attr:`page` / :attr:`bbox` / :attr:`page_char_range` MUST be
      ``None`` (XML carries no post-typesetting page concept) and
      :attr:`geometry_fidelity` MUST be ``'absent'``.
    - ``route in {'docling', 'mineru'}`` — PDF routes. :attr:`page`
      MUST be populated (every PDF backend knows which page a block sat
      on); :attr:`xpath` MUST be ``None``. :attr:`bbox` is *optional*:
      a backend that cannot pin geometry for a block (a merged or
      synthesised block) emits ``bbox=None`` rather than failing the
      whole extract. When ``bbox`` is present it MUST be graded
      ``'exact'`` / ``'approximate'``; when absent it MUST be
      ``'absent'``. This is the relaxation in
      ``docs/mineru-backend-spec.md`` §2.2 — provenance stays present
      and honestly labelled rather than silently coarsened.

    The route-conditional invariants are enforced in
    :meth:`__attrs_post_init__` so a route confusion fails loudly at
    construction time rather than producing a quietly-wrong record.
    """

    route: Route
    xpath: str | None = None
    page: int | None = None
    bbox: BBox | None = None
    page_char_range: CharRange | None = None
    geometry_fidelity: GeometryFidelity = 'absent'

    def __attrs_post_init__(self) -> None:
        if self.route in _XML_ROUTES:
            xml_violations = [
                name
                for name in ('page', 'bbox', 'page_char_range')
                if getattr(self, name) is not None
            ]
            if xml_violations:
                raise ValueError(
                    f'route={self.route!r} carries XML provenance; '
                    f'these fields must be None: {xml_violations}'
                )
            if self.geometry_fidelity != 'absent':
                raise ValueError(
                    f'route={self.route!r} carries no page geometry; '
                    f"geometry_fidelity must be 'absent', got {self.geometry_fidelity!r}"
                )
        elif self.route in _PDF_ROUTES:
            if self.xpath is not None:
                raise ValueError(
                    f'route={self.route!r} carries PDF-page provenance; xpath must be None'
                )
            if self.page is None:
                raise ValueError(f'route={self.route!r} requires `page` to be populated')
            if self.bbox is None and self.geometry_fidelity != 'absent':
                raise ValueError(
                    "bbox is None but geometry_fidelity != 'absent'; "
                    f'got {self.geometry_fidelity!r} (cannot grade geometry that is not there)'
                )
            if self.bbox is not None and self.geometry_fidelity == 'absent':
                raise ValueError(
                    "bbox is present but geometry_fidelity == 'absent'; "
                    "grade it 'exact' (text-layer) or 'approximate' (model-predicted)"
                )


@frozen
class InlineRef:
    """A citation marker inside a block's text.

    Attributes
    ----------
    char_range : CharRange
        Offsets into the containing block's ``text`` field; the
        verbatim-anchor gate (``agentic-buildout-sketch.md`` §5.3)
        depends on these being exact, half-open, and stable across
        re-runs.
    surface_form : str
        The marker as it appears in the text — ``'[12]'``,
        ``'Smith et al., 2019'``, ``'Fig. 3a'``, etc.
    ref_id : str | None
        The :class:`Reference` id this marker resolves to, when
        resolution is available. ``None`` is normal on the docling
        route until a marker-match pass runs.
    ref_id_source : RefIdSource | None
        Which pass populated :attr:`ref_id`. ``None`` iff
        :attr:`ref_id` is ``None``.
    """

    char_range: CharRange
    surface_form: str
    ref_id: str | None = None
    ref_id_source: RefIdSource | None = None

    def __attrs_post_init__(self) -> None:
        # Honesty invariant: source tag and id presence must agree.
        if (self.ref_id is None) != (self.ref_id_source is None):
            raise ValueError(
                'InlineRef.ref_id and ref_id_source must be both None or both set; '
                f'got ref_id={self.ref_id!r} ref_id_source={self.ref_id_source!r}'
            )


@frozen
class InlineMath:
    """A run of inline (in-line-with-prose) mathematics inside a block.

    The sibling of :class:`InlineRef` for math rather than citations.
    Inline math is the documented loss point on the PDF routes
    (``project_pdf_inline_math_loss``, ``project_springer_inline_math_bloat``):
    a backend that recovers it (MinerU's ``inline_equation`` spans) records
    each run here with half-open offsets into the owning block's ``text``
    and the LaTeX it parsed, so the math is preserved *in place* without
    a lossy markdown round-trip (``CLAUDE.md``: markdown round-trips are
    rejected). Empty on routes that do not yet recover inline math.

    Attributes
    ----------
    char_range : CharRange
        Offsets into the containing block's ``text`` field; same
        exact / half-open / stable contract as :attr:`InlineRef.char_range`.
    latex : str
        The recovered LaTeX source for this run (no surrounding ``$``).
    """

    char_range: CharRange
    latex: str


@frozen
class TableCell:
    """One cell of a :class:`TableBlock`.

    Spans are preserved as the source emitted them; we never expand a
    spanned cell into duplicates (downstream consumers that need a dense
    grid can do that themselves). ``kind`` is a content tag, not a
    schema discriminator — the field name :class:`TableBlock` would
    have collided with the Block union's ``type`` discriminator.
    """

    text: str
    kind: Literal['th', 'td']
    rowspan: int = 1
    colspan: int = 1


# ---------------------------------------------------------------------------
# Block union (tagged on ``type``)
# ---------------------------------------------------------------------------


@frozen
class TextBlock:
    """Prose paragraph or section-introducing heading run.

    Inline citation markers are recorded in :attr:`inline_refs` and
    inline mathematics in :attr:`inline_math`, both with half-open
    offsets into :attr:`text`.
    """

    text: str
    provenance: Provenance
    section_path: tuple[str | None, ...] = ()
    inline_refs: tuple[InlineRef, ...] = ()
    inline_math: tuple[InlineMath, ...] = ()
    type: Literal['text'] = 'text'


@frozen
class TableBlock:
    """A publisher table or a docling :class:`docling_core.types.TableItem`.

    The XML adapter populates :attr:`cells` from the publisher's
    ``<table>`` cells with spans preserved. The docling adapter
    populates from ``TableItem.data`` (where TableFormer's predicted
    dense grid lives) — also with spans preserved.

    :attr:`inline_refs` is populated when the caption contains
    cross-references; offsets index into :attr:`caption` text, not into
    cell text. Cell-level inline refs are not modelled in this cut.
    """

    id: str | None
    label: str | None
    caption: str | None
    n_rows: int
    n_cols: int
    cells: tuple[tuple[TableCell, ...], ...]
    provenance: Provenance
    section_path: tuple[str | None, ...] = ()
    inline_refs: tuple[InlineRef, ...] = ()
    type: Literal['table'] = 'table'


@frozen
class FigureBlock:
    """A figure float. We never carry pixel data (``overview.md`` non-goal).

    For docling, the figure's bbox / page live in :attr:`provenance`;
    for XML, the caption text plus the section context are typically
    all we have.
    """

    id: str | None
    label: str | None
    caption: str | None
    provenance: Provenance
    section_path: tuple[str | None, ...] = ()
    inline_refs: tuple[InlineRef, ...] = ()
    type: Literal['figure'] = 'figure'


@frozen
class EquationBlock:
    """A display-mode equation.

    XML routes carry the original MathML in :attr:`mathml`; the PDF
    routes (docling, mineru) carry a LaTeX rendering in :attr:`latex`
    (docling's ``do_formula_enrichment=True`` per
    ``docs/docling-settings-buildout.md`` §1.2; MinerU's formula head per
    ``docs/mineru-backend-spec.md`` §4).

    :attr:`text` is the human-readable rendering the anchor gate keys
    on — for XML this is the MathML's text content, for the PDF routes it
    is the LaTeX source. Exactly one of :attr:`mathml` / :attr:`latex` is
    populated, matching :attr:`provenance` ``.route``.

    XML adapters in E0.5a do not yet emit instances of this class —
    block-level ``<disp-formula>`` extraction is a follow-on. First
    instances arrive via the docling adapter (E0.5b).
    """

    id: str | None
    text: str
    mathml: str | None
    latex: str | None
    provenance: Provenance
    section_path: tuple[str | None, ...] = ()
    inline_refs: tuple[InlineRef, ...] = ()
    type: Literal['equation'] = 'equation'

    def __attrs_post_init__(self) -> None:
        has_mathml = self.mathml is not None
        has_latex = self.latex is not None
        if has_mathml == has_latex:
            raise ValueError(
                'EquationBlock requires exactly one of `mathml` or `latex`; '
                f'got mathml={has_mathml} latex={has_latex}'
            )
        if has_mathml and self.provenance.route not in _XML_ROUTES:
            raise ValueError(
                f'EquationBlock.mathml is only populated on XML routes; '
                f'got route={self.provenance.route!r}'
            )
        if has_latex and self.provenance.route not in _PDF_ROUTES:
            raise ValueError(
                f'EquationBlock.latex is only populated on the PDF routes; '
                f'got route={self.provenance.route!r}'
            )


Block = TextBlock | TableBlock | FigureBlock | EquationBlock
"""Tagged-union of block kinds. cattrs dispatches on the ``type``
field; see :mod:`litspectraits.normalize.hooks`."""


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


@frozen
class ParsedReference:
    """Structured citation fields the extractor could recover up-front.

    Populated by the XML adapters from JATS ``<element-citation>`` /
    ``<mixed-citation>`` and Elsevier ``<ce:bib-reference>``. The
    docling adapter cannot populate this (PDF reference lists are
    plain text); the citation-parsing follow-on pass will.
    """

    authors: tuple[str, ...] = ()
    title: str | None = None
    source: str | None = None
    year: int | None = None
    doi: str | None = None


@frozen
class ResolvedReference:
    """Outputs of the downstream reference-resolution pass.

    Populated only after a separate resolver runs against CrossRef
    and / or the local corpus. For E0.5a/b this is always ``None`` on
    fresh :class:`Reference` records — the field exists so the schema
    is forward-compatible with the citation-chain following commitment
    in ``docs/overview.md``.

    Attributes
    ----------
    target_doi : str | None
        DOI the citation resolves to.
    target_artifact_sha : str | None
        SHA-256 of the artifact in our corpus, when the citation
        resolves to a paper we also ingested. Enables collapse-to-primary
        of duplicated citation chains (``overview.md``).
    """

    target_doi: str | None = None
    target_artifact_sha: str | None = None


@frozen
class Reference:
    """Bibliography entry with the raw / parsed / resolved split.

    :attr:`raw_text` is always populated and is the canonical anchor
    for the entry. :attr:`parsed` is populated by the XML adapters and
    by the future citation-parsing pass on the docling route.
    :attr:`resolved` is populated by the future reference-resolver pass.
    """

    id: str
    raw_text: str
    parsed: ParsedReference | None = None
    resolved: ResolvedReference | None = None


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


@frozen
class Completeness:
    """Per-document summary of which optional features are populated.

    Lets the measurement layer's confidence gate branch on route
    quality without re-deriving it from the blocks. Persisted into
    ``normalized/sha256/<aa>/<sha>/meta.json`` (``docs/normalized-documents-discussion.md``
    §3.5).
    """

    has_structured_refs: bool
    has_inline_ref_ids: bool
    has_equations: bool
    # 'publisher' — table structure from XML markup (exact); 'tableformer' —
    # docling's table model; 'mineru' — MinerU's table model (parsed from its
    # HTML output). All three are "where the cell grid came from"; only
    # 'publisher' is markup-exact (docs/mineru-backend-spec.md §2, §4).
    table_source: Literal['publisher', 'tableformer', 'mineru']


@frozen
class Document:
    """Normalised representation of one ingested artifact.

    The :attr:`route` field is the document's primary route — every
    :attr:`blocks` entry's ``provenance.route`` must match it (validated
    in :meth:`__attrs_post_init__`). The gap-fill exception
    (``CLAUDE.md`` "source_kind") will relax this when that pass lands;
    for now route purity is enforced.
    """

    route: Route
    blocks: tuple[Block, ...]
    references: tuple[Reference, ...]
    completeness: Completeness
    title: str | None = None
    abstract: str | None = None
    # Schema versioning: bump when the on-disk shape changes. See
    # ``docs/normalized-documents-discussion.md`` §3.5.
    schema_name: str = field(default='litspectraits-normalized-document')
    # v2: PDF routes gained `mineru`; Provenance gained `geometry_fidelity`;
    # TextBlock gained `inline_math` (docs/mineru-backend-spec.md §2.5).
    schema_version: str = field(default='2')

    def __attrs_post_init__(self) -> None:
        bad = [
            (idx, block.provenance.route)
            for idx, block in enumerate(self.blocks)
            if block.provenance.route != self.route
        ]
        if bad:
            raise ValueError(
                f'Document.route={self.route!r} but the following blocks carry '
                f'a different provenance.route (gap-fill not yet supported): {bad}'
            )
