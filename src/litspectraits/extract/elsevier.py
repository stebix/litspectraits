"""Elsevier full-text XML extractor (``docs/overview-v3.md`` §11).

Elsevier's full-text retrieval endpoint returns its own ``<full-text-
retrieval-response>`` envelope (declared in
``http://www.elsevier.com/xml/svapi/article/dtd``). The body lives under
``<originalText>`` / ``<xocs:doc>`` and is typically written in Elsevier's
Common Element Pool (CEP) — ``<ce:section>`` / ``<ce:para>`` /
``<ce:table>`` etc. — distinct from JATS in both element names *and* the
table model (CALS/OASIS in CEP vs HTML-style in JATS).

The on-disk shape (``documents/sha256/<aa>/<sha>/document.json``) is the same
JATS-flavored dict shape :mod:`litspectraits.extract.jats` emits:

- ``front``: title + abstract text.
- ``sections``: flattened list with ``id`` / ``title`` / ``level`` /
  ``path`` / ``blocks``; nested ``<ce:section>`` is flattened the same
  way JATS ``<sec>`` is.
- ``tables``: 2-D ``cells`` grid (CALS ``<row>`` / ``<entry>`` projected
  into rows/cols with best-effort ``rowspan`` from ``morerows`` and
  ``colspan`` from ``namest`` / ``nameend``).
- ``figures``: caption + section path; **no pixel data**.
- ``references``: raw text plus the easy structured fields (authors,
  title, source, year, DOI) extracted from ``<ce:bib-reference>``.

The shape mismatch with JATS lives in the *walker*, not the dict — so
the agent triad's normaliser doesn't need an Elsevier-specific branch
(``docs/overview-v3.md`` §11).

Two limitations worth knowing about:

- **CEP only.** A JATS-via-Elsevier artifact (where ``<originalText>``
  wraps a true ``<article>`` body) parses fine but yields zero
  ``<section>`` descendants for our walker — it would surface as an
  :class:`~litspectraits.errors.EmptyDocumentError`. Re-routing through
  :mod:`litspectraits.extract.jats` is the right response; we'll add the
  branch when we observe it on real corpus data.
- **CALS colspans are heuristic.** Real CEP tables reference
  ``<colspec colname="col1"/>`` etc. and put the spanning entry's
  ``namest="col1"`` / ``nameend="col3"`` attributes on the cell. Without
  resolving the ``colspec`` map we approximate by parsing trailing
  digits, which works for the common ``col<N>`` naming and degrades to
  ``colspan=1`` otherwise. ``morerows`` (rowspan) is exact.
"""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import structlog
from lxml import etree

from litspectraits.errors import (
    EmptyDocumentError,
    MalformedDocumentError,
    MissingArtifactError,
    WrongFormatForExtractorError,
)
from litspectraits.extract._lxml_helpers import (
    Counts,
    XrefSpan,
    all_descendants,
    ancestor_section_path,
    commit_document,
    first_child,
    first_child_text,
    first_descendant,
    first_descendant_text,
    full_text,
    local_findall,
    serialize_document,
    serialize_mathml,
    walk_paragraph_with_offsets,
)
from litspectraits.manifest import AcquisitionRecord, Extractor, ExtractRecord, Format
from litspectraits.store import ArtifactStore

# Schema is owned by us, not by Elsevier upstream. Bump ``SCHEMA_VERSION`` on
# any breaking change to the dict shape (field rename, semantic shift) so a
# downstream reader can refuse mismatched docs cleanly. Distinct from the
# JATS schema name so a future reader can branch on it cheaply, even though
# the field shape is intentionally aligned.
# SCHEMA_VERSION bumped to '2' when the xref descriptor gained ``start`` /
# ``end`` byte offsets — see :mod:`litspectraits.extract.jats` for the
# same change and rationale. The two XML extractors bump in lockstep so
# the normaliser can treat them uniformly.
# Version '3' adds display-mode equation blocks (``<ce:formula>``
# carrying MathML) as a third in-section block kind alongside paragraphs.
SCHEMA_NAME: Final = 'litspectraits-elsevier-extract'
SCHEMA_VERSION: Final = '3'

# Soft-cap on the inline label preserved for a ``<ce:cross-ref>`` (the
# visible bracketed citation marker). Same rationale as
# :data:`litspectraits.extract.jats._XREF_LABEL_MAX`.
_XREF_LABEL_MAX: Final = 200

_logger: Final = structlog.get_logger('litspectraits.extract.elsevier')


