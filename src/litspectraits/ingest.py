"""V3 ingest orchestrator (``docs/overview-v3.md`` §0, §17.7).

The single, quasi-linear happy path. No fall-through, no candidate
ranking, no silent skip on cache hit unless the operator opts in.
Every failure mode is a typed :class:`~litspectraits.errors.IngestError`
subclass and bubbles unchanged; nothing is materialized on failure.

Steps
-----

1. **Normalize** — :func:`litspectraits.doi.normalize` strips the URL or
   ``doi:`` prefix and lowercases. :class:`~litspectraits.doi.InvalidDOIError`
   is a ``ValueError`` subclass — *not* an :class:`IngestError` — and
   bubbles to the caller as-is.
2. **Metadata** — :func:`litspectraits.metadata.fetch_metadata` against
   CrossRef. 404 → :class:`~litspectraits.errors.DOINotFoundError`. Other
   HTTP / parse errors propagate as raw ``httpx.HTTPError`` (see
   ``docs/triage.md`` entry M5-1; deferred until the Step 9 CLI needs an
   exit code for "CrossRef is down").
3. **Dispatch** — :func:`~litspectraits.metadata.publisher_for_doi`
   resolves the publisher; an observability-only cross-check
   (:func:`~litspectraits.metadata.warn_on_publisher_mismatch`) warns when
   CrossRef's free-text publisher disagrees. Routing is unaffected (§6).
4. **Retrieve** — :func:`~litspectraits.retrievers.dispatch.retriever_for`
   plus ``await retriever.fetch(...)`` produces a
   :class:`~litspectraits.manifest.RetrievePayload`. The retriever owns
   credential checks, rate-limiting, the SDK call, defence-in-depth sniff,
   and SDK-exception → :class:`IngestError` translation.
5. **Validate** — :func:`litspectraits.sniff.verify` re-runs the magic-byte
   check at the orchestrator boundary so the
   :meth:`~litspectraits.store.ArtifactStore.commit` invariant ("nothing
   reaches ``commit`` without passing through ``verify``", §3) holds even
   for retriever bugs. Cheap: a 4 KiB re-read.
6. **Commit** — assemble the :class:`~litspectraits.manifest.AcquisitionRecord`
   and call :meth:`~litspectraits.store.ArtifactStore.commit`. Atomic
   ``os.replace`` from ``tmp/`` happens inside the store; idempotency on
   ``(sha256, byte_size)`` lets a refetch of identical bytes reuse the
   existing artifact (§3, §10).

Cache-hit short-circuit
-----------------------

The default is **refetch on every call** — even when
:meth:`ArtifactStore.find_by_doi` returns an existing manifest. Pass
``cache_hit_ok=True`` to short-circuit on a hit (return the existing
record without touching CrossRef or the publisher). See §10: refetch-by-
default keeps the loud-failure model symmetric ("I asked for an ingest,
I expect fresh bytes or a loud error") and avoids silent skips that look
indistinguishable from successful fetches in batch logs.

Failure semantics
-----------------

The orchestrator never catches an :class:`IngestError`. On a sniff or
commit failure the staged ``payload.tmp_path`` is left in place; the
next :class:`~litspectraits.store.ArtifactStore` construction wipes
``tmp/`` (§3), so the leak is bounded to the current process lifetime.
Active cleanup would require a ``try``/``finally`` around the validate-
and-commit block — kept out for now to honour the "no excessive
exception catching" rule (CLAUDE.md). Revisit if a long-lived batch
ingest accumulates enough abandoned bytes in ``tmp/`` to matter.
"""

from datetime import UTC, datetime
from typing import Final

import httpx
import structlog

from litspectraits import __version__
from litspectraits.config import Settings
from litspectraits.doi import normalize
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Publisher,
    RetrievePayload,
)
from litspectraits.metadata import (
    fetch_metadata,
    publisher_for_doi,
    warn_on_publisher_mismatch,
)
from litspectraits.progress import report_stage
from litspectraits.retrievers.dispatch import retriever_for
from litspectraits.sniff import verify
from litspectraits.store import ArtifactStore

