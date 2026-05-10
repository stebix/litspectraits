"""All permitted candidates fail -> AcquisitionExhaustedError with full attempts log."""

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


def _avail(kind: SourceKind, url: str, version: Version = Version.PUBLISHED) -> Availability:
    return Availability(
        source_kind=kind,
        version=version,
        format=Format.PDF,
        access=Access.OPEN,
        url=url,
        media_type='application/pdf',
    )


@respx.mock
async def test_acquire_exhausted_raises_with_attempts(store: ArtifactStore) -> None:
    a = _avail(SourceKind.JATS_EUROPEPMC, 'https://epmc.example/x.xml')
    b = _avail(SourceKind.PDF_UNPAYWALL, 'https://unpaywall.example/y.pdf')
    c = _avail(SourceKind.PDF_UNPAYWALL, 'https://unpaywall.example/z.pdf')
    result = ResolveResult(
        doi='10.1002/mrm.x',
        chosen=a,
        candidates=(a, b, c),
        excluded=(),
        probes=(ProbeOutcome(probe='europepmc', availabilities=(a,)),),
        policy_name='published_first',
        resolved_at=datetime.now(UTC),
    )
    respx.get('https://epmc.example/x.xml').mock(return_value=httpx.Response(404))
    respx.get('https://unpaywall.example/y.pdf').mock(return_value=httpx.Response(503))
    respx.get('https://unpaywall.example/z.pdf').mock(
        return_value=httpx.Response(200, content=b'<html>oops</html>')
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AcquisitionExhaustedError) as exc_info:
            await acquire(result, store=store, client=client)

    err = exc_info.value
    assert err.doi == '10.1002/mrm.x'
    assert len(err.attempts) == 3
    assert err.attempts[0].outcome is AttemptOutcome.HTTP_4XX
    assert err.attempts[1].outcome is AttemptOutcome.HTTP_5XX
    assert err.attempts[2].outcome is AttemptOutcome.MAGIC_BYTE_MISMATCH
