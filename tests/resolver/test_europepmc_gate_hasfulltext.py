"""EPMC probe yields nothing without hasFullText=Y AND isOpenAccess=Y.

Regression for `10.1002/mrm.26701`: EPMC reported `inEPMC=Y, isOpenAccess=N`
and the probe confidently advertised a fullTextXML URL that 404'd on fetch.
Tightened gate now requires all three flags to be ``Y``.
"""

import httpx
import pytest
import respx

from litspectraits.config import Settings
from litspectraits.resolver.probes.base import ProbeContext
from litspectraits.resolver.probes.europepmc import EuropePMCProbe


@pytest.fixture
def ctx_factory(settings: Settings):
    async def _make(client: httpx.AsyncClient) -> ProbeContext:
        return ProbeContext(
            doi='10.1002/mrm.26701',
            settings=settings,
            client=client,
            crossref_metadata=None,
            store=None,
        )

    return _make


def _payload(*, in_epmc: str, has_full: str, is_oa: str) -> dict[str, object]:
    return {
        'resultList': {
            'result': [
                {
                    'inEPMC': in_epmc,
                    'hasFullText': has_full,
                    'isOpenAccess': is_oa,
                    'source': 'MED',
                    'id': '12345',
                    'license': 'cc-by',
                }
            ]
        }
    }


@respx.mock
async def test_no_yield_when_has_full_text_is_no(ctx_factory) -> None:
    respx.get(url__startswith='https://www.ebi.ac.uk/europepmc/').mock(
        return_value=httpx.Response(200, json=_payload(in_epmc='Y', has_full='N', is_oa='Y'))
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await EuropePMCProbe().run(ctx)

    assert outcome.error is None
    assert outcome.availabilities == ()


@respx.mock
async def test_no_yield_when_is_open_access_is_no(ctx_factory) -> None:
    respx.get(url__startswith='https://www.ebi.ac.uk/europepmc/').mock(
        return_value=httpx.Response(200, json=_payload(in_epmc='Y', has_full='Y', is_oa='N'))
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await EuropePMCProbe().run(ctx)

    assert outcome.error is None
    assert outcome.availabilities == ()


@respx.mock
async def test_yields_when_all_three_flags_are_y(ctx_factory) -> None:
    respx.get(url__startswith='https://www.ebi.ac.uk/europepmc/').mock(
        return_value=httpx.Response(200, json=_payload(in_epmc='Y', has_full='Y', is_oa='Y'))
    )

    async with httpx.AsyncClient() as client:
        ctx = await ctx_factory(client)
        outcome = await EuropePMCProbe().run(ctx)

    assert outcome.error is None
    assert len(outcome.availabilities) == 1
