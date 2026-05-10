"""Tests for :mod:`litspectraits.retrievers.springer`.

The real ``springernature_api_client.tdm.TDMAPI`` makes a network call
inside ``search``. Each case monkeypatches the SDK module's ``TDMAPI``
class with a lightweight fake, plus the small set of exception classes
the retriever catches.
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    MalformedArtifactError,
    MissingCredentialError,
    PublisherAPIError,
    RateLimitExhaustedError,
)
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
