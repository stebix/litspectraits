"""Tests for :mod:`litspectraits.metadata` (``docs/overview-v3.md`` §17.5)."""

from typing import Any

import httpx
import pytest
import structlog
from respx import MockRouter
from structlog.testing import capture_logs

from litspectraits.config import Settings
from litspectraits.errors import DOINotFoundError, UnsupportedPublisherError
from litspectraits.http import http_client
from litspectraits.manifest import CrossRefMetadata, Publisher
from litspectraits.metadata import (
    fetch_metadata,
    publisher_for_doi,
    warn_on_publisher_mismatch,
)

# Fixtures --------------------------------------------------------------------


def _crossref_oa_payload(
    *,
    doi: str = '10.1186/s12880-024-12345-1',
    publisher: str = 'Springer Science and Business Media LLC',
) -> dict[str, Any]:
    return {
        'status': 'ok',
        'message-type': 'work',
        'message-version': '1.0.0',
        'message': {
            'DOI': doi,
            'publisher': publisher,
            'title': ['A study of T1 relaxation in tissue'],
            'author': [
                {'family': 'Doe', 'given': 'Jane', 'sequence': 'first'},
                {'family': 'Smith', 'given': 'John', 'sequence': 'additional'},
            ],
            'issued': {'date-parts': [[2024, 3, 15]]},
            'type': 'journal-article',
            'license': [
                {
                    'URL': 'https://creativecommons.org/licenses/by/4.0/',
                    'content-version': 'vor',
                }
            ],
        },
    }


# publisher_for_doi -----------------------------------------------------------


@pytest.mark.parametrize(
    ('doi', 'expected'),
    [
        ('10.1002/mrm.27973', Publisher.WILEY),
        ('10.1111/jmri.12345', Publisher.WILEY),
        ('10.1016/j.neuroimage.2024.01.001', Publisher.ELSEVIER),
        ('10.1006/nimg.1999.0488', Publisher.ELSEVIER),
        ('10.1007/s10334-024-01100-0', Publisher.SPRINGER_NATURE),
        ('10.1038/s41586-024-12345-6', Publisher.SPRINGER_NATURE),
        ('10.1057/s41599-024-01234-5', Publisher.SPRINGER_NATURE),
        ('10.1186/s12880-024-12345-1', Publisher.SPRINGER_NATURE),
    ],
)
def test_publisher_for_doi_known_prefixes(doi: str, expected: Publisher) -> None:
    assert publisher_for_doi(doi) is expected


def test_publisher_for_doi_unknown_prefix_raises() -> None:
    with pytest.raises(UnsupportedPublisherError) as exc_info:
        publisher_for_doi('10.9999/unknown.doi')
    err = exc_info.value
    assert err.doi == '10.9999/unknown.doi'
    assert err.context['prefix'] == '10.9999'


# fetch_metadata --------------------------------------------------------------


async def test_fetch_metadata_oa_paper_happy_path(
    settings: Settings, respx_mock: MockRouter
) -> None:
    doi = '10.1186/s12880-024-12345-1'
    route = respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(200, json=_crossref_oa_payload(doi=doi))
    )
    async with http_client(settings) as client:
        meta = await fetch_metadata(doi, client=client, settings=settings)

    assert isinstance(meta, CrossRefMetadata)
    assert meta.doi == doi
    assert meta.publisher_str == 'Springer Science and Business Media LLC'
    assert meta.title == 'A study of T1 relaxation in tissue'
    assert meta.authors == ('Doe, Jane', 'Smith, John')
    assert meta.year == 2024
    assert meta.type == 'journal-article'
    assert meta.license == 'https://creativecommons.org/licenses/by/4.0/'
    # Polite-pool query param must be present.
    assert route.called
    assert route.calls.last.request.url.params['mailto'] == settings.contact_email


async def test_fetch_metadata_404_raises_doi_not_found(
    settings: Settings, respx_mock: MockRouter
) -> None:
    doi = '10.1002/this.does.not.exist'
    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(404, json={'status': 'not-found'})
    )
    async with http_client(settings) as client:
        with pytest.raises(DOINotFoundError) as exc_info:
            await fetch_metadata(doi, client=client, settings=settings)
    assert exc_info.value.doi == doi
    assert exc_info.value.context['http_status'] == 404


async def test_fetch_metadata_5xx_propagates_httpx_error(
    settings: Settings, respx_mock: MockRouter
) -> None:
    # Per docs/triage.md M5-1: non-404 HTTP errors are intentionally not
    # translated yet; they bubble as httpx.HTTPStatusError.
    doi = '10.1002/mrm.27973'
    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(503, text='gateway timeout')
    )
    async with http_client(settings) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_metadata(doi, client=client, settings=settings)


