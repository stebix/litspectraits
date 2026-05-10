"""PMC OA probe — JATS via NCBI's PMC Open Access subset.

Three steps:

1. ID-converter: DOI -> PMCID.
2. PMC OA Web Service: confirms the PMCID is in the OA subset and exposes
   license + tarball links.
3. EFetch: a stable URL serving the JATS XML directly.

Note
----
Determining whether a PMC article is the **published version** vs an **NIH
author manuscript** requires inspecting the JATS payload (or extra
metadata). This probe currently reports :data:`Version.PUBLISHED` for all
hits and flags author-manuscript discrimination as a known refinement.
"""

import time
from typing import Final
from xml.etree import ElementTree as ET

import structlog

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

_IDCONV_URL: Final = 'https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/'
_OA_URL: Final = 'https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi'
_EFETCH_URL: Final = (
    'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={numeric_id}&rettype=xml'
)

log = structlog.get_logger('litspectraits.resolver.pmc')


class PMCProbe:
    """Probe NCBI PMC for a JATS-XML rendition of the DOI."""

    name: str = 'pmc'

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        start = time.monotonic()
        pmcid = await self._lookup_pmcid(ctx)
        if pmcid is None:
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        license_str = await self._oa_license(ctx, pmcid)
        if license_str is None:
            log.debug('pmc.not_in_oa', doi=ctx.doi, pmcid=pmcid)
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        numeric_id = pmcid.removeprefix('PMC')
        availability = Availability(
            source_kind=SourceKind.JATS_PMC,
            version=Version.PUBLISHED,
            format=Format.JATS,
            access=Access.OPEN,
            url=_EFETCH_URL.format(numeric_id=numeric_id),
            media_type='application/xml',
            license=license_str,
            extra=make_extra(pmcid=pmcid),
        )
        return ProbeOutcome(
            probe=self.name,
            availabilities=(availability,),
            duration_ms=measure(start),
        )

    async def _lookup_pmcid(self, ctx: ProbeContext) -> str | None:
        params = {
            'ids': ctx.doi,
            'format': 'json',
            'tool': 'litspectraits',
            'email': ctx.settings.contact_email,
        }
        resp = await ctx.client.get(_IDCONV_URL, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        records = resp.json().get('records') or []
        for record in records:
            pmcid = record.get('pmcid')
            if pmcid:
                return pmcid
        return None

    async def _oa_license(self, ctx: ProbeContext, pmcid: str) -> str | None:
        """Return the OA license string if ``pmcid`` is in the OA subset, else ``None``."""
        resp = await ctx.client.get(_OA_URL, params={'id': pmcid})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _parse_oa_license(resp.text)


def _parse_oa_license(body: str) -> str | None:
    """Parse the PMC OA service XML and return the license string, if any.

    The OA endpoint returns a 200 envelope even for non-OA articles; only
    OA records carry one or more ``<link>`` children pointing at tarball /
    PDF / package locations. Treat the absence of ``<link>`` as the
    canonical 'not in OA subset' signal — the ``license`` attribute alone
    is not sufficient.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    if root.find('error') is not None:
        return None
    record = root.find('.//record')
    if record is None:
        return None
    if record.find('link') is None:
        return None
    return record.attrib.get('license') or ''
