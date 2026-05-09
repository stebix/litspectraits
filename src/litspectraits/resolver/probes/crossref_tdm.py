"""CrossRef TDM probe — JATS / PDF behind a Click-Through token.

Reads the publisher-deposited ``link`` array on the already-fetched
CrossRef record (no extra HTTP). Filters for entries marked
``intended-application=text-mining`` (or ``similarity-checking`` as a
fallback) and synthesizes one :class:`Availability` per matching entry.

These availabilities are tagged ``access=tdm_token``. The resolver retains
them in ``candidates`` for the audit trail but only ever selects them when
a Crossref Click-Through token is configured.
"""

import time
from collections.abc import Iterable
from typing import Final

from litspectraits.resolver.probes.base import ProbeContext, measure
from litspectraits.resolver.probes.crossref import CrossRefLink
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    SourceKind,
    make_extra,
)
from litspectraits.resolver.types import Version as VersionEnum

_TDM_APPLICATIONS: Final = frozenset({'text-mining', 'similarity-checking'})


class CrossRefTDMProbe:
    """Surface publisher-deposited TDM links from the CrossRef record."""

    name: str = 'crossref_tdm'

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        start = time.monotonic()
        record = ctx.crossref_metadata
        if record is None:
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        availabilities = tuple(_links_to_availabilities(record.links))
        return ProbeOutcome(
            probe=self.name,
            availabilities=availabilities,
            duration_ms=measure(start),
        )


def _links_to_availabilities(links: Iterable[CrossRefLink]) -> list[Availability]:
    out: list[Availability] = []
    for link in links:
        if link.intended_application not in _TDM_APPLICATIONS:
            continue
        format_, source_kind, media_type = _classify(link.content_type)
        if format_ is None or source_kind is None:
            continue
        out.append(
            Availability(
                source_kind=source_kind,
                version=VersionEnum.PUBLISHED,
                format=format_,
                access=Access.TDM_TOKEN,
                url=link.url,
                media_type=media_type or '',
                license=None,
                extra=make_extra(intended_application=link.intended_application),
            )
        )
    return out


def _classify(
    content_type: str | None,
) -> tuple[Format | None, SourceKind | None, str | None]:
    """Map the CrossRef ``content-type`` string onto our format / source enums."""
    if content_type is None:
        return None, None, None
    ct = content_type.lower()
    if 'xml' in ct:
        return Format.JATS, SourceKind.JATS_CROSSREF_TDM, content_type
    if 'pdf' in ct:
        return Format.PDF, SourceKind.PDF_CROSSREF_TDM, content_type
    return None, None, None
