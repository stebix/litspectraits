"""Tests for the v3 :mod:`litspectraits.store` artifact store."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from litspectraits.errors import IntegrityError
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    Publisher,
)
from litspectraits.store import ArtifactStore, DOIIndexEntry


def _crossref_metadata(doi: str = '10.1002/mrm.27973') -> CrossRefMetadata:
    return CrossRefMetadata(
        doi=doi,
        publisher_str='Wiley',
        title='Some Quantitative MRI Paper',
        authors=('Doe, Jane',),
        year=2024,
        type='journal-article',
        license='https://creativecommons.org/licenses/by/4.0/',
    )


def _make_record(
    *,
    store: ArtifactStore,
    doi: str = '10.1002/mrm.27973',
    sha256: str = 'a' * 64,
    fmt: Format = Format.PDF,
    publisher: Publisher = Publisher.WILEY,
    byte_size: int = 12345,
) -> AcquisitionRecord:
    return AcquisitionRecord(
        doi=doi,
        sha256=sha256,
        artifact_path=store.artifact_relpath(sha256, fmt),
        format=fmt,
        publisher=publisher,
        metadata=_crossref_metadata(doi=doi),
        fetched_url='https://api.wiley.com/onlinelibrary/tdm/v1/articles/' + doi,
        fetched_at=datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='wiley_tdm 1.0.0',
        byte_size=byte_size,
        origin='auto',
        manual_provenance=None,
    )


def _stage(store: ArtifactStore, name: str, payload: bytes) -> Path:
    src = store.tmp_dir / name
    src.write_bytes(payload)
    return src


def test_init_creates_layout_and_clears_tmp(tmp_path: Path) -> None:
    stale = tmp_path / 'tmp' / 'stale.part'
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b'leftover')

    store = ArtifactStore(tmp_path)

    assert (tmp_path / 'artifacts').is_dir()
    assert (tmp_path / 'manifests').is_dir()
    assert (tmp_path / 'documents').is_dir()
    assert (tmp_path / 'normalized').is_dir()
    assert (tmp_path / 'index').is_dir()
    assert store.tmp_dir.is_dir()
    assert not stale.exists()


def test_document_dir_is_sharded_under_sha256(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = 'ab' + 'c' * 62
    assert store.document_dir(sha) == tmp_path / 'documents' / 'sha256' / 'ab' / sha


def test_normalized_dir_is_sharded_under_sha256(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = '01' + 'd' * 62
    assert store.normalized_dir(sha) == tmp_path / 'normalized' / 'sha256' / '01' / sha


def test_artifact_path_pdf_uses_pdf_dir_and_extension(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = 'ab' + 'c' * 62
    path = store.artifact_path(sha, Format.PDF)
    assert path == tmp_path / 'artifacts' / 'pdf' / 'sha256' / 'ab' / f'{sha}.pdf'


def test_artifact_path_jats_uses_jats_dir_and_xml_extension(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = '01' + 'd' * 62
    path = store.artifact_path(sha, Format.JATS_XML)
    assert path == tmp_path / 'artifacts' / 'jats' / 'sha256' / '01' / f'{sha}.xml'


def test_artifact_path_elsevier_uses_elsevier_dir_and_xml_extension(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = 'ff' + 'e' * 62
    path = store.artifact_path(sha, Format.ELSEVIER_XML)
    assert path == tmp_path / 'artifacts' / 'elsevier' / 'sha256' / 'ff' / f'{sha}.xml'


def test_artifact_relpath_is_relative_to_data_dir(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = 'aa' + 'b' * 62
    relpath = store.artifact_relpath(sha, Format.PDF)
    assert relpath == f'artifacts/pdf/sha256/aa/{sha}.pdf'


def test_manifest_path_uses_manifests_root_with_sharding(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    sha = 'aa' + '1' * 62
    assert store.manifest_path(sha) == (
        tmp_path / 'manifests' / 'sha256' / 'aa' / f'{sha}.manifest.json'
    )


def test_commit_moves_artifact_writes_manifest_and_appends_index(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b'%PDF-1.7\n... fake pdf bytes ...\n%%EOF'
    src = _stage(store, 'fetch-1.part', payload)
    record = _make_record(store=store, byte_size=len(payload))

    dest = store.commit(src=src, record=record)

    assert dest == store.artifact_path(record.sha256, record.format)
    assert dest.read_bytes() == payload
    assert not src.exists()
    assert store.manifest_path(record.sha256).is_file()
    assert store.index_path.is_file()


def test_commit_persisted_manifest_round_trips(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b'%PDF-1.7\nbody\n%%EOF'
    src = _stage(store, 'fetch-2.part', payload)
    record = _make_record(store=store, byte_size=len(payload))

    store.commit(src=src, record=record)
    assert store.read_manifest(record.sha256) == record


def test_commit_index_entry_carries_format_column(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b'<?xml version="1.0"?><article/>'
    src = _stage(store, 'fetch-jats.part', payload)
    record = _make_record(
        store=store,
        sha256='b' * 64,
        fmt=Format.JATS_XML,
        publisher=Publisher.SPRINGER_NATURE,
        byte_size=len(payload),
    )

    store.commit(src=src, record=record)

    line = store.index_path.read_text(encoding='utf-8').strip()
    entry = json.loads(line)
    assert entry == {
        'doi': record.doi,
        'sha256': record.sha256,
        'format': 'jats_xml',
        'added_at': entry['added_at'],
    }
    assert entry['added_at'].endswith('+00:00')


def test_commit_is_idempotent_on_same_bytes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b'%PDF-1.7\nidem\n%%EOF'
    record = _make_record(store=store, byte_size=len(payload))

    src1 = _stage(store, 'fetch-a.part', payload)
    store.commit(src=src1, record=record)
    src2 = _stage(store, 'fetch-b.part', payload)
    store.commit(src=src2, record=record)

    dest = store.artifact_path(record.sha256, record.format)
    assert dest.read_bytes() == payload
    assert not src2.exists()
    lines = [
        line for line in store.index_path.read_text(encoding='utf-8').splitlines() if line.strip()
    ]
    assert len(lines) == 2


def test_commit_raises_integrity_error_on_size_mismatch(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b'%PDF-1.7\noriginal\n%%EOF'
    src1 = _stage(store, 'fetch-a.part', payload)
    record = _make_record(store=store, byte_size=len(payload))
    store.commit(src=src1, record=record)

    tampered_record = _make_record(store=store, byte_size=len(payload) + 999)
    src2 = _stage(store, 'fetch-b.part', payload)

    with pytest.raises(IntegrityError) as excinfo:
        store.commit(src=src2, record=tampered_record)
    assert excinfo.value.doi == record.doi
    assert excinfo.value.context['existing_size'] == len(payload)
    assert excinfo.value.context['incoming_size'] == len(payload) + 999


def test_commit_rejects_src_outside_tmp_dir(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    rogue = tmp_path / 'somewhere-else.part'
    rogue.write_bytes(b'%PDF-1.7\n%%EOF')
    record = _make_record(store=store, byte_size=rogue.stat().st_size)

    with pytest.raises(ValueError, match='must live under'):
        store.commit(src=rogue, record=record)


def test_commit_rejects_relpath_mismatch(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b'%PDF-1.7\n%%EOF'
    src = _stage(store, 'fetch.part', payload)
    record = _make_record(store=store, byte_size=len(payload))
    bogus = AcquisitionRecord(
        doi=record.doi,
        sha256=record.sha256,
        artifact_path='artifacts/pdf/sha256/aa/wrong.pdf',
        format=record.format,
        publisher=record.publisher,
        metadata=record.metadata,
        fetched_url=record.fetched_url,
        fetched_at=record.fetched_at,
        fetcher_version=record.fetcher_version,
        sdk_version=record.sdk_version,
        byte_size=record.byte_size,
        origin=record.origin,
        manual_provenance=record.manual_provenance,
    )

    with pytest.raises(ValueError, match='does not match computed relpath'):
        store.commit(src=src, record=bogus)


def test_find_by_doi_returns_none_for_unknown_doi(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    assert store.find_by_doi('10.1002/never-ingested') is None


def test_find_by_doi_returns_latest_when_multiple_shas_for_same_doi(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload_old = b'%PDF-1.7\nold\n%%EOF'
    payload_new = b'%PDF-1.7\nnew bytes that differ\n%%EOF'

    record_old = _make_record(store=store, sha256='c' * 64, byte_size=len(payload_old))
    record_new = _make_record(store=store, sha256='d' * 64, byte_size=len(payload_new))

    store.commit(src=_stage(store, 'old.part', payload_old), record=record_old)
    store.commit(src=_stage(store, 'new.part', payload_new), record=record_new)

    found = store.find_by_doi(record_new.doi)
    assert found == record_new


def test_find_by_doi_skips_unrelated_dois(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload_a = b'%PDF-1.7\na\n%%EOF'
    payload_b = b'%PDF-1.7\nb\n%%EOF'

    record_a = _make_record(
        store=store, doi='10.1002/aaa', sha256='1' * 64, byte_size=len(payload_a)
    )
    record_b = _make_record(
        store=store, doi='10.1002/bbb', sha256='2' * 64, byte_size=len(payload_b)
    )

    store.commit(src=_stage(store, 'a.part', payload_a), record=record_a)
    store.commit(src=_stage(store, 'b.part', payload_b), record=record_b)

    assert store.find_by_doi('10.1002/aaa') == record_a
    assert store.find_by_doi('10.1002/bbb') == record_b


# ---------------------------------------------------------------------------
# iter_index
# ---------------------------------------------------------------------------


def test_iter_index_empty_when_no_index_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    assert store.iter_index() == []


def test_iter_index_rolls_up_one_entry_per_doi_in_first_seen_order(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload_a = b'%PDF-1.7\na\n%%EOF'
    payload_b = b'%PDF-1.7\nb\n%%EOF'
    record_a = _make_record(
        store=store, doi='10.1002/aaa', sha256='1' * 64, byte_size=len(payload_a)
    )
    record_b = _make_record(
        store=store, doi='10.1002/bbb', sha256='2' * 64, byte_size=len(payload_b)
    )
    store.commit(src=_stage(store, 'a.part', payload_a), record=record_a)
    store.commit(src=_stage(store, 'b.part', payload_b), record=record_b)

    entries = store.iter_index()

    assert [entry.doi for entry in entries] == ['10.1002/aaa', '10.1002/bbb']
    assert entries[0].formats == {Format.PDF: '1' * 64}
    assert entries[1].formats == {Format.PDF: '2' * 64}
    assert all(entry.latest_added_at is not None for entry in entries)


def test_iter_index_collapses_dual_format_doi(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    pdf_payload = b'%PDF-1.7\n%%EOF'
    xml_payload = b'<article/>'
    pdf = _make_record(
        store=store,
        doi='10.1002/dual',
        sha256='1' * 64,
        fmt=Format.PDF,
        byte_size=len(pdf_payload),
    )
    jats = _make_record(
        store=store,
        doi='10.1002/dual',
        sha256='2' * 64,
        fmt=Format.JATS_XML,
        publisher=Publisher.SPRINGER_NATURE,
        byte_size=len(xml_payload),
    )
    store.commit(src=_stage(store, 'a.part', pdf_payload), record=pdf)
    store.commit(src=_stage(store, 'b.part', xml_payload), record=jats)

    entries = store.iter_index()

    assert len(entries) == 1
    assert entries[0].formats == {Format.PDF: '1' * 64, Format.JATS_XML: '2' * 64}


def test_iter_index_latest_added_at_is_the_max_across_lines(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    # Hand-write index lines with explicit, out-of-order timestamps for one DOI.
    with store.index_path.open('w', encoding='utf-8') as fp:
        fp.write(
            json.dumps(
                {
                    'doi': '10.1002/x',
                    'sha256': '1' * 64,
                    'format': 'pdf',
                    'added_at': '2026-05-01T00:00:00+00:00',
                }
            )
            + '\n'
        )
        fp.write(
            json.dumps(
                {
                    'doi': '10.1002/x',
                    'sha256': '1' * 64,
                    'format': 'pdf',
                    'added_at': '2026-05-09T00:00:00+00:00',
                }
            )
            + '\n'
        )

    (entry,) = store.iter_index()

    assert entry.latest_added_at == datetime(2026, 5, 9, tzinfo=UTC)


def test_iter_index_tolerates_missing_added_at(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    with store.index_path.open('w', encoding='utf-8') as fp:
        fp.write(json.dumps({'doi': '10.1002/x', 'sha256': '1' * 64, 'format': 'pdf'}) + '\n')

    (entry,) = store.iter_index()

    assert entry == DOIIndexEntry(
        doi='10.1002/x', formats={Format.PDF: '1' * 64}, latest_added_at=None
    )


def test_iter_index_raises_on_malformed_line(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    with store.index_path.open('w', encoding='utf-8') as fp:
        fp.write(json.dumps({'doi': '10.1002/x', 'sha256': '1' * 64, 'format': 'pdf'}) + '\n')
        fp.write('{not json}\n')

    with pytest.raises(ValueError, match='malformed entry on line 2'):
        store.iter_index()
