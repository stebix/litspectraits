"""Mid-stream connection drop unlinks the .part file."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from litspectraits.acquisition.attempt import AttemptOutcome
from litspectraits.acquisition.fetch import acquire
from litspectraits.acquisition.store import AcquisitionExhaustedError, ArtifactStore
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    ResolveResult,
    SourceKind,
    Version,
)


async def _flaky_pdf_stream() -> AsyncIterator[bytes]:
    # First chunk must exceed SNIFF_BYTES (4096) so the sniff passes and the
    # temp file gets opened — otherwise the drop fires before any disk I/O.
    yield b'%PDF-1.4\n' + b'D' * 8000
    raise httpx.ReadError('connection reset by peer')


def _result() -> ResolveResult:
    chosen = Availability(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.PUBLISHED,
        format=Format.PDF,
        access=Access.OPEN,
        url='https://flaky.example/paper.pdf',
        media_type='application/pdf',
    )
    return ResolveResult(
        doi='10.1002/mrm.flaky',
        chosen=chosen,
        candidates=(chosen,),
        excluded=(),
        probes=(ProbeOutcome(probe='unpaywall', availabilities=(chosen,)),),
        policy_name='published_first',
        resolved_at=datetime.now(UTC),
    )


@respx.mock
async def test_partial_part_file_unlinked_on_mid_stream_drop(store: ArtifactStore) -> None:
    respx.get('https://flaky.example/paper.pdf').mock(
        return_value=httpx.Response(200, content=_flaky_pdf_stream())
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AcquisitionExhaustedError) as exc_info:
            await acquire(_result(), store=store, client=client)

    err = exc_info.value
    assert len(err.attempts) == 1
    assert err.attempts[0].outcome is AttemptOutcome.CONNECTION_DROPPED

    # No .part files left in the tmp dir.
    leftovers = list(store.tmp_dir.glob('*.part'))
    assert leftovers == [], f'unexpected leftover temp files: {leftovers}'
