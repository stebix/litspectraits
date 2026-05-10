"""Streamed acquisition with sniff-before-write and route fall-through.

The chosen route is only the *resolver's* best guess at what is fetchable.
Probes work from upstream metadata that lags reality — PMC's OA index, an
Unpaywall mirror that has gone offline, an EPMC ``inEPMC=Y`` flag that does
not actually serve the JATS payload. Acquisition treats those answers as
hypotheses, not facts:

1. The first 4 KiB of the response body is read into memory and validated
   against the format's magic bytes before any temp file is opened. This
   catches paywall HTML served as ``Content-Type: application/pdf``,
   landing-page redirects, and CAPTCHA / error pages.
2. On any non-success outcome (4xx, 5xx, magic-byte mismatch, mid-stream
   drop, empty body) the route is recorded as an
   :class:`~litspectraits.acquisition.attempt.AcquisitionAttempt` and
   acquire falls through to the next permitted candidate, capped at
   :data:`MAX_ACQUIRE_ATTEMPTS`.
3. Successful routes commit atomically (``os.replace``) so a half-written
   ``.part`` file is never visible at the canonical path.

5xx and 4xx are treated identically here — both fall through immediately.
There is no retry layer in step 1 of the pipeline. Pathological flapping
that would benefit from per-attempt retry is deferred to a later step.
"""

import contextlib
import hashlib
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
import structlog
from attrs import frozen

from litspectraits import __version__
from litspectraits.acquisition.attempt import AcquisitionAttempt, AttemptOutcome
from litspectraits.acquisition.manifest import AcquisitionRecord, Origin
from litspectraits.acquisition.sniff import PREVIEW_BYTES, SNIFF_BYTES, magic_bytes_match
from litspectraits.acquisition.store import (
    AcquisitionExhaustedError,
    ArtifactStore,
    NoSourceAvailableError,
)
from litspectraits.resolver.types import Availability, ResolveResult

_CHUNK: Final = 1 << 16  # 64 KiB
MAX_ACQUIRE_ATTEMPTS: Final = 4
"""Hard cap on candidates tried per acquire call. Bounds publisher-side load
when many resolved routes happen to be broken (rare, but possible on long
embargoed records)."""

log = structlog.get_logger('litspectraits.acquisition.fetch')


@frozen
class _Payload:
    """Internal handoff between :func:`_try_fetch` and :func:`_commit`."""

    sha256: str
    byte_size: int
    tmp_path: Path


async def acquire(
    result: ResolveResult,
    *,
    store: ArtifactStore,
    client: httpx.AsyncClient,
    force: bool = False,
) -> AcquisitionRecord:
    """Acquire the artifact for ``result``, falling through on per-route failures.

    Walks ``result.permitted_candidates`` in policy order (capped at
    :data:`MAX_ACQUIRE_ATTEMPTS`) until one route succeeds. Every attempted
    route — successful or not — is recorded on the returned
    :class:`AcquisitionRecord` so the audit trail is complete.

    Cache lookup is performed once up front against ``result.chosen``; a
    matching record short-circuits without any network I/O. Fall-through
    candidates are not cache-checked.

    Raises
    ------
    NoSourceAvailableError
        If ``result.chosen`` is ``None``.
    AcquisitionExhaustedError
        If every permitted candidate failed within the attempt cap. The
        full attempts log is attached.
    """
    if result.chosen is None:
        raise NoSourceAvailableError(f'no fetchable source for DOI {result.doi}')

    if not force:
        for existing in store.find_by_doi(result.doi):
            if existing.source.url == result.chosen.url:
                log.info(
                    'acquire.cache_hit',
                    doi=result.doi,
                    sha256=existing.sha256,
                    source_kind=existing.source.source_kind,
                )
                return existing

    queue: tuple[Availability, ...] = result.permitted_candidates[:MAX_ACQUIRE_ATTEMPTS]
    attempts: list[AcquisitionAttempt] = []
    bound = log.bind(doi=result.doi)
    for availability in queue:
        attempt, payload = await _try_fetch(client, availability, store.tmp_dir)
        attempts.append(attempt)
        if attempt.outcome is AttemptOutcome.SUCCESS and payload is not None:
            bound.info(
                'acquire.stored',
                sha256=payload.sha256,
                source_kind=availability.source_kind,
                bytes=payload.byte_size,
                attempts=len(attempts),
            )
            return _commit(result, availability, payload, store, tuple(attempts))
        bound.warning(
            'acquire.attempt_failed',
            source_kind=availability.source_kind,
            outcome=attempt.outcome.value,
            http_status=attempt.http_status,
        )
    raise AcquisitionExhaustedError(result.doi, tuple(attempts))


