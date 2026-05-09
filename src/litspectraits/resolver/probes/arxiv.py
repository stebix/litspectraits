"""arXiv probe — LaTeX source tarball.

The DOI -> arXiv ID mapping is taken from CrossRef relations
(``has-preprint`` / ``is-preprint-of``) where available; failing that, an
arXiv ``query`` API call is made. The probe never selects without a known
arXiv ID.
"""

import time
from typing import Final

from litspectraits.resolver.probes.base import ProbeContext, measure
from litspectraits.resolver.probes.crossref import CrossRefRecord
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    SourceKind,
    Version,
    make_extra,
)

_QUERY_URL: Final = 'http://export.arxiv.org/api/query'
_SOURCE_URL: Final = 'https://arxiv.org/e-print/{arxiv_id}'

# Used to find <id>http://arxiv.org/abs/2401.12345v2</id> in Atom XML
_ATOM_ID_PREFIX: Final = 'http://arxiv.org/abs/'


class ArXivProbe:
    """Probe arXiv for a LaTeX-source rendition of the DOI."""

    name: str = 'arxiv'

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        start = time.monotonic()
        arxiv_id = _arxiv_id_from_crossref(ctx.crossref_metadata)
        if arxiv_id is None:
            arxiv_id = await self._lookup_via_arxiv_api(ctx)
        if arxiv_id is None:
            return ProbeOutcome(probe=self.name, duration_ms=measure(start))

        availability = Availability(
            source_kind=SourceKind.LATEX_ARXIV,
            version=Version.PREPRINT,
            format=Format.LATEX,
            access=Access.OPEN,
            url=_SOURCE_URL.format(arxiv_id=arxiv_id),
            media_type='application/x-eprint-tar',
            license=None,
            extra=make_extra(arxiv_id=arxiv_id),
        )
        return ProbeOutcome(
            probe=self.name,
            availabilities=(availability,),
            duration_ms=measure(start),
        )

    async def _lookup_via_arxiv_api(self, ctx: ProbeContext) -> str | None:
        params = {
            'search_query': f'doi:{ctx.doi}',
            'start': '0',
            'max_results': '1',
        }
        resp = await ctx.client.get(_QUERY_URL, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _arxiv_id_from_atom(resp.text)


def _arxiv_id_from_crossref(record: CrossRefRecord | None) -> str | None:
    if record is None:
        return None
    for relation in record.relations:
        if relation.id_type.lower() == 'arxiv':
            return relation.id
    return None


def _arxiv_id_from_atom(body: str) -> str | None:
    """Parse the arXiv Atom feed and return the first matching arXiv ID, if any.

    Avoids a full XML parse for what is a one-shot string scan; the feed is
    well-defined and the prefix is stable.
    """
    idx = body.find(f'<id>{_ATOM_ID_PREFIX}')
    if idx == -1:
        return None
    start = idx + len('<id>') + len(_ATOM_ID_PREFIX)
    end = body.find('</id>', start)
    if end == -1:
        return None
    return body[start:end]
