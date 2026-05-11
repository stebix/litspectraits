"""PDF extractor via ``docling`` (``docs/overview-v3.md`` §11; full spec in
``docs/extract-pdf-plan.md``).

Six-stage pipeline mirroring the ingest happy path: preflight → convert →
structural sanity → serialize → commit. Every failure raises a typed
:class:`~litspectraits.errors.ExtractError` subclass before anything is
written under ``documents/<sha>/`` (``extract-pdf-plan.md`` §3, §5).

Imports of ``docling`` itself are lazy and live behind :func:`_load_docling`
— the ``[extract]`` extra is opt-in (``extract-pdf-plan.md`` §0, §2.4); a
corpus that is 80% Elsevier+Springer should never need the model weights
installed. Conversion itself is CPU/GPU-bound and runs under
:func:`asyncio.to_thread`, the same threading discipline as the Wiley /
Springer SDK retrievers (``overview-v3.md`` §7.1, §7.2).

The on-disk shape is ``docling_document.export_to_dict()`` **verbatim** —
no transformation, no markdown round-trip, no flattening. The future
canonical-``Document`` normaliser re-walks this dict; that re-walk is cheap
because every node already carries page + bbox provenance
(``extract-pdf-plan.md`` §2.1, §3 stage 4, §9).
"""

import asyncio
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Final

import structlog

from litspectraits.errors import (
    DoclingConversionError,
    DoclingDegradedError,
    DoclingImportError,
    EmptyDocumentError,
    ExtractIntegrityError,
    MissingArtifactError,
    ParseDegradedError,
    SerializationError,
    WrongFormatForExtractorError,
)
from litspectraits.manifest import AcquisitionRecord, Extractor, ExtractRecord, Format
from litspectraits.store import ArtifactStore

# MR papers are ≥2 pages of body text; 500 chars is roughly 80 words, an
# order of magnitude below any genuine corpus member. A PDF that parses
# but yields fewer characters has almost certainly fallen out of layout
# recognition into header/footer furniture only.
FLOOR_CHARS: Final[int] = 500

# Zero blocks means docling produced nothing usable — almost always a
# scanned PDF served without a text layer, where ``do_ocr=False`` leaves us
# empty-handed (``extract-pdf-plan.md`` §3 stage 3).
MIN_TEXT_BLOCKS: Final[int] = 1

# Defensive; should be unreachable post-Stage 2.
MIN_PAGES: Final[int] = 1

_DIST_NAME: Final = 'docling'
_DOCUMENT_FILENAME: Final = 'document.json'
_META_FILENAME: Final = 'meta.json'

# Section-header label as docling emits it on ``TextItem.label``. Compared
# as a string so a future docling label-enum reshuffle does not silently
# break the count.
_SECTION_HEADER_LABEL: Final = 'section_header'

_logger: Final = structlog.get_logger('litspectraits.extract.pdf')


@dataclass(frozen=True)
class _Counts:
    """Structural counters extracted from a ``DoclingDocument``.

    Computed in a single walk and consumed by both the structural-sanity
    check (Stage 3) and the meta.json builder (Stage 4) so the document
    tree is iterated only once.
    """

    n_pages: int
    n_text_blocks: int
    n_section_headers: int
    n_tables: int
    n_figures: int
    char_count: int


@dataclass(frozen=True)
class _DoclingAdapter:
    """Bundle of lazily-imported docling symbols + a built converter.

    Constructed once per :func:`extract_pdf` call. The pattern matches the
    retrievers' ``_SdkHandle`` shape (``retrievers/wiley.py``) — keeping
    the import-and-build dance in a single helper makes it cheap to swap
    the docling adapter for a fake in tests.
    """

    converter: Any
    status_cls: Any
    version: str
    pipeline_view: dict[str, Any]