async def _try_fetch(
    client: httpx.AsyncClient,
    availability: Availability,
    tmp_dir: Path,
) -> tuple[AcquisitionAttempt, _Payload | None]:
    """Attempt one route end-to-end and return its outcome.

    On success the temp file is written and a :class:`_Payload` is
    returned; callers commit it via :func:`_commit`. On any failure the
    temp file (if any was opened) is unlinked before returning, so the
    caller can fall through cleanly.
    """
    started = datetime.now(UTC)
    t0 = time.monotonic()
    tmp_path: Path | None = None
    try:
        async with client.stream('GET', availability.url) as response:
            if 400 <= response.status_code < 500:
                return _attempt(
                    availability,
                    AttemptOutcome.HTTP_4XX,
                    started,
                    t0,
                    http_status=response.status_code,
                ), None
            if 500 <= response.status_code < 600:
                return _attempt(
                    availability,
                    AttemptOutcome.HTTP_5XX,
                    started,
                    t0,
                    http_status=response.status_code,
                ), None
            response.raise_for_status()

            hasher = hashlib.sha256()
            head = bytearray()
            byte_size = 0
            wrote_to_disk = False
            fh = None  # opened lazily after sniff passes
            try:
                async for chunk in response.aiter_bytes(_CHUNK):
                    if not chunk:
                        continue
                    if not wrote_to_disk:
                        head.extend(chunk)
                        if len(head) >= SNIFF_BYTES:
                            if not magic_bytes_match(availability.format, bytes(head)):
                                return _attempt(
                                    availability,
                                    AttemptOutcome.MAGIC_BYTE_MISMATCH,
                                    started,
                                    t0,
                                    http_status=response.status_code,
                                    sniffed_prefix=bytes(head[:PREVIEW_BYTES]),
                                ), None
                            tmp_path = tmp_dir / f'fetch-{secrets.token_hex(8)}.part'
                            fh = tmp_path.open('wb')
                            buffered = bytes(head)
                            hasher.update(buffered)
                            fh.write(buffered)
                            byte_size = len(buffered)
                            wrote_to_disk = True
                        continue
                    hasher.update(chunk)
                    fh.write(chunk)  # type: ignore[union-attr]
                    byte_size += len(chunk)

                # Body shorter than SNIFF_BYTES — sniff what we have.
                if not wrote_to_disk:
                    if not head:
                        return _attempt(
                            availability,
                            AttemptOutcome.EMPTY_BODY,
                            started,
                            t0,
                            http_status=response.status_code,
                        ), None
                    if not magic_bytes_match(availability.format, bytes(head)):
                        return _attempt(
                            availability,
                            AttemptOutcome.MAGIC_BYTE_MISMATCH,
                            started,
                            t0,
                            http_status=response.status_code,
                            sniffed_prefix=bytes(head[:PREVIEW_BYTES]),
                        ), None
                    tmp_path = tmp_dir / f'fetch-{secrets.token_hex(8)}.part'
                    fh = tmp_path.open('wb')
                    buffered = bytes(head)
                    hasher.update(buffered)
                    fh.write(buffered)
                    byte_size = len(buffered)
                    wrote_to_disk = True
            finally:
                if fh is not None:
                    fh.close()

            if byte_size == 0:
                if tmp_path is not None:
                    _unlink(tmp_path)
                return _attempt(
                    availability,
                    AttemptOutcome.EMPTY_BODY,
                    started,
                    t0,
                    http_status=response.status_code,
                ), None

            assert tmp_path is not None  # type narrowing for the SUCCESS branch
            return (
                _attempt(
                    availability,
                    AttemptOutcome.SUCCESS,
                    started,
                    t0,
                    http_status=response.status_code,
                ),
                _Payload(sha256=hasher.hexdigest(), byte_size=byte_size, tmp_path=tmp_path),
            )
    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
        if tmp_path is not None:
            _unlink(tmp_path)
        return _attempt(
            availability,
            AttemptOutcome.CONNECTION_DROPPED,
            started,
            t0,
            error=str(exc),
        ), None


def _commit(
    result: ResolveResult,
    availability: Availability,
    payload: _Payload,
    store: ArtifactStore,
    attempts: tuple[AcquisitionAttempt, ...],
) -> AcquisitionRecord:
    """Move the temp file into the canonical sharded path and write the manifest."""
    final_path = store.shard_path(availability.format, payload.sha256)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(payload.tmp_path, final_path)
    record = AcquisitionRecord(
        doi=result.doi,
        sha256=payload.sha256,
        artifact_path=str(final_path.relative_to(store.data_dir).as_posix()),
        source=availability,
        resolve_result=result,
        fetched_at=datetime.now(UTC),
        fetcher_version=__version__,
        byte_size=payload.byte_size,
        origin=Origin.AUTO,
        manual_provenance=None,
        attempts=attempts,
    )
    store.write_manifest(record)
    return record


def _attempt(
    availability: Availability,
    outcome: AttemptOutcome,
    started: datetime,
    t0: float,
    *,
    http_status: int | None = None,
    sniffed_prefix: bytes | None = None,
    error: str | None = None,
) -> AcquisitionAttempt:
    return AcquisitionAttempt(
        availability=availability,
        outcome=outcome,
        http_status=http_status,
        sniffed_prefix=sniffed_prefix,
        duration_ms=int((time.monotonic() - t0) * 1000),
        attempted_at=started,
        error=error,
    )


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
