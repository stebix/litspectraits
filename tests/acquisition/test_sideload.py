"""Manual sideload tests — magic bytes, manifest, idempotency."""

from pathlib import Path

import pytest

from litspectraits.acquisition.manifest import Origin
from litspectraits.acquisition.sideload import sideload
from litspectraits.acquisition.store import ArtifactStore, MalformedArtifactError
from litspectraits.config import Settings
from litspectraits.resolver.types import Format, SourceKind, Version


def _write_pdf(path: Path) -> None:
    path.write_bytes(b'%PDF-1.4\n' + b'X' * 1024 + b'\n%%EOF\n')


def test_sideload_records_manifest_with_provenance(
    settings: Settings, store: ArtifactStore, tmp_path: Path
) -> None:
    src = tmp_path / 'paper.pdf'
    _write_pdf(src)

    record = sideload(
        doi='10.1002/mrm.27973',
        path=src,
        version=Version.PUBLISHED,
        format=Format.PDF,
        license_assertion='wiley-tdm-internal-use-only',
        note='via uni-wuerzburg library proxy',
        source_url='https://onlinelibrary.wiley.com/doi/pdf/10.1002/mrm.27973',
        settings=settings,
        store=store,
    )

    assert record.origin is Origin.MANUAL
    assert record.source.source_kind is SourceKind.MANUAL
    assert record.source.version is Version.PUBLISHED
    assert record.manual_provenance is not None
    assert record.manual_provenance.operator == settings.contact_email
    assert record.manual_provenance.note == 'via uni-wuerzburg library proxy'
    assert (store.data_dir / record.artifact_path).exists()


def test_sideload_rejects_format_mismatch(
    settings: Settings, store: ArtifactStore, tmp_path: Path
) -> None:
    src = tmp_path / 'fake.pdf'
    src.write_bytes(b'<html>not a pdf</html>')

    with pytest.raises(MalformedArtifactError):
        sideload(
            doi='10.1002/mrm.27973',
            path=src,
            version=Version.PUBLISHED,
            format=Format.PDF,
            license_assertion='unknown',
            note='',
            source_url=None,
            settings=settings,
            store=store,
        )


def test_sideload_idempotent_on_same_bytes(
    settings: Settings, store: ArtifactStore, tmp_path: Path
) -> None:
    src = tmp_path / 'paper.pdf'
    _write_pdf(src)

    first = sideload(
        doi='10.1002/mrm.27973',
        path=src,
        version=Version.PUBLISHED,
        format=Format.PDF,
        license_assertion='cc-by',
        note='',
        source_url=None,
        settings=settings,
        store=store,
    )
    second = sideload(
        doi='10.1002/mrm.27973',
        path=src,
        version=Version.PUBLISHED,
        format=Format.PDF,
        license_assertion='cc-by',
        note='',
        source_url=None,
        settings=settings,
        store=store,
    )

    assert first.sha256 == second.sha256
    assert first.fetched_at == second.fetched_at
