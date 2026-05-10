"""Europe PMC probe — JATS via the EPMC fullTextXML endpoint.

Europe PMC indexes both PubMed Central (``PMC`` source) and the preprint
mirror (``PPR`` source). It frequently exposes JATS for records that NCBI
PMC does not, including author manuscripts and preprints. The version axis
is derived from EPMC's ``source`` field:

============  =====================
``source``    Mapped version
============  =====================
``MED``       PUBLISHED
``PMC``       PUBLISHED
``PPR``       PREPRINT
``AGR`` etc.  PUBLISHED
============  =====================

Author-manuscript vs version-of-record cannot be reliably told apart from
``source`` alone — ``inEPMC=Y`` + ``hasFullText=Y`` paired with NCBI
metadata would be required for full discrimination. Flagged as a known
refinement.
"""

import time
from collections.abc import Mapping
from typing import Any, Final

from litspectraits.resolver.probes.base import ProbeContext, measure
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    SourceKind,
    Version,
    make_extra,
)

_SEARCH_URL: Final = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
_FULLTEXT_URL: Final = (
    'https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{external_id}/fullTextXML'
)

_VERSION_BY_SOURCE: Final[Mapping[str, Version]] = {
    'MED': Version.PUBLISHED,
    'PMC': Version.PUBLISHED,
    'PPR': Version.PREPRINT,
}


class EuropePMCProbe:
    """Probe Europe PMC for a JATS-XML rendition of the DOI."""

    name: str = 'europepmc'

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        start = time.monotonic()
        params = {
            'query': f'DOI:{ctx.doi}',
            'resultType': 'core',
            'format': 'json',
            'pageSize': '1',
        }
        resp = await ctx.client.get(_SEARCH_URL, params=params)
        resp.raise_for_status()
        hit = _first_hit(resp.json())
        if hit is None or not _hit_has_open_fulltext(hit):
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        source = hit.get('source')
        external_id = hit.get('id') or hit.get('pmcid') or hit.get('pmid')
        if not source or not external_id:
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        version = _VERSION_BY_SOURCE.get(source, Version.PUBLISHED)
        availability = Availability(
            source_kind=SourceKind.JATS_EUROPEPMC,
            version=version,
            format=Format.JATS,
            access=Access.OPEN,
            url=_FULLTEXT_URL.format(source=source, external_id=external_id),
            media_type='application/xml',
            license=hit.get('license'),
            extra=make_extra(epmc_source=source, epmc_id=str(external_id)),
        )
        return ProbeOutcome(
            probe=self.name,
            availabilities=(availability,),
            duration_ms=measure(start),
        )


def _first_hit(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    result_list = payload.get('resultList') or {}
    results = result_list.get('result') or []
    if not results:
        return None
    return results[0]


def _hit_has_open_fulltext(hit: Mapping[str, Any]) -> bool:
    """All three flags must be ``Y`` for the fullTextXML endpoint to actually serve.

    EPMC's ``inEPMC`` says 'we have a record', not 'we have full text we'll
    let you fetch'. ``hasFullText`` and ``isOpenAccess`` together gate the
    bytes — observed-in-the-wild after ``10.1002/mrm.26701`` slipped
    through the looser ``inEPMC=Y`` check and 404'd on fetch.
    """
    return (
        hit.get('inEPMC') == 'Y'
        and hit.get('hasFullText') == 'Y'
        and hit.get('isOpenAccess') == 'Y'
    )
