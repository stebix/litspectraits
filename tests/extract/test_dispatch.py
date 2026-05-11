"""Tests for :mod:`litspectraits.extract._dispatch` (step 10a bootstrap).

The dispatcher itself does very little — it only routes on
:attr:`AcquisitionRecord.format`. Until 10b/10c/10d land their leaf
extractors, every leg raises :class:`NotImplementedError`. These tests
pin that wiring so a future commit that swaps in a real extractor can do
so one format at a time without breaking the others.

The asserted message text is part of the contract because step 10a
intentionally leaves operator-visible breadcrumbs naming the future step
that will land each leg ('lands in step 10b', etc.).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
        (Format.PDF, Publisher.WILEY, '10b'),
        (Format.JATS_XML, Publisher.SPRINGER_NATURE, '10c'),
        (Format.ELSEVIER_XML, Publisher.ELSEVIER, '10d'),
    ],
)
async def test_dispatch_raises_not_implemented_per_format(
    fmt: Format, publisher: Publisher, expected_step: str, store: ArtifactStore
) -> None:
    record = _acquisition_record(fmt=fmt, publisher=publisher)
    with pytest.raises(NotImplementedError) as excinfo:
        await extract(record, store)
    assert expected_step in str(excinfo.value)


async def test_dispatch_accepts_reextract_kwarg(store: ArtifactStore) -> None:
    """``reextract`` is the only keyword arg the dispatch exposes today.

    The leaf extractors will consume it in 10b/c/d. Pinning the keyword
    here means a future signature drift (e.g. renaming to
    ``--overwrite``) is caught at the dispatch layer rather than per
    extractor.
    """
    record = _acquisition_record(fmt=Format.PDF, publisher=Publisher.WILEY)
    with pytest.raises(NotImplementedError):
        await extract(record, store, reextract=True)


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
