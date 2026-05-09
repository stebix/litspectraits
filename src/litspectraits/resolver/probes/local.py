"""Local-store probe — surfaces already-acquired artifacts as availabilities.

Runs alongside the network probes and feeds candidates through the same
:class:`~litspectraits.resolver.policy.RankingPolicy` as everything else.
This is what makes manual sideloads first-class: a sideloaded
``(published, pdf)`` outranks an Unpaywall ``(preprint, pdf)``
automatically — no special "manual wins" rule needed.
"""

import time

from attrs import evolve

from litspectraits.resolver.probes.base import ProbeContext, measure
from litspectraits.resolver.types import (
    Access,
    Availability,
    ProbeOutcome,
    SourceKind,
)


class LocalStoreProbe:
    """Probe the on-disk artifact store for prior acquisitions of the DOI."""

    name: str = 'local'

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        start = time.monotonic()
        if ctx.store is None:
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        records = ctx.store.find_by_doi(ctx.doi)
        availabilities: list[Availability] = []
        for rec in records:
            absolute = ctx.store.absolute_path(rec)
            availabilities.append(
                evolve(
                    rec.source,
                    source_kind=SourceKind.LOCAL_CACHE,
                    access=Access.OPEN,
                    url=f'file://{absolute}',
                )
            )
        return ProbeOutcome(
            probe=self.name,
            availabilities=tuple(availabilities),
            duration_ms=measure(start),
        )
