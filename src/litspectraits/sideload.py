"""Operator-retrieved artifact registration (``docs/overview-v3.md`` §9, §17.8).

PDF-only sideload path for DOIs where TDM access is unavailable (typical
case: a library-proxy PDF for a Wiley title we lack a TDM token for).
Hash + magic-byte verify + atomic commit, with a mandatory
:class:`~litspectraits.manifest.ManualProvenance` block — operator email,
license assertion, optional source URL, free-form note — written into the
manifest as the legal trail.

CrossRef metadata is fetched the same way as the auto-ingest path so
manifests are one shape regardless of origin. We do **not** ship a
``--no-metadata`` flag: that escape hatch would re-introduce the
abstract-only-manifest hole that v3 closed for auto-ingest. Operators
who hit a CrossRef outage during sideload retry once CrossRef recovers;
their local PDF is unaffected.

Order of operations
-------------------

1. Normalize the DOI (``InvalidDOIError`` propagates as a plain
   ``ValueError`` subclass — not in the :class:`IngestError` taxonomy).
2. Pre-stage magic-byte sniff so non-PDF inputs fail before any bytes
   are copied into the store.
3. Stream-copy the source into ``store.tmp_dir``, computing sha256 and
   byte size in the same pass.
4. Idempotency: ``store.find_by_doi(doi)``; if the existing record's
   sha256 matches, drop the staged copy and return the existing record
   untouched (§9). No CrossRef call happens on the no-op path.
5. Fetch CrossRef metadata + dispatch publisher via the §6 prefix table.
6. Defence-in-depth re-sniff at the orchestrator boundary, same as
   :mod:`litspectraits.ingest`.
7. Build the :class:`AcquisitionRecord` (``origin='manual'``,
   ``manual_provenance`` populated, ``sdk_version='manual'``,
   ``fetched_url=''``) and call
   :meth:`~litspectraits.store.ArtifactStore.commit`.

Failure semantics
-----------------

The sideload never catches the errors it surfaces — same fail-loud
contract as the auto-ingest orchestrator. A staged tmp file is left
behind on any post-stage failure and reaped on the next
:class:`~litspectraits.store.ArtifactStore` construction (§3).
"""

import uuid
from datetime import UTC, datetime
from hashlib import sha256 as _sha256
from pathlib import Path
from typing import Final

import httpx
import structlog

from litspectraits import __version__
from litspectraits.config import Settings
from litspectraits.doi import normalize
from litspectraits.manifest import (
    AcquisitionRecord,
    Format,
    ManualProvenance,
)
from litspectraits.metadata import (
    fetch_metadata,
    publisher_for_doi,
    warn_on_publisher_mismatch,
)
from litspectraits.sniff import verify
from litspectraits.store import ArtifactStore

_HASH_CHUNK_BYTES: Final = 1024 * 1024  # 1 MiB

# Sentinel for ``AcquisitionRecord.sdk_version`` on manual sideloads:
# no publisher SDK is in the loop. Distinguishable from any real
# ``wiley_tdm`` / ``springernature_api_client`` version string at a
# glance in manifests and log lines.
_SDK_VERSION_MANUAL: Final = 'manual'

_logger: Final = structlog.get_logger('litspectraits.sideload')


