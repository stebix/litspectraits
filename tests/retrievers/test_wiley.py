"""Tests for :mod:`litspectraits.retrievers.wiley`.

The real ``wiley_tdm.TDMClient`` makes a network call in its ``__init__``
(``IPUtils.get_ip_address``) and validates the token as a UUID. Both are
tedious for unit tests, so each case monkeypatches the SDK module's
``TDMClient`` symbol with a lightweight fake that exercises the same
``download_pdf`` contract our retriever depends on.
"""

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum, auto
from http import HTTPStatus
from pathlib import Path
from typing import ClassVar

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
from litspectraits.retrievers.wiley import WileyRetriever

# A minimal valid PDF body — magic bytes + a trailing %%EOF, enough to
# satisfy our sniffer and the manifest's byte-size assertions.
_FAKE_PDF: bytes = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n'


class _FakeStatus(Enum):
    SUCCESS = auto()
    ACCESS_DENIED = auto()
    UNKNOWN_DOI = auto()
    KNOWN_ISSUE = auto()
    API_ERROR = auto()
    EXISTING_FILE = auto()
    STORAGE_ERROR = auto()
    INVALID_DOI = auto()
    NETWORK_ERROR = auto()


@dataclass
class _FakeResult:
    """Stands in for ``wiley_tdm.DownloadResult``."""

    doi: str
    status: _FakeStatus
    comment: str | None = None
    path: Path | None = None
    size: int | None = None
    duration: float | None = None
    api_status: HTTPStatus | None = None


class _FakeTDMClient:
    """Stand-in for ``wiley_tdm.TDMClient``.

    Class-level ``_responses`` keys DOIs to either a callable
    ``(download_dir, doi) -> _FakeResult`` or a plain ``_FakeResult``.
    Tests configure this before invoking the retriever.
    """

    _responses: ClassVar[dict[str, object]] = {}
    _calls: ClassVar[list[tuple[str, Path]]] = []

    def __init__(self, *, download_dir: Path) -> None:
        # Mirror the real SDK's behavior of asserting the env token is set
        # at construction time — so the ``_patched_env`` contract gets
        # exercised by the test.
        import os

        if not os.environ.get('TDM_API_TOKEN'):
            raise ValueError('TDM_API_TOKEN env variable not set')
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.skip_existing_files = True

    def download_pdf(self, doi: str) -> _FakeResult:
        type(self)._calls.append((doi, self.download_dir))
        recipe = type(self)._responses.get(doi)
        if recipe is None:
            raise AssertionError(f'no fake response configured for {doi!r}')
        if callable(recipe):
            return recipe(self.download_dir, doi)  # type: ignore[no-any-return]
        return recipe  # type: ignore[return-value]

    @classmethod
    def reset(cls) -> None:
        cls._responses = {}
        cls._calls = []


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeTDMClient]]:
    """Install the fake ``wiley_tdm`` module into ``sys.modules``.

    The retriever lazy-imports inside ``fetch``; replacing the module here
    means the real SDK is never loaded by the test, even on a system with
    the ``[wiley]`` extra installed.
    """
    fake_module = type(sys)('wiley_tdm')
    fake_module.TDMClient = _FakeTDMClient  # type: ignore[attr-defined]
    fake_module.DownloadStatus = _FakeStatus  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'wiley_tdm', fake_module)
    _FakeTDMClient.reset()
    yield _FakeTDMClient
    _FakeTDMClient.reset()


@pytest.fixture
def meta() -> CrossRefMetadata:
    return CrossRefMetadata(
        doi='10.1002/mrm.27973',
        publisher_str='Wiley',
        title='Demo paper',
        authors=('Doe, Jane',),
        year=2024,
        type='journal-article',
        license=None,
    )


def _success_writer(content: bytes = _FAKE_PDF) -> Callable[[Path, str], _FakeResult]:
    def _write(download_dir: Path, doi: str) -> _FakeResult:
        # The real SDK encodes the DOI into the filename; we keep this
        # simple — sanitize the slash so the path is creatable.
        safe = doi.replace('/', '_')
        out = download_dir / f'{safe}.pdf'
        out.write_bytes(content)
        return _FakeResult(
            doi=doi,
            status=_FakeStatus.SUCCESS,
            path=out,
            size=len(content),
            duration=0.01,
            api_status=HTTPStatus.OK,
        )

    return _write


# Missing credential ----------------------------------------------------------


