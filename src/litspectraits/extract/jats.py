"""JATS XML extractor (``docs/overview-v3.md`` §11).

JATS is what Springer Nature's TDM endpoint returns; the publisher has
already encoded section hierarchy, tables, references, and citation
anchors, so this extractor's job is to lift that structure into a stable
dict shape — *richer* than docling-on-PDF (§11) precisely because we
don't have to reconstruct anything.

The on-disk shape (``documents/sha256/<aa>/<sha>/document.json``) is a
publisher-agnostic-flavored dict:

- ``front``: title + abstract text.
- ``sections``: flattened list with ``id`` / ``title`` / ``level`` /
  ``path`` (root-to-leaf section ids) / ``blocks`` (paragraphs with
  inline-citation ``xrefs`` preserved).
- ``tables``: 2-D ``cells`` grid with ``rowspan`` / ``colspan`` and the
  caption + section path the table sits under.
- ``figures``: caption + section path; **no pixel data** (overview.md
  non-goal).
- ``references``: raw text plus the easy-to-recover structured fields
  (authors, title, source, year, DOI).

Cross-publisher normalization is **explicitly deferred** to the agent
triad work (§11) — the agent-side normaliser walks this dict; we don't
pre-collapse JATS quirks into a canonical form here.

The XML may or may not carry the JATS namespace (Springer Nature returns
the bare DTD form; other publishers use ``xmlns="https://jats.nlm.nih.gov/..."``);
all xpath uses ``local-name()`` so both shapes parse the same way. Same
defence the Elsevier retriever runs (``retrievers/elsevier.py``).
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
    walk_paragraph_with_offsets,
)
from litspectraits.manifest import AcquisitionRecord, Extractor, ExtractRecord, Format
from litspectraits.store import ArtifactStore

# Schema is owned by us, not by JATS upstream. Bump ``SCHEMA_VERSION`` on any
# breaking change to the dict shape (field rename, semantic shift) so a
# downstream reader can refuse mismatched docs cleanly.
# SCHEMA_VERSION bumped to '2' when the xref descriptor gained ``start`` /
# ``end`` byte offsets into the assembled paragraph text. The normaliser
# (E0.5a) depends on those offsets for the verbatim-anchor gate
# (``docs/normalized-documents-discussion.md`` §1.4). Bumping the version
# means an on-disk ``document.json`` written by an older extractor will
# fail the integrity check on re-extract — that is intentional; force
# a re-extract with ``--reextract`` after upgrading.
SCHEMA_NAME: Final = 'litspectraits-jats-extract'
SCHEMA_VERSION: Final = '2'

# Soft-cap on the inline label preserved for an `<xref>` (the visible
# bracketed citation marker, e.g. "[12]" or "Smith et al., 2019"). Long
# values almost always indicate the xref wraps a sentence and the label is
# meaningless; keeping a few hundred chars max keeps document.json compact.
_XREF_LABEL_MAX: Final = 200

_logger: Final = structlog.get_logger('litspectraits.extract.jats')


# Accumulator captured once per extraction; mutated by the walkers and
# frozen into ``Counts`` at the end. Mutable on purpose — using
# ``Counts`` as the working buffer would produce dataclass churn for no
# benefit.
@dataclass
class _Acc:
    text_blocks: int = 0
    section_headers: int = 0
    tables: int = 0
    figures: int = 0
    references: int = 0
    chars: int = 0


async def extract_jats(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
) -> ExtractRecord:
    """Convert a JATS artifact to ``documents/sha256/<aa>/<sha>/document.json`` + ``meta.json``.

    Five-stage pipeline mirroring :func:`~litspectraits.extract.pdf.extract_pdf`:
    preflight → parse (``lxml``) → walk → serialize → commit. Every failure
    raises an :class:`~litspectraits.errors.ExtractError` subclass before
    any on-disk write.

    Idempotent on identical extraction output: if
    ``documents/sha256/<aa>/<sha>/document.json`` already exists with bytes matching
    the freshly-serialized dict, the on-disk files are left untouched and
    the returned :class:`~litspectraits.manifest.ExtractRecord` describes
    the just-completed run.

    Parameters
    ----------
    record : AcquisitionRecord
        Manifest of the committed JATS artifact. ``record.format`` must
        be :attr:`Format.JATS_XML`.
    store : ArtifactStore
        Used to resolve ``record.artifact_path`` and to stage the
        document/meta writes under ``store.tmp_dir``.
    reextract : bool, default False
        Overwrite an existing ``document.json`` whose bytes differ from
        the new extraction. Without this flag a divergent re-extract
        raises :class:`~litspectraits.errors.ExtractIntegrityError`.

    Returns
    -------
    ExtractRecord

    Raises
    ------
    WrongFormatForExtractorError
        ``record.format`` is not :attr:`Format.JATS_XML`.
    MissingArtifactError
        ``record.artifact_path`` does not resolve to an existing file.
    MalformedDocumentError
        ``lxml`` raised ``XMLSyntaxError`` or the document carries no
        recognizable JATS root.
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

    document, counts = _walk(root)
    if counts.n_text_blocks == 0:
        raise EmptyDocumentError(
            doi=record.doi,
            extractor=Extractor.JATS.value,
            n_text_blocks=0,
            n_sections=counts.n_section_headers,
            hint=(
                'JATS parsed but recovered zero non-empty paragraph blocks; '
                'artifact likely a stub envelope or front-matter-only response'
            ),
        )
    document['schema_name'] = SCHEMA_NAME
    document['schema_version'] = SCHEMA_VERSION

    body = serialize_document(doi=record.doi, document=document, extractor=Extractor.JATS)
    return commit_document(
        record=record,
        store=store,
        counts=counts,
        body=body,
        reextract=reextract,
        extractor=Extractor.JATS,
        schema_name=SCHEMA_NAME,
        schema_version=SCHEMA_VERSION,
        logger=_logger,
    )


