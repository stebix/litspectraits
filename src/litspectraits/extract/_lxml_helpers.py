"""Shared plumbing for the lxml-backed XML extractors.

:mod:`litspectraits.extract.jats` and :mod:`litspectraits.extract.elsevier`
duplicated a substantial amount of infrastructure through 10c/10d while
we waited to see whether the third extractor (Elsevier) would share the
same shape. With both concretes landed the answer is: every helper here
was byte-identical between them apart from a few extractor-enum and
schema-name substitutions, so they live in one place now.

Scope is deliberately narrow:

- **xpath / text walking** parameterized on local-name, namespace-agnostic
  via the same ``./*[local-name()=$name]`` predicate both walkers already
  used;
- **atomic file IO** + sha256 hashing — generic enough that the file-name
  on the module is a slight misnomer, but co-located here per
  ``docs/overview-v3.md`` §21 10d follow-up so the two XML extractors
  have one obvious place to look;
- **document serialization + commit** logic, both parameterized on the
  caller's :class:`~litspectraits.manifest.Extractor` enum and the
  schema-name / schema-version pair so per-extractor diagnostics
  (``SerializationError``, log lines) stay informative.

PDF extraction (:mod:`litspectraits.extract.pdf`) keeps its own commit
function. Docling produces an extra ``pipeline`` block and a real
``n_pages`` value that the XML side doesn't carry; threading two
optional fields through the unified contract would be more code than
the small file-level duplication saves.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

from lxml import etree

from litspectraits._io import atomic_write, file_sha256
from litspectraits.errors import ExtractIntegrityError, SerializationError
from litspectraits.manifest import AcquisitionRecord, Extractor, ExtractRecord
from litspectraits.store import ArtifactStore

# Re-export atomic_write / file_sha256 so existing call sites continue to
# import them from this module. New code should import directly from
# :mod:`litspectraits._io`.
__all__ = ['atomic_write', 'file_sha256']

# Filenames under ``documents/sha256/<aa>/<sha>/``. Kept here so the two XML
# extractors and the (separate) PDF extractor agree on the on-disk
# convention without each carrying its own copy.
DOCUMENT_FILENAME: Final = 'document.json'
META_FILENAME: Final = 'meta.json'

# Local-name predicate so namespace-prefix variants (``jats:sec`` vs
# ``sec``, ``ce:section`` vs ``section``) both match. ``iterfind`` does
# not support the local-name() XPath function — its grammar is a strict
# subset of XPath 1.0 — so we use ``xpath()``. ``./`` anchors at the
# receiver, ``*`` selects all direct children. The ``$name`` xpath
# variable means callers don't have to escape ``name`` for embedding
# into the expression — keeps the helpers safe against accidental
# injection if a future caller passes user-controlled strings.
LOCAL_NAME_CHILDREN: Final = './*[local-name()=$name]'


@dataclass(frozen=True)
class Counts:
    """Structural counters tallied during a single XML document walk.

    Used by both XML extractors. Frozen so callers can pass it through
    :func:`commit_document` without worrying about mutation. The walker
    is expected to keep its own mutable accumulator (the two extractors
    have their own ``_Acc``) and snapshot it into :class:`Counts` once
    the walk completes — that pattern keeps the walker code free of
    ``dataclass.replace`` churn.

    PDF extraction has its own counter type because it carries
    ``n_pages`` and omits ``n_references``.
    """

    n_text_blocks: int
    n_section_headers: int
    n_tables: int
    n_figures: int
    n_equations: int
    n_references: int
    char_count: int


# ---------------------------------------------------------------------------
# lxml / xpath helpers
# ---------------------------------------------------------------------------


def local_findall(node: etree._Element | None, name: str) -> list[etree._Element]:
    """Direct children with ``local-name() == name``, namespace-agnostic."""
    if node is None:
        return []
    # ``./*[local-name()=$name]`` always yields a node-set, never a scalar,
    # so the cast is sound; lxml's union return type is the conservative
    # one for arbitrary xpath expressions.
    return cast(list[etree._Element], node.xpath(LOCAL_NAME_CHILDREN, name=name))


def first_child(node: etree._Element | None, name: str) -> etree._Element | None:
    matches = local_findall(node, name)
    return matches[0] if matches else None


def first_descendant(node: etree._Element | None, name: str) -> etree._Element | None:
    if node is None:
        return None
    for child in node.iter():
        if etree.QName(child.tag).localname == name:
            return child
    return None


def all_descendants(node: etree._Element, name: str) -> list[etree._Element]:
    return [child for child in node.iter() if etree.QName(child.tag).localname == name]


def first_child_text(node: etree._Element | None, name: str) -> str | None:
    child = first_child(node, name)
    if child is None:
        return None
    text = full_text(child).strip()
    return text or None


def first_descendant_text(node: etree._Element | None, name: str) -> str | None:
    child = first_descendant(node, name)
    if child is None:
        return None
    text = full_text(child).strip()
    return text or None


def full_text(node: etree._Element | None) -> str:
    """Concatenated text content of ``node`` and its descendants.

    Whitespace is preserved as-is — both JATS paragraphs and Elsevier
    CEP paragraphs carry significant whitespace around inline
    cross-reference markers, and collapsing it would make ``[ 12 ]``
    indistinguishable from ``[12]`` for downstream citation matching.
    """
    if node is None:
        return ''
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(full_text(child))
        if child.tail:
            parts.append(child.tail)
    return ''.join(parts)


@dataclass(frozen=True)
class XrefSpan:
    """An inline cross-reference element with its byte offsets into the parent text.

    Produced by :func:`walk_paragraph_with_offsets`. ``start`` / ``end`` are
    half-open offsets into the paragraph's assembled :func:`full_text`
    result, so ``paragraph_text[start:end] == full_text(element)`` byte-for-byte.
    """

    element: etree._Element
    start: int
    end: int


def walk_paragraph_with_offsets(
    paragraph: etree._Element,
    *,
    xref_localnames: tuple[str, ...],
) -> tuple[str, list[XrefSpan]]:
    """Assemble paragraph text and locate inline cross-references.

    The returned text is byte-identical to ``full_text(paragraph)`` — the
    walk mirrors :func:`full_text` exactly (recurse into every child,
    appending ``node.text`` then each ``child`` then ``child.tail``) and
    simply tracks a cursor alongside the assembled parts. Any direct or
    nested child whose local-name matches ``xref_localnames`` is emitted
    as an :class:`XrefSpan` with offsets into the assembled text.

    This is the deterministic offset re-walk that
    ``docs/normalized-documents-discussion.md`` Part 1.4 calls out as
    "the only new code" for E0.5a. JATS uses ``xref_localnames=('xref',)``;
    Elsevier CEP uses ``('cross-ref',)``.

    Parameters
    ----------
    paragraph : lxml.etree._Element
        The ``<p>`` / ``<ce:para>`` element to walk. Other element kinds
        are accepted; the function never inspects ``paragraph.tag``.
    xref_localnames : tuple[str, ...]
        Local-names (namespace-stripped) that count as cross-references.
        Matching is namespace-agnostic via :func:`etree.QName`.

    Returns
    -------
    text : str
        ``full_text(paragraph)`` — byte-identical, including whitespace.
    spans : list[XrefSpan]
        Cross-references in document order, with half-open offsets into
        ``text``. Nested cross-references (rare but possible) are
        emitted alongside their enclosing parent, each with its own
        offset range.
    """
    parts: list[str] = []
    spans: list[XrefSpan] = []
    cursor = 0

    def _recurse(node: etree._Element) -> None:
        nonlocal cursor
        if node.text:
            parts.append(node.text)
            cursor += len(node.text)
        for child in node:
            local = etree.QName(child.tag).localname
            if local in xref_localnames:
                start = cursor
                _recurse(child)
                spans.append(XrefSpan(element=child, start=start, end=cursor))
            else:
                _recurse(child)
            if child.tail:
                parts.append(child.tail)
                cursor += len(child.tail)

    _recurse(paragraph)
    return ''.join(parts), spans


def serialize_mathml(math: etree._Element) -> str:
    """Render a ``<math>`` element back to its XML string, namespaces intact.

    Used by the XML extractors when surfacing a display-mode equation in
    the publisher dict: the verbatim MathML lands in ``equation['mathml']``
    so the anchor gate downstream can compare an emitted value string
    against an exact bytes-of-source-MathML view rather than a re-rendered
    one. ``pretty_print=False`` keeps the byte-shape stable across walks
    (the on-disk ``document.json`` itself is pretty-printed; equation
    payloads should not nest a second formatting layer that introduces
    serialise-ambiguity).
    """
    return etree.tostring(math, encoding='unicode', pretty_print=False)


def ancestor_section_path(node: etree._Element, *, section_localname: str) -> list[str | None]:
    """Walk up to the first non-section ancestor, recording section ids.

    Parameters
    ----------
    node : lxml.etree._Element
        Starting node. Its own tag is **not** considered an ancestor.
    section_localname : str
        The local-name to treat as a section boundary — ``'sec'`` for
        JATS, ``'section'`` for Elsevier CEP. Required keyword to make
        the call site read-as-spec at each extractor.
    """
    path: list[str | None] = []
    parent = node.getparent()
    while parent is not None:
        if etree.QName(parent.tag).localname == section_localname:
            path.append(parent.get('id'))
        parent = parent.getparent()
    return list(reversed(path))


# ---------------------------------------------------------------------------
# Serialize + commit
# ---------------------------------------------------------------------------


def serialize_document(*, doi: str, document: dict[str, Any], extractor: Extractor) -> bytes:
    """Render the walker's dict as the canonical ``document.json`` bytes.

    Indent + sort keys so the on-disk shape is stable across walks;
    trailing newline matches the rest of the project's JSON files.

    :class:`SerializationError` carries the per-extractor enum value so
    the CLI error panel and structured logs identify which extractor
    raised — JATS and Elsevier walk different XML shapes and recovering
    that detail downstream matters.
    """
    try:
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            doi=doi,
            extractor=extractor.value,
            hint='extracted document carried a value json.dumps could not encode',
            error=str(exc),
        ) from exc
    return (text + '\n').encode('utf-8')


def commit_document(
    *,
    record: AcquisitionRecord,
    store: ArtifactStore,
    counts: Counts,
    body: bytes,
    reextract: bool,
    extractor: Extractor,
    schema_name: str,
    schema_version: str,
    logger: Any,
) -> ExtractRecord:
    """Atomically install ``document.json`` + ``meta.json``; enforce integrity.

    Decision tree:

    - absent → write both files atomically, log success.
    - present and bytes match → no-op (don't even rewrite ``meta.json``,
      so an idempotent re-extract leaves the directory bit-identical).
    - present and bytes differ + ``reextract=False`` →
      :class:`ExtractIntegrityError`, no writes.
    - present and bytes differ + ``reextract=True`` → overwrite both
      files atomically.

    Parameters
    ----------
    record, store, counts, body, reextract
        Standard handles threaded from the extractor.
    extractor : Extractor
        Carried into :class:`ExtractRecord`'s ``extractor`` field and
        ``meta.json``'s ``extractor`` column.
    schema_name, schema_version : str
        Owned by the caller (``SCHEMA_NAME`` / ``SCHEMA_VERSION`` at the
        top of each extractor module) so the on-disk shape stays
        self-describing. ``extractor_version`` is derived as
        ``f'{schema_name}/{schema_version}'``.
    logger
        ``structlog`` logger bound to the caller's per-extractor name
        (``litspectraits.extract.jats``, ``litspectraits.extract.elsevier``)
        so operators can grep on the right channel.
    """
    target_dir = store.document_dir(record.sha256)
    target_doc = target_dir / DOCUMENT_FILENAME
    target_meta = target_dir / META_FILENAME

    new_sha = hashlib.sha256(body).hexdigest()
    if target_doc.exists():
        existing_sha = file_sha256(target_doc)
        if existing_sha == new_sha:
            logger.info(
                'extract no-op; document.json bytes unchanged',
                doi=record.doi,
                sha256=record.sha256,
                document_sha256=new_sha,
            )
            return _build_extract_record(
                record=record,
                counts=counts,
                extracted_at=datetime.now(tz=UTC),
                extractor=extractor,
                schema_name=schema_name,
                schema_version=schema_version,
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
    extract_record = _build_extract_record(
        record=record,
        counts=counts,
        extracted_at=extracted_at,
        extractor=extractor,
        schema_name=schema_name,
        schema_version=schema_version,
    )
    meta_payload = _build_meta(
        record=record,
        counts=counts,
        extracted_at=extracted_at,
        extractor=extractor,
        schema_name=schema_name,
        schema_version=schema_version,
    )
    meta_bytes = (json.dumps(meta_payload, indent=2, sort_keys=True) + '\n').encode('utf-8')

    atomic_write(tmp_dir=store.tmp_dir, target=target_doc, body=body)
    atomic_write(tmp_dir=store.tmp_dir, target=target_meta, body=meta_bytes)

    logger.info(
        'extract committed',
        doi=record.doi,
        sha256=record.sha256,
        document_sha256=new_sha,
        n_text_blocks=counts.n_text_blocks,
        n_section_headers=counts.n_section_headers,
        n_tables=counts.n_tables,
        n_figures=counts.n_figures,
        n_equations=counts.n_equations,
        n_references=counts.n_references,
        char_count=counts.char_count,
    )
    return extract_record


def _build_extract_record(
    *,
    record: AcquisitionRecord,
    counts: Counts,
    extracted_at: datetime,
    extractor: Extractor,
    schema_name: str,
    schema_version: str,
) -> ExtractRecord:
    return ExtractRecord(
        sha256=record.sha256,
        extractor=extractor,
        extractor_version=f'{schema_name}/{schema_version}',
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
    counts: Counts,
    extracted_at: datetime,
    extractor: Extractor,
    schema_name: str,
    schema_version: str,
) -> dict[str, Any]:
    """Assemble ``meta.json`` for an XML extractor.

    No ``pipeline`` block — neither XML extractor has tunable knobs
    beyond the hard-wired lxml parser settings — and no ``n_pages``
    (XML carries no post-typesetting page concept). Symmetric with the
    PDF ``meta.json`` everywhere else.
    """
    return {
        'extractor': extractor.value,
        'extractor_version': f'{schema_name}/{schema_version}',
        'schema_name': schema_name,
        'schema_version': schema_version,
        'format': record.format.value,
        'source_sha256': record.sha256,
        'extracted_at': extracted_at.isoformat(),
        'n_text_blocks': counts.n_text_blocks,
        'n_section_headers': counts.n_section_headers,
        'n_tables': counts.n_tables,
        'n_figures': counts.n_figures,
        'n_equations': counts.n_equations,
        'n_references': counts.n_references,
        'char_count': counts.char_count,
    }
