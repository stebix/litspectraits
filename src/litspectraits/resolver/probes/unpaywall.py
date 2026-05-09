"""Unpaywall probe — open-access PDF locations.

Unpaywall returns up to N OA locations per DOI, each tagged with
``version`` (``publishedVersion`` / ``acceptedVersion`` / ``submittedVersion``).
This probe yields **one Availability per location**, so the policy can
prefer a published-version PDF over an accepted-manuscript PDF from the
same DOI.
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

_UNPAYWALL_URL: Final = 'https://api.unpaywall.org/v2/{doi}'

_VERSION_BY_UNPAYWALL: Final[Mapping[str, Version]] = {
    'publishedVersion': Version.PUBLISHED,
    'acceptedVersion': Version.ACCEPTED_MANUSCRIPT,
    'submittedVersion': Version.PREPRINT,
}


class UnpaywallProbe:
    """Probe Unpaywall for OA PDF locations of the DOI."""

    name: str = 'unpaywall'

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        start = time.monotonic()
        url = _UNPAYWALL_URL.format(doi=ctx.doi)
        params = {'email': ctx.settings.contact_email}
        resp = await ctx.client.get(url, params=params)
        if resp.status_code == 404:
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))
        resp.raise_for_status()
        payload = resp.json()
        availabilities = tuple(_locations_to_availabilities(payload))
        return ProbeOutcome(
            probe=self.name,
            availabilities=availabilities,
            duration_ms=measure(start),
        )


def _locations_to_availabilities(payload: Mapping[str, Any]) -> list[Availability]:
    locations = payload.get('oa_locations') or []
    out: list[Availability] = []
    for idx, loc in enumerate(locations):
        pdf_url = loc.get('url_for_pdf')
        if not pdf_url:
            continue
        version_raw = loc.get('version') or ''
        version = _VERSION_BY_UNPAYWALL.get(version_raw)
        if version is None:
            continue
        out.append(
            Availability(
                source_kind=SourceKind.PDF_UNPAYWALL,
                version=version,
                format=Format.PDF,
                access=Access.OPEN,
                url=str(pdf_url),
                media_type='application/pdf',
                license=loc.get('license'),
                extra=make_extra(
                    oa_location_idx=str(idx),
                    repository=loc.get('repository_institution'),
                    host_type=loc.get('host_type'),
                ),
            )
        )
    return out