async def extract_pdf(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
    model_cache_dir: Path | None = None,
) -> ExtractRecord:
    """Convert a PDF artifact to ``documents/<sha>/document.json`` + ``meta.json``.

    Implements the six-stage pipeline from ``extract-pdf-plan.md`` §3.
    Commits to ``documents/<sha>/`` only on full success; every failure
    raises an :class:`~litspectraits.errors.ExtractError` subclass before
    any on-disk write.

    Idempotent on identical extraction output: if
    ``documents/<sha>/document.json`` already exists with bytes matching
    the freshly-serialized dict, the on-disk files are left untouched and
    the returned :class:`~litspectraits.manifest.ExtractRecord` describes
    the just-completed run (the new run's stats and the existing dict are
    necessarily consistent because the dict bytes are identical).

    Parameters
    ----------
    record : AcquisitionRecord
        Manifest of the committed PDF. ``record.format`` must be
        :attr:`Format.PDF`.
    store : ArtifactStore
        Used to resolve ``record.artifact_path`` to an absolute path, to
        stage the document/meta writes under ``store.tmp_dir``, and to
        compute the canonical ``documents/<sha>/`` directory.
    reextract : bool, default False
        Overwrite an existing ``document.json`` whose bytes differ from
        the new extraction. Without this flag a divergent re-extract
        raises :class:`~litspectraits.errors.ExtractIntegrityError` —
        silent overwrites are never acceptable (``overview-v3.md`` §3, §10;
        ``extract-pdf-plan.md`` §3 stage 5).
    model_cache_dir : pathlib.Path | None, default None
        Directory holding the docling model weights. ``None`` leaves docling
        on its default cache (``~/.cache/docling/models``); when set it is
        forwarded as ``PdfPipelineOptions.artifacts_path``. Sourced from
        :attr:`~litspectraits.config.Settings.docling_model_cache_dir`
        (``LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR``).

    Returns
    -------
    ExtractRecord
        In-memory description of the extraction. The persisted form is
        ``documents/<sha>/meta.json``; this struct is not separately
        written.

    Raises
    ------
    WrongFormatForExtractorError
        ``record.format`` is not :attr:`Format.PDF`. Defensive guard
        against a future dispatcher refactor that bypasses the format
        match.
    MissingArtifactError
        ``record.artifact_path`` does not resolve to an existing file
        under ``store.data_dir``.
    DoclingImportError
        The ``[extract]`` extra is not installed.
    DoclingConversionError
        Docling reported ``ConversionStatus.FAILURE``.
    DoclingDegradedError
        Docling reported ``ConversionStatus.PARTIAL_SUCCESS`` —
        ``extract-pdf-plan.md`` §2.2: almost always a TableFormer
        failure, and tables are exactly where the corpus's measurement
        values live.
    EmptyDocumentError
        Zero non-empty text blocks after a successful conversion.
    ParseDegradedError
        ``char_count`` below :data:`FLOOR_CHARS`.
    SerializationError
        ``export_to_dict()`` returned an object that ``json.dumps``
        rejects.
    ExtractIntegrityError
        Existing ``document.json`` differs and ``reextract=False``.
    """
    artifact_path = _preflight(record=record, store=store)

    sdk = _load_docling(doi=record.doi, model_cache_dir=model_cache_dir)
    convert_result = await asyncio.to_thread(sdk.converter.convert, artifact_path)
    _check_conversion_status(doi=record.doi, result=convert_result, status_cls=sdk.status_cls)

    document = convert_result.document
    counts = _count_structure(document)
    _check_structure(doi=record.doi, counts=counts)
    if counts.n_section_headers == 0:
        _logger.warning(
            'pdf has no section headers; flat structure (review article?)',
            doi=record.doi,
            sha256=record.sha256,
        )

    body = _serialize_document(doi=record.doi, document=document)
    return _commit(
        record=record,
        store=store,
        sdk=sdk,
        counts=counts,
        body=body,
        reextract=reextract,
    )


# Stage 1 — preflight ---------------------------------------------------------