# Accumulator captured once per extraction; mutated by the walkers and
# frozen into ``Counts`` at the end. Same idiom as
# :class:`litspectraits.extract.jats._Acc`.
@dataclass
class _Acc:
    text_blocks: int = 0
    section_headers: int = 0
    tables: int = 0
    figures: int = 0
    equations: int = 0
    references: int = 0
    chars: int = 0


async def extract_elsevier(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
) -> ExtractRecord:
    """Convert an Elsevier artifact to ``documents/sha256/<aa>/<sha>/{document,meta}.json``.

    Five-stage pipeline mirroring :func:`~litspectraits.extract.jats.extract_jats`:
    preflight → parse (``lxml``) → META_ABS guard → walk → serialize → commit.
    Every failure raises an :class:`~litspectraits.errors.ExtractError`
    subclass before any on-disk write.

    The META_ABS guard is defence-in-depth against an artifact that slipped
    past the retriever's :class:`~litspectraits.errors.EntitlementDowngradeError`
    check (e.g. a stale sideload from before the retriever check landed).
    Sniffing keys on the root element name, which is identical between the
    full envelope and the abstract-only downgrade, so the body-presence
    check has to happen at parse time.

    Parameters
    ----------
    record : AcquisitionRecord
        Manifest of the committed Elsevier artifact. ``record.format`` must
        be :attr:`Format.ELSEVIER_XML`.
    store : ArtifactStore
        Used to resolve ``record.artifact_path`` and to stage the
        document/meta writes under ``store.tmp_dir``.
    reextract : bool, default False
        Overwrite an existing ``document.json`` whose bytes differ from the
        new extraction. Without this flag a divergent re-extract raises
        :class:`~litspectraits.errors.ExtractIntegrityError`.

    Returns
    -------
    ExtractRecord

    Raises
    ------
    WrongFormatForExtractorError
        ``record.format`` is not :attr:`Format.ELSEVIER_XML`.
    MissingArtifactError
        ``record.artifact_path`` does not resolve to an existing file.
    MalformedDocumentError
        ``lxml`` raised ``XMLSyntaxError``, the parsed root is not
        ``<full-text-retrieval-response>``, or the envelope lacks the
        ``<originalText>`` / ``<xocs:doc>`` full-text subtree (the
        META_ABS downgrade shape).
    EmptyDocumentError
        Zero non-empty text blocks recovered after a successful parse.
    SerializationError
        ``json.dumps`` rejected the structured payload.
    ExtractIntegrityError
        Existing ``document.json`` differs and ``reextract=False``.
    """
    artifact_path = _preflight(record=record, store=store)

    body_bytes = await asyncio.to_thread(artifact_path.read_bytes)
    root = _parse(doi=record.doi, body=body_bytes)
    _assert_has_full_text(doi=record.doi, root=root)

    document, counts = _walk(root)
    if counts.n_text_blocks == 0:
        raise EmptyDocumentError(
            doi=record.doi,
            extractor=Extractor.ELSEVIER.value,
            n_text_blocks=0,
            n_sections=counts.n_section_headers,
            hint=(
                'Elsevier XML parsed but recovered zero paragraph blocks; '
                'artifact may be JATS-via-Elsevier (re-route through extract_jats) '
                'or a stub envelope'
            ),
        )
    document['schema_name'] = SCHEMA_NAME
    document['schema_version'] = SCHEMA_VERSION

    body = serialize_document(
        doi=record.doi, document=document, extractor=Extractor.ELSEVIER
    )
    return commit_document(
        record=record,
        store=store,
        counts=counts,
        body=body,
        reextract=reextract,
        extractor=Extractor.ELSEVIER,
        schema_name=SCHEMA_NAME,
        schema_version=SCHEMA_VERSION,
        logger=_logger,
    )


# Stage 1 — preflight ---------------------------------------------------------


def _preflight(*, record: AcquisitionRecord, store: ArtifactStore) -> Path:
    """Resolve the artifact path and assert format + existence."""
    if record.format is not Format.ELSEVIER_XML:
        raise WrongFormatForExtractorError(
            doi=record.doi,
            expected=Format.ELSEVIER_XML.value,
            actual=record.format.value,
            extractor=Extractor.ELSEVIER.value,
        )
    artifact_path = store.data_dir / record.artifact_path
    if not artifact_path.is_file():
        raise MissingArtifactError(
            doi=record.doi,
            sha256=record.sha256,
            artifact_path=str(artifact_path),
            hint='re-run `litspectraits ingest <doi>` to refetch the artifact',
        )
    return artifact_path


