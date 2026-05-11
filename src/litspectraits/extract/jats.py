"""JATS XML extractor (``docs/overview-v3.md`` §11).

JATS is what Springer Nature's TDM endpoint returns; the publisher has
already encoded section hierarchy, tables, references, and citation
anchors, so this extractor's job is to lift that structure into a stable
dict shape — *richer* than docling-on-PDF (§11) precisely because we
don't have to reconstruct anything.

The on-disk shape (``documents/<sha>/document.json``) is a
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

# Schema is owned by us, not by JATS upstream. Bump ``SCHEMA_VERSION`` on any
# breaking change to the dict shape (field rename, semantic shift) so a
# downstream reader can refuse mismatched docs cleanly.
SCHEMA_NAME: Final = 'litspectraits-jats-extract'
SCHEMA_VERSION: Final = '1'

_DOCUMENT_FILENAME: Final = 'document.json'
_META_FILENAME: Final = 'meta.json'

# Soft-cap on the inline label preserved for an `<xref>` (the visible
# bracketed citation marker, e.g. "[12]" or "Smith et al., 2019"). Long
# values almost always indicate the xref wraps a sentence and the label is
# meaningless; keeping a few hundred chars max keeps document.json compact.
_XREF_LABEL_MAX: Final = 200

_logger: Final = structlog.get_logger('litspectraits.extract.jats')

# Local-name predicates so namespace prefix variants (``jats:sec`` vs ``sec``)
# both match. ``iterfind`` does not support the local-name() XPath function
# (its grammar is a strict subset of XPath 1.0), so we use ``xpath()``.
# ``./`` anchors at the receiver, ``*`` selects all direct children.
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
# frozen into ``_Counts`` at the end. Mutable on purpose — using
# ``_Counts`` as the working buffer would produce dataclass churn for no
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
    """Convert a JATS artifact to ``documents/<sha>/document.json`` + ``meta.json``.

    Five-stage pipeline mirroring :func:`~litspectraits.extract.pdf.extract_pdf`:
    preflight → parse (``lxml``) → walk → serialize → commit. Every failure
    raises an :class:`~litspectraits.errors.ExtractError` subclass before
    any on-disk write.

    Idempotent on identical extraction output: if
    ``documents/<sha>/document.json`` already exists with bytes matching
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


