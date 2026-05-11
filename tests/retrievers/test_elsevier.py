"""Tests for :mod:`litspectraits.retrievers.elsevier`.

Elsevier has no SDK we wrap; tests drive ``httpx`` directly via ``respx``.
"""

from pathlib import Path

import httpx
import pytest
from respx import MockRouter, Route

from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    EntitlementDowngradeError,
    MalformedArtifactError,
    MissingCredentialError,
    PublisherAPIError,
    RateLimitExhaustedError,
)
from litspectraits.http import http_client
from litspectraits.manifest import CrossRefMetadata, Format, Publisher
from litspectraits.retrievers.elsevier import ElsevierRetriever

# A minimal full-text envelope. ``<originalText>`` is the marker our
# entitlement check requires.
_FULL_TEXT_BODY: bytes = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<full-text-retrieval-response '
    b'xmlns="http://www.elsevier.com/xml/svapi/article/dtd">\n'
    b'  <coredata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">'
    b'Demo</dc:title></coredata>\n'
    b'  <originalText>\n'
    b'    <doc xmlns="http://www.elsevier.com/xml/xocs/dtd">\n'
    b'      <body>full body text here</body>\n'
    b'    </doc>\n'
    b'  </originalText>\n'
    b'</full-text-retrieval-response>\n'
)

# Same envelope shape, but no ``<originalText>`` — Elsevier's silent
# abstract-only downgrade.
_META_ABS_BODY: bytes = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<full-text-retrieval-response '
    b'xmlns="http://www.elsevier.com/xml/svapi/article/dtd">\n'
    b'  <coredata>\n'
    b'    <dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Demo</dc:title>\n'
    b'    <dc:description xmlns:dc="http://purl.org/dc/elements/1.1/">'
    b'A short abstract.</dc:description>\n'
    b'  </coredata>\n'
    b'</full-text-retrieval-response>\n'
)


@pytest.fixture
def meta() -> CrossRefMetadata:
    return CrossRefMetadata(
        doi='10.1016/j.neuroimage.2024.01.001',
        publisher_str='Elsevier BV',
        title='Demo paper',
        authors=('Doe, Jane',),
        year=2024,
        type='journal-article',
        license=None,
    )


def _route(respx_mock: MockRouter, doi: str) -> Route:
    return respx_mock.get(f'https://api.elsevier.com/content/article/doi/{doi}')


# Missing credential ----------------------------------------------------------


async def test_missing_key_raises_missing_credential(
    settings: Settings, meta: CrossRefMetadata, tmp_path: Path
) -> None:
    retriever = ElsevierRetriever()
    async with http_client(settings) as client:
        with pytest.raises(MissingCredentialError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings
            )
    assert exc_info.value.context['publisher'] == Publisher.ELSEVIER.value


# Auth rejected --------------------------------------------------------------


