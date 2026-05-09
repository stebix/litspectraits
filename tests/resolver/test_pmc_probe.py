"""PMC probe tests against canned NCBI responses via respx."""

import httpx
import pytest
import respx

from litspectraits.config import Settings
from litspectraits.resolver.probes.base import ProbeContext
from litspectraits.resolver.probes.pmc import PMCProbe
from litspectraits.resolver.types import Access, Format, SourceKind, Version

_OA_OK = """<?xml version="1.0"?>
<OA>
  <records returned-count="1" total-count="1">
    <record id="PMC1234567" license="cc-by">
      <link format="pdf" href="ftp://example/foo.pdf"/>
    </record>
  </records>
</OA>"""

_OA_NOT_OPEN = """<?xml version="1.0"?>
<OA><error code="idIsNotOpenAccess">No</error></OA>"""


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
async def test_pmc_probe_returns_jats_for_oa_paper(ctx_factory) -> None:
    respx.get(url__startswith='https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/').mock(
        return_value=httpx.Response(
            200, json={'records': [{'pmcid': 'PMC1234567', 'doi': '10.1002/mrm.27973'}]}
        )
    )
    respx.get(url__startswith='https://www.ncbi.nlm.nih.gov/pmc/utils/oa/').mock(
        return_value=httpx.Response(200, text=_OA_OK)
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await PMCProbe().run(ctx)

    assert outcome.error is None
    assert len(outcome.availabilities) == 1
    avail = outcome.availabilities[0]
    assert avail.source_kind is SourceKind.JATS_PMC
    assert avail.version is Version.PUBLISHED
    assert avail.format is Format.JATS
    assert avail.access is Access.OPEN
    assert '1234567' in avail.url
    assert avail.license == 'cc-by'
    assert dict(avail.extra)['pmcid'] == 'PMC1234567'


@respx.mock
async def test_pmc_probe_returns_empty_when_not_oa(ctx_factory) -> None:
    respx.get(url__startswith='https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/').mock(
        return_value=httpx.Response(200, json={'records': [{'pmcid': 'PMC1234567'}]})
    )
    respx.get(url__startswith='https://www.ncbi.nlm.nih.gov/pmc/utils/oa/').mock(
        return_value=httpx.Response(200, text=_OA_NOT_OPEN)
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await PMCProbe().run(ctx)

    assert outcome.error is None
    assert outcome.availabilities == ()


@respx.mock
async def test_pmc_probe_returns_empty_when_no_pmcid(ctx_factory) -> None:
    respx.get(url__startswith='https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/').mock(
        return_value=httpx.Response(200, json={'records': [{'errmsg': 'not found'}]})
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await PMCProbe().run(ctx)

    assert outcome.availabilities == ()
