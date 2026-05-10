"""CrossRef metadata + DOI-prefix dispatch (``docs/overview-v3.md`` §6, §17.5).

Three responsibilities, kept in one module because they all key on the DOI:

1. :func:`fetch_metadata` — GET CrossRef's polite-pool ``/works/{doi}``
   endpoint, map the response into :class:`CrossRefMetadata`. DOI 404 is
   the fast-fail case (``DOINotFoundError``); other HTTP errors propagate
   as ``httpx.HTTPError`` (see ``docs/triage.md`` for the deferred wrap).
2. :func:`publisher_for_doi` — dispatch a normalized DOI to its
   :class:`Publisher` via :data:`_PUBLISHER_BY_PREFIX`. Unknown prefixes
   raise :class:`UnsupportedPublisherError`.
3. :func:`warn_on_publisher_mismatch` — observability-only cross-check
   between CrossRef's free-text ``publisher`` field and the dispatched
   publisher. Mismatch logs a warning; it never blocks the happy path
   because the prefix table is authoritative (§6).

The CrossRef call uses the polite pool: a ``mailto=`` query parameter
sourced from ``LITSPECTRAITS_CONTACT_EMAIL``. The ``User-Agent`` header
already carries the same address via :func:`litspectraits.http.user_agent`
on the shared :class:`httpx.AsyncClient`.
"""

import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx
import structlog

from litspectraits.config import Settings
from litspectraits.errors import DOINotFoundError, UnsupportedPublisherError
from litspectraits.manifest import CrossRefMetadata, Publisher

_CROSSREF_BASE: Final = 'https://api.crossref.org/works'

_PUBLISHER_BY_PREFIX: Final[dict[str, Publisher]] = {
    # Wiley
    '10.1002': Publisher.WILEY,
    '10.1111': Publisher.WILEY,
    # Elsevier
    '10.1016': Publisher.ELSEVIER,
    '10.1006': Publisher.ELSEVIER,
    # Springer Nature
    '10.1007': Publisher.SPRINGER_NATURE,
    '10.1038': Publisher.SPRINGER_NATURE,
    '10.1057': Publisher.SPRINGER_NATURE,
    '10.1186': Publisher.SPRINGER_NATURE,
}

# Substrings used to cross-check CrossRef's free-text ``publisher`` field
# against the dispatched :class:`Publisher`. Matching is case-insensitive
# and observability-only — drift here never blocks dispatch.
_PUBLISHER_NAME_TOKENS: Final[dict[Publisher, tuple[str, ...]]] = {
    Publisher.WILEY: ('wiley',),
    Publisher.ELSEVIER: ('elsevier',),
    Publisher.SPRINGER_NATURE: (
        'springer',
        'nature',
        'biomed central',
        'palgrave',
    ),
}

_logger: Final = structlog.get_logger('litspectraits.metadata')


def publisher_for_doi(doi: str) -> Publisher:
    """Dispatch a normalized DOI to its publisher.

    Parameters
    ----------
    doi : str
        Normalized DOI (lowercased, prefix-stripped — see
        :func:`litspectraits.doi.normalize`).

    Returns
    -------
    Publisher
        The publisher whose retriever handles this DOI.

    Raises
    ------
    UnsupportedPublisherError
        If the DOI's registrant prefix is not in
        :data:`_PUBLISHER_BY_PREFIX`. Extending the table is a one-line
        change per added publisher; any unknown prefix fails loudly
        rather than falling through to a generic / OA-mirror path (§6).
    """
    prefix, _, _ = doi.partition('/')
    publisher = _PUBLISHER_BY_PREFIX.get(prefix)
    if publisher is None:
        raise UnsupportedPublisherError(doi=doi, prefix=prefix)
    return publisher


