"""Tests for :mod:`litspectraits.retrievers.springer`.

Two test slices, one per tier:

* **TDM tier** — the ``springernature_api_client.tdm.TDMAPI`` SDK is
  monkeypatched at module level with :class:`_FakeTDMAPI`; tests use the
  shared :func:`settings_with_creds` fixture (both keys set, TDM wins).
* **Open Access tier** — direct ``respx`` mocks of the
  ``api.springernature.com/openaccess/jats`` endpoint; tests use the
  :func:`settings_with_oa_creds` fixture (TDM key intentionally blank
  so the tier dispatch falls through to OA).

The TDM dispatch never reaches the network in the OA tests (and vice
versa), so the slices are pleasantly independent — no cross-mocking.
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from respx import MockRouter, Route

from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    MalformedArtifactError,
    MissingCredentialError,
    NotOpenAccessError,
    PublisherAPIError,
    RateLimitExhaustedError,
)
from litspectraits.http import http_client
from litspectraits.manifest import CrossRefMetadata, Format, Publisher
from litspectraits.retrievers.springer import SpringerRetriever

_FAKE_JATS: bytes = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">\n'
    b'  <front><article-meta><title-group><article-title>Demo</article-title>'
    b'</title-group></article-meta></front>\n'
    b'  <body><sec><p>Body.</p></sec></body>\n'
    b'</article>\n'
)


# Fake SDK exception hierarchy (mirrors ``springernature_api_client.exceptions``).


class _APIError(Exception):
    pass


class _InvalidAPIKeyError(_APIError):
    pass


class _RateLimitExceededError(_APIError):
    pass


class _APIRequestError(_APIError):
    pass


class _FakeTDMAPI:
    """Stand-in for ``springernature_api_client.tdm.TDMAPI``."""

    _responses: ClassVar[dict[str, object]] = {}
    _calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *, api_key: str | None = None) -> None:
        if not api_key:
            raise _InvalidAPIKeyError('No API key provided.')
        self.api_key = api_key

    def search(
        self,
        *,
        q: str,
        p: int = 1,
        s: int = 1,
        fetch_all: bool = False,
        is_premium: bool = False,
    ) -> str:
        type(self)._calls.append(
            {'q': q, 'p': p, 's': s, 'fetch_all': fetch_all, 'is_premium': is_premium}
        )
        # The real SDK prints to stdout; mirror that to confirm our
        # ``redirect_stdout`` actually muzzles it.
        print(f'Fetching TDM: query={q!r}, page={p}, start={s}')
        recipe = type(self)._responses.get(q)
        if recipe is None:
            raise AssertionError(f'no fake response configured for {q!r}')
        if isinstance(recipe, BaseException):
            raise recipe
        if callable(recipe):
            return recipe()  # type: ignore[no-any-return]
        if isinstance(recipe, str):
            return recipe
        raise AssertionError(f'unexpected recipe type {type(recipe).__name__}')

    @classmethod
    def reset(cls) -> None:
        cls._responses = {}
        cls._calls = []


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeTDMAPI]]:
    fake_pkg = type(sys)('springernature_api_client')
    fake_tdm = type(sys)('springernature_api_client.tdm')
    fake_tdm.TDMAPI = _FakeTDMAPI  # type: ignore[attr-defined]
    fake_exc = type(sys)('springernature_api_client.exceptions')
    fake_exc.APIRequestError = _APIRequestError  # type: ignore[attr-defined]
    fake_exc.InvalidAPIKeyError = _InvalidAPIKeyError  # type: ignore[attr-defined]
    fake_exc.RateLimitExceededError = _RateLimitExceededError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'springernature_api_client', fake_pkg)
    monkeypatch.setitem(sys.modules, 'springernature_api_client.tdm', fake_tdm)
    monkeypatch.setitem(sys.modules, 'springernature_api_client.exceptions', fake_exc)
    _FakeTDMAPI.reset()
    yield _FakeTDMAPI
    _FakeTDMAPI.reset()


@pytest.fixture
def meta() -> CrossRefMetadata:
    return CrossRefMetadata(
        doi='10.1186/s12880-024-12345-1',
        publisher_str='Springer Nature',
        title='Demo paper',
        authors=('Doe, Jane',),
        year=2024,
        type='journal-article',
        license=None,
    )


# Missing credential ----------------------------------------------------------


async def test_missing_key_raises_missing_credential(
    settings: Settings, meta: CrossRefMetadata, tmp_path: Path
) -> None:
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MissingCredentialError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings
            )
    assert exc_info.value.context['publisher'] == Publisher.SPRINGER_NATURE.value


async def test_missing_sdk_extra_raises_missing_credential(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, 'springernature_api_client', None)
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MissingCredentialError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    hint = exc_info.value.context['hint']
    assert isinstance(hint, str)
    assert 'install the [springer] extra' in hint


async def test_sdk_invalid_key_raises_missing_credential(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
) -> None:
    fake_sdk._responses[f'doi:{meta.doi}'] = _InvalidAPIKeyError('rejected at construct')
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MissingCredentialError):
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )


# Auth rejected --------------------------------------------------------------


async def test_401_chained_through_request_error_raises_auth_rejected(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
) -> None:
    # Fabricate the SDK's flatten-via-requests pattern: APIRequestError
    # wraps a requests-like HTTPError carrying response.status_code = 401.
    class _FakeResponse:
        status_code = 401

    class _FakeHTTPError(Exception):
        response = _FakeResponse()

    cause = _FakeHTTPError('401 unauthorized')
    flattened = _APIRequestError('401 Client Error')
    flattened.__cause__ = cause
    fake_sdk._responses[f'doi:{meta.doi}'] = flattened
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthRejectedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['http_status'] == 401


async def test_request_error_without_status_falls_back_to_publisher_api_error(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
) -> None:
    fake_sdk._responses[f'doi:{meta.doi}'] = _APIRequestError('connection reset')
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(PublisherAPIError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['http_status'] is None


# Rate limit -----------------------------------------------------------------


async def test_rate_limit_raises_rate_limit_exhausted(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
) -> None:
    fake_sdk._responses[f'doi:{meta.doi}'] = _RateLimitExceededError('quota exhausted')
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(RateLimitExhaustedError):
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )


# Success --------------------------------------------------------------------


async def test_success_returns_payload_under_tmp_dir(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_sdk._responses[f'doi:{meta.doi}'] = _FAKE_JATS.decode('utf-8')
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        payload = await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
        )
    assert payload.format is Format.JATS_XML
    assert payload.byte_size == len(_FAKE_JATS)
    assert len(payload.sha256) == 64
    assert payload.tmp_path.is_file()
    assert payload.tmp_path.parent == tmp_path
    assert payload.tmp_path.suffix == '.part'
    assert payload.fetched_url.startswith('https://')
    assert payload.sdk_version.startswith('springernature-api-client ')
    # Confirm we passed q='doi:<doi>', p=1, s=1, is_premium=True.
    call = fake_sdk._calls[-1]
    assert call['q'] == f'doi:{meta.doi}'
    assert call['p'] == 1
    assert call['s'] == 1
    assert call['is_premium'] is True
    # And that the SDK's `print()` was muzzled.
    captured = capsys.readouterr()
    assert 'Fetching TDM' not in captured.out


# Empty / multi response edge cases ------------------------------------------


async def test_empty_response_raises_publisher_api_error(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
) -> None:
    fake_sdk._responses[f'doi:{meta.doi}'] = '   '
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(PublisherAPIError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    hint = exc_info.value.context['hint']
    assert isinstance(hint, str)
    assert 'empty body' in hint


async def test_html_response_raises_malformed_artifact(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
) -> None:
    fake_sdk._responses[f'doi:{meta.doi}'] = '<!DOCTYPE html><html><body>err</body></html>'
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MalformedArtifactError):
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )


async def test_two_articles_in_response_raises_publisher_api_error(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
) -> None:
    # ``<article-meta>`` does not start with ``<article`` followed by a
    # space or ``>``, so it does NOT trigger the count. Only true
    # ``<article ...>`` opening tags do. Two of those = two records.
    body = (
        '<?xml version="1.0"?>\n'
        '<article-set>'
        '<article xmlns="x"><body>one</body></article>'
        '<article xmlns="x"><body>two</body></article>'
        '</article-set>'
    )
    fake_sdk._responses[f'doi:{meta.doi}'] = body
    retriever = SpringerRetriever()
    async with httpx.AsyncClient() as client:
        # The sniff requires the *root* to be ``<article>``; ``<article-set>``
        # fails the JATS sniff and surfaces as MalformedArtifactError.
        with pytest.raises(MalformedArtifactError):
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )


# Configurable rate ----------------------------------------------------------


def test_rate_per_second_uses_spec_default_when_not_overridden() -> None:
    retriever = SpringerRetriever()
    assert retriever.rate_per_second == 5.0


def test_rate_per_second_honors_constructor_override() -> None:
    retriever = SpringerRetriever(rate_per_second=2.0)
    assert retriever.rate_per_second == 2.0


# ============================================================================
# Open Access tier (no TDM key, just SPRINGER_OA_API_KEY) — talks to
# api.springernature.com/openaccess/jats via real httpx + respx mocks.
# ============================================================================

_OA_BASE_URL = 'https://api.springernature.com/openaccess/jats'

# Minimal Springer-shaped Open Access envelope wrapping a single article.
# The real endpoint adds more chrome (``<apiMessage>``, ``<query>``,
# ``<result>`` totals), but the retriever only navigates to ``<article>``
# via local-name xpath — every other element is incidental noise.
_OA_ENVELOPE_ONE_ARTICLE: bytes = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<response>\n'
    b'  <result><total>1</total><start>1</start><pageLength>1</pageLength></result>\n'
    b'  <records>\n'
    b'    <article xmlns:xlink="http://www.w3.org/1999/xlink" '
    b'article-type="research-article">\n'
    b'      <front><article-meta><title-group><article-title>OA Demo'
    b'</article-title></title-group></article-meta></front>\n'
    b'      <body><sec><p>OA body text.</p></sec></body>\n'
    b'    </article>\n'
    b'  </records>\n'
    b'</response>\n'
)

# Envelope with zero records — the canonical "DOI is real but not OA"
# shape. ``<records/>`` self-closes; ``<result>`` reports total 0.
_OA_ENVELOPE_ZERO_RECORDS: bytes = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<response>\n'
    b'  <result><total>0</total><start>1</start><pageLength>1</pageLength></result>\n'
    b'  <records/>\n'
    b'</response>\n'
)

# Defensive case: two ``<article>`` elements in one OA response. Our
# ``p=1, s=1`` query should never produce this, but the retriever still
# refuses it loudly rather than silently picking one.
_OA_ENVELOPE_TWO_ARTICLES: bytes = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<response>\n'
    b'  <records>\n'
    b'    <article><body>one</body></article>\n'
    b'    <article><body>two</body></article>\n'
    b'  </records>\n'
    b'</response>\n'
)


def _oa_route(respx_mock: MockRouter) -> Route:
    return respx_mock.get(_OA_BASE_URL)


# --- Tier dispatch ----------------------------------------------------------


async def test_oa_path_taken_when_only_oa_key_set(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    """With ``springer_tdm_api_key=None`` and ``springer_oa_api_key`` set,
    the retriever must hit the Open Access endpoint — not the SDK."""
    route = _oa_route(respx_mock).mock(
        return_value=httpx.Response(
            200, content=_OA_ENVELOPE_ONE_ARTICLE, headers={'Content-Type': 'application/xml'}
        )
    )
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        payload = await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
        )
    assert route.called
    assert payload.format is Format.JATS_XML
    # ``fetched_url`` carries the OA endpoint, not the TDM one — useful
    # when chasing "which tier produced this manifest" from disk.
    assert _OA_BASE_URL in payload.fetched_url
    assert payload.sdk_version.startswith('litspectraits-springer-oa ')


async def test_tdm_path_preferred_when_both_keys_set(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMAPI],
    respx_mock: MockRouter,
) -> None:
    """``settings_with_creds`` populates both keys; TDM must win.

    The OA respx route is set up but should never be called. Conversely
    the fake TDM SDK's ``_calls`` list should have exactly one entry.
    """
    fake_sdk._responses[f'doi:{meta.doi}'] = _FAKE_JATS.decode('utf-8')
    oa_route = _oa_route(respx_mock).mock(
        return_value=httpx.Response(500, text='should not be called')
    )
    retriever = SpringerRetriever()
    async with http_client(settings_with_creds) as client:
        payload = await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
        )
    assert not oa_route.called, 'TDM tier should pre-empt the OA path entirely'
    assert len(fake_sdk._calls) == 1
    assert _OA_BASE_URL not in payload.fetched_url


# --- OA happy path ----------------------------------------------------------


async def test_oa_success_unwraps_article_and_passes_sniff(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    """Staged file must be rooted at ``<article>``, not ``<response>``.

    The OA envelope wraps records in ``<response>``/``<records>``; the
    JATS sniff (``sniff.py``) only recognizes ``<article>`` as a root,
    so the retriever's unwrap step is what stands between us and an
    unconditional ``MalformedArtifactError`` on every OA fetch.
    """
    _oa_route(respx_mock).mock(
        return_value=httpx.Response(
            200, content=_OA_ENVELOPE_ONE_ARTICLE, headers={'Content-Type': 'application/xml'}
        )
    )
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        payload = await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
        )
    staged = payload.tmp_path.read_bytes()
    assert staged.lstrip(b'\xef\xbb\xbf').lstrip().startswith(b'<?xml')
    # After XML prolog: the first opening tag should be ``<article``.
    # ``<response>`` and ``<records>`` must have been stripped.
    assert b'<article' in staged
    assert b'<response' not in staged
    assert b'<records' not in staged


async def test_oa_query_is_bare_doi_without_openaccess_filter(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    """The query must be a bare ``doi:<doi>`` — *not* ``openaccess:true``-filtered.

    Despite the endpoint name, the ``openaccess:`` filter operator is a
    premium feature: ``q=doi:<doi> openaccess:true`` is rejected 403
    "premium feature" by the free dev-portal key. Pinning the absence of
    that term here means a well-meaning "follow the docs example" change
    re-introducing it surfaces as a test failure rather than a 403 in
    production.
    """
    route = _oa_route(respx_mock).mock(
        return_value=httpx.Response(
            200, content=_OA_ENVELOPE_ONE_ARTICLE, headers={'Content-Type': 'application/xml'}
        )
    )
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
        )
    q = route.calls.last.request.url.params['q']
    assert q == f'doi:{meta.doi}'
    assert 'openaccess' not in q
    # API key must travel as a query parameter (not a header) — Springer's
    # documented call shape, and what the live endpoint accepts.
    assert route.calls.last.request.url.params['api_key'] == 'dummy-springer-oa-key'


# --- OA error mapping -------------------------------------------------------


async def test_oa_zero_records_raises_not_open_access(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    """200 + zero ``<article>`` elements ⇒ the DOI exists but isn't OA.

    The retriever must distinguish this from a generic ``PublisherAPIError``
    so the CLI panel can point at the right recourse ("get TDM tier or
    sideload"), not a vague "publisher 5xx" hint.
    """
    _oa_route(respx_mock).mock(
        return_value=httpx.Response(
            200, content=_OA_ENVELOPE_ZERO_RECORDS, headers={'Content-Type': 'application/xml'}
        )
    )
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        with pytest.raises(NotOpenAccessError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
            )
    assert exc_info.value.context['publisher'] == Publisher.SPRINGER_NATURE.value
    assert exc_info.value.context['tier'] == 'openaccess'
    hint = exc_info.value.context['hint']
    assert isinstance(hint, str)
    assert 'SPRINGER_TDM_API_KEY' in hint  # recourse signposted


async def test_oa_401_raises_auth_rejected(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _oa_route(respx_mock).mock(return_value=httpx.Response(401, text='unauthorized'))
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        with pytest.raises(AuthRejectedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
            )
    assert exc_info.value.context['http_status'] == 401
    assert exc_info.value.context['tier'] == 'openaccess'


async def test_oa_403_raises_auth_rejected(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _oa_route(respx_mock).mock(return_value=httpx.Response(403, text='forbidden'))
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        with pytest.raises(AuthRejectedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
            )
    assert exc_info.value.context['http_status'] == 403


async def test_oa_429_raises_rate_limit_exhausted(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _oa_route(respx_mock).mock(return_value=httpx.Response(429, headers={'Retry-After': '30'}))
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        with pytest.raises(RateLimitExhaustedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
            )
    assert exc_info.value.context['retry_after'] == '30'


async def test_oa_500_raises_publisher_api_error(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    _oa_route(respx_mock).mock(return_value=httpx.Response(500, text='boom'))
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        with pytest.raises(PublisherAPIError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
            )
    assert exc_info.value.context['http_status'] == 500


async def test_oa_malformed_xml_raises_publisher_api_error(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    """200 + non-XML body ⇒ ``PublisherAPIError``, not :class:`NotOpenAccessError`.

    Distinguishes "Springer served garbage" (their fault) from "DOI is
    not open-access" (our recourse). Both are 200s, so the test pins
    that the retriever leans on the parse outcome, not the status code.
    """
    _oa_route(respx_mock).mock(return_value=httpx.Response(200, text='not even close to xml'))
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        with pytest.raises(PublisherAPIError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
            )
    hint = exc_info.value.context.get('hint')
    assert isinstance(hint, str)
    assert 'did not parse as XML' in hint


async def test_oa_two_articles_raises_publisher_api_error(
    settings_with_oa_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    respx_mock: MockRouter,
) -> None:
    """Two ``<article>`` elements in one response — defensive guard.

    Our ``p=1, s=1`` query should never trigger this, but if Springer
    ever changes shape we want the retriever to refuse rather than
    silently pick one. Distinct from the OA zero-records case (which is
    :class:`NotOpenAccessError`) and the malformed-XML case (which is a
    parse failure).
    """
    _oa_route(respx_mock).mock(
        return_value=httpx.Response(
            200, content=_OA_ENVELOPE_TWO_ARTICLES, headers={'Content-Type': 'application/xml'}
        )
    )
    retriever = SpringerRetriever()
    async with http_client(settings_with_oa_creds) as client:
        with pytest.raises(PublisherAPIError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_oa_creds
            )
    assert exc_info.value.context['articles_found'] == 2
    assert exc_info.value.context['tier'] == 'openaccess'