_logger: Final = structlog.get_logger('litspectraits.ingest')


async def ingest(
    raw_doi: str,
    *,
    settings: Settings,
    store: ArtifactStore,
    client: httpx.AsyncClient,
    cache_hit_ok: bool = False,
) -> AcquisitionRecord:
    """Run the v3 happy path for a single DOI.

    Parameters
    ----------
    raw_doi : str
        Operator-supplied DOI in any accepted form (URL, ``doi:`` prefixed,
        or bare). The normalized form is bound into log context and
        persisted in the manifest.
    settings : Settings
        Process settings — credentials, rate limits, contact email.
    store : ArtifactStore
        Filesystem store. The orchestrator passes
        :attr:`ArtifactStore.tmp_dir` to the retriever for staging and
        calls :meth:`ArtifactStore.commit` on success.
    client : httpx.AsyncClient
        Shared client (typically built by
        :func:`litspectraits.http.http_client`). Used by CrossRef and the
        Elsevier retriever; the Wiley and Springer SDKs ignore it.
    cache_hit_ok : bool, optional
        When ``True`` and a manifest already exists for the normalized
        DOI, return that record without making any network call. Default
        is ``False`` (refetch every call). See §10.

    Returns
    -------
    AcquisitionRecord
        The freshly-committed manifest, or — on a cache hit when
        ``cache_hit_ok=True`` — the existing one.

    Raises
    ------
    litspectraits.doi.InvalidDOIError
        ``raw_doi`` does not have DOI shape. Bubbles as a ``ValueError``
        subclass; not part of the :class:`IngestError` taxonomy.
    litspectraits.errors.IngestError
        Any happy-path failure. Subclass identity is the contract;
        :mod:`litspectraits.cli` (Step 9) dispatches exit codes off
        ``type(exc)``.
    """
    doi = normalize(raw_doi)
    with structlog.contextvars.bound_contextvars(doi=doi):
        if cache_hit_ok:
            existing = store.find_by_doi(doi)
            if existing is not None:
                _logger.info(
                    'cache hit; short-circuit',
                    sha256=existing.sha256,
                    artifact_path=existing.artifact_path,
                )
                return existing
        report_stage('Fetching CrossRef metadata…')
        meta = await fetch_metadata(doi, client=client, settings=settings)
        publisher = publisher_for_doi(doi)
        warn_on_publisher_mismatch(meta, publisher)
        retriever = retriever_for(publisher)
        report_stage(f'Retrieving from {publisher.value}…')
        payload = await retriever.fetch(
            doi, meta, client=client, tmp_dir=store.tmp_dir, settings=settings
        )
        report_stage('Validating bytes…')
        verify(payload.tmp_path, expected=payload.format, doi=doi)
        record = _build_record(
            doi=doi, meta=meta, publisher=publisher, payload=payload, store=store
        )
        report_stage('Committing to store…')
        store.commit(src=payload.tmp_path, record=record)
        _logger.info(
            'ingest committed',
            publisher=publisher.value,
            sha256=record.sha256,
            byte_size=record.byte_size,
            artifact_path=record.artifact_path,
        )
        return record


def _build_record(
    *,
    doi: str,
    meta: CrossRefMetadata,
    publisher: Publisher,
    payload: RetrievePayload,
    store: ArtifactStore,
) -> AcquisitionRecord:
    """Assemble the manifest from retriever output + CrossRef metadata.

    Kept as a small helper so :func:`ingest` reads as the six-step
    diagram from §0 without inline-record-construction noise.
    """
    return AcquisitionRecord(
        doi=doi,
        sha256=payload.sha256,
        artifact_path=store.artifact_relpath(payload.sha256, payload.format),
        format=payload.format,
        publisher=publisher,
        metadata=meta,
        fetched_url=payload.fetched_url,
        fetched_at=datetime.now(tz=UTC),
        fetcher_version=__version__,
        sdk_version=payload.sdk_version,
        byte_size=payload.byte_size,
        origin='auto',
        manual_provenance=None,
    )
