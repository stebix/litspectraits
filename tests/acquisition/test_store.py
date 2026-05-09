"""ArtifactStore layout and DOI index tests."""

from datetime import UTC, datetime

from litspectraits.acquisition.manifest import AcquisitionRecord, Origin
from litspectraits.acquisition.store import ArtifactStore
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    SourceKind,
    Version,
)


def _make_record(doi: str, sha256: str) -> AcquisitionRecord:
    return AcquisitionRecord(
        doi=doi,
        sha256=sha256,
        artifact_path=f'artifacts/pdf/sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}.pdf',
        source=Availability(
            source_kind=SourceKind.PDF_UNPAYWALL,
            version=Version.PUBLISHED,
            format=Format.PDF,
            access=Access.OPEN,
            url='https://example.com/x.pdf',
            media_type='application/pdf',
        ),
        resolve_result=None,
        fetched_at=datetime.now(UTC),
        fetcher_version='0.0.0+test',
        byte_size=42,
        origin=Origin.AUTO,
        manual_provenance=None,
    )


def test_shard_path_uses_two_level_prefix(store: ArtifactStore) -> None:
    sha = 'abcdef0123456789' * 4
    path = store.shard_path(Format.PDF, sha)
    assert path.relative_to(store.data_dir).parts[:5] == (
        'artifacts',
        'pdf',
        'sha256',
        'ab',
        'cd',
    )
    assert path.name.endswith('.pdf')
    assert sha in path.name


def test_manifest_path_mirrors_shard_path(store: ArtifactStore) -> None:
    sha = 'abcdef0123456789' * 4
    manifest = store.manifest_path(sha)
    assert manifest.relative_to(store.data_dir).parts[:4] == (
        'manifests',
        'sha256',
        'ab',
        'cd',
    )
    assert manifest.name == f'{sha}.manifest.json'


def test_write_and_find_by_doi_round_trip(store: ArtifactStore) -> None:
    sha = 'a' * 64
    record = _make_record('10.1002/mrm.27973', sha)
    store.write_manifest(record)

    found = store.find_by_doi('10.1002/mrm.27973')
    assert len(found) == 1
    assert found[0].sha256 == sha
    assert found[0].doi == record.doi
    assert found[0].source.source_kind is SourceKind.PDF_UNPAYWALL


def test_find_by_doi_deduplicates_repeated_index_entries(store: ArtifactStore) -> None:
    sha = 'b' * 64
    record = _make_record('10.1002/mrm.27973', sha)
    store.write_manifest(record)
    store.write_manifest(record)  # appends a duplicate index line

    found = store.find_by_doi('10.1002/mrm.27973')
    assert len(found) == 1