# Stage 2 — parse -------------------------------------------------------------


def _parse(*, doi: str, body: bytes) -> etree._Element:
    """Run ``lxml.etree.fromstring`` and assert the Elsevier root.

    DTD resolution is disabled — Elsevier's DTDs reference internal-only
    URIs we have no business fetching at extract time.
    """
    parser = etree.XMLParser(
        load_dtd=False,
        no_network=True,
        recover=False,
        resolve_entities=False,
    )
    try:
        root = etree.fromstring(body, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise MalformedDocumentError(
            doi=doi,
            extractor=Extractor.ELSEVIER.value,
            hint='lxml could not parse the Elsevier body',
            error=str(exc),
        ) from exc
    local_root = etree.QName(root.tag).localname
    if local_root != 'full-text-retrieval-response':
        raise MalformedDocumentError(
            doi=doi,
            extractor=Extractor.ELSEVIER.value,
            hint='Elsevier root must be <full-text-retrieval-response>',
            actual_root=local_root,
        )
    return root


def _assert_has_full_text(*, doi: str, root: etree._Element) -> None:
    """Reject a META_ABS envelope that slipped past the retriever.

    The retriever raises
    :class:`~litspectraits.errors.EntitlementDowngradeError` on this shape,
    but the on-disk sniff is root-element-keyed and the META_ABS envelope
    shares the same ``<full-text-retrieval-response>`` root — so a stale
    artifact (or a future sideload-bypass) can still reach the extractor
    without an ``<originalText>`` body. Loud failure here mirrors the
    retriever-side check; surfaced as
    :class:`~litspectraits.errors.MalformedDocumentError` because the
    extract-side taxonomy doesn't carry an entitlement class.
    """
    original_text = first_descendant(root, 'originalText')
    xocs_doc = _first_xocs_doc(root)
    if original_text is None and xocs_doc is None:
        raise MalformedDocumentError(
            doi=doi,
            extractor=Extractor.ELSEVIER.value,
            hint=(
                'Elsevier envelope lacks <originalText> / <xocs:doc> — looks '
                'like a META_ABS abstract-only response. Re-fetch via '
                '`litspectraits ingest <doi>` to validate entitlement'
            ),
        )


# Stage 3 — walk --------------------------------------------------------------


def _walk(root: etree._Element) -> tuple[dict[str, Any], Counts]:
    """Walk the Elsevier tree once, building the dict and tallying counts."""
    acc = _Acc()
    front = _extract_front(root, acc=acc)
    sections = _extract_sections(root, acc=acc)
    tables = _extract_tables(root, acc=acc)
    figures = _extract_figures(root, acc=acc)
    references = _extract_references(root, acc=acc)
    document: dict[str, Any] = {
        'front': front,
        'sections': sections,
        'tables': tables,
        'figures': figures,
        'references': references,
    }
    counts = Counts(
        n_text_blocks=acc.text_blocks,
        n_section_headers=acc.section_headers,
        n_tables=acc.tables,
        n_figures=acc.figures,
        n_equations=acc.equations,
        n_references=acc.references,
        char_count=acc.chars,
    )
    return document, counts


def _extract_front(root: etree._Element, *, acc: _Acc) -> dict[str, Any]:
    """Pull title + abstract from ``<coredata>``.

    Elsevier's ``<coredata>`` carries Dublin Core fields (``dc:title``,
    ``dc:description``) for the title and abstract. CrossRef metadata
    already covers authors / year / journal, so we only surface the bits
    that live in the artifact.
    """
    coredata = first_descendant(root, 'coredata')
    if coredata is None:
        return {'title': None, 'abstract': None}
    title = None
    abstract = None
    title_el = first_descendant(coredata, 'title')
    if title_el is not None:
        title = full_text(title_el).strip() or None
        if title:
            acc.chars += len(title)
    description_el = first_descendant(coredata, 'description')
    if description_el is not None:
        text = full_text(description_el).strip()
        if text:
            abstract = text
            acc.chars += len(text)
    return {'title': title, 'abstract': abstract}


def _extract_sections(root: etree._Element, *, acc: _Acc) -> list[dict[str, Any]]:
    """Flatten the section tree into a depth-tagged list.

    Top-level sections sit under ``<ce:sections>``; we walk its direct
    ``<ce:section>`` children. If the container is absent (some article-
    type variants), fall back to "outermost ``<section>`` descendants of
    the root" so we still catch the content. Sections without an ``id``
    keep ``None`` so downstream code that expects every node to have an
    id sees the gap instead of being lied to.
    """
    sections: list[dict[str, Any]] = []

    def _walk_sec(sec: etree._Element, path: list[str | None]) -> None:
        sec_id = sec.get('id')
        sec_path: list[str | None] = [*path, sec_id]
        title_el = first_child(sec, 'section-title')
        title = full_text(title_el) if title_el is not None else ''
        if title:
            acc.section_headers += 1
            acc.chars += len(title)
        blocks = _extract_blocks(sec, acc=acc)
        sections.append(
            {
                'id': sec_id,
                'title': title or None,
                'level': len(sec_path),
                'path': sec_path,
                'blocks': blocks,
            }
        )
        for child in local_findall(sec, 'section'):
            _walk_sec(child, sec_path)

    body_container = first_descendant(root, 'sections')
    if body_container is not None:
        for sec in local_findall(body_container, 'section'):
            _walk_sec(sec, [])
        floating_blocks = _extract_blocks(body_container, acc=acc)
        if floating_blocks:
            sections.insert(
                0,
                {
                    'id': None,
                    'title': None,
                    'level': 1,
                    'path': [None],
                    'blocks': floating_blocks,
                },
            )
        return sections

    for sec in _outermost_sections(root):
        _walk_sec(sec, [])
    return sections


def _outermost_sections(root: etree._Element) -> list[etree._Element]:
    """Section descendants with no ancestor section, anywhere under ``root``.

    Fallback for envelopes that don't wrap top-level sections in a
    ``<ce:sections>`` container.
    """
    outermost: list[etree._Element] = []
    for el in root.iter():
        if etree.QName(el.tag).localname != 'section':
            continue
        ancestor = el.getparent()
        has_section_ancestor = False
        while ancestor is not None:
            if etree.QName(ancestor.tag).localname == 'section':
                has_section_ancestor = True
                break
            ancestor = ancestor.getparent()
        if not has_section_ancestor:
            outermost.append(el)
    return outermost


def _extract_blocks(parent: etree._Element, *, acc: _Acc) -> list[dict[str, Any]]:
    """Pull paragraph + display-equation blocks from ``parent``'s direct children.

    Nested ``<ce:section>`` content is handled by the section walker;
    iterating recursive descendants here would double-count those
    paragraphs. Reading order is preserved across the two block kinds
    (paragraph + equation), matching the JATS extractor.
    """
    blocks: list[dict[str, Any]] = []
    for child in parent:
        local = etree.QName(child.tag).localname
        if local == 'para':
            block = _paragraph_to_block(child)
            if block['text']:
                blocks.append(block)
                acc.text_blocks += 1
                acc.chars += len(block['text'])
        elif local == 'formula':
            equation = _formula_to_block(child)
            if equation is not None:
                blocks.append(equation)
                acc.equations += 1
                acc.chars += len(equation['text'])
    return blocks


def _paragraph_to_block(p: etree._Element) -> dict[str, Any]:
    """Turn a ``<ce:para>`` into a paragraph block, preserving inline cross-refs.

    Field shape mirrors :func:`litspectraits.extract.jats._paragraph_to_block`:
    the publisher-side attribute name differs (``refid`` here vs ``rid`` in
    JATS) but we surface it as ``rid`` so downstream code stays
    publisher-agnostic. ``start`` / ``end`` byte offsets land alongside,
    same purpose as the JATS extractor (E0.5a anchor support).
    """
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('cross-ref',))
    return {
        'type': 'paragraph',
        'text': text,
        'xrefs': [_xref_descriptor(span) for span in spans],
    }