# Stage 1 — preflight ---------------------------------------------------------


def _preflight(*, record: AcquisitionRecord, store: ArtifactStore) -> Path:
    """Resolve the artifact path and assert format + existence."""
    if record.format is not Format.JATS_XML:
        raise WrongFormatForExtractorError(
            doi=record.doi,
            expected=Format.JATS_XML.value,
            actual=record.format.value,
            extractor=Extractor.JATS.value,
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
    """Run ``lxml.etree.fromstring`` and assert a JATS root.

    Anything that breaks here is a malformed-document failure (the sniff
    at ingest already validated the root element name; if lxml chokes the
    artifact is corrupted). DTD resolution is disabled — JATS DOCTYPEs
    reference external DTDs that we have no business fetching at extract
    time.
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
            extractor=Extractor.JATS.value,
            hint='lxml could not parse the JATS body',
            error=str(exc),
        ) from exc
    local_root = etree.QName(root.tag).localname
    if local_root != 'article':
        raise MalformedDocumentError(
            doi=doi,
            extractor=Extractor.JATS.value,
            hint='JATS root must be <article>',
            actual_root=local_root,
        )
    return root


# Stage 3 — walk --------------------------------------------------------------


def _walk(root: etree._Element) -> tuple[dict[str, Any], Counts]:
    """Walk the JATS tree once, building the dict and tallying counts."""
    acc = _Acc()
    front = _extract_front(root, acc=acc)
    body_root = first_child(root, 'body')
    sections = _extract_sections(body_root, acc=acc)
    tables = _extract_tables(body_root, acc=acc)
    figures = _extract_figures(body_root, acc=acc)
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
        n_references=acc.references,
        char_count=acc.chars,
    )
    return document, counts


def _extract_front(root: etree._Element, *, acc: _Acc) -> dict[str, Any]:
    """Pull title + abstract out of ``<front>``.

    The CrossRef metadata side already carries authors / year / journal,
    so we only surface the bits that are *only* in the artifact
    (title text proper and the abstract paragraphs).
    """
    title = None
    abstract: list[str] = []
    front = first_child(root, 'front')
    if front is None:
        return {'title': None, 'abstract': None}
    title_el = first_descendant(front, 'article-title')
    if title_el is not None:
        title = full_text(title_el)
        acc.chars += len(title)
    # Abstract typically sits under ``<article-meta>``, not directly under
    # ``<front>``; walk the whole subtree so we find it either way.
    for abstract_el in all_descendants(front, 'abstract'):
        for p in local_findall(abstract_el, 'p'):
            text = full_text(p)
            if text:
                abstract.append(text)
                acc.chars += len(text)
    return {
        'title': title,
        'abstract': '\n\n'.join(abstract) if abstract else None,
    }


def _extract_sections(body: etree._Element | None, *, acc: _Acc) -> list[dict[str, Any]]:
    """Flatten the section tree into a depth-tagged list.

    Sections without an ``id`` attribute keep ``None`` so downstream code
    that expects every node to have an id sees the gap instead of being
    lied to. ``path`` is the chain of section ids from the root; an
    unidentified ancestor leaves ``None`` in its slot.
    """
    if body is None:
        return []
    sections: list[dict[str, Any]] = []

    def _walk_sec(sec: etree._Element, path: list[str | None]) -> None:
        sec_id = sec.get('id')
        sec_path: list[str | None] = [*path, sec_id]
        title_el = first_child(sec, 'title')
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
        for child in local_findall(sec, 'sec'):
            _walk_sec(child, sec_path)

    for sec in local_findall(body, 'sec'):
        _walk_sec(sec, [])
    # JATS bodies sometimes carry top-level paragraphs outside any <sec>.
    # Surface them under a synthetic section so the dict shape stays
    # uniform (one bucket of blocks ↔ one section entry).
    floating_blocks = _extract_blocks(body, acc=acc)
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


def _extract_blocks(parent: etree._Element, *, acc: _Acc) -> list[dict[str, Any]]:
    """Pull paragraph blocks out of ``parent``'s direct children.

    Nested ``<sec>`` content is handled by the section walker; iterating
    recursive descendants here would double-count those paragraphs.
    """
    blocks: list[dict[str, Any]] = []
    for child in parent:
        if etree.QName(child.tag).localname != 'p':
            continue
        block = _paragraph_to_block(child)
        if block['text']:
            blocks.append(block)
            acc.text_blocks += 1
            acc.chars += len(block['text'])
    return blocks


def _paragraph_to_block(p: etree._Element) -> dict[str, Any]:
    """Turn a ``<p>`` element into a paragraph block, preserving inline xrefs.

    The full text-with-tails is what a reader sees; each xref descriptor
    records the surface label + ``rid`` + ``ref-type`` *and* the
    ``(start, end)`` byte offsets into the assembled text so the
    normaliser can rebuild a verbatim-anchor-eligible ``InlineRef``
    without re-walking the XML (E0.5a, ``docs/normalized-documents-discussion.md``
    §1.4).
    """
    text, spans = walk_paragraph_with_offsets(p, xref_localnames=('xref',))
    return {
        'type': 'paragraph',
        'text': text,
        'xrefs': [_xref_descriptor(span) for span in spans],
    }


def _xref_descriptor(span: XrefSpan) -> dict[str, Any]:
    xref = span.element
    label = (full_text(xref) or '').strip()
    if len(label) > _XREF_LABEL_MAX:
        label = label[:_XREF_LABEL_MAX]
    return {
        'rid': xref.get('rid'),
        'ref_type': xref.get('ref-type'),
        'label': label or None,
        'start': span.start,
        'end': span.end,
    }


def _extract_tables(body: etree._Element | None, *, acc: _Acc) -> list[dict[str, Any]]:
    if body is None:
        return []
    tables: list[dict[str, Any]] = []
    for wrap in body.iter():
        if etree.QName(wrap.tag).localname != 'table-wrap':
            continue
        table_el = first_descendant(wrap, 'table')
        if table_el is None:
            continue
        cells = _table_cells(table_el)
        n_rows = len(cells)
        n_cols = max((len(row) for row in cells), default=0)
        caption = _table_caption_text(wrap)
        if caption:
            acc.chars += len(caption)
        tables.append(
            {
                'id': wrap.get('id'),
                'label': first_child_text(wrap, 'label'),
                'caption': caption,
                'section_path': ancestor_section_path(wrap, section_localname='sec'),
                'n_rows': n_rows,
                'n_cols': n_cols,
                'cells': cells,
            }
        )
        acc.tables += 1
    return tables


def _table_cells(table: etree._Element) -> list[list[dict[str, Any]]]:
    """Project a ``<table>`` into a row-major 2-D list of cell dicts.

    Spans are preserved verbatim from the JATS attributes; we do not
    expand them into duplicated cells. Downstream consumers that want a
    rendered grid can do that themselves.
    """
    rows_out: list[list[dict[str, Any]]] = []
    for row in all_descendants(table, 'tr'):
        row_cells: list[dict[str, Any]] = []
        for cell in row:
            local = etree.QName(cell.tag).localname
            if local not in {'th', 'td'}:
                continue
            row_cells.append(
                {
                    'text': full_text(cell),
                    'type': local,
                    'rowspan': _int_attr(cell, 'rowspan', default=1),
                    'colspan': _int_attr(cell, 'colspan', default=1),
                }
            )
        rows_out.append(row_cells)
    return rows_out


def _table_caption_text(wrap: etree._Element) -> str | None:
    caption = first_child(wrap, 'caption')
    if caption is None:
        return None
    parts = [full_text(p) for p in local_findall(caption, 'p')]
    text = '\n\n'.join(p for p in parts if p)
    return text or full_text(caption) or None


def _extract_figures(body: etree._Element | None, *, acc: _Acc) -> list[dict[str, Any]]:
    if body is None:
        return []
    figures: list[dict[str, Any]] = []
    for fig in body.iter():
        if etree.QName(fig.tag).localname != 'fig':
            continue
        caption = _table_caption_text(fig)  # same <caption><p>...</p> shape
        if caption:
            acc.chars += len(caption)
        figures.append(
            {
                'id': fig.get('id'),
                'label': first_child_text(fig, 'label'),
                'caption': caption,
                'section_path': ancestor_section_path(fig, section_localname='sec'),
            }
        )
        acc.figures += 1
    return figures


def _extract_references(root: etree._Element, *, acc: _Acc) -> list[dict[str, Any]]:
    """Pull the back-matter reference list.

    Only the easy-to-recover structured fields are surfaced; full
    citation parsing (anystyle / refextract / LLM-extract) is a separate
    post-pass per ``extract-pdf-plan.md`` §1.
    """
    refs: list[dict[str, Any]] = []
    for ref in root.iter():
        if etree.QName(ref.tag).localname != 'ref':
            continue
        citation = first_descendant(ref, 'element-citation')
        if citation is None:
            citation = first_descendant(ref, 'mixed-citation')
        scope = citation if citation is not None else ref
        raw_text = full_text(scope).strip()
        raw_text = re.sub(r'\s+', ' ', raw_text)
        if raw_text:
            acc.chars += len(raw_text)
        refs.append(
            {
                'id': ref.get('id'),
                'raw_text': raw_text or None,
                'authors': _ref_authors(scope),
                'title': first_descendant_text(scope, 'article-title'),
                'source': first_descendant_text(scope, 'source'),
                'year': first_descendant_text(scope, 'year'),
                'doi': _ref_doi(scope),
            }
        )
        acc.references += 1
    return refs


def _ref_authors(ref: etree._Element) -> list[str]:
    """Return ``"Surname, Given"`` strings, mirroring CrossRef's author shape."""
    authors: list[str] = []
    for name in ref.iter():
        if etree.QName(name.tag).localname != 'name':
            continue
        surname = first_child_text(name, 'surname')
        given = first_child_text(name, 'given-names')
        if surname and given:
            authors.append(f'{surname}, {given}')
        elif surname:
            authors.append(surname)
    return authors


def _ref_doi(ref: etree._Element) -> str | None:
    for pub_id in ref.iter():
        if etree.QName(pub_id.tag).localname != 'pub-id':
            continue
        if pub_id.get('pub-id-type') == 'doi':
            text = full_text(pub_id).strip()
            return text or None
    return None


# JATS-specific helpers -------------------------------------------------------


def _int_attr(node: etree._Element, name: str, *, default: int) -> int:
    """Parse an integer attribute, falling back to ``default`` on absence / shape error.

    Used to read ``rowspan`` / ``colspan`` off table cells where a
    malformed value is more likely than a missing one — JATS tables in
    the wild occasionally carry stray non-integer strings, and a
    silently-tolerated default beats raising mid-walk.
    """
    raw = node.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Re-export so ``extract/jats.py`` is the obvious place to look for the
# field definitions a downstream reader needs to align with.
__all__ = ['SCHEMA_NAME', 'SCHEMA_VERSION', 'extract_jats']