async def test_missing_token_raises_missing_credential(
    settings: Settings, meta: CrossRefMetadata, tmp_path: Path
) -> None:
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MissingCredentialError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings
            )
    assert exc_info.value.doi == meta.doi
    assert exc_info.value.context['publisher'] == Publisher.WILEY.value


async def test_missing_sdk_extra_raises_missing_credential(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate ``[wiley]`` extra not installed: import fails inside fetch().
    monkeypatch.setitem(sys.modules, 'wiley_tdm', None)
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MissingCredentialError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    hint = exc_info.value.context['hint']
    assert isinstance(hint, str)
    assert 'install the [wiley] extra' in hint


# Auth rejected --------------------------------------------------------------


async def test_access_denied_raises_auth_rejected(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMClient],
) -> None:
    fake_sdk._responses[meta.doi] = _FakeResult(
        doi=meta.doi,
        status=_FakeStatus.ACCESS_DENIED,
        comment='token rejected',
        api_status=HTTPStatus.FORBIDDEN,
    )
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthRejectedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['publisher'] == Publisher.WILEY.value
    assert exc_info.value.context['api_status'] == 403


# Success --------------------------------------------------------------------


async def test_success_returns_payload_under_tmp_dir(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMClient],
) -> None:
    fake_sdk._responses[meta.doi] = _success_writer()
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        payload = await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
        )
    assert payload.format is Format.PDF
    assert payload.byte_size == len(_FAKE_PDF)
    assert len(payload.sha256) == 64
    # The retriever stages under tmp_path; the orchestrator moves it later.
    assert payload.tmp_path.is_file()
    assert payload.tmp_path.parent == tmp_path
    assert payload.tmp_path.suffix == '.part'
    # Provenance fields populated.
    assert payload.fetched_url.endswith(meta.doi)
    assert payload.sdk_version.startswith('wiley-tdm ')


async def test_token_only_lives_during_construction(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TDM_API_TOKEN`` must not leak into the ambient env after fetch."""
    monkeypatch.delenv('TDM_API_TOKEN', raising=False)
    fake_sdk._responses[meta.doi] = _success_writer()
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        await retriever.fetch(
            meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
        )
    import os

    assert 'TDM_API_TOKEN' not in os.environ


# 429 retry exhausted --------------------------------------------------------


async def test_persistent_429_raises_rate_limit_exhausted(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Squash the inter-attempt sleeps so the test is fast.
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr('litspectraits.retrievers.wiley.asyncio.sleep', _no_sleep)
    fake_sdk._responses[meta.doi] = _FakeResult(
        doi=meta.doi,
        status=_FakeStatus.API_ERROR,
        api_status=HTTPStatus.TOO_MANY_REQUESTS,
        comment='throttled',
    )
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(RateLimitExhaustedError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['publisher'] == Publisher.WILEY.value
    assert exc_info.value.context['attempts'] == 3
    # All three attempts should have been issued.
    assert len(fake_sdk._calls) == 3


# Other SDK statuses ---------------------------------------------------------


async def test_unknown_doi_status_raises_publisher_api_error(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMClient],
) -> None:
    fake_sdk._responses[meta.doi] = _FakeResult(
        doi=meta.doi,
        status=_FakeStatus.UNKNOWN_DOI,
        comment='not found at publisher',
        api_status=HTTPStatus.NOT_FOUND,
    )
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(PublisherAPIError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['sdk_status'] == 'UNKNOWN_DOI'
    assert exc_info.value.context['api_status'] == 404


# Magic-byte defence in depth ------------------------------------------------


async def test_paywall_html_as_pdf_raises_malformed_artifact(
    settings_with_creds: Settings,
    meta: CrossRefMetadata,
    tmp_path: Path,
    fake_sdk: type[_FakeTDMClient],
) -> None:
    # SDK reports SUCCESS but the body is HTML — our sniff catches it.
    html_body = b'<!DOCTYPE html>\n<html><body>Paywall</body></html>\n'
    fake_sdk._responses[meta.doi] = _success_writer(content=html_body)
    retriever = WileyRetriever()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MalformedArtifactError) as exc_info:
            await retriever.fetch(
                meta.doi, meta, client=client, tmp_dir=tmp_path, settings=settings_with_creds
            )
    assert exc_info.value.context['expected'] == Format.PDF.value


# Configurable rate ----------------------------------------------------------


def test_rate_per_second_uses_spec_default_when_not_overridden() -> None:
    retriever = WileyRetriever()
    assert retriever.rate_per_second == 3.0


def test_rate_per_second_honors_constructor_override() -> None:
    retriever = WileyRetriever(rate_per_second=7.5)
    assert retriever.rate_per_second == 7.5
