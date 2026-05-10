"""Tests for :mod:`litspectraits.ingest` (``docs/overview-v3.md`` §17.7).

The orchestrator is purely glue: CrossRef → publisher dispatch →
retriever → sniff → store.commit. These tests inject fake retrievers
via ``monkeypatch.setattr('litspectraits.ingest.retriever_for', ...)``
so the real SDKs (``wiley_tdm``, ``springernature_api_client``) never
load and the assertions can focus on the orchestration contract:

- happy-path commit per publisher / format,
- cache-hit short-circuit only when ``cache_hit_ok=True``,
- DOI bound into ``structlog.contextvars`` for the entire ingest call,
- a representative loud failure per :class:`IngestError` subclass that
  the orchestrator can plausibly surface (the rest are retriever-internal
  and covered in ``tests/retrievers/``).
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar, Final

import httpx
import pytest
import structlog
from respx import MockRouter, Route
from structlog.testing import capture_logs

from litspectraits.config import Settings
from litspectraits.doi import InvalidDOIError
from litspectraits.errors import (
    AuthRejectedError,
    DOINotFoundError,
    EntitlementDowngradeError,
    MalformedArtifactError,
    MissingCredentialError,
    UnsupportedPublisherError,
)
from litspectraits.http import http_client
from litspectraits.ingest import ingest
from litspectraits.manifest import AcquisitionRecord, Format, Publisher, RetrievePayload
from litspectraits.metadata import CrossRefMetadata
from litspectraits.store import ArtifactStore

# Fixture bodies sized large enough to make sniff happy and small enough
# to keep the test suite fast. Each is real magic-byte-valid for its
# format so the orchestrator's defence-in-depth ``verify`` passes.
_FAKE_PDF: Final[bytes] = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n'
_FAKE_JATS: Final[bytes] = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<article xmlns="http://jats.nlm.nih.gov" article-type="research-article">'
    b'<front><article-meta><title-group><article-title>x</article-title>'
    b'</title-group></article-meta></front></article>'
)
_FAKE_ELSEVIER: Final[bytes] = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<full-text-retrieval-response xmlns="http://www.elsevier.com/xml/svapi/article/dtd">'
    b'<originalText>body</originalText>'
    b'</full-text-retrieval-response>'
)


# ---------------------------------------------------------------------------
# Fake retriever — protocol-shaped, controllable per test
# ---------------------------------------------------------------------------


FetchHook = Callable[[str, CrossRefMetadata, Path], RetrievePayload]


class _FakeRetriever:
    """Configurable stand-in for a real :class:`Retriever`.

    The test passes a ``hook(doi, meta, tmp_dir) -> RetrievePayload`` so
    the body of "what does the publisher return" stays in the test that
    cares about it. Hooks may also raise an :class:`IngestError`
    subclass to exercise the orchestrator's no-swallowing contract.
    """

    rate_per_second: ClassVar[float] = 0.0

    def __init__(
        self, *, publisher: Publisher, fmt: Format, hook: FetchHook
    ) -> None:
        # Set as instance attrs (not ClassVars) because each test wires
        # its own publisher / format. Reusing the class across tests
        # without per-instance overrides would make the wiring brittle.
        self.publisher = publisher  # type: ignore[misc]
        self.format = fmt  # type: ignore[misc]
        self._hook = hook
        self.calls: list[dict[str, object]] = []

    async def fetch(
        self,
        doi: str,
        meta: CrossRefMetadata,
        *,
        client: httpx.AsyncClient,
        tmp_dir: Path,
        settings: Settings,
    ) -> RetrievePayload:
        del client, settings  # orchestrator passes them; fake doesn't need them
        self.calls.append({'doi': doi, 'tmp_dir': tmp_dir})
        return self._hook(doi, meta, tmp_dir)


def _stage_payload(
    *,
    tmp_dir: Path,
    body: bytes,
    fmt: Format,
    fetched_url: str,
    sdk_version: str = 'fake-sdk 0.0.1',
    suffix: str = '.part',
) -> RetrievePayload:
    """Write ``body`` into ``tmp_dir`` and build a matching payload.

    Mirrors what a real retriever does: stage to a unique ``*.part``
    filename inside ``tmp_dir``, hash, then hand the orchestrator a
    payload pointing at that file.
    """
    import hashlib
    import secrets

    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f'fake-{secrets.token_hex(8)}{suffix}'
    path.write_bytes(body)
    return RetrievePayload(
        sha256=hashlib.sha256(body).hexdigest(),
        byte_size=len(body),
        tmp_path=path,
        format=fmt,
        fetched_url=fetched_url,
        sdk_version=sdk_version,
    )


# ---------------------------------------------------------------------------
# Per-publisher CrossRef fixtures
# ---------------------------------------------------------------------------


def _crossref_payload(*, doi: str, publisher: str) -> dict[str, object]:
    return {
        'status': 'ok',
        'message-type': 'work',
        'message-version': '1.0.0',
        'message': {
            'DOI': doi,
            'publisher': publisher,
            'title': ['A demo paper'],
            'author': [{'family': 'Doe', 'given': 'Jane', 'sequence': 'first'}],
            'issued': {'date-parts': [[2025, 1, 15]]},
            'type': 'journal-article',
            'license': [{'URL': 'https://creativecommons.org/licenses/by/4.0/'}],
        },
    }


def _mock_crossref(
    respx_mock: MockRouter, *, doi: str, publisher: str
) -> Route:
    return respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(200, json=_crossref_payload(doi=doi, publisher=publisher))
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(settings: Settings) -> ArtifactStore:
    return ArtifactStore(settings.data_dir)


@pytest.fixture
def install_retriever(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Publisher, _FakeRetriever], None]]:
    """Register a :class:`_FakeRetriever` to be returned by ``retriever_for``.

    Multiple registrations stack: the orchestrator's ``retriever_for``
    is monkeypatched to look up the registered fake by publisher and
    raise if a publisher with no registration is requested. That keeps
    "unexpected publisher dispatched" loud rather than silently invoking
    the real retriever.
    """
    registry: dict[Publisher, _FakeRetriever] = {}

    def _fake_retriever_for(publisher: Publisher) -> _FakeRetriever:
        if publisher not in registry:
            raise AssertionError(
                f'no fake retriever registered for {publisher.value!r}'
            )
        return registry[publisher]

    monkeypatch.setattr('litspectraits.ingest.retriever_for', _fake_retriever_for)

    def _register(publisher: Publisher, retriever: _FakeRetriever) -> None:
        registry[publisher] = retriever

    yield _register


# ---------------------------------------------------------------------------
# Per-publisher happy-path integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('doi', 'publisher', 'fmt', 'body', 'crossref_publisher'),
    [
        (
            '10.1002/mrm.27973',
            Publisher.WILEY,
            Format.PDF,
            _FAKE_PDF,
            'John Wiley & Sons, Ltd.',
        ),
        (
            '10.1186/s12880-024-12345-1',
            Publisher.SPRINGER_NATURE,
            Format.JATS_XML,
            _FAKE_JATS,
            'Springer Science and Business Media LLC',
        ),
        (
            '10.1016/j.neuroimage.2024.01.001',
            Publisher.ELSEVIER,
            Format.ELSEVIER_XML,
            _FAKE_ELSEVIER,
            'Elsevier BV',
        ),
    ],
)
async def test_ingest_happy_path_per_publisher(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
    doi: str,
    publisher: Publisher,
    fmt: Format,
    body: bytes,
    crossref_publisher: str,
) -> None:
    crossref_route = _mock_crossref(respx_mock, doi=doi, publisher=crossref_publisher)
    fake = _FakeRetriever(
        publisher=publisher,
        fmt=fmt,
        hook=lambda d, _meta, tmp_dir: _stage_payload(
            tmp_dir=tmp_dir,
            body=body,
            fmt=fmt,
            fetched_url=f'https://example.test/{d}',
        ),
    )
    install_retriever(publisher, fake)

    async with http_client(settings) as client:
        record = await ingest(doi, settings=settings, store=store, client=client)

    assert isinstance(record, AcquisitionRecord)
    assert record.doi == doi
    assert record.publisher is publisher
    assert record.format is fmt
    assert record.byte_size == len(body)
    assert record.origin == 'auto'
    assert record.manual_provenance is None

    # CrossRef and the retriever each saw exactly one call.
    assert crossref_route.call_count == 1
    assert len(fake.calls) == 1
    assert fake.calls[0]['doi'] == doi
    assert fake.calls[0]['tmp_dir'] == store.tmp_dir

    # Artifact landed at the canonical sharded path with the right bytes.
    artifact = settings.data_dir / record.artifact_path
    assert artifact.is_file()
    assert artifact.read_bytes() == body
    assert store.manifest_path(record.sha256).is_file()

    # Index carries one row with the correct format column.
    lines = [
        line
        for line in store.index_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert f'"format": "{fmt.value}"' in lines[0]


# ---------------------------------------------------------------------------
# DOI normalization — accepted forms reach the same record
# ---------------------------------------------------------------------------


async def test_ingest_normalizes_doi_url_form(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    raw = 'https://doi.org/10.1002/MRM.27973'
    normalized = '10.1002/mrm.27973'
    _mock_crossref(respx_mock, doi=normalized, publisher='Wiley')
    fake = _FakeRetriever(
        publisher=Publisher.WILEY,
        fmt=Format.PDF,
        hook=lambda d, _meta, tmp_dir: _stage_payload(
            tmp_dir=tmp_dir,
            body=_FAKE_PDF,
            fmt=Format.PDF,
            fetched_url=f'https://example.test/{d}',
        ),
    )
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        record = await ingest(raw, settings=settings, store=store, client=client)

    assert record.doi == normalized
    assert fake.calls[0]['doi'] == normalized


async def test_ingest_invalid_doi_raises_invalid_doi_error(
    settings: Settings, store: ArtifactStore
) -> None:
    async with http_client(settings) as client:
        with pytest.raises(InvalidDOIError):
            await ingest('not a doi', settings=settings, store=store, client=client)


# ---------------------------------------------------------------------------
# Cache-hit short-circuit — only when explicitly opted in
# ---------------------------------------------------------------------------


async def test_ingest_default_refetches_even_when_cached(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    """Default mode: an existing manifest is *not* a reason to skip the network."""
    doi = '10.1002/mrm.27973'
    crossref_route = _mock_crossref(respx_mock, doi=doi, publisher='Wiley')
    fake = _FakeRetriever(
        publisher=Publisher.WILEY,
        fmt=Format.PDF,
        hook=lambda d, _meta, tmp_dir: _stage_payload(
            tmp_dir=tmp_dir,
            body=_FAKE_PDF,
            fmt=Format.PDF,
            fetched_url=f'https://example.test/{d}',
        ),
    )
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        first = await ingest(doi, settings=settings, store=store, client=client)
        second = await ingest(doi, settings=settings, store=store, client=client)

    # Same bytes → same sha256 → store.commit's idempotency reuses the
    # canonical artifact, but the index gets a fresh line per call.
    assert first.sha256 == second.sha256
    assert crossref_route.call_count == 2
    assert len(fake.calls) == 2
    lines = [
        line
        for line in store.index_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]
    assert len(lines) == 2


async def test_ingest_cache_hit_ok_short_circuits_when_cached(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    doi = '10.1002/mrm.27973'
    crossref_route = _mock_crossref(respx_mock, doi=doi, publisher='Wiley')
    fake = _FakeRetriever(
        publisher=Publisher.WILEY,
        fmt=Format.PDF,
        hook=lambda d, _meta, tmp_dir: _stage_payload(
            tmp_dir=tmp_dir,
            body=_FAKE_PDF,
            fmt=Format.PDF,
            fetched_url=f'https://example.test/{d}',
        ),
    )
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        first = await ingest(doi, settings=settings, store=store, client=client)
        second = await ingest(
            doi, settings=settings, store=store, client=client, cache_hit_ok=True
        )

    assert second == first
    # Second call did not touch CrossRef or the retriever.
    assert crossref_route.call_count == 1
    assert len(fake.calls) == 1


async def test_ingest_cache_hit_ok_falls_through_when_not_cached(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    doi = '10.1002/mrm.27973'
    _mock_crossref(respx_mock, doi=doi, publisher='Wiley')
    fake = _FakeRetriever(
        publisher=Publisher.WILEY,
        fmt=Format.PDF,
        hook=lambda d, _meta, tmp_dir: _stage_payload(
            tmp_dir=tmp_dir,
            body=_FAKE_PDF,
            fmt=Format.PDF,
            fetched_url=f'https://example.test/{d}',
        ),
    )
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        record = await ingest(
            doi, settings=settings, store=store, client=client, cache_hit_ok=True
        )

    assert record.doi == doi
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Loud-failure surface
# ---------------------------------------------------------------------------


async def test_ingest_doi_404_raises_doi_not_found_error(
    settings: Settings, store: ArtifactStore, respx_mock: MockRouter
) -> None:
    doi = '10.1002/this.does.not.exist'
    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(404, json={'status': 'not-found'})
    )
    async with http_client(settings) as client:
        with pytest.raises(DOINotFoundError) as exc_info:
            await ingest(doi, settings=settings, store=store, client=client)
    assert exc_info.value.doi == doi


async def test_ingest_unknown_prefix_raises_unsupported_publisher(
    settings: Settings, store: ArtifactStore, respx_mock: MockRouter
) -> None:
    doi = '10.9999/unknown.example'
    _mock_crossref(respx_mock, doi=doi, publisher='Unknown Press')
    async with http_client(settings) as client:
        with pytest.raises(UnsupportedPublisherError) as exc_info:
            await ingest(doi, settings=settings, store=store, client=client)
    assert exc_info.value.doi == doi
    assert exc_info.value.context['prefix'] == '10.9999'


def _raise(exc: Exception) -> FetchHook:
    """Build a fetch hook that immediately raises ``exc``."""

    def _hook(_doi: str, _meta: CrossRefMetadata, _tmp_dir: Path) -> RetrievePayload:
        raise exc

    return _hook


@pytest.mark.parametrize(
    ('exc_factory', 'exc_cls'),
    [
        (
            lambda doi: MissingCredentialError(
                doi=doi, publisher=Publisher.WILEY.value, hint='set token'
            ),
            MissingCredentialError,
        ),
        (
            lambda doi: AuthRejectedError(
                doi=doi, publisher=Publisher.WILEY.value, http_status=403
            ),
            AuthRejectedError,
        ),
        (
            lambda doi: EntitlementDowngradeError(
                doi=doi, publisher=Publisher.ELSEVIER.value, hint='META_ABS'
            ),
            EntitlementDowngradeError,
        ),
    ],
)
async def test_ingest_bubbles_retriever_errors_unchanged(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
    exc_factory: Callable[[str], Exception],
    exc_cls: type[Exception],
) -> None:
    """The orchestrator never swallows :class:`IngestError` from a retriever."""
    doi = '10.1002/mrm.27973'
    _mock_crossref(respx_mock, doi=doi, publisher='Wiley')
    fake = _FakeRetriever(
        publisher=Publisher.WILEY,
        fmt=Format.PDF,
        hook=_raise(exc_factory(doi)),
    )
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        with pytest.raises(exc_cls):
            await ingest(doi, settings=settings, store=store, client=client)

    # Nothing was committed.
    assert not list((settings.data_dir / 'artifacts').rglob('*.pdf'))
    assert not list((settings.data_dir / 'manifests').rglob('*.manifest.json'))
    assert not store.index_path.exists() or store.index_path.read_text() == ''


async def test_ingest_orchestrator_sniff_catches_format_lie(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    """A retriever that claims PDF but stages HTML must be caught.

    The retriever's own ``verify`` would normally catch this; the test
    bypasses it by going through the fake. The orchestrator's
    defence-in-depth sniff at the validate step is what enforces the
    "nothing reaches commit without verify" invariant from §3.
    """
    doi = '10.1002/mrm.27973'
    _mock_crossref(respx_mock, doi=doi, publisher='Wiley')
    html_body = b'<!DOCTYPE html>\n<html><body>Paywall</body></html>\n'
    fake = _FakeRetriever(
        publisher=Publisher.WILEY,
        fmt=Format.PDF,
        hook=lambda d, _meta, tmp_dir: _stage_payload(
            tmp_dir=tmp_dir,
            body=html_body,
            fmt=Format.PDF,  # the lie — bytes are HTML, claim is PDF
            fetched_url=f'https://example.test/{d}',
        ),
    )
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        with pytest.raises(MalformedArtifactError) as exc_info:
            await ingest(doi, settings=settings, store=store, client=client)

    assert exc_info.value.doi == doi
    assert exc_info.value.context['expected'] == Format.PDF.value
    # No commit happened.
    assert not list((settings.data_dir / 'artifacts').rglob('*.pdf'))


# ---------------------------------------------------------------------------
# Observability: DOI binding + publisher cross-check warning
# ---------------------------------------------------------------------------


async def test_ingest_binds_doi_to_log_contextvars(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    doi = '10.1002/mrm.27973'
    _mock_crossref(respx_mock, doi=doi, publisher='Wiley')

    captured_doi: list[object] = []

    def _hook(d: str, _meta: CrossRefMetadata, tmp_dir: Path) -> RetrievePayload:
        # Inside the retriever call, the orchestrator must already have
        # bound DOI into structlog's contextvars. We sample it here to
        # confirm — bound_contextvars() is what makes that work.
        captured_doi.append(structlog.contextvars.get_contextvars().get('doi'))
        return _stage_payload(
            tmp_dir=tmp_dir,
            body=_FAKE_PDF,
            fmt=Format.PDF,
            fetched_url=f'https://example.test/{d}',
        )

    fake = _FakeRetriever(publisher=Publisher.WILEY, fmt=Format.PDF, hook=_hook)
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        await ingest(doi, settings=settings, store=store, client=client)

    assert captured_doi == [doi]
    # And after the call returns, the contextvar is unwound.
    assert structlog.contextvars.get_contextvars().get('doi') is None


async def test_ingest_logs_publisher_mismatch_warning(
    settings: Settings,
    store: ArtifactStore,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    """CrossRef-publisher / dispatch mismatch warns but does not block."""
    doi = '10.1002/example.mismatch'  # Wiley prefix
    _mock_crossref(respx_mock, doi=doi, publisher='Hindawi Limited')
    fake = _FakeRetriever(
        publisher=Publisher.WILEY,
        fmt=Format.PDF,
        hook=lambda d, _meta, tmp_dir: _stage_payload(
            tmp_dir=tmp_dir,
            body=_FAKE_PDF,
            fmt=Format.PDF,
            fetched_url=f'https://example.test/{d}',
        ),
    )
    install_retriever(Publisher.WILEY, fake)

    async with http_client(settings) as client:
        with capture_logs() as logs:
            record = await ingest(doi, settings=settings, store=store, client=client)

    assert record.publisher is Publisher.WILEY
    warnings = [
        entry
        for entry in logs
        if entry.get('event') == 'crossref publisher string does not match dispatched publisher'
    ]
    assert len(warnings) == 1
    assert warnings[0]['log_level'] == 'warning'
    assert warnings[0]['doi'] == doi
