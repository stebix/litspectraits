"""Magic-byte sniff catches paywall HTML served as application/pdf."""

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

_PAYWALL_HTML = (
    b'<!DOCTYPE html><html><head><title>Sign in</title></head>'
    b'<body><h1>Subscription required</h1></body></html>'
)
_PDF = b'%PDF-1.4\n' + b'C' * 8192 + b'\n%%EOF\n'


def _result() -> ResolveResult:
    chosen = Availability(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.PUBLISHED,
        format=Format.PDF,
        access=Access.OPEN,
        url='https://publisher.example/paywall.pdf',
        media_type='application/pdf',
    )
    fallback = Availability(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.ACCEPTED_MANUSCRIPT,
        format=Format.PDF,
        access=Access.OPEN,
        url='https://repo.example/manuscript.pdf',
        media_type='application/pdf',
    )
    return ResolveResult(
        doi='10.1002/mrm.99999',
        chosen=chosen,
        candidates=(chosen, fallback),
        excluded=(),
        probes=(ProbeOutcome(probe='unpaywall', availabilities=(chosen, fallback)),),
        policy_name='published_first',
        resolved_at=datetime.now(UTC),
    )


@respx.mock
async def test_acquire_falls_through_on_magic_byte_mismatch(store: ArtifactStore) -> None:
    respx.get('https://publisher.example/paywall.pdf').mock(
        return_value=httpx.Response(
            200, content=_PAYWALL_HTML, headers={'content-type': 'application/pdf'}
        )
    )
    respx.get('https://repo.example/manuscript.pdf').mock(
        return_value=httpx.Response(200, content=_PDF)
    )

    async with httpx.AsyncClient() as client:
        record = await acquire(_result(), store=store, client=client)

    assert record.source.url == 'https://repo.example/manuscript.pdf'
    assert len(record.attempts) == 2
    first = record.attempts[0]
    assert first.outcome is AttemptOutcome.MAGIC_BYTE_MISMATCH
    assert first.sniffed_prefix is not None
    assert first.sniffed_prefix.startswith(b'<!DOCTYPE html')
    assert record.attempts[1].outcome is AttemptOutcome.SUCCESS