def _formula_to_block(formula: etree._Element) -> dict[str, Any] | None:
    """Turn a ``<ce:formula>`` into an equation block dict.

    Same MathML-required contract as
    :func:`litspectraits.extract.jats._disp_formula_to_block`: only
    formulas carrying a ``<math>`` (MathML) descendant get surfaced; a
    bare textual formula returns ``None`` so the section's block list
    stays clean (no half-empty equation entries).
    """
    math_el = first_descendant(formula, 'math')
    if math_el is None:
        return None
    text = full_text(math_el)
    return {
        'type': 'equation',
        'id': formula.get('id'),
        'text': text,
        'mathml': serialize_mathml(math_el),
    }


def _xref_descriptor(span: XrefSpan) -> dict[str, Any]:
    xref = span.element
    label = (full_text(xref) or '').strip()
    if len(label) > _XREF_LABEL_MAX:
        label = label[:_XREF_LABEL_MAX]
    return {
        'rid': xref.get('refid'),
        # CEP doesn't encode an analogue of JATS's ``ref-type``; keep the
        # field present (publisher-agnostic shape) and ``None``.
        'ref_type': None,
        'label': label or None,
        'start': span.start,
        'end': span.end,
    }


def _extract_tables(root: etree._Element, *, acc: _Acc) -> list[dict[str, Any]]:
    if root is None:
        return []
    tables: list[dict[str, Any]] = []
    for table in root.iter():
        if etree.QName(table.tag).localname != 'table':
            continue
        cells = _table_cells(table)
        n_rows = len(cells)
        n_cols = max((len(row) for row in cells), default=0)
        caption = _ce_caption_text(table)
        if caption:
            acc.chars += len(caption)
        tables.append(
            {
                'id': table.get('id'),
                'label': first_child_text(table, 'label'),
                'caption': caption,
                'section_path': ancestor_section_path(table, section_localname='section'),
                'n_rows': n_rows,
                'n_cols': n_cols,
                'cells': cells,
            }
        )
        acc.tables += 1
    return tables


