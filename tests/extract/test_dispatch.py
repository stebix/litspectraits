"""Tests for :mod:`litspectraits.extract._dispatch`.

The dispatcher itself does very little — it only routes on
:attr:`AcquisitionRecord.format`. All three legs are now wired (PDF
step 10b, JATS step 10c, Elsevier step 10d).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import litspectraits.extract._dispatch as dispatch_mod
from litspectraits.errors import BackendNotApplicableError, MissingArtifactError
from litspectraits.extract import extract
from litspectraits.extract.backend_ids import DEFAULT_PDF_BACKEND, DOCLING_STANDARD, MINERU
from litspectraits.extract.mineru import DEFAULT_MINERU_EFFORT, DEFAULT_MINERU_ENGINE
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    Publisher,
)
from litspectraits.store import ArtifactStore


def _crossref_metadata() -> CrossRefMetadata:
    return CrossRefMetadata(
        doi='10.1002/mrm.27973',
        publisher_str='Wiley',
        title='Some Quantitative MRI Paper',
        authors=('Doe, Jane',),
        year=2024,
        type='journal-article',
        license=None,
    )


def _acquisition_record(*, fmt: Format, publisher: Publisher) -> AcquisitionRecord:
    return AcquisitionRecord(
        doi='10.1002/mrm.27973',
        sha256='a' * 64,
        artifact_path=f'artifacts/{fmt.value.split("_")[0]}/sha256/aa/' + 'a' * 64 + '.bin',
        format=fmt,
        publisher=publisher,
        metadata=_crossref_metadata(),
        fetched_url='https://example.org/article',
        fetched_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='test-fixture 0.0.0',
        byte_size=12345,
        origin='auto',
        manual_provenance=None,
    )


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(data_dir=tmp_path)


# ``backend`` selects the leg's default-signature leaf: the PDF leg now
# defaults to ``extract_mineru`` (different signature), so the docling leaf is
# reached by passing ``docling-standard`` explicitly; the XML legs tolerate the
# default (mineru) backend since it means "unset" there. MinerU routing has its
# own dedicated tests below.
@pytest.mark.parametrize(
    ('fmt', 'publisher', 'fake_attr', 'backend'),
    [
        (Format.PDF, Publisher.WILEY, 'extract_pdf', DOCLING_STANDARD),
        (Format.JATS_XML, Publisher.SPRINGER_NATURE, 'extract_jats', DEFAULT_PDF_BACKEND),
        (Format.ELSEVIER_XML, Publisher.ELSEVIER, 'extract_elsevier', DEFAULT_PDF_BACKEND),
    ],
)
async def test_branch_routes_through_leaf_extractor(
    fmt: Format,
    publisher: Publisher,
    fake_attr: str,
    backend: str,
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the dispatch contract: positional record + store, keyword reextract."""
    captured: dict[str, object] = {}

    async def _fake_leaf(
        record: AcquisitionRecord,
        store: ArtifactStore,
        *,
        reextract: bool = False,
        model_cache_dir: Path | None = None,
    ) -> object:
        captured['record'] = record
        captured['store'] = store
        captured['reextract'] = reextract
        captured['model_cache_dir'] = model_cache_dir
        return f'sentinel-{fake_attr}'

    monkeypatch.setattr(dispatch_mod, fake_attr, _fake_leaf)
    record = _acquisition_record(fmt=fmt, publisher=publisher)
    result = await extract(record, store, backend=backend, reextract=True)
    assert result == f'sentinel-{fake_attr}'
    assert captured['record'] is record
    assert captured['store'] is store
    assert captured['reextract'] is True