def _preflight(*, record: AcquisitionRecord, store: ArtifactStore) -> Path:
    """Resolve the artifact path and assert format + existence.

    The sniff at ingest time already validated ``%PDF-`` (``sniff.verify``);
    we deliberately do **not** re-sniff here. The preflight is purely
    "the dispatcher routed correctly and the file is still on disk."
    """
    if record.format is not Format.PDF:
        raise WrongFormatForExtractorError(
            doi=record.doi,
            expected=Format.PDF.value,
            actual=record.format.value,
            extractor='docling',
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


# Stage 2 — convert -----------------------------------------------------------


def _load_docling(*, doi: str, model_cache_dir: Path | None = None) -> _DoclingAdapter:
    """Lazy-import docling and build a converter with our academic-PDF settings.

    Matches ``extract-pdf-plan.md`` §4 verbatim: ``do_ocr=False``,
    ``TableFormerMode.ACCURATE``, ``do_cell_matching=True``,
    ``AcceleratorDevice.AUTO``. See the plan-doc for the rationale on
    each switch.

    Parameters
    ----------
    doi : str
        Bound onto any :class:`~litspectraits.errors.DoclingImportError`.
    model_cache_dir : pathlib.Path | None, default None
        When set, forwarded as ``PdfPipelineOptions.artifacts_path`` so
        docling resolves the layout + TableFormer weights from this
        directory instead of ``~/.cache/docling/models``. ``None`` keeps
        docling's default lookup.

    Tests bypass this by monkeypatching the symbol; the import-error path
    is exercised by patching ``sys.modules['docling'] = None``.
    """
    try:
        from docling.datamodel.accelerator_options import (  # pyright: ignore[reportMissingImports]
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.base_models import (  # pyright: ignore[reportMissingImports]
            ConversionStatus,
            InputFormat,
        )
        from docling.datamodel.pipeline_options import (  # pyright: ignore[reportMissingImports]
            PdfPipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.document_converter import (  # pyright: ignore[reportMissingImports]
            DocumentConverter,
            PdfFormatOption,
        )
    except ImportError as exc:
        raise DoclingImportError(
            doi=doi,
            extractor='docling',
            hint='install the [extract] extra (uv sync --extra extract)',
            import_error=str(exc),
        ) from exc

    pipeline_options = PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=True,
        ),
        generate_picture_images=False,
        images_scale=1.0,
        accelerator_options=AcceleratorOptions(
            device=AcceleratorDevice.AUTO,
        ),
        # ``None`` keeps docling's own ``~/.cache/docling/models`` lookup;
        # a path (from ``LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR``) points it
        # at an out-of-tree weights directory, decoupled from ``data_dir``.
        artifacts_path=model_cache_dir,
    )
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )
    pipeline_view: dict[str, Any] = {
        'do_ocr': False,
        'do_table_structure': True,
        'table_mode': 'accurate',
        'do_cell_matching': True,
        'device': _resolve_accelerator_label(),
    }
    return _DoclingAdapter(
        converter=converter,
        status_cls=ConversionStatus,
        version=_docling_version(),
        pipeline_view=pipeline_view,
    )


def _docling_version() -> str:
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:  # pragma: no cover — defensive
        return 'unknown'


def _resolve_accelerator_label() -> str:
    """Best-effort probe of which device ``AcceleratorDevice.AUTO`` resolves to.

    Reports ``'cuda'``/``'mps'``/``'cpu'`` when ``torch`` is importable
    (always true in practice when docling is installed; ``torch`` is a
    transitive dep). The value lands in ``meta.json``'s ``pipeline.device``
    field so an operator can spot "extraction silently fell back to CPU"
    without opening doctor (``extract-pdf-plan.md`` §4, §6).

    Falls back to ``'auto'`` in the impossible-but-defensive case where
    docling is loaded without torch.
    """
    try:
        import torch  # pyright: ignore[reportMissingImports]
    except ImportError:  # pragma: no cover — torch is a docling dep
        return 'auto'
    if torch.cuda.is_available():
        return 'cuda'
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'


def _check_conversion_status(*, doi: str, result: Any, status_cls: Any) -> None:
    """Translate docling's ``ConversionStatus`` into our error tree.

    ``PARTIAL_SUCCESS`` is loud rather than warn (``extract-pdf-plan.md``
    §2.2): a partial success today is almost always a TableFormer failure,
    and silently committing a half-extracted document is the kind of
    failure we cannot detect downstream. The verbatim ``result.errors``
    list rides along on the exception's ``context`` for the operator.
    """
    status = result.status
    if status is status_cls.SUCCESS:
        return
    errors = _stringify_errors(getattr(result, 'errors', None))
    if status is status_cls.PARTIAL_SUCCESS:
        raise DoclingDegradedError(
            doi=doi,
            extractor='docling',
            status=str(status),
            errors=errors,
            hint='partial success is treated as failure — the corpus needs full tables',
        )
    if status is status_cls.FAILURE:
        raise DoclingConversionError(
            doi=doi,
            extractor='docling',
            status=str(status),
            errors=errors,
        )
    # An unknown status (e.g. SKIPPED on a future docling release) is also a
    # FAILURE from our point of view — refuse to commit anything we did not
    # explicitly recognize.
    raise DoclingConversionError(
        doi=doi,
        extractor='docling',
        status=str(status),
        errors=errors,
        hint='unrecognized ConversionStatus; treat as failure',
    )


