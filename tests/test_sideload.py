"""Tests for :mod:`litspectraits.sideload` (``docs/overview-v3.md`` §9, §17.8).

The sideload module is small glue (sniff → stage+hash → idempotency
short-circuit → CrossRef → commit). These tests assert observable
behaviour: PDF magic-byte gate, ``ManualProvenance`` shape,
``(doi, sha256)`` idempotency, publisher dispatch from the DOI prefix,
and that fail-loud errors bubble unwrapped.

CrossRef is mocked with ``respx``; the real network is never touched.
The autouse ``settings`` fixture from ``conftest.py`` is the shared
zero-creds settings (sideload doesn't need publisher tokens).
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC
from pathlib import Path
from typing import Final

import httpx
import pytest
import respx
import structlog
from structlog.testing import capture_logs

from litspectraits.config import Settings
from litspectraits.doi import InvalidDOIError
from litspectraits.errors import (
    DOINotFoundError,
    MalformedArtifactError,
    UnsupportedPublisherError,
)
from litspectraits.manifest import (
    AcquisitionRecord,
    Format,
    ManualProvenance,
    Publisher,
)
from litspectraits.sideload import sideload
from litspectraits.store import ArtifactStore

# Minimal valid PDF body: %PDF- magic header, a single trivial object,
# and the trailing %%EOF marker. ~70 bytes — small enough to keep tests
# fast, big enough that the sniff sees the magic at offset 0.
_MINIMAL_PDF: Final[bytes] = (
    b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n'
    b'1 0 obj\n<<>>\nendobj\n'
    b'trailer<<>>\n%%EOF\n'
)


def _crossref_payload(doi: str, *, publisher: str = 'Wiley') -> dict[str, object]:
    return {
        'status': 'ok',
        'message-type': 'work',
        'message-version': '1.0.0',
        'message': {
            'DOI': doi,
            'publisher': publisher,
            'title': ['A sideloaded paper'],
            'author': [{'family': 'Doe', 'given': 'Jane'}],
            'issued': {'date-parts': [[2024, 5, 10]]},
            'type': 'journal-article',
            'license': [{'URL': 'https://creativecommons.org/licenses/by/4.0/'}],
        },
    }


def _mock_crossref(
    respx_mock: respx.MockRouter, *, doi: str, publisher: str = 'Wiley'
) -> respx.Route:
    return respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(200, json=_crossref_payload(doi, publisher=publisher))
    )


@pytest.fixture
def pdf_file(tmp_path: Path) -> Path:
    """A minimal PDF the operator wants to sideload."""
    path = tmp_path / 'source.pdf'
    path.write_bytes(_MINIMAL_PDF)
    return path


@pytest.fixture
def store(settings: Settings) -> ArtifactStore:
    return ArtifactStore(settings.data_dir)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_writes_artifact_manifest_and_index(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """A valid PDF commits cleanly with ManualProvenance populated."""
    doi = '10.1002/sideload.001'
    _mock_crossref(respx_mock, doi=doi, publisher='Wiley')
    record = await sideload(
        doi,
        pdf_file,
        license_assertion='wiley-tdm-internal-use-only',
        source_url='https://onlinelibrary.wiley.com/doi/pdf/10.1002/sideload.001',
        note='via uni-wuerzburg library proxy',
        settings=settings,
        store=store,
        client=client,
    )
    assert record.doi == doi
    assert record.publisher is Publisher.WILEY
    assert record.format is Format.PDF
    assert record.origin == 'manual'
    assert record.sdk_version == 'manual'
    assert record.fetched_url == ''
    assert record.byte_size == len(_MINIMAL_PDF)
    assert record.metadata.title == 'A sideloaded paper'

    artifact_abs = settings.data_dir / record.artifact_path
    assert artifact_abs.is_file()
    assert artifact_abs.read_bytes() == _MINIMAL_PDF
    assert store.manifest_path(record.sha256).is_file()

    index_lines = (settings.data_dir / 'index' / 'by_doi.jsonl').read_text().splitlines()
    assert len(index_lines) == 1
    entry = json.loads(index_lines[0])
    assert entry == {
        'doi': doi,
        'sha256': record.sha256,
        'format': 'pdf',
        'added_at': entry['added_at'],  # tz-aware ISO string; assert separately
    }
    # added_at is tz-aware ISO 8601 (UTC).
    assert entry['added_at'].endswith('+00:00')


async def test_manual_provenance_populated_from_settings_and_args(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """Every ManualProvenance field reflects the call site."""
    doi = '10.1002/sideload.002'
    _mock_crossref(respx_mock, doi=doi)
    record = await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        source_url='https://example.org/paper.pdf',
        note='retrieved 2026-05-11',
        settings=settings,
        store=store,
        client=client,
    )
    prov = record.manual_provenance
    assert isinstance(prov, ManualProvenance)
    assert prov.operator == settings.contact_email
    assert prov.license_assertion == 'cc-by-4.0'
    assert prov.source_url == 'https://example.org/paper.pdf'
    assert prov.note == 'retrieved 2026-05-11'
    assert prov.retrieved_at.tzinfo is UTC


async def test_optional_source_url_and_note_default_correctly(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """source_url defaults to None; note defaults to empty string."""
    doi = '10.1002/sideload.003'
    _mock_crossref(respx_mock, doi=doi)
    record = await sideload(
        doi,
        pdf_file,
        license_assertion='unknown',
        settings=settings,
        store=store,
        client=client,
    )
    assert record.manual_provenance is not None
    assert record.manual_provenance.source_url is None
    assert record.manual_provenance.note == ''


async def test_doi_url_form_is_normalized(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """URL-form DOI is normalized before any downstream lookup."""
    normalized = '10.1002/sideload.004'
    _mock_crossref(respx_mock, doi=normalized)
    record = await sideload(
        f'https://doi.org/{normalized}',
        pdf_file,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    assert record.doi == normalized


async def test_record_roundtrips_through_find_by_doi(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """A freshly-constructed store reads back the committed manifest."""
    doi = '10.1002/sideload.005'
    _mock_crossref(respx_mock, doi=doi)
    committed = await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    fresh = ArtifactStore(settings.data_dir)
    loaded = fresh.find_by_doi(doi)
    assert loaded == committed


# ---------------------------------------------------------------------------
# Loud failures
# ---------------------------------------------------------------------------


async def test_non_pdf_input_raises_malformed_before_network_call(
    settings: Settings,
    store: ArtifactStore,
    tmp_path: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """HTML body sniffed as not-PDF — CrossRef is never called.

    The sniff is the first gate by design (§9): we must never copy
    paywall HTML or an error page into the artifact store, and the
    test asserts the fail-fast ordering by registering an exploding
    CrossRef mock that would surface if reached.
    """
    fake = tmp_path / 'fake.pdf'
    fake.write_bytes(b'<html><body>paywall</body></html>')
    # Tripwire: a CrossRef call would land here and fail the test loudly.
    crossref = respx_mock.get(
        'https://api.crossref.org/works/10.1002/sideload.006'
    ).mock(side_effect=AssertionError('crossref must not be called when sniff fails'))
    with pytest.raises(MalformedArtifactError) as exc_info:
        await sideload(
            '10.1002/sideload.006',
            fake,
            license_assertion='unknown',
            settings=settings,
            store=store,
            client=client,
        )
    assert exc_info.value.context['expected'] == 'pdf'
    assert not crossref.called
    # Nothing landed in the artifact store.
    assert not list((settings.data_dir / 'artifacts').rglob('*.pdf'))
    assert not list((settings.data_dir / 'manifests').rglob('*.json'))


async def test_invalid_doi_raises_before_sniff(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
) -> None:
    """A non-DOI input is rejected before the file is even opened."""
    with pytest.raises(InvalidDOIError):
        await sideload(
            'not a doi',
            pdf_file,
            license_assertion='unknown',
            settings=settings,
            store=store,
            client=client,
        )


async def test_unsupported_publisher_prefix_raises(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """A DOI whose prefix isn't in the v3 dispatch table fails loudly.

    Sideload of a non-{wiley,elsevier,springer_nature} DOI is treated
    the same as auto-ingest: ``UnsupportedPublisherError``. Operators
    who need to broaden scope extend the prefix table in ``metadata.py``
    rather than sneaking it in through sideload (§6).
    """
    doi = '10.9999/foo.bar'
    _mock_crossref(respx_mock, doi=doi, publisher='Mystery Press')
    with pytest.raises(UnsupportedPublisherError) as exc_info:
        await sideload(
            doi,
            pdf_file,
            license_assertion='unknown',
            settings=settings,
            store=store,
            client=client,
        )
    assert exc_info.value.doi == doi
    assert exc_info.value.context['prefix'] == '10.9999'


async def test_crossref_404_raises_doi_not_found(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """A fake DOI surfaces as DOINotFoundError, not an IntegrityError."""
    doi = '10.1002/never.exists'
    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(DOINotFoundError):
        await sideload(
            doi,
            pdf_file,
            license_assertion='unknown',
            settings=settings,
            store=store,
            client=client,
        )


async def test_missing_pdf_file_raises_oserror_subclass(
    settings: Settings,
    store: ArtifactStore,
    tmp_path: Path,
    client: httpx.AsyncClient,
) -> None:
    """Nonexistent path: stdlib FileNotFoundError, not an IngestError.

    Library callers see a stdlib exception; the CLI layer maps Typer's
    path-existence check to exit 2 *before* this function is invoked.
    """
    with pytest.raises(FileNotFoundError):
        await sideload(
            '10.1002/sideload.007',
            tmp_path / 'does-not-exist.pdf',
            license_assertion='unknown',
            settings=settings,
            store=store,
            client=client,
        )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_idempotent_no_op_on_same_doi_and_sha(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """Second sideload of same DOI + same bytes returns the existing record.

    Asserts §9's "idempotent on (doi, sha256)" guarantee: no second
    manifest write, no second index entry, no second CrossRef call. The
    returned record is byte-identical to the first call's output
    (specifically: same ``fetched_at`` timestamp).
    """
    doi = '10.1002/sideload.008'
    crossref_route = _mock_crossref(respx_mock, doi=doi)
    first = await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    assert crossref_route.call_count == 1
    second = await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    # No additional CrossRef call: the no-op short-circuits before
    # dispatch.
    assert crossref_route.call_count == 1
    assert second == first
    # Index has exactly one entry; no append on the no-op path.
    index_lines = (settings.data_dir / 'index' / 'by_doi.jsonl').read_text().splitlines()
    assert len(index_lines) == 1
    # And the tmp dir is clean — the staged copy from the no-op was removed.
    assert list(store.tmp_dir.iterdir()) == []


async def test_idempotent_no_op_emits_structured_log(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """The no-op path logs a 'sideload no-op' event for audit trails."""
    structlog.contextvars.clear_contextvars()
    doi = '10.1002/sideload.009'
    _mock_crossref(respx_mock, doi=doi)
    await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    with capture_logs() as logs:
        await sideload(
            doi,
            pdf_file,
            license_assertion='cc-by-4.0',
            settings=settings,
            store=store,
            client=client,
        )
    events = [log['event'] for log in logs]
    assert 'sideload no-op; (doi, sha256) already present' in events


async def test_resideload_with_different_bytes_writes_new_record(
    settings: Settings,
    store: ArtifactStore,
    tmp_path: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """Same DOI, different sha256 → fresh commit + index append.

    The index is append-only; ``find_by_doi`` returns the latest entry,
    which gives us "the operator updated the artifact" semantics for
    free without a special-cased mutation path.
    """
    doi = '10.1002/sideload.010'
    crossref_route = _mock_crossref(respx_mock, doi=doi)
    first_pdf = tmp_path / 'first.pdf'
    first_pdf.write_bytes(_MINIMAL_PDF)
    second_pdf = tmp_path / 'second.pdf'
    second_pdf.write_bytes(_MINIMAL_PDF + b'\n% revision 2\n')

    first = await sideload(
        doi,
        first_pdf,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    second = await sideload(
        doi,
        second_pdf,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    assert first.sha256 != second.sha256
    # CrossRef called twice: this is not a no-op.
    assert crossref_route.call_count == 2
    # Two index entries; find_by_doi returns the later one.
    index_lines = (settings.data_dir / 'index' / 'by_doi.jsonl').read_text().splitlines()
    assert len(index_lines) == 2
    assert store.find_by_doi(doi) == second


# ---------------------------------------------------------------------------
# Manifest shape
# ---------------------------------------------------------------------------


async def test_manifest_origin_and_provenance_block_persisted(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """origin=='manual' and manual_provenance survive JSON round-trip."""
    doi = '10.1002/sideload.011'
    _mock_crossref(respx_mock, doi=doi)
    record = await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        source_url='https://example.org/paper.pdf',
        note='retrieved 2026-05-11',
        settings=settings,
        store=store,
        client=client,
    )
    loaded = store.read_manifest(record.sha256)
    assert loaded.origin == 'manual'
    assert loaded.manual_provenance is not None
    assert loaded.manual_provenance.operator == settings.contact_email
    assert loaded.manual_provenance.note == 'retrieved 2026-05-11'
    assert loaded.manual_provenance.source_url == 'https://example.org/paper.pdf'
    assert loaded == record


async def test_default_publisher_dispatch_for_each_v3_prefix(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """One representative DOI per publisher dispatches correctly.

    Sideload reuses the prefix table from :mod:`litspectraits.metadata`;
    this test pins that contract for the manual path so a refactor of
    the dispatch table can't silently change sideload behaviour.
    """
    cases = [
        ('10.1002/sideload.wiley', Publisher.WILEY, 'Wiley'),
        ('10.1186/s12345-024-12345-1', Publisher.SPRINGER_NATURE, 'BioMed Central'),
        ('10.1016/j.foo.2024.01.001', Publisher.ELSEVIER, 'Elsevier BV'),
    ]
    for doi, expected_publisher, crossref_publisher in cases:
        _mock_crossref(respx_mock, doi=doi, publisher=crossref_publisher)
        # Fresh PDF body per DOI so we don't trip idempotency (would
        # be a different DOI but same sha256 — see triage S8-2).
        body = _MINIMAL_PDF + f'\n% doi:{doi}\n'.encode('ascii')
        path = pdf_file.parent / f'{doi.replace("/", "_")}.pdf'
        path.write_bytes(body)
        record = await sideload(
            doi,
            path,
            license_assertion='cc-by-4.0',
            settings=settings,
            store=store,
            client=client,
        )
        assert record.publisher is expected_publisher


# ---------------------------------------------------------------------------
# Structlog binding
# ---------------------------------------------------------------------------


async def test_doi_and_origin_bound_into_log_context(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """``doi`` and ``origin='manual'`` are bound for the whole sideload call.

    The orchestrator binds DOI once via ``structlog.contextvars`` so
    downstream structured logs are correlatable. The CrossRef call
    happens inside the bound scope; we sample the contextvars from the
    respx side-effect to confirm. ``capture_logs()`` does not merge
    contextvars (it short-circuits the processor chain), so the direct
    contextvars read is the right contract test.
    """
    structlog.contextvars.clear_contextvars()
    doi = '10.1002/sideload.012'
    sampled: dict[str, object] = {}

    def _sample(request: httpx.Request) -> httpx.Response:
        sampled.update(structlog.contextvars.get_contextvars())
        return httpx.Response(200, json=_crossref_payload(doi))

    respx_mock.get(f'https://api.crossref.org/works/{doi}').mock(side_effect=_sample)

    await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    assert sampled.get('doi') == doi
    assert sampled.get('origin') == 'manual'
    # And after the call returns, the contextvars are unwound.
    assert structlog.contextvars.get_contextvars().get('doi') is None


# ---------------------------------------------------------------------------
# Type sanity
# ---------------------------------------------------------------------------


async def test_returned_record_is_acquisition_record(
    settings: Settings,
    store: ArtifactStore,
    pdf_file: Path,
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    doi = '10.1002/sideload.013'
    _mock_crossref(respx_mock, doi=doi)
    record = await sideload(
        doi,
        pdf_file,
        license_assertion='cc-by-4.0',
        settings=settings,
        store=store,
        client=client,
    )
    assert isinstance(record, AcquisitionRecord)