@pytest.mark.parametrize(
    ('fmt', 'publisher', 'fake_attr', 'backend'),
    [
        (Format.PDF, Publisher.WILEY, 'extract_pdf', DOCLING_STANDARD),
        (Format.JATS_XML, Publisher.SPRINGER_NATURE, 'extract_jats', DEFAULT_PDF_BACKEND),
        (Format.ELSEVIER_XML, Publisher.ELSEVIER, 'extract_elsevier', DEFAULT_PDF_BACKEND),
    ],
)
async def test_branch_default_reextract_is_false(
    fmt: Format,
    publisher: Publisher,
    fake_attr: str,
    backend: str,
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_leaf(
        record: AcquisitionRecord,
        store: ArtifactStore,
        *,
        reextract: bool = False,
        model_cache_dir: Path | None = None,
    ) -> object:
        del model_cache_dir
        captured['reextract'] = reextract
        return None

    monkeypatch.setattr(dispatch_mod, fake_attr, _fake_leaf)
    record = _acquisition_record(fmt=fmt, publisher=publisher)
    await extract(record, store, backend=backend)
    assert captured['reextract'] is False


async def test_pdf_branch_forwards_model_cache_dir(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The PDF leg forwards ``model_cache_dir`` to ``extract_pdf``.

    The XML legs carry no model, so the kwarg is intentionally only
    threaded into :func:`~litspectraits.extract.pdf.extract_pdf`.
    """
    captured: dict[str, object] = {}

    async def _fake_extract_pdf(
        record: AcquisitionRecord,
        store: ArtifactStore,
        *,
        reextract: bool = False,
        model_cache_dir: Path | None = None,
    ) -> object:
        del record, store, reextract
        captured['model_cache_dir'] = model_cache_dir
        return None

    monkeypatch.setattr(dispatch_mod, 'extract_pdf', _fake_extract_pdf)
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)
    weights_dir = tmp_path / 'docling-weights'
    # ``model_cache_dir`` is the docling weight cache — exercised on the docling
    # leg, which is now reached by an explicit ``--backend docling-standard``.
    await extract(record, store, backend=DOCLING_STANDARD, model_cache_dir=weights_dir)
    assert captured['model_cache_dir'] == weights_dir


@pytest.mark.parametrize(
    ('fmt', 'publisher'),
    [
        (Format.PDF, Publisher.WILEY),
        (Format.JATS_XML, Publisher.SPRINGER_NATURE),
        (Format.ELSEVIER_XML, Publisher.ELSEVIER),
    ],
)
async def test_branch_propagates_missing_artifact_error(
    fmt: Format, publisher: Publisher, store: ArtifactStore
) -> None:
    """End-to-end: dispatch → real leaf extractor → preflight error.

    Picks the cheapest preflight failure (the artifact path under
    ``store.data_dir`` does not exist) so the dispatch wiring is exercised
    against the real leaf without needing docling or an XML fixture.
    """
    record = _acquisition_record(fmt=fmt, publisher=publisher)
    with pytest.raises(MissingArtifactError):
        await extract(record, store)


async def test_pdf_mineru_backend_routes_to_extract_mineru(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``backend='mineru'`` on a PDF reaches ``extract_mineru``, not docling."""
    captured: dict[str, object] = {}

    async def _fake_mineru(
        record: AcquisitionRecord,
        store: ArtifactStore,
        *,
        reextract: bool = False,
        model_cache_dir: Path | None = None,
        engine: str = DEFAULT_MINERU_ENGINE,
        effort: str = DEFAULT_MINERU_EFFORT,
    ) -> object:
        del store, model_cache_dir
        captured['record'] = record
        captured['reextract'] = reextract
        captured['engine'] = engine
        captured['effort'] = effort
        return 'sentinel-mineru'

    async def _fail_pdf(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('docling must not run when backend=mineru')

    monkeypatch.setattr(dispatch_mod, 'extract_mineru', _fake_mineru)
    monkeypatch.setattr(dispatch_mod, 'extract_pdf', _fail_pdf)
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)

    result = await extract(record, store, backend=MINERU, reextract=True)

    assert result == 'sentinel-mineru'
    assert captured['record'] is record
    assert captured['reextract'] is True
    # Defaults ride along untouched when the caller doesn't set them.
    assert captured['engine'] == DEFAULT_MINERU_ENGINE
    assert captured['effort'] == DEFAULT_MINERU_EFFORT


async def test_pdf_mineru_engine_and_effort_thread_through(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mineru_engine`` / ``mineru_effort`` reach ``extract_mineru`` verbatim."""
    captured: dict[str, object] = {}

    async def _fake_mineru(
        record: AcquisitionRecord,
        store: ArtifactStore,
        *,
        reextract: bool = False,
        model_cache_dir: Path | None = None,
        engine: str = DEFAULT_MINERU_ENGINE,
        effort: str = DEFAULT_MINERU_EFFORT,
    ) -> object:
        del record, store, reextract, model_cache_dir
        captured['engine'] = engine
        captured['effort'] = effort
        return 'sentinel-mineru'

    monkeypatch.setattr(dispatch_mod, 'extract_mineru', _fake_mineru)
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)

    await extract(
        record, store, backend=MINERU, mineru_engine='hybrid-engine', mineru_effort='high'
    )

    assert captured['engine'] == 'hybrid-engine'
    assert captured['effort'] == 'high'


async def test_pdf_unknown_backend_raises_backend_not_applicable(
    store: ArtifactStore,
) -> None:
    """An unknown PDF backend id fails loud before any conversion work."""
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)
    with pytest.raises(BackendNotApplicableError):
        await extract(record, store, backend='no-such-backend')


async def test_mineru_engine_on_docling_backend_raises(store: ArtifactStore) -> None:
    """A non-default ``--mineru-engine`` with any other ``--backend`` is rejected."""
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)
    with pytest.raises(BackendNotApplicableError):
        await extract(record, store, backend=DOCLING_STANDARD, mineru_engine='hybrid-engine')


async def test_mineru_effort_on_xml_raises(store: ArtifactStore) -> None:
    """A non-default ``--mineru-effort`` on an XML artifact is rejected, not ignored."""
    record = _acquisition_record(fmt=Format.JATS_XML, publisher=Publisher.SPRINGER_NATURE)
    with pytest.raises(BackendNotApplicableError):
        await extract(record, store, mineru_effort='high')


async def test_default_mineru_knobs_on_docling_backend_are_tolerated(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving ``--mineru-engine``/``--mineru-effort`` at default never trips the guard."""

    async def _fake_pdf(
        record: AcquisitionRecord,
        store: ArtifactStore,
        *,
        reextract: bool = False,
        model_cache_dir: Path | None = None,
    ) -> object:
        del record, store, reextract, model_cache_dir
        return 'sentinel-docling'

    monkeypatch.setattr(dispatch_mod, 'extract_pdf', _fake_pdf)
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)

    result = await extract(record, store, backend=DOCLING_STANDARD)

    assert result == 'sentinel-docling'


@pytest.mark.parametrize(
    ('fmt', 'publisher'),
    [
        (Format.JATS_XML, Publisher.SPRINGER_NATURE),
        (Format.ELSEVIER_XML, Publisher.ELSEVIER),
    ],
)
async def test_non_default_backend_on_xml_raises(
    fmt: Format, publisher: Publisher, store: ArtifactStore
) -> None:
    """``--backend`` is meaningless on XML; an explicit choice is rejected.

    Uses ``docling-standard`` as the explicit non-default: since the promotion,
    ``mineru`` *is* the default backend (== "unset"), so it no longer trips the
    XML guard — ``docling-standard`` is now the value that does.
    """
    record = _acquisition_record(fmt=fmt, publisher=publisher)
    with pytest.raises(BackendNotApplicableError):
        await extract(record, store, backend=DOCLING_STANDARD)


async def test_default_backend_on_xml_is_tolerated(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default backend means "unset" — it must not trip the XML guard."""
    captured: dict[str, object] = {}

    async def _fake_jats(
        record: AcquisitionRecord,
        store: ArtifactStore,
        *,
        reextract: bool = False,
    ) -> object:
        del store, reextract
        captured['record'] = record
        return 'sentinel-jats'

    monkeypatch.setattr(dispatch_mod, 'extract_jats', _fake_jats)
    record = _acquisition_record(fmt=Format.JATS_XML, publisher=Publisher.SPRINGER_NATURE)

    result = await extract(record, store, backend=DEFAULT_PDF_BACKEND)

    assert result == 'sentinel-jats'
    assert captured['record'] is record


def test_dispatch_branches_cover_every_format() -> None:
    """Catch a future ``Format`` value that lands without an extractor leg.

    Without this guard, a new enum value would silently match no branch
    and the dispatcher would return ``None`` (the implicit fall-off of an
    inexhaustive ``match``), which would then fail later in extremely
    confusing ways. We make the gap loud at the wiring step.
    """
    # When a new Format is added, this test must be updated alongside
    # ``extract/_dispatch.py``'s match block.
    assert {fmt for fmt in Format} == {
        Format.PDF,
        Format.JATS_XML,
        Format.ELSEVIER_XML,
    }