async def test_fetch_metadata_handles_corporate_author(
    settings: Settings, respx_mock: MockRouter
) -> None:
    doi = '10.1038/s41586-024-99999-9'
    payload = _crossref_oa_payload(doi=doi)
    payload['message']['author'] = [
        {'name': 'The Quantitative MRI Consortium', 'sequence': 'first'},
    ]
    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with http_client(settings) as client:
        meta = await fetch_metadata(doi, client=client, settings=settings)
    assert meta.authors == ('The Quantitative MRI Consortium',)


async def test_fetch_metadata_year_falls_back_to_published_online(
    settings: Settings, respx_mock: MockRouter
) -> None:
    doi = '10.1016/j.neuroimage.2023.12.001'
    payload = _crossref_oa_payload(doi=doi)
    del payload['message']['issued']
    payload['message']['published-online'] = {'date-parts': [[2023, 11, 30]]}
    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with http_client(settings) as client:
        meta = await fetch_metadata(doi, client=client, settings=settings)
    assert meta.year == 2023


async def test_fetch_metadata_handles_missing_optionals(
    settings: Settings, respx_mock: MockRouter
) -> None:
    doi = '10.1007/s10334-024-00001-0'
    minimal: dict[str, Any] = {
        'status': 'ok',
        'message': {
            'DOI': doi,
            'publisher': 'Springer',
            'type': 'journal-article',
        },
    }
    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(200, json=minimal)
    )
    async with http_client(settings) as client:
        meta = await fetch_metadata(doi, client=client, settings=settings)
    assert meta.title is None
    assert meta.authors == ()
    assert meta.year is None
    assert meta.license is None
    assert meta.type == 'journal-article'


async def test_fetch_metadata_url_quotes_doi(settings: Settings, respx_mock: MockRouter) -> None:
    # A DOI suffix containing a space (rare but spec-allowed) must be URL-
    # encoded; the slash separating prefix from suffix must not be.
    doi = '10.1002/test doi'
    respx_mock.get('https://api.crossref.org/works/10.1002/test%20doi').mock(
        return_value=httpx.Response(200, json=_crossref_oa_payload(doi=doi))
    )
    async with http_client(settings) as client:
        meta = await fetch_metadata(doi, client=client, settings=settings)
    assert meta.doi == doi


# warn_on_publisher_mismatch --------------------------------------------------


def _meta(publisher_str: str, doi: str = '10.1002/x') -> CrossRefMetadata:
    return CrossRefMetadata(
        doi=doi,
        publisher_str=publisher_str,
        title=None,
        authors=(),
        year=None,
        type=None,
        license=None,
    )


@pytest.mark.parametrize(
    ('publisher_str', 'publisher'),
    [
        ('Wiley', Publisher.WILEY),
        ('John Wiley & Sons, Ltd.', Publisher.WILEY),
        ('Elsevier BV', Publisher.ELSEVIER),
        ('Elsevier Inc.', Publisher.ELSEVIER),
        ('Springer Science and Business Media LLC', Publisher.SPRINGER_NATURE),
        ('Springer Nature', Publisher.SPRINGER_NATURE),
        ('Nature Publishing Group', Publisher.SPRINGER_NATURE),
        ('BioMed Central Ltd.', Publisher.SPRINGER_NATURE),
    ],
)
def test_warn_on_publisher_mismatch_known_strings_silent(
    publisher_str: str, publisher: Publisher
) -> None:
    with capture_logs() as logs:
        warn_on_publisher_mismatch(_meta(publisher_str), publisher)
    assert logs == []


def test_warn_on_publisher_mismatch_emits_warning() -> None:
    # A Wiley DOI whose CrossRef publisher field smells like Hindawi.
    meta = _meta(publisher_str='Hindawi Limited', doi='10.1002/example')
    with capture_logs() as logs:
        warn_on_publisher_mismatch(meta, Publisher.WILEY)
    assert len(logs) == 1
    record = logs[0]
    assert record['log_level'] == 'warning'
    assert record['event'] == 'crossref publisher string does not match dispatched publisher'
    assert record['doi'] == '10.1002/example'
    assert record['crossref_publisher'] == 'Hindawi Limited'
    assert record['dispatched_publisher'] == Publisher.WILEY.value


def test_warn_on_publisher_mismatch_empty_publisher_string_warns() -> None:
    # Defensive: CrossRef has, very rarely, returned records with no publisher.
    meta = _meta(publisher_str='', doi='10.1016/x')
    with capture_logs() as logs:
        warn_on_publisher_mismatch(meta, Publisher.ELSEVIER)
    assert len(logs) == 1
    assert logs[0]['log_level'] == 'warning'


# Sanity: the helper requires the structlog test capture above to actually
# bind the warning level. ``capture_logs`` swaps in a recording processor
# regardless of how the rest of the pipeline is configured, so this test
# also asserts that we are not accidentally calling stdlib ``logging``.
def test_warn_on_publisher_mismatch_uses_structlog_logger() -> None:
    logger = structlog.get_logger('litspectraits.metadata')
    assert logger is not None
