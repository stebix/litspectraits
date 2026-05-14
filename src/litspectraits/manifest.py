"""V3 data model.

All records are :func:`attrs.frozen`. (De)serialization goes through the
module-level :data:`converter` (a :class:`cattrs.Converter`), which carries
ISO-8601 hooks for :class:`datetime.datetime`. ``cattrs`` already
round-trips :class:`pathlib.Path` natively, so no hook is registered for it.

Records are persisted as JSON under ``manifests/sha256/<aa>/<sha>.manifest.json``
(``docs/overview-v3.md`` §3, §4).
"""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from attrs import frozen
from cattrs import Converter


class Format(StrEnum):
    """Stored artifact format.

    The value is the canonical short identifier used in the index file's
    ``format`` column and in log lines. The mapping to artifact path is
    in ``store.py`` (§3).
    """

    PDF = 'pdf'
    JATS_XML = 'jats_xml'
    ELSEVIER_XML = 'elsevier_xml'


class Publisher(StrEnum):
    """Publisher dispatched by DOI prefix (§6).

    The value matches the lowercase identifier used in env-var names and
    rate-limit settings (e.g. ``Publisher.WILEY`` ↔ ``rate_limit_wiley``).
    """

    WILEY = 'wiley'
    ELSEVIER = 'elsevier'
    SPRINGER_NATURE = 'springer_nature'


class Extractor(StrEnum):
    """Extraction implementation identifier (§11, ``extract-pdf-plan.md`` §6).

    One-to-one with :class:`Format` today:

    - :attr:`DOCLING` ↔ :attr:`Format.PDF`
    - :attr:`JATS` ↔ :attr:`Format.JATS_XML`
    - :attr:`ELSEVIER` ↔ :attr:`Format.ELSEVIER_XML`

    Recorded explicitly on :class:`ExtractRecord` (rather than derived from
    ``format`` at read time) so a future PDF-extractor swap — e.g.
    docling → some alternative — only changes this value, leaving the
    schema shape and the ``documents/sha256/<aa>/<sha>/`` layout untouched.
    """

    DOCLING = 'docling'
    JATS = 'jats'
    ELSEVIER = 'elsevier'


@frozen
class CrossRefMetadata:
    """CrossRef-derived bibliographic metadata for a DOI (§6).

    Attributes
    ----------
    doi : str
        Normalized DOI.
    publisher_str : str
        Raw CrossRef ``publisher`` field (``'Wiley'``, ``'Elsevier BV'``, …).
        Cross-checked against the prefix-based dispatch table for log
        warnings; never used for routing.
    title : str | None
    authors : tuple[str, ...]
        ``Family, Given`` strings as CrossRef returns them; empty when
        CrossRef has no author array.
    year : int | None
        Issued year, when present.
    type : str | None
        CrossRef work-type (``'journal-article'``, ``'book-chapter'``, …).
    license : str | None
        License URL or SPDX-shaped string when CrossRef carries one.

    Notes
    -----
    The field name ``type`` shadows the builtin and matches the CrossRef
    payload key verbatim. Ruff's ``A`` (flake8-builtins) group is not
    enabled; if it ever is, prefer ``# noqa: A003`` over renaming so the
    field name keeps mirroring the upstream schema.
    """

    doi: str
    publisher_str: str
    title: str | None
    authors: tuple[str, ...]
    year: int | None
    type: str | None
    license: str | None


@frozen
class RetrievePayload:
    """Successful retriever output (§7).

    Returned by every retriever on success. Failures raise an
    :class:`~litspectraits.errors.IngestError` subclass instead, so this
    struct never carries error state.

    Attributes
    ----------
    sha256 : str
        Hex digest of the bytes at ``tmp_path``.
    byte_size : int
    tmp_path : pathlib.Path
        Staging file inside ``<data_dir>/tmp/``; the ingest orchestrator
        is responsible for atomic ``os.replace`` into the final shard.
    format : Format
        Format inferred from the publisher contract; cross-checked against
        the magic-byte sniff before commit.
    fetched_url : str
        Post-redirect URL recorded for provenance.
    sdk_version : str
        Free-form SDK / fetcher identifier (e.g. ``'wiley_tdm 1.0.0'``).

    Notes
    -----
    ``format`` shadows the builtin; same trade-off as
    :class:`CrossRefMetadata.type`. The name is canonical across the
    spec (§3, §4) and re-named ``fmt`` would be the larger sin.
    """

    sha256: str
    byte_size: int
    tmp_path: Path
    format: Format
    fetched_url: str
    sdk_version: str