def _stringify_errors(raw: Any) -> list[str]:
    if not raw:
        return []
    return [str(item) for item in raw]


# Stage 3 — structural sanity -------------------------------------------------


def _count_structure(document: Any) -> _Counts:
    """Walk ``DoclingDocument`` once and tally the per-record counters.

    Uses the flat ``texts`` / ``tables`` / ``pictures`` collections rather
    than walking ``body`` because they are the most stable public surface
    across docling 2.x minor versions.

    Section headers are reported separately and **excluded** from
    ``n_text_blocks`` so the two counts are disjoint — a 200-word paper
    with 9 headings reads as ``(n_text_blocks=N, n_section_headers=9)``
    rather than double-counting the heading text into both buckets. The
    plan-doc is silent on this; the disjoint reading is the one that
    survives a future "drop heading text from blocks" downstream pass
    without breaking the floor check.

    ``char_count`` includes heading text — the floor is about content
    density, not block-type discrimination.
    """
    texts = list(getattr(document, 'texts', []) or [])
    tables = list(getattr(document, 'tables', []) or [])
    pictures = list(getattr(document, 'pictures', []) or [])

    n_section_headers = 0
    n_text_blocks = 0
    char_count = 0
    for item in texts:
        text = getattr(item, 'text', '') or ''
        char_count += len(text)
        is_header = str(getattr(item, 'label', '')) == _SECTION_HEADER_LABEL
        if is_header:
            n_section_headers += 1
        elif text:
            n_text_blocks += 1

    n_pages_method = getattr(document, 'num_pages', None)
    if callable(n_pages_method):
        raw_pages: Any = n_pages_method()
        n_pages = int(raw_pages)
    else:  # pragma: no cover — defensive against API drift
        pages = getattr(document, 'pages', None) or {}
        n_pages = len(pages)

    return _Counts(
        n_pages=n_pages,
        n_text_blocks=n_text_blocks,
        n_section_headers=n_section_headers,
        n_tables=len(tables),
        n_figures=len(pictures),
        char_count=char_count,
    )


def _check_structure(*, doi: str, counts: _Counts) -> None:
    """Hard checks on the post-conversion structural skeleton.

    Order matters: the empty-document check fires before the floor check
    so an OCR-off scanned-PDF case surfaces as :class:`EmptyDocumentError`
    (the operator hint says "rerun with --ocr") rather than the more
    generic :class:`ParseDegradedError`.
    """
    if counts.n_pages < MIN_PAGES:  # pragma: no cover — defensive
        raise DoclingConversionError(
            doi=doi,
            extractor='docling',
            n_pages=counts.n_pages,
            hint='conversion succeeded but document has zero pages',
        )
    if counts.n_text_blocks < MIN_TEXT_BLOCKS:
        raise EmptyDocumentError(
            doi=doi,
            extractor='docling',
            n_text_blocks=counts.n_text_blocks,
            n_pages=counts.n_pages,
            hint=(
                'zero text blocks recovered — likely a scanned PDF served '
                'without a text layer; refetch the publisher TDM version'
            ),
        )
    if counts.char_count < FLOOR_CHARS:
        raise ParseDegradedError(
            doi=doi,
            extractor='docling',
            char_count=counts.char_count,
            floor_chars=FLOOR_CHARS,
            n_text_blocks=counts.n_text_blocks,
            hint='layout recognition probably failed; most content filtered as furniture',
        )


# Stage 4 — serialize ---------------------------------------------------------


def _serialize_document(*, doi: str, document: Any) -> bytes:
    """Render docling's verbatim ``export_to_dict()`` output as JSON bytes.

    The dict shape is the source of truth for the structural skeleton and
    must not be transformed (``extract-pdf-plan.md`` §2.1, §3 stage 4,
    §6). The trailing newline matches our manifest convention so the file
    is friendly to text editors.
    """
    try:
        payload = document.export_to_dict()
    except Exception as exc:
        raise SerializationError(
            doi=doi,
            extractor='docling',
            hint='docling.export_to_dict() raised',
            error=str(exc),
        ) from exc
    try:
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            doi=doi,
            extractor='docling',
            hint='docling document carried a value json.dumps could not encode',
            error=str(exc),
        ) from exc
    return (text + '\n').encode('utf-8')