def _table_cells(table: etree._Element) -> list[list[dict[str, Any]]]:
    """Project a CALS ``<table>`` into a row-major 2-D list of cell dicts.

    Spans are encoded best-effort: ``rowspan`` from ``morerows`` (exact),
    ``colspan`` from ``namest``/``nameend`` via trailing-digit parsing
    (heuristic; see the module-level docstring). Downstream consumers
    that want a rendered grid resolve spans themselves.
    """
    rows_out: list[list[dict[str, Any]]] = []
    for row in all_descendants(table, 'row'):
        row_cells: list[dict[str, Any]] = []
        for cell in row:
            local = etree.QName(cell.tag).localname
            if local != 'entry':
                continue
            row_cells.append(
                {
                    'text': full_text(cell),
                    'type': 'entry',
                    'rowspan': _cals_rowspan(cell),
                    'colspan': _cals_colspan(cell),
                }
            )
        rows_out.append(row_cells)
    return rows_out


def _cals_rowspan(cell: etree._Element) -> int:
    """Resolve CALS ``morerows`` ("extra rows after this one") to rowspan.

    A cell with ``morerows="2"`` spans the current row plus the next two —
    so rowspan == 3.
    """
    morerows = cell.get('morerows')
    if not morerows:
        return 1
    try:
        return 1 + int(morerows)
    except ValueError:
        return 1


_COL_DIGIT_RE: Final = re.compile(r'(\d+)$')


def _cals_colspan(cell: etree._Element) -> int:
    """Best-effort CALS ``namest``/``nameend`` resolution.

    Real CEP tables reference ``<colspec colname="col1"/>`` etc. and
    encode the span via ``namest="col1" nameend="col3"`` on the cell.
    Without resolving the colspec map we approximate by parsing trailing
    digits, which works for the common ``col<N>`` naming and degrades to
    ``colspan=1`` otherwise (the worst-case behavior is "underreport
    span", never "lie"). Refine if and when this matters for downstream
    table parsing.
    """
    namest = cell.get('namest')
    nameend = cell.get('nameend')
    if not namest or not nameend:
        return 1
    start_match = _COL_DIGIT_RE.search(namest)
    end_match = _COL_DIGIT_RE.search(nameend)
    if start_match is None or end_match is None:
        return 1
    start = int(start_match.group(1))
    end = int(end_match.group(1))
    if end < start:
        return 1
    return end - start + 1


def _ce_caption_text(wrap: etree._Element) -> str | None:
    """Concatenate ``<ce:caption>/<ce:simple-para>`` text.

    Falls back to the caption's full text when no ``<ce:simple-para>``
    children are present (a minority pattern; some article types embed
    caption text directly).
    """
    caption = first_child(wrap, 'caption')
    if caption is None:
        return None
    parts = [full_text(p) for p in local_findall(caption, 'simple-para')]
    text = '\n\n'.join(p for p in parts if p)
    return text or full_text(caption) or None


def _extract_figures(root: etree._Element, *, acc: _Acc) -> list[dict[str, Any]]:
    if root is None:
        return []
    figures: list[dict[str, Any]] = []
    for fig in root.iter():
        if etree.QName(fig.tag).localname != 'figure':
            continue
        caption = _ce_caption_text(fig)
        if caption:
            acc.chars += len(caption)
        figures.append(
            {
                'id': fig.get('id'),
                'label': first_child_text(fig, 'label'),
                'caption': caption,
                'section_path': ancestor_section_path(fig, section_localname='section'),
            }
        )
        acc.figures += 1
    return figures


