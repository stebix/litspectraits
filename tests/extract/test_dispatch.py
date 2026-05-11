"""Tests for :mod:`litspectraits.extract._dispatch`.

The dispatcher itself does very little — it only routes on
:attr:`AcquisitionRecord.format`. Step 10a stubbed every leg as
:class:`NotImplementedError` with operator-visible breadcrumbs; step 10b
wired the PDF leg through to :func:`~litspectraits.extract.pdf.extract_pdf`
so the JATS / Elsevier breadcrumbs are the only ones still asserted here.

The asserted breadcrumb text is part of the contract — operators reading
``litspectraits extract`` against an XML record should see the
"lands in step 10c/10d" hint.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import litspectraits.extract._dispatch as dispatch_mod
from litspectraits.errors import MissingArtifactError
from litspectraits.extract import extract
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


@pytest.mark.parametrize(
    ('fmt', 'publisher', 'expected_step'),
    [
        (Format.JATS_XML, Publisher.SPRINGER_NATURE, '10c'),
        (Format.ELSEVIER_XML, Publisher.ELSEVIER, '10d'),
    ],
)
async def test_xml_branches_still_raise_not_implemented(
    fmt: Format, publisher: Publisher, expected_step: str, store: ArtifactStore
) -> None:
    record = _acquisition_record(fmt=fmt, publisher=publisher)
    with pytest.raises(NotImplementedError) as excinfo:
        await extract(record, store)
    assert expected_step in str(excinfo.value)


async def test_pdf_branch_routes_through_extract_pdf(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PDF leg is wired (step 10b); pin the call shape so the dispatch
    contract (positional record + store, keyword reextract) does not drift.
    """
    captured: dict[str, object] = {}

    async def _fake_extract_pdf(
        record: AcquisitionRecord, store: ArtifactStore, *, reextract: bool = False
    ) -> object:
        captured['record'] = record
        captured['store'] = store
        captured['reextract'] = reextract
        return 'sentinel-extract-record'

    monkeypatch.setattr(dispatch_mod, 'extract_pdf', _fake_extract_pdf)
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)
    result = await extract(record, store, reextract=True)
    assert result == 'sentinel-extract-record'
    assert captured['record'] is record
    assert captured['store'] is store
    assert captured['reextract'] is True


async def test_pdf_branch_default_reextract_is_false(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def _fake_extract_pdf(
        record: AcquisitionRecord, store: ArtifactStore, *, reextract: bool = False
    ) -> object:
        captured['reextract'] = reextract
        return None

    monkeypatch.setattr(dispatch_mod, 'extract_pdf', _fake_extract_pdf)
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)
    await extract(record, store)
    assert captured['reextract'] is False


async def test_pdf_branch_propagates_missing_artifact_error(
    store: ArtifactStore,
) -> None:
    """End-to-end: dispatch → real ``extract_pdf`` → preflight error.

    Picks the cheapest preflight failure (the artifact path under
    ``store.data_dir`` does not exist) so the dispatch wiring is exercised
    against the real PDF extractor without needing docling.
    """
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)
    with pytest.raises(MissingArtifactError):
        await extract(record, store)


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
