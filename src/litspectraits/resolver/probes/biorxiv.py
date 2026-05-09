"""bioRxiv / medRxiv probe — JATS for preprints.

The bioRxiv API returns a list of versions; we surface the latest version
(highest ``version`` field) for each detected server. Both ``biorxiv`` and
``medrxiv`` are tried because the DOI prefix alone (``10.1101``) does not
distinguish them.
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

_DETAILS_URL: Final = 'https://api.biorxiv.org/details/{server}/{doi}'
_SERVERS: Final = ('biorxiv', 'medrxiv')


class BioRxivProbe:
    """Probe bioRxiv / medRxiv for a JATS-XML rendition of the DOI."""

    name: str = 'biorxiv'

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        start = time.monotonic()
        if not ctx.doi.startswith('10.1101/'):
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        for server in _SERVERS:
            availability = await self._try_server(ctx, server)
            if availability is not None:
                return ProbeOutcome(
                    probe=self.name,
                    availabilities=(availability,),
                    duration_ms=measure(start),
                )
        return ProbeOutcome(probe=self.name, duration_ms=measure(start))

    async def _try_server(self, ctx: ProbeContext, server: str) -> Availability | None:
        url = _DETAILS_URL.format(server=server, doi=ctx.doi)
        resp = await ctx.client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        payload = resp.json()
        latest = _latest_version(payload)
        if latest is None or not latest.get('jatsxml'):
            return None
        return Availability(
            source_kind=SourceKind.JATS_BIORXIV,
            version=Version.PREPRINT,
            format=Format.JATS,
            access=Access.OPEN,
            url=str(latest['jatsxml']),
            media_type='application/xml',
            license=latest.get('license'),
            extra=make_extra(biorxiv_server=server, biorxiv_version=str(latest.get('version'))),
        )


def _latest_version(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    collection = payload.get('collection') or []
    if not collection:
        return None
    return max(collection, key=lambda entry: int(entry.get('version', 0)))