async def sideload(
    raw_doi: str,
    pdf_path: Path,
    *,
    license_assertion: str,
    source_url: str | None = None,
    note: str = '',
    settings: Settings,
    store: ArtifactStore,
    client: httpx.AsyncClient,
) -> AcquisitionRecord:
    """Register an operator-retrieved PDF for ``raw_doi`` in the local store.

    Parameters
    ----------
    raw_doi : str
        Operator-supplied DOI in any accepted form (URL, ``doi:`` prefixed,
        or bare). Normalized before any downstream use.
    pdf_path : pathlib.Path
        Path to the PDF on the operator's filesystem. The file is *copied*
        into the store; the source is never moved or modified.
    license_assertion : str
        License the operator is asserting for the artifact (SPDX
        identifier where applicable, free-form otherwise). Mandatory: the
        legal trail for proxies that permit the fetch but forbid
        redistribution lives only in the manifest.
    source_url : str | None, optional
        URL the operator retrieved the PDF from. Recorded in
        :attr:`ManualProvenance.source_url`. Recommended but optional.
    note : str, optional
        Free-form note about retrieval (e.g. ``'via uni-wuerzburg library
        proxy'``). Empty by default.
    settings : Settings
        Process settings. ``contact_email`` becomes the operator on the
        :class:`ManualProvenance` record and the polite-pool mailto on
        the CrossRef call.
    store : ArtifactStore
        Filesystem store. Staging happens under :attr:`store.tmp_dir`;
        commit goes through :meth:`ArtifactStore.commit`.
    client : httpx.AsyncClient
        Shared client built by :func:`litspectraits.http.http_client`.

    Returns
    -------
    AcquisitionRecord
        The freshly-committed manifest, or — on an idempotent re-run of
        the same ``(doi, sha256)`` — the existing record without
        modification.

    Raises
    ------
    litspectraits.doi.InvalidDOIError
        ``raw_doi`` is not a valid DOI shape. Plain ``ValueError``
        subclass; not part of the :class:`IngestError` taxonomy.
    litspectraits.errors.MalformedArtifactError
        Sniff rejected ``pdf_path`` — magic bytes are not ``%PDF-``.
    litspectraits.errors.UnsupportedPublisherError
        The DOI prefix is not in the v3 dispatch table (§6).
    litspectraits.errors.DOINotFoundError
        CrossRef returned 404 for ``raw_doi``.
    litspectraits.errors.IntegrityError
        sha256 collision against an existing artifact with a different
        byte size (very unlikely for a legitimate PDF).
    OSError
        ``pdf_path`` is missing or unreadable. Propagates as a stdlib
        error rather than being wrapped — the CLI maps it to exit 2 via
        Typer's path-existence check, and library callers get a clean
        stdlib exception.
    """
    doi = normalize(raw_doi)
    with structlog.contextvars.bound_contextvars(doi=doi, origin='manual'):
        verify(pdf_path, expected=Format.PDF, doi=doi)
        staged, sha_hex, byte_size = _stage_pdf(pdf_path, store=store)
        existing = store.find_by_doi(doi)
        if existing is not None and existing.sha256 == sha_hex:
            staged.unlink()
            _logger.info(
                'sideload no-op; (doi, sha256) already present',
                sha256=sha_hex,
                artifact_path=existing.artifact_path,
            )
            return existing
        meta = await fetch_metadata(doi, client=client, settings=settings)
        publisher = publisher_for_doi(doi)
        warn_on_publisher_mismatch(meta, publisher)
        verify(staged, expected=Format.PDF, doi=doi)
        provenance = ManualProvenance(
            operator=settings.contact_email,
            retrieved_at=datetime.now(tz=UTC),
            source_url=source_url,
            note=note,
            license_assertion=license_assertion,
        )
        record = AcquisitionRecord(
            doi=doi,
            sha256=sha_hex,
            artifact_path=store.artifact_relpath(sha_hex, Format.PDF),
            format=Format.PDF,
            publisher=publisher,
            metadata=meta,
            fetched_url='',
            fetched_at=datetime.now(tz=UTC),
            fetcher_version=__version__,
            sdk_version=_SDK_VERSION_MANUAL,
            byte_size=byte_size,
            origin='manual',
            manual_provenance=provenance,
        )
        store.commit(src=staged, record=record)
        _logger.info(
            'sideload committed',
            publisher=publisher.value,
            sha256=sha_hex,
            byte_size=byte_size,
            artifact_path=record.artifact_path,
        )
        return record


def _stage_pdf(source: Path, *, store: ArtifactStore) -> tuple[Path, str, int]:
    """Stream-copy ``source`` into ``store.tmp_dir``, hashing in the same pass.

    Single read of the source: the digest, byte count, and staged file
    fall out together. The staged file name is randomized (``uuid4``)
    rather than digest-keyed so collisions are impossible even before
    the digest is known.

    Returns
    -------
    tuple[pathlib.Path, str, int]
        ``(staged_path, hex_digest, byte_size)``.
    """
    staging = store.tmp_dir / f'sideload-{uuid.uuid4().hex}.pdf.part'
    digest = _sha256()
    size = 0
    with source.open('rb') as src, staging.open('wb') as dst:
        while chunk := src.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            dst.write(chunk)
            size += len(chunk)
    return staging, digest.hexdigest(), size