async def fetch_metadata(
    doi: str,
    *,
    client: httpx.AsyncClient,
    settings: Settings,
) -> CrossRefMetadata:
    """Fetch CrossRef metadata for ``doi`` from the polite pool.

    Parameters
    ----------
    doi : str
        Normalized DOI.
    client : httpx.AsyncClient
        Shared client built by :func:`litspectraits.http.http_client`.
        Carries the polite-pool ``User-Agent`` already.
    settings : Settings
        Source of ``contact_email`` for the polite-pool query parameter.

    Returns
    -------
    CrossRefMetadata
        Parsed bibliographic record. Optional fields (``title``,
        ``year``, ``type``, ``license``) are ``None`` when absent;
        ``authors`` is an empty tuple when CrossRef has no author array.

    Raises
    ------
    DOINotFoundError
        CrossRef returned 404. The error context carries
        ``http_status=404``.

    Notes
    -----
    Other HTTP failures (5xx, network, malformed JSON) are not
    translated into the v3 :class:`IngestError` taxonomy yet — they
    propagate as raw ``httpx.HTTPError`` / ``KeyError``. This is a
    deliberate gap; the orchestrator (Step 7) will decide whether to
    wrap them in a typed ``MetadataAPIError`` or rely on the existing
    ``PublisherAPIError`` semantics. See ``docs/triage.md`` entry M5-1.
    """
    url = f'{_CROSSREF_BASE}/{urllib.parse.quote(doi, safe="/")}'
    response = await client.get(url, params={'mailto': settings.contact_email})
    if response.status_code == 404:
        raise DOINotFoundError(doi=doi, http_status=404)
    response.raise_for_status()
    payload = response.json()
    return _parse_crossref_message(doi, payload['message'])


def warn_on_publisher_mismatch(metadata: CrossRefMetadata, publisher: Publisher) -> None:
    """Log a warning when CrossRef's publisher string mismatches the dispatch.

    The DOI-prefix table is authoritative for routing; this is purely
    an observability cross-check that surfaces CrossRef registry drift
    (e.g. a Wiley DOI showing a vendor name from a society co-publishing
    arrangement). Mismatch never blocks the happy path.

    Parameters
    ----------
    metadata : CrossRefMetadata
        Fetched record. ``publisher_str`` is the free-text CrossRef field.
    publisher : Publisher
        The publisher dispatched from the prefix table.
    """
    if _publisher_name_matches(metadata.publisher_str, publisher):
        return
    _logger.warning(
        'crossref publisher string does not match dispatched publisher',
        doi=metadata.doi,
        crossref_publisher=metadata.publisher_str,
        dispatched_publisher=publisher.value,
    )


def _publisher_name_matches(publisher_str: str, publisher: Publisher) -> bool:
    needle = publisher_str.lower()
    return any(token in needle for token in _PUBLISHER_NAME_TOKENS[publisher])


def _parse_crossref_message(doi: str, message: Mapping[str, Any]) -> CrossRefMetadata:
    return CrossRefMetadata(
        doi=doi,
        publisher_str=message.get('publisher', ''),
        title=_first_or_none(message.get('title') or ()),
        authors=_format_authors(message.get('author') or ()),
        year=_extract_year(message),
        type=message.get('type'),
        license=_extract_license(message),
    )


def _first_or_none(items: Sequence[str]) -> str | None:
    return items[0] if items else None


def _format_authors(authors: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    formatted: list[str] = []
    for author in authors:
        family = (author.get('family') or '').strip()
        given = (author.get('given') or '').strip()
        if family and given:
            formatted.append(f'{family}, {given}')
        elif family:
            formatted.append(family)
        elif name := (author.get('name') or '').strip():
            # Corporate / consortium authors carry only ``name``.
            formatted.append(name)
    return tuple(formatted)


def _extract_year(message: Mapping[str, Any]) -> int | None:
    # ``issued`` is the canonical earliest date in CrossRef's model;
    # the ``published-*`` keys carry the print/online dates when those
    # diverge from issued. Fall through in that order.
    for key in ('issued', 'published-print', 'published-online'):
        node = message.get(key)
        if not isinstance(node, Mapping):
            continue
        date_parts = node.get('date-parts')
        if not date_parts or not date_parts[0]:
            continue
        year = date_parts[0][0]
        if isinstance(year, int):
            return year
    return None


def _extract_license(message: Mapping[str, Any]) -> str | None:
    for entry in message.get('license') or ():
        url = entry.get('URL')
        if url:
            return url
    return None
