"""Acquire falls through from a 404 chosen route to the next permitted candidate."""

from datetime import UTC, datetime

import httpx
import respx

from litspectraits.acquisition.attempt import AttemptOutcome
from litspectraits.acquisition.fetch import acquire
from litspectraits.acquisition.store import ArtifactStore
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    ResolveResult,
    SourceKind,
    Version,
)

_PDF = b'%PDF-1.4\n' + b'B' * 4096 + b'\n%%EOF\n'


def _result() -> ResolveResult:
    chosen = Availability(
        source_kind=SourceKind.JATS_EUROPEPMC,
        version=Version.PUBLISHED,
        format=Format.JATS,
        access=Access.OPEN,
        url='https://epmc.example/full.xml',
        media_type='application/xml',
    )
    fallback = Availability(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.PUBLISHED,
        format=Format.PDF,
        access=Access.OPEN,
        url='https://unpaywall.example/paper.pdf',
        media_type='application/pdf',
    )
    return ResolveResult(
        doi='10.1002/mrm.26701',
        chosen=chosen,
        candidates=(chosen, fallback),
        excluded=(),
        probes=(ProbeOutcome(probe='europepmc', availabilities=(chosen,)),),
        policy_name='published_first',
        resolved_at=datetime.now(UTC),
    )


@respx.mock
async def test_acquire_falls_through_on_404(store: ArtifactStore) -> None:
    respx.get('https://epmc.example/full.xml').mock(return_value=httpx.Response(404))
    respx.get('https://unpaywall.example/paper.pdf').mock(
        return_value=httpx.Response(200, content=_PDF)
    )

    async with httpx.AsyncClient() as client:
        record = await acquire(_result(), store=store, client=client)

    assert record.source.source_kind is SourceKind.PDF_UNPAYWALL
    assert record.byte_size == len(_PDF)
    assert len(record.attempts) == 2
    first, second = record.attempts
    assert first.outcome is AttemptOutcome.HTTP_4XX
    assert first.http_status == 404
    assert first.availability.source_kind is SourceKind.JATS_EUROPEPMC
    assert second.outcome is AttemptOutcome.SUCCESS
    assert second.availability.source_kind is SourceKind.PDF_UNPAYWALL
