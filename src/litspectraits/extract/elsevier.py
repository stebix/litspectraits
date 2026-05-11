"""Elsevier full-text XML extractor (``docs/overview-v3.md`` §11).

Elsevier's full-text retrieval endpoint returns its own ``<full-text-
retrieval-response>`` envelope (declared in
``http://www.elsevier.com/xml/svapi/article/dtd``). The body lives under
``<originalText>`` / ``<xocs:doc>`` and is typically written in Elsevier's
Common Element Pool (CEP) — ``<ce:section>`` / ``<ce:para>`` /
``<ce:table>`` etc. — distinct from JATS in both element names *and* the
table model (CALS/OASIS in CEP vs HTML-style in JATS).

The on-disk shape (``documents/<sha>/document.json``) is the same
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
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import structlog
from lxml import etree

from litspectraits.errors import (
    EmptyDocumentError,
    ExtractIntegrityError,
    MalformedDocumentError,
    MissingArtifactError,
    SerializationError,
    WrongFormatForExtractorError,
)
from litspectraits.manifest import AcquisitionRecord, Extractor, ExtractRecord, Format
from litspectraits.store import ArtifactStore

# Schema is owned by us, not by Elsevier upstream. Bump ``SCHEMA_VERSION`` on
# any breaking change to the dict shape (field rename, semantic shift) so a
# downstream reader can refuse mismatched docs cleanly. Distinct from the
# JATS schema name so a future reader can branch on it cheaply, even though
# the field shape is intentionally aligned.
SCHEMA_NAME: Final = 'litspectraits-elsevier-extract'
SCHEMA_VERSION: Final = '1'

_DOCUMENT_FILENAME: Final = 'document.json'
_META_FILENAME: Final = 'meta.json'

# Soft-cap on the inline label preserved for a ``<ce:cross-ref>`` (the
# visible bracketed citation marker). Same rationale as
# :data:`litspectraits.extract.jats._XREF_LABEL_MAX`.
_XREF_LABEL_MAX: Final = 200

_logger: Final = structlog.get_logger('litspectraits.extract.elsevier')

# Local-name predicate so namespace-prefix variants (``ce:section`` vs the
# default-namespaced ``section``) both match. See
# :mod:`litspectraits.extract.jats` for the design.
_LOCAL_NAME_CHILDREN: Final = './*[local-name()=$name]'


@dataclass(frozen=True)
class _Counts:
    """Structural counters tallied during the single document walk."""

    n_text_blocks: int
    n_section_headers: int
    n_tables: int
    n_figures: int
    n_references: int
    char_count: int


# Accumulator captured once per extraction; mutated by the walkers and
# frozen into ``_Counts`` at the end. Same idiom as
# :class:`litspectraits.extract.jats._Acc`.
@dataclass
class _Acc:
    text_blocks: int = 0
    section_headers: int = 0
    tables: int = 0
    figures: int = 0
    references: int = 0
    chars: int = 0


async def extract_elsevier(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
) -> ExtractRecord:
    """Convert an Elsevier artifact to ``documents/<sha>/document.json`` + ``meta.json``.

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

    body = _serialize_document(doi=record.doi, document=document)
    return _commit(
        record=record,
        store=store,
        counts=counts,
        body=body,
        reextract=reextract,
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
    original_text = _first_descendant(root, 'originalText')
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


def _walk(root: etree._Element) -> tuple[dict[str, Any], _Counts]:
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
    counts = _Counts(
        n_text_blocks=acc.text_blocks,
        n_section_headers=acc.section_headers,
        n_tables=acc.tables,
        n_figures=acc.figures,
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
    coredata = _first_descendant(root, 'coredata')
    if coredata is None:
        return {'title': None, 'abstract': None}
    title = None
    abstract = None
    title_el = _first_descendant(coredata, 'title')
    if title_el is not None:
        title = _full_text(title_el).strip() or None
        if title:
            acc.chars += len(title)
    description_el = _first_descendant(coredata, 'description')
    if description_el is not None:
        text = _full_text(description_el).strip()
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
        title_el = _first_child(sec, 'section-title')
        title = _full_text(title_el) if title_el is not None else ''
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
        for child in _local_findall(sec, 'section'):
            _walk_sec(child, sec_path)

    body_container = _first_descendant(root, 'sections')
    if body_container is not None:
        for sec in _local_findall(body_container, 'section'):
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
    """Pull paragraph blocks out of ``parent``'s direct children.

    Nested ``<ce:section>`` content is handled by the section walker;
    iterating recursive descendants here would double-count those
    paragraphs.
    """
    blocks: list[dict[str, Any]] = []
    for child in parent:
        if etree.QName(child.tag).localname != 'para':
            continue
        block = _paragraph_to_block(child)
        if block['text']:
            blocks.append(block)
            acc.text_blocks += 1
            acc.chars += len(block['text'])
    return blocks


def _paragraph_to_block(p: etree._Element) -> dict[str, Any]:
    """Turn a ``<ce:para>`` into a paragraph block, preserving inline cross-refs.

    Field shape mirrors :func:`litspectraits.extract.jats._paragraph_to_block`:
    the publisher-side attribute name differs (``refid`` here vs ``rid`` in
    JATS) but we surface it as ``rid`` so downstream code stays
    publisher-agnostic.
    """
    return {
        'type': 'paragraph',
        'text': _full_text(p),
        'xrefs': [_xref_descriptor(x) for x in _local_findall(p, 'cross-ref')],
    }


def _xref_descriptor(xref: etree._Element) -> dict[str, Any]:
    label = (_full_text(xref) or '').strip()
    if len(label) > _XREF_LABEL_MAX:
        label = label[:_XREF_LABEL_MAX]
    return {
        'rid': xref.get('refid'),
        # CEP doesn't encode an analogue of JATS's ``ref-type``; keep the
        # field present (publisher-agnostic shape) and ``None``.
        'ref_type': None,
        'label': label or None,
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
                'label': _first_child_text(table, 'label'),
                'caption': caption,
                'section_path': _ancestor_section_path(table),
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
    for row in _all_descendants(table, 'row'):
        row_cells: list[dict[str, Any]] = []
        for cell in row:
            local = etree.QName(cell.tag).localname
            if local != 'entry':
                continue
            row_cells.append(
                {
                    'text': _full_text(cell),
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
    caption = _first_child(wrap, 'caption')
    if caption is None:
        return None
    parts = [_full_text(p) for p in _local_findall(caption, 'simple-para')]
    text = '\n\n'.join(p for p in parts if p)
    return text or _full_text(caption) or None


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
                'label': _first_child_text(fig, 'label'),
                'caption': caption,
                'section_path': _ancestor_section_path(fig),
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
        source_text_el = _first_descendant(ref, 'source-text')
        if source_text_el is not None:
            raw_text = _full_text(source_text_el).strip()
        else:
            raw_text = _full_text(ref).strip()
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
        surname = _first_descendant_text(author, 'surname')
        given = _first_descendant_text(author, 'given-name')
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
        maintitle = _first_descendant(contribution, 'maintitle')
        if maintitle is not None:
            text = _full_text(maintitle).strip()
            if text:
                return text
    return None


def _ref_source(ref: etree._Element) -> str | None:
    """Pull the journal / source title from inside ``<sb:host>``."""
    for host in ref.iter():
        if etree.QName(host.tag).localname != 'host':
            continue
        maintitle = _first_descendant(host, 'maintitle')
        if maintitle is not None:
            text = _full_text(maintitle).strip()
            if text:
                return text
    return None


def _ref_year(ref: etree._Element) -> str | None:
    """Pull the year from ``<sb:date>``."""
    for date in ref.iter():
        if etree.QName(date.tag).localname != 'date':
            continue
        text = _full_text(date).strip()
        if text:
            return text
    return None


def _ref_doi(ref: etree._Element) -> str | None:
    """Pull the DOI from ``<ce:doi>``."""
    for doi_el in ref.iter():
        if etree.QName(doi_el.tag).localname != 'doi':
            continue
        text = _full_text(doi_el).strip()
        if text:
            return text
    return None


# XPath / text helpers --------------------------------------------------------
#
# Mirror :mod:`litspectraits.extract.jats`. Cross-extractor consolidation is
# the natural follow-up to 10d (the JATS module foreshadows this). Left
# duplicated for this commit to keep the diff focused on landing the
# Elsevier extractor.


def _local_findall(node: etree._Element | None, name: str) -> list[etree._Element]:
    if node is None:
        return []
    return cast(list[etree._Element], node.xpath(_LOCAL_NAME_CHILDREN, name=name))


def _first_child(node: etree._Element | None, name: str) -> etree._Element | None:
    matches = _local_findall(node, name)
    return matches[0] if matches else None


def _first_descendant(node: etree._Element | None, name: str) -> etree._Element | None:
    if node is None:
        return None
    for child in node.iter():
        if etree.QName(child.tag).localname == name:
            return child
    return None


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


def _all_descendants(node: etree._Element, name: str) -> list[etree._Element]:
    return [child for child in node.iter() if etree.QName(child.tag).localname == name]


def _first_child_text(node: etree._Element | None, name: str) -> str | None:
    child = _first_child(node, name)
    if child is None:
        return None
    text = _full_text(child).strip()
    return text or None


def _first_descendant_text(node: etree._Element | None, name: str) -> str | None:
    child = _first_descendant(node, name)
    if child is None:
        return None
    text = _full_text(child).strip()
    return text or None


def _full_text(node: etree._Element | None) -> str:
    """Concatenated text content of ``node`` and its descendants.

    Whitespace inside the element is preserved as-is — CEP paragraphs use
    significant whitespace around inline ``<ce:cross-ref>`` elements, and
    collapsing it would make ``[ 12 ]`` indistinguishable from ``[12]``
    for downstream citation matching.
    """
    if node is None:
        return ''
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(_full_text(child))
        if child.tail:
            parts.append(child.tail)
    return ''.join(parts)


def _ancestor_section_path(node: etree._Element) -> list[str | None]:
    """Walk up to the first non-section ancestor, recording section ids."""
    path: list[str | None] = []
    parent = node.getparent()
    while parent is not None:
        if etree.QName(parent.tag).localname == 'section':
            path.append(parent.get('id'))
        parent = parent.getparent()
    return list(reversed(path))


# Stage 4 — serialize ---------------------------------------------------------


def _serialize_document(*, doi: str, document: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            doi=doi,
            extractor=Extractor.ELSEVIER.value,
            hint='extracted document carried a value json.dumps could not encode',
            error=str(exc),
        ) from exc
    return (text + '\n').encode('utf-8')


# Stage 5 — commit ------------------------------------------------------------


def _commit(
    *,
    record: AcquisitionRecord,
    store: ArtifactStore,
    counts: _Counts,
    body: bytes,
    reextract: bool,
) -> ExtractRecord:
    """Atomically install ``document.json`` + ``meta.json``; enforce integrity.

    Same idempotency-on-identical-bytes contract as
    :func:`litspectraits.extract.jats._commit`. Duplicated rather than
    abstracted; see the module-level note on consolidation.
    """
    target_dir = store.document_dir(record.sha256)
    target_doc = target_dir / _DOCUMENT_FILENAME
    target_meta = target_dir / _META_FILENAME

    new_sha = hashlib.sha256(body).hexdigest()
    if target_doc.exists():
        existing_sha = _file_sha256(target_doc)
        if existing_sha == new_sha:
            _logger.info(
                'extract no-op; document.json bytes unchanged',
                doi=record.doi,
                sha256=record.sha256,
                document_sha256=new_sha,
            )
            return _build_extract_record(
                record=record, counts=counts, extracted_at=datetime.now(tz=UTC)
            )
        if not reextract:
            raise ExtractIntegrityError(
                doi=record.doi,
                sha256=record.sha256,
                existing_document_sha256=existing_sha,
                incoming_document_sha256=new_sha,
                hint='re-extracted document differs; pass --reextract to overwrite',
            )

    target_dir.mkdir(parents=True, exist_ok=True)
    extracted_at = datetime.now(tz=UTC)
    extract_record = _build_extract_record(record=record, counts=counts, extracted_at=extracted_at)
    meta_payload = _build_meta(record=record, counts=counts, extracted_at=extracted_at)
    meta_bytes = (json.dumps(meta_payload, indent=2, sort_keys=True) + '\n').encode('utf-8')

    _atomic_write(tmp_dir=store.tmp_dir, target=target_doc, body=body)
    _atomic_write(tmp_dir=store.tmp_dir, target=target_meta, body=meta_bytes)

    _logger.info(
        'extract committed',
        doi=record.doi,
        sha256=record.sha256,
        document_sha256=new_sha,
        n_text_blocks=counts.n_text_blocks,
        n_section_headers=counts.n_section_headers,
        n_tables=counts.n_tables,
        n_figures=counts.n_figures,
        n_references=counts.n_references,
        char_count=counts.char_count,
    )
    return extract_record


def _build_extract_record(
    *,
    record: AcquisitionRecord,
    counts: _Counts,
    extracted_at: datetime,
) -> ExtractRecord:
    return ExtractRecord(
        sha256=record.sha256,
        extractor=Extractor.ELSEVIER,
        extractor_version=f'{SCHEMA_NAME}/{SCHEMA_VERSION}',
        extracted_at=extracted_at,
        n_text_blocks=counts.n_text_blocks,
        n_section_headers=counts.n_section_headers,
        n_tables=counts.n_tables,
        n_figures=counts.n_figures,
        char_count=counts.char_count,
        n_pages=None,
    )


def _build_meta(
    *,
    record: AcquisitionRecord,
    counts: _Counts,
    extracted_at: datetime,
) -> dict[str, Any]:
    """Assemble ``meta.json``.

    Symmetric with the JATS ``meta.json``: no ``pipeline`` block (Elsevier
    extraction has no tunable knobs beyond the lxml parser settings, which
    are hard-wired) and no ``n_pages`` (Elsevier XML carries no page
    concept).
    """
    return {
        'extractor': Extractor.ELSEVIER.value,
        'extractor_version': f'{SCHEMA_NAME}/{SCHEMA_VERSION}',
        'schema_name': SCHEMA_NAME,
        'schema_version': SCHEMA_VERSION,
        'format': record.format.value,
        'source_sha256': record.sha256,
        'extracted_at': extracted_at.isoformat(),
        'n_text_blocks': counts.n_text_blocks,
        'n_section_headers': counts.n_section_headers,
        'n_tables': counts.n_tables,
        'n_figures': counts.n_figures,
        'n_references': counts.n_references,
        'char_count': counts.char_count,
    }


def _atomic_write(*, tmp_dir: Path, target: Path, body: bytes) -> None:
    """Stage to ``<tmp_dir>/<rand>.part`` then ``os.replace`` into place."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    staging = tmp_dir / f'{target.name}.{secrets.token_hex(8)}.part'
    staging.write_bytes(body)
    os.replace(staging, target)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as fp:
        while chunk := fp.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ['SCHEMA_NAME', 'SCHEMA_VERSION', 'extract_elsevier']