def _walk(root: etree._Element) -> tuple[dict[str, Any], _Counts]:
    """Walk the JATS tree once, building the dict and tallying counts."""
    acc = _Acc()
    front = _extract_front(root, acc=acc)
    body_root = _first_child(root, 'body')
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
    """Pull title + abstract out of ``<front>``.

    The CrossRef metadata side already carries authors / year / journal,
    so we only surface the bits that are *only* in the artifact
    (title text proper and the abstract paragraphs).
    """
    title = None
    abstract: list[str] = []
    front = _first_child(root, 'front')
    if front is None:
        return {'title': None, 'abstract': None}
    title_el = _first_descendant(front, 'article-title')
    if title_el is not None:
        title = _full_text(title_el)
        acc.chars += len(title)
    # Abstract typically sits under ``<article-meta>``, not directly under
    # ``<front>``; walk the whole subtree so we find it either way.
    for abstract_el in _all_descendants(front, 'abstract'):
        for p in _local_findall(abstract_el, 'p'):
            text = _full_text(p)
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
        title_el = _first_child(sec, 'title')
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
        for child in _local_findall(sec, 'sec'):
            _walk_sec(child, sec_path)

    for sec in _local_findall(body, 'sec'):
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

    The full text-with-tails is what a reader sees; the xref descriptors
    record the surface label + ``rid`` + ``ref-type`` so downstream code
    can resolve ``[12]`` back to ``references[id == 'R12']``.
    """
    return {
        'type': 'paragraph',
        'text': _full_text(p),
        'xrefs': [_xref_descriptor(x) for x in _local_findall(p, 'xref')],
    }


def _xref_descriptor(xref: etree._Element) -> dict[str, Any]:
    label = (_full_text(xref) or '').strip()
    if len(label) > _XREF_LABEL_MAX:
        label = label[:_XREF_LABEL_MAX]
    return {
        'rid': xref.get('rid'),
        'ref_type': xref.get('ref-type'),
        'label': label or None,
    }


def _extract_tables(body: etree._Element | None, *, acc: _Acc) -> list[dict[str, Any]]:
    if body is None:
        return []
    tables: list[dict[str, Any]] = []
    for wrap in body.iter():
        if etree.QName(wrap.tag).localname != 'table-wrap':
            continue
        table_el = _first_descendant(wrap, 'table')
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
                'label': _first_child_text(wrap, 'label'),
                'caption': caption,
                'section_path': _ancestor_section_path(wrap),
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
    for row in _all_descendants(table, 'tr'):
        row_cells: list[dict[str, Any]] = []
        for cell in row:
            local = etree.QName(cell.tag).localname
            if local not in {'th', 'td'}:
                continue
            row_cells.append(
                {
                    'text': _full_text(cell),
                    'type': local,
                    'rowspan': _int_attr(cell, 'rowspan', default=1),
                    'colspan': _int_attr(cell, 'colspan', default=1),
                }
            )
        rows_out.append(row_cells)
    return rows_out


def _table_caption_text(wrap: etree._Element) -> str | None:
    caption = _first_child(wrap, 'caption')
    if caption is None:
        return None
    parts = [_full_text(p) for p in _local_findall(caption, 'p')]
    text = '\n\n'.join(p for p in parts if p)
    return text or _full_text(caption) or None


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
                'label': _first_child_text(fig, 'label'),
                'caption': caption,
                'section_path': _ancestor_section_path(fig),
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
        citation = _first_descendant(ref, 'element-citation')
        if citation is None:
            citation = _first_descendant(ref, 'mixed-citation')
        scope = citation if citation is not None else ref
        raw_text = _full_text(scope).strip()
        raw_text = re.sub(r'\s+', ' ', raw_text)
        if raw_text:
            acc.chars += len(raw_text)
        refs.append(
            {
                'id': ref.get('id'),
                'raw_text': raw_text or None,
                'authors': _ref_authors(scope),
                'title': _first_descendant_text(scope, 'article-title'),
                'source': _first_descendant_text(scope, 'source'),
                'year': _first_descendant_text(scope, 'year'),
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
        surname = _first_child_text(name, 'surname')
        given = _first_child_text(name, 'given-names')
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
            text = _full_text(pub_id).strip()
            return text or None
    return None


# XPath / text helpers --------------------------------------------------------


def _local_findall(node: etree._Element | None, name: str) -> list[etree._Element]:
    """Children with local-name == ``name``, namespace-agnostic.

    The ``$name`` xpath variable means we don't have to escape ``name``
    for embedding into the expression — keeps the helper safe against
    accidental injection if a future caller passes user-controlled
    strings.
    """
    if node is None:
        return []
    # ``./*[local-name()=$name]`` always yields a node-set, never a scalar,
    # so the cast is sound; lxml's union return type is the conservative
    # one for arbitrary xpath expressions.
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

    Whitespace inside the element is preserved as-is — JATS uses
    significant whitespace inside paragraphs, and collapsing it would
    make ``[ 12 ]`` indistinguishable from ``[12]`` for downstream
    citation matching.
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
        if etree.QName(parent.tag).localname == 'sec':
            path.append(parent.get('id'))
        parent = parent.getparent()
    return list(reversed(path))


def _int_attr(node: etree._Element, name: str, *, default: int) -> int:
    raw = node.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Stage 4 — serialize ---------------------------------------------------------


def _serialize_document(*, doi: str, document: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            doi=doi,
            extractor=Extractor.JATS.value,
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

    Decision tree mirrors :func:`litspectraits.extract.pdf._commit` —
    same idempotency-on-identical-bytes contract. Duplicated rather than
    extracted to a shared helper because the ``ExtractRecord`` /
    ``meta.json`` shape differs per-format (no ``pipeline`` block here,
    ``n_pages=None``); a parameterized abstraction would be more code
    than the duplication. Revisit after 10d when all three concretes are
    in hand.
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
        extractor=Extractor.JATS,
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

    No ``pipeline`` block (JATS extraction has no tunable knobs beyond
    the lxml parser settings, which are hard-wired) and no ``n_pages``
    (JATS carries no post-typesetting page concept). Symmetric with the
    PDF ``meta.json`` everywhere else.
    """
    return {
        'extractor': Extractor.JATS.value,
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


# Re-export so ``extract/jats.py`` is the obvious place to look for the
# field definitions a downstream reader needs to align with.
__all__ = ['SCHEMA_NAME', 'SCHEMA_VERSION', 'extract_jats']