def _extract_references(root: etree._Element, *, acc: _Acc) -> list[dict[str, Any]]:
    """Pull the bibliography.

    CEP encodes references as ``<ce:bib-reference>``. The raw text comes
    from ``<ce:source-text>`` when present (Elsevier's pre-rendered
    citation string); otherwise we fall back to the concatenated text of
    the reference element. Structured fields walk into the nested
    ``<sb:reference>`` markup (Elsevier's structured-bibliography schema)
    via local-name xpath so namespace-prefix variants don't matter.
    """
    refs: list[dict[str, Any]] = []
    for ref in root.iter():
        if etree.QName(ref.tag).localname != 'bib-reference':
            continue
        source_text_el = first_descendant(ref, 'source-text')
        if source_text_el is not None:
            raw_text = full_text(source_text_el).strip()
        else:
            raw_text = full_text(ref).strip()
        raw_text = re.sub(r'\s+', ' ', raw_text)
        if raw_text:
            acc.chars += len(raw_text)
        refs.append(
            {
                'id': ref.get('id'),
                'raw_text': raw_text or None,
                'authors': _ref_authors(ref),
                'title': _ref_title(ref),
                'source': _ref_source(ref),
                'year': _ref_year(ref),
                'doi': _ref_doi(ref),
            }
        )
        acc.references += 1
    return refs


def _ref_authors(ref: etree._Element) -> list[str]:
    """Return ``"Surname, Given"`` strings, mirroring CrossRef's author shape.

    CEP encodes authors as ``<sb:author><ce:surname/><ce:given-name/></sb:author>``.
    """
    authors: list[str] = []
    for author in ref.iter():
        if etree.QName(author.tag).localname != 'author':
            continue
        surname = first_descendant_text(author, 'surname')
        given = first_descendant_text(author, 'given-name')
        if surname and given:
            authors.append(f'{surname}, {given}')
        elif surname:
            authors.append(surname)
    return authors


def _ref_title(ref: etree._Element) -> str | None:
    """Pull the article title from ``<sb:contribution>/<sb:title>/<sb:maintitle>``.

    Restricted to the *contribution* subtree so we don't accidentally pull
    the journal title (which lives in ``<sb:host>/.../<sb:maintitle>``).
    """
    for contribution in ref.iter():
        if etree.QName(contribution.tag).localname != 'contribution':
            continue
        maintitle = first_descendant(contribution, 'maintitle')
        if maintitle is not None:
            text = full_text(maintitle).strip()
            if text:
                return text
    return None


def _ref_source(ref: etree._Element) -> str | None:
    """Pull the journal / source title from inside ``<sb:host>``."""
    for host in ref.iter():
        if etree.QName(host.tag).localname != 'host':
            continue
        maintitle = first_descendant(host, 'maintitle')
        if maintitle is not None:
            text = full_text(maintitle).strip()
            if text:
                return text
    return None


def _ref_year(ref: etree._Element) -> str | None:
    """Pull the year from ``<sb:date>``."""
    for date in ref.iter():
        if etree.QName(date.tag).localname != 'date':
            continue
        text = full_text(date).strip()
        if text:
            return text
    return None


def _ref_doi(ref: etree._Element) -> str | None:
    """Pull the DOI from ``<ce:doi>``."""
    for doi_el in ref.iter():
        if etree.QName(doi_el.tag).localname != 'doi':
            continue
        text = full_text(doi_el).strip()
        if text:
            return text
    return None


# Elsevier-specific helpers ---------------------------------------------------


def _first_xocs_doc(node: etree._Element) -> etree._Element | None:
    """Match the namespace-scoped ``<xocs:doc>`` (not a bare ``<doc>``).

    A generic ``<doc>`` could exist in some Elsevier sub-namespaces (e.g.
    inside ``<dochead>``); pinning the namespace URI keeps the META_ABS
    guard from being fooled by an unrelated ``<doc>`` element.
    """
    for el in node.iter():
        qname = etree.QName(el.tag)
        if qname.localname == 'doc' and qname.namespace == 'http://www.elsevier.com/xml/xocs/dtd':
            return el
    return None


__all__ = ['SCHEMA_NAME', 'SCHEMA_VERSION', 'extract_elsevier']