async def test_401_raises_auth_rejected(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _route(respx_mock, meta.doi).mock(return_value=httpx.Response(401, text='unauthorized'))
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        with pytest.raises(AuthRejectedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['http_status'] == 401


async def test_403_raises_auth_rejected(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _route(respx_mock, meta.doi).mock(return_value=httpx.Response(403, text='forbidden'))
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        with pytest.raises(AuthRejectedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['http_status'] == 403


# Rate limit -----------------------------------------------------------------


async def test_429_raises_rate_limit_exhausted(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _route(respx_mock, meta.doi).mock(
        return_value=httpx.Response(429, headers={'Retry-After': '60'})
    )
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        with pytest.raises(RateLimitExhaustedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['retry_after'] == '60'


# Generic publisher error ----------------------------------------------------


async def test_500_raises_publisher_api_error(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _route(respx_mock, meta.doi).mock(return_value=httpx.Response(500, text='boom'))
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        with pytest.raises(PublisherAPIError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['http_status'] == 500


# Entitlement downgrade ------------------------------------------------------


async def test_meta_abs_envelope_raises_entitlement_downgrade(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _route(respx_mock, meta.doi).mock(
        return_value=httpx.Response(
            200, content=_META_ABS_BODY, headers={'Content-Type': 'text/xml'}
        )
    )
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        with pytest.raises(EntitlementDowngradeError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['publisher'] == Publisher.ELSEVIER.value


# Magic-byte mismatch (sniff fail) -------------------------------------------


async def test_html_body_raises_malformed_artifact(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _route(respx_mock, meta.doi).mock(
        return_value=httpx.Response(200, text='<html><body>nope</body></html>')
    )
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        with pytest.raises(MalformedArtifactError):
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )


# Headers --------------------------------------------------------------------


async def test_headers_include_apikey_and_optional_insttoken(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    route = _route(respx_mock, meta.doi).mock(
        return_value=httpx.Response(
            200, content=_FULL_TEXT_BODY, headers={'Content-Type': 'text/xml'}
        )
    )
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
        )
    assert route.called
    sent_headers = route.calls.last.request.headers
    assert sent_headers['x-els-apikey'] == 'dummy-elsevier-key'
    assert sent_headers['x-els-insttoken'] == 'dummy-elsevier-insttoken'
    assert sent_headers['accept'] == 'text/xml'
    # ``view=FULL`` must always be sent — it's the toggle that distinguishes
    # full-text from META_ABS server-side.
    assert route.calls.last.request.url.params['view'] == 'FULL'


async def test_insttoken_omitted_when_unset(
    settings: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    # Inject only the api key so the request goes through but no insttoken.
    settings_only_key = Settings(
        contact_email=settings.contact_email,
        data_dir=settings.data_dir,
        docling_model_cache_dir=settings.docling_model_cache_dir,
        http_timeout_s=settings.http_timeout_s,
        log_format=settings.log_format,
        wiley_tdm_token=None,
        springer_oa_api_key=None,
        springer_tdm_api_key=None,
        elsevier_api_key='only-key',
        elsevier_insttoken=None,
        rate_limit_wiley=settings.rate_limit_wiley,
        rate_limit_springer=settings.rate_limit_springer,
        rate_limit_elsevier=settings.rate_limit_elsevier,
        expected_egress_cidrs=(),
    )
    route = _route(respx_mock, meta.doi).mock(
        return_value=httpx.Response(
            200, content=_FULL_TEXT_BODY, headers={'Content-Type': 'text/xml'}
        )
    )
    retriever = ElsevierRetriever()
    async with http_client(settings_only_key) as client:
        await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_only_key
        )
    sent_headers = route.calls.last.request.headers
    assert 'x-els-insttoken' not in sent_headers


# Success --------------------------------------------------------------------


async def test_full_text_response_returns_payload(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _route(respx_mock, meta.doi).mock(
        return_value=httpx.Response(
            200, content=_FULL_TEXT_BODY, headers={'Content-Type': 'text/xml'}
        )
    )
    retriever = ElsevierRetriever()
    async with http_client(settings_with_creds) as client:
        payload = await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
        )
    assert payload.format is Format.ELSEVIER_XML
    assert payload.byte_size == len(_FULL_TEXT_BODY)
    assert len(payload.sha256) == 64
    assert payload.tmp_path.is_file()
    assert payload.tmp_path.parent == tmp_path
    assert payload.tmp_path.suffix == '.part'
    assert 'view=FULL' in payload.fetched_url
    assert payload.sdk_version.startswith('litspectraits-elsevier ')


# Configurable rate ----------------------------------------------------------


def test_rate_per_second_uses_spec_default_when_not_overridden() -> None:
    retriever = ElsevierRetriever()
    assert retriever.rate_per_second == 6.0


def test_rate_per_second_honors_constructor_override() -> None:
    retriever = ElsevierRetriever(rate_per_second=1.0)
    assert retriever.rate_per_second == 1.0