@frozen
class ManualProvenance:
    """Operator-sideload provenance (§9).

    Mandatory on any record with ``origin='manual'``. The license
    assertion is the legal trail required by some publisher proxies that
    permit the fetch but forbid redistribution.
    """

    operator: str
    retrieved_at: datetime
    source_url: str | None
    note: str
    license_assertion: str


@frozen
class AcquisitionRecord:
    """Persistent manifest written for every committed artifact (§3, §4).

    Stored at ``manifests/sha256/<aa>/<sha>.manifest.json``. The
    ``format`` field drives extractor dispatch in ``extract/_dispatch.py``;
    ``publisher`` is recorded for audit trails and corpus slicing.

    ``manual_provenance`` is non-``None`` iff ``origin == 'manual'``.

    Notes
    -----
    ``origin`` is kept as an inline ``Literal`` per spec (§4); this means
    helpers that take ``origin: str`` and pass it through need a local
    ``# type: ignore[arg-type]`` (see ``tests/test_manifest.py``). Promote
    to a named alias (``Origin = Literal['auto', 'manual']``) and re-export
    if more than one or two call-sites force the ignore.
    """

    doi: str
    sha256: str
    artifact_path: str
    format: Format
    publisher: Publisher
    metadata: CrossRefMetadata
    fetched_url: str
    fetched_at: datetime
    fetcher_version: str
    sdk_version: str
    byte_size: int
    origin: Literal['auto', 'manual']
    manual_provenance: ManualProvenance | None


@frozen
class ExtractRecord:
    """In-memory result of a successful extraction (§11, ``extract-pdf-plan.md`` §7).

    Returned by :func:`litspectraits.extract.extract` and the per-format
    extractors. The persisted form is ``documents/sha256/<aa>/<sha>/meta.json`` next to
    the canonical ``document.json``; this record itself is not separately
    serialized (no ``manifests/...extract.json`` file). Round-tripping it
    through :data:`converter` is supported and used by tests to pin the
    schema shape.

    Attributes
    ----------
    sha256 : str
        SHA-256 of the *source* artifact, matching
        :attr:`AcquisitionRecord.sha256`. The directory ``documents/sha256/<aa>/<sha>/``
        is keyed on this value, so extract outputs co-locate with the
        artifact they describe.
    extractor : Extractor
    extractor_version : str
        Free-form version string for the underlying implementation library
        (e.g. ``'docling 2.x.y'``). Mirrors :attr:`AcquisitionRecord.sdk_version`
        in shape and intent.
    extracted_at : datetime
        Tz-aware UTC timestamp of the commit. Producers must pass
        ``datetime.now(tz=UTC)``; the converter does not coerce naive
        datetimes (same invariant as :class:`AcquisitionRecord.fetched_at`).
    n_text_blocks : int
    n_section_headers : int
        May legitimately be zero for review articles and short
        communications with flat structure
        (``extract-pdf-plan.md`` §3 stage 3).
    n_tables : int
    n_figures : int
    char_count : int
        Total characters across all text blocks. The threshold for
        :class:`~litspectraits.errors.ParseDegradedError` is checked
        against this value during PDF extraction.
    n_pages : int | None
        Page count for PDF; ``None`` for JATS and Elsevier XML, which
        carry no page concept post-typesetting. The asymmetry is encoded
        in the type rather than papered over with ``0``.
    """

    sha256: str
    extractor: Extractor
    extractor_version: str
    extracted_at: datetime
    n_text_blocks: int
    n_section_headers: int
    n_tables: int
    n_figures: int
    char_count: int
    n_pages: int | None


def _build_converter() -> Converter:
    """Build the module-level converter with our datetime hooks.

    ``Path`` round-trips natively in ``cattrs >= 24``; no hook needed.

    Notes
    -----
    No ``bytes`` hook is registered: no field in the model holds binary
    data today. If a record ever carries raw bytes (e.g. an embedded
    payload), register a base64 round-trip hook here — JSON cannot carry
    raw bytes, and cattrs has no useful default.

    The datetime hooks accept naive datetimes silently:
    ``datetime.fromisoformat`` will round-trip a naive value back to a
    naive value, and ``isoformat`` happily emits one. Producers
    (``ingest.py``, ``sideload.py``) must always pass tz-aware UTC
    datetimes (``datetime.now(tz=UTC)``). This invariant is documented in
    ``docs/overview-v3.md`` §4 but **not enforced by the converter** —
    revisit if naive datetimes ever leak into manifests.
    """
    converter = Converter()
    converter.register_unstructure_hook(datetime, lambda value: value.isoformat())
    converter.register_structure_hook(datetime, lambda value, _type: datetime.fromisoformat(value))
    return converter


converter: Final = _build_converter()
