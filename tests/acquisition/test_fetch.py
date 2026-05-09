"""Acquisition fetch tests — streamed download with hash-on-the-fly."""

import hashlib
from datetime import UTC, datetime

import httpx
import pytest
import respx

from litspectraits.acquisition.fetch import acquire
from litspectraits.acquisition.manifest import Origin
from litspectraits.acquisition.store import ArtifactStore, NoSourceAvailableError
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    ResolveResult,
    SourceKind,
    Version,
)

_PDF_BYTES = b'%PDF-1.4\n' + b'A' * 4096 + b'\n%%EOF\n'


def _result(url: str = 'https://example.com/paper.pdf') -> ResolveResult:
    chosen = Availability(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.PUBLISHED,
        format=Format.PDF,
        access=Access.OPEN,
        url=url,
        media_type='application/pdf',
    )
    return ResolveResult(
        doi='10.1002/mrm.27973',
        chosen=chosen,
        candidates=(chosen,),
        excluded=(),
        probes=(ProbeOutcome(probe='unpaywall', availabilities=(chosen,)),),
        policy_name='published_first',
        resolved_at=datetime.now(UTC),
    )


@respx.mock
async def test_acquire_streams_and_hashes_correctly(store: ArtifactStore) -> None:
    respx.get('https://example.com/paper.pdf').mock(
        return_value=httpx.Response(200, content=_PDF_BYTES)
    )
    expected_sha = hashlib.sha256(_PDF_BYTES).hexdigest()

    async with httpx.AsyncClient() as client:
        record = await acquire(_result(), store=store, client=client)

    assert record.sha256 == expected_sha
    assert record.byte_size == len(_PDF_BYTES)
    assert record.origin is Origin.AUTO
    assert (store.data_dir / record.artifact_path).read_bytes() == _PDF_BYTES


@respx.mock
async def test_acquire_is_idempotent_on_same_url(store: ArtifactStore) -> None:
    respx.get('https://example.com/paper.pdf').mock(
        return_value=httpx.Response(200, content=_PDF_BYTES)
    )

    async with httpx.AsyncClient() as client:
        first = await acquire(_result(), store=store, client=client)
        second = await acquire(_result(), store=store, client=client)

    assert first.sha256 == second.sha256
    assert first.fetched_at == second.fetched_at  # second was a cache hit


async def test_acquire_raises_when_no_source(store: ArtifactStore) -> None:
    result = ResolveResult(
        doi='10.1002/mrm.27973',
        chosen=None,
        candidates=(),
        excluded=(),
        probes=(),
        policy_name='published_first',
        resolved_at=datetime.now(UTC),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NoSourceAvailableError):
            await acquire(result, store=store, client=client)