# Stage 5 — commit ------------------------------------------------------------


def _commit(
    *,
    record: AcquisitionRecord,
    store: ArtifactStore,
    sdk: _DoclingAdapter,
    counts: _Counts,
    body: bytes,
    reextract: bool,
) -> ExtractRecord:
    """Atomically install ``document.json`` + ``meta.json``; enforce integrity.

    Decision tree on ``documents/<sha>/document.json``:

    - absent → write both files atomically, log success.
    - present and bytes match → no-op (don't even rewrite ``meta.json``,
      so an idempotent re-extract leaves the directory bit-identical).
    - present and bytes differ + ``reextract=False`` →
      :class:`ExtractIntegrityError`, no writes.
    - present and bytes differ + ``reextract=True`` → overwrite both
      files atomically.
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
                record=record,
                sdk=sdk,
                counts=counts,
                extracted_at=datetime.now(tz=UTC),
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
        record=record, sdk=sdk, counts=counts, extracted_at=extracted_at
    )
    meta_payload = _build_meta(record=record, sdk=sdk, counts=counts, extracted_at=extracted_at)
    meta_bytes = (json.dumps(meta_payload, indent=2, sort_keys=True) + '\n').encode('utf-8')

    _atomic_write(tmp_dir=store.tmp_dir, target=target_doc, body=body)
    _atomic_write(tmp_dir=store.tmp_dir, target=target_meta, body=meta_bytes)

    _logger.info(
        'extract committed',
        doi=record.doi,
        sha256=record.sha256,
        document_sha256=new_sha,
        n_pages=counts.n_pages,
        n_text_blocks=counts.n_text_blocks,
        n_section_headers=counts.n_section_headers,
        n_tables=counts.n_tables,
        n_figures=counts.n_figures,
        char_count=counts.char_count,
    )
    return extract_record


def _build_extract_record(
    *,
    record: AcquisitionRecord,
    sdk: _DoclingAdapter,
    counts: _Counts,
    extracted_at: datetime,
) -> ExtractRecord:
    return ExtractRecord(
        sha256=record.sha256,
        extractor=Extractor.DOCLING,
        extractor_version=f'{_DIST_NAME} {sdk.version}',
        extracted_at=extracted_at,
        n_text_blocks=counts.n_text_blocks,
        n_section_headers=counts.n_section_headers,
        n_tables=counts.n_tables,
        n_figures=counts.n_figures,
        char_count=counts.char_count,
        n_pages=counts.n_pages,
    )


def _build_meta(
    *,
    record: AcquisitionRecord,
    sdk: _DoclingAdapter,
    counts: _Counts,
    extracted_at: datetime,
) -> dict[str, Any]:
    """Assemble ``meta.json`` per ``extract-pdf-plan.md`` §6.

    Includes the ``pipeline`` block — the extraction-time equivalent of
    the ingest manifest's ``sdk_version`` — so a later commit can decide
    "is this extraction stale because we bumped TableFormer mode?"
    without re-running.
    """
    return {
        'extractor': Extractor.DOCLING.value,
        'extractor_version': f'{_DIST_NAME} {sdk.version}',
        'format': record.format.value,
        'source_sha256': record.sha256,
        'extracted_at': extracted_at.isoformat(),
        'n_pages': counts.n_pages,
        'n_text_blocks': counts.n_text_blocks,
        'n_section_headers': counts.n_section_headers,
        'n_tables': counts.n_tables,
        'n_figures': counts.n_figures,
        'char_count': counts.char_count,
        'pipeline': dict(sdk.pipeline_view),
    }


def _atomic_write(*, tmp_dir: Path, target: Path, body: bytes) -> None:
    """Stage to ``<tmp_dir>/<rand>.part`` then ``os.replace`` into place.

    The random suffix means two concurrent extractions on the same sha
    cannot collide on the staging filename even if (somehow) two extracts
    of the same artifact arrive in parallel — same defence as the store's
    manifest write (``store.py``).
    """
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
