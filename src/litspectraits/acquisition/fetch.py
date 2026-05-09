"""Streamed acquisition with hash-on-the-fly.

Downloads the artifact for ``ResolveResult.chosen`` to a temp file, hashing
as bytes flow, then atomically renames into the sharded artifact path. The
manifest is written last so a stored manifest always implies a present
artifact.
"""

import hashlib
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
import structlog

from litspectraits import __version__
from litspectraits.acquisition.manifest import AcquisitionRecord, Origin
from litspectraits.acquisition.store import (
    ArtifactStore,
    IntegrityError,
    NoSourceAvailableError,
)
from litspectraits.resolver.types import ResolveResult

_CHUNK: Final = 1 << 16  # 64 KiB

log = structlog.get_logger('litspectraits.acquisition.fetch')


async def acquire(
    result: ResolveResult,
    *,
    store: ArtifactStore,
    client: httpx.AsyncClient,
    force: bool = False,
) -> AcquisitionRecord:
    """Acquire the artifact for the chosen availability of ``result``.

    Idempotent on the underlying ``(doi, source.url)`` pair: when a
    matching record already exists in the store and ``force`` is false, the
    existing record is returned without re-downloading.

    Raises
    ------
    NoSourceAvailableError
        If ``result.chosen`` is ``None``.
    IntegrityError
        If the response stream produces zero bytes.
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

    sha256, byte_size, tmp_path = await _stream_to_temp(client, result.chosen.url, store.tmp_dir)
    if byte_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise IntegrityError(f'empty response from {result.chosen.url}')

    final_path = store.shard_path(result.chosen.format, sha256)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_path, final_path)

    record = AcquisitionRecord(
        doi=result.doi,
        sha256=sha256,
        artifact_path=str(final_path.relative_to(store.data_dir).as_posix()),
        source=result.chosen,
        resolve_result=result,
        fetched_at=datetime.now(UTC),
        fetcher_version=__version__,
        byte_size=byte_size,
        origin=Origin.AUTO,
        manual_provenance=None,
    )
    store.write_manifest(record)
    log.info(
        'acquire.stored',
        doi=result.doi,
        sha256=sha256,
        source_kind=result.chosen.source_kind,
        bytes=byte_size,
    )
    return record


async def _stream_to_temp(
    client: httpx.AsyncClient, url: str, tmp_dir: Path
) -> tuple[str, int, Path]:
    """Stream ``url`` to a temp file under ``tmp_dir``; return ``(sha256, size, path)``."""
    tmp_path = tmp_dir / f'fetch-{secrets.token_hex(8)}.part'
    hasher = hashlib.sha256()
    total = 0
    async with client.stream('GET', url) as resp:
        resp.raise_for_status()
        with tmp_path.open('wb') as fh:
            async for chunk in resp.aiter_bytes(_CHUNK):
                if not chunk:
                    continue
                hasher.update(chunk)
                fh.write(chunk)
                total += len(chunk)
    return hasher.hexdigest(), total, tmp_path
