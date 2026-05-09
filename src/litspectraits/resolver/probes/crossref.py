"""Shared CrossRef metadata access.

CrossRef's ``/works/{doi}`` payload is consumed by several probes (TDM
link, arXiv relation lookup, …). The orchestrator fetches it once at the
start of every resolve and exposes it through :class:`ProbeContext`.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import structlog
from attrs import frozen

from litspectraits.config import Settings

_CROSSREF_WORKS_URL = 'https://api.crossref.org/works/{doi}'

log = structlog.get_logger('litspectraits.resolver.crossref')


@frozen
class CrossRefLink:
    """A single entry from CrossRef's ``link`` array."""

    url: str
    content_type: str | None
    intended_application: str | None


@frozen
class CrossRefRelation:
    """A single relation entry (e.g. ``has-preprint`` -> arXiv id)."""

    relation_type: str
    id: str
    id_type: str


@frozen
class CrossRefRecord:
    """Parsed subset of CrossRef's ``message`` payload.

    Attributes
    ----------
    raw : Mapping[str, Any]
        The full payload, kept for probes that want to read fields we did
        not parse out explicitly.
    """

    doi: str
    type: str | None
    publisher: str | None
    title: str | None
    container_title: str | None
    issued_year: int | None
    links: tuple[CrossRefLink, ...]
    relations: tuple[CrossRefRelation, ...]
    raw: Mapping[str, Any]


async def fetch_crossref(
    client: httpx.AsyncClient, settings: Settings, doi: str
) -> CrossRefRecord | None:
    """Fetch the CrossRef ``works`` record for a DOI.

    Returns ``None`` on 404 (DOI not registered with CrossRef). Other HTTP
    errors raise.
    """
    url = _CROSSREF_WORKS_URL.format(doi=doi)
    params = {'mailto': settings.contact_email}
    log.debug('crossref.fetch', url=url, doi=doi)
    resp = await client.get(url, params=params)
    if resp.status_code == 404:
        log.warning('crossref.not_found', doi=doi)
        return None
    resp.raise_for_status()
    payload = resp.json()['message']
    return _parse_record(doi, payload)


def _parse_record(doi: str, msg: Mapping[str, Any]) -> CrossRefRecord:
    return CrossRefRecord(
        doi=doi,
        type=msg.get('type'),
        publisher=msg.get('publisher'),
        title=_first(msg.get('title')),
        container_title=_first(msg.get('container-title')),
        issued_year=_issued_year(msg.get('issued')),
        links=tuple(_parse_link(link) for link in msg.get('link', [])),
        relations=tuple(_parse_relations(msg.get('relation', {}))),
        raw=msg,
    )


def _first(seq: Sequence[str] | None) -> str | None:
    return seq[0] if seq else None


def _issued_year(issued: Mapping[str, Any] | None) -> int | None:
    if not issued:
        return None
    parts = issued.get('date-parts')
    if not parts or not parts[0]:
        return None
    year = parts[0][0]
    return int(year) if isinstance(year, int) else None


def _parse_link(link: Mapping[str, Any]) -> CrossRefLink:
    return CrossRefLink(
        url=link.get('URL', ''),
        content_type=link.get('content-type'),
        intended_application=link.get('intended-application'),
    )


def _parse_relations(rel: Mapping[str, Any]) -> list[CrossRefRelation]:
    out: list[CrossRefRelation] = []
    for rel_type, entries in rel.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            id_value = entry.get('id')
            id_type = entry.get('id-type')
            if not id_value or not id_type:
                continue
            out.append(CrossRefRelation(relation_type=rel_type, id=id_value, id_type=id_type))
    return out
