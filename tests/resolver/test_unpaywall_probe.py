"""Unpaywall probe — multiple Availabilities per DOI based on oa_locations."""

import httpx
import pytest
import respx

from litspectraits.config import Settings
from litspectraits.resolver.probes.base import ProbeContext
from litspectraits.resolver.probes.unpaywall import UnpaywallProbe
from litspectraits.resolver.types import Access, Format, SourceKind, Version


@pytest.fixture
def ctx_factory(settings: Settings):
    async def _make(client: httpx.AsyncClient) -> ProbeContext:
        return ProbeContext(
            doi='10.1002/mrm.27973',
            settings=settings,
            client=client,
            crossref_metadata=None,
            store=None,
        )

    return _make


@respx.mock
async def test_unpaywall_yields_one_availability_per_oa_location(ctx_factory) -> None:
    payload = {
        'is_oa': True,
        'oa_locations': [
            {
                'url_for_pdf': 'https://example.com/published.pdf',
                'version': 'publishedVersion',
                'license': 'cc-by',
                'host_type': 'publisher',
            },
            {
                'url_for_pdf': 'https://repo.example.com/preprint.pdf',
                'version': 'submittedVersion',
                'license': None,
                'host_type': 'repository',
            },
            {
                'url_for_pdf': None,  # filtered out: no PDF url
                'version': 'publishedVersion',
            },
        ],
    }
    respx.get(url__startswith='https://api.unpaywall.org/v2/').mock(
        return_value=httpx.Response(200, json=payload)
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await UnpaywallProbe().run(ctx)

    assert outcome.error is None
    assert len(outcome.availabilities) == 2
    versions = [a.version for a in outcome.availabilities]
    assert Version.PUBLISHED in versions
    assert Version.PREPRINT in versions
    for a in outcome.availabilities:
        assert a.source_kind is SourceKind.PDF_UNPAYWALL
        assert a.format is Format.PDF
        assert a.access is Access.OPEN


@respx.mock
async def test_unpaywall_returns_empty_on_404(ctx_factory) -> None:
    respx.get(url__startswith='https://api.unpaywall.org/v2/').mock(
        return_value=httpx.Response(404)
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await UnpaywallProbe().run(ctx)

    assert outcome.availabilities == ()
    assert outcome.error is None


@respx.mock
async def test_unpaywall_skips_when_parent_is_oa_false(ctx_factory) -> None:
    """Closed-access papers carry oa_locations=[] and is_oa=false at the top level."""
    payload = {'is_oa': False, 'oa_locations': []}
    respx.get(url__startswith='https://api.unpaywall.org/v2/').mock(
        return_value=httpx.Response(200, json=payload)
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await UnpaywallProbe().run(ctx)

    assert outcome.availabilities == ()
    assert outcome.error is None
