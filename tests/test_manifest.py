"""Round-trip tests for the v3 data model."""

import json
from datetime import UTC, datetime
from pathlib import Path

from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    ManualProvenance,
    Publisher,
    RetrievePayload,
    converter,
)


def _crossref_metadata() -> CrossRefMetadata:
    return CrossRefMetadata(
        doi='10.1002/mrm.27973',
        publisher_str='Wiley',
        title='Some Quantitative MRI Paper',
        authors=('Doe, Jane', 'Smith, John'),
        year=2024,
        type='journal-article',
        license='https://creativecommons.org/licenses/by/4.0/',
    )


def _manual_provenance() -> ManualProvenance:
    return ManualProvenance(
        operator='ops@example.org',
        retrieved_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        source_url='https://example.com/proxy/article',
        note='retrieved via Würzburg library proxy',
        license_assertion='institutional access; not for redistribution',
    )


def _acquisition_record(
    *, origin: str, manual_provenance: ManualProvenance | None
) -> AcquisitionRecord:
    return AcquisitionRecord(
        doi='10.1002/mrm.27973',
        sha256='a' * 64,
        artifact_path='artifacts/pdf/sha256/aa/' + 'a' * 64 + '.pdf',
        format=Format.PDF,
        publisher=Publisher.WILEY,
        metadata=_crossref_metadata(),
        fetched_url='https://api.wiley.com/onlinelibrary/tdm/v1/articles/10.1002%2Fmrm.27973',
        fetched_at=datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='wiley_tdm 1.0.0',
        byte_size=12345,
        origin=origin,  # type: ignore[arg-type]
        manual_provenance=manual_provenance,
    )


def test_format_enum_values_are_canonical_strings() -> None:
    assert Format.PDF.value == 'pdf'
    assert Format.JATS_XML.value == 'jats_xml'
    assert Format.ELSEVIER_XML.value == 'elsevier_xml'


def test_publisher_enum_values_are_canonical_strings() -> None:
    assert Publisher.WILEY.value == 'wiley'
    assert Publisher.ELSEVIER.value == 'elsevier'
    assert Publisher.SPRINGER_NATURE.value == 'springer_nature'


def test_crossref_metadata_roundtrip_with_optional_fields_populated() -> None:
    meta = _crossref_metadata()
    raw = converter.unstructure(meta)
    assert raw['authors'] == ['Doe, Jane', 'Smith, John']
    assert converter.structure(raw, CrossRefMetadata) == meta


def test_crossref_metadata_roundtrip_with_none_optionals() -> None:
    meta = CrossRefMetadata(
        doi='10.1038/s41586-024-00000-0',
        publisher_str='Springer Nature',
        title=None,
        authors=(),
        year=None,
        type=None,
        license=None,
    )
    raw = converter.unstructure(meta)
    assert raw['authors'] == []
    assert raw['title'] is None
    assert converter.structure(raw, CrossRefMetadata) == meta


def test_retrieve_payload_roundtrip_preserves_path_and_format(tmp_path: Path) -> None:
    payload = RetrievePayload(
        sha256='b' * 64,
        byte_size=98765,
        tmp_path=tmp_path / 'fetch-abc.part',
        format=Format.PDF,
        fetched_url='https://api.wiley.com/onlinelibrary/tdm/v1/articles/10.1002%2Fmrm.27973',
        sdk_version='wiley_tdm 1.0.0',
    )
    raw = converter.unstructure(payload)
    assert raw['format'] == 'pdf'
    assert isinstance(raw['tmp_path'], str)

    restored = converter.structure(raw, RetrievePayload)
    assert restored == payload
    assert isinstance(restored.tmp_path, Path)


def test_manual_provenance_roundtrip_uses_iso_datetime() -> None:
    prov = _manual_provenance()
    raw = converter.unstructure(prov)
    assert isinstance(raw['retrieved_at'], str)
    assert raw['retrieved_at'].startswith('2026-05-10T12:00:00')
    assert converter.structure(raw, ManualProvenance) == prov


def test_acquisition_record_auto_origin_roundtrip() -> None:
    record = _acquisition_record(origin='auto', manual_provenance=None)
    raw = converter.unstructure(record)
    assert raw['origin'] == 'auto'
    assert raw['manual_provenance'] is None
    assert raw['publisher'] == 'wiley'
    assert raw['format'] == 'pdf'
    assert converter.structure(raw, AcquisitionRecord) == record


def test_acquisition_record_manual_origin_roundtrip_includes_provenance() -> None:
    record = _acquisition_record(origin='manual', manual_provenance=_manual_provenance())
    raw = converter.unstructure(record)
    assert raw['origin'] == 'manual'
    assert raw['manual_provenance'] is not None
    assert raw['manual_provenance']['operator'] == 'ops@example.org'
    assert converter.structure(raw, AcquisitionRecord) == record


def test_acquisition_record_unstructured_is_json_serializable() -> None:
    """The on-disk manifest format is JSON; the unstructured shape must
    pass through ``json.dumps`` / ``json.loads`` without custom encoders."""
    record = _acquisition_record(origin='manual', manual_provenance=_manual_provenance())
    raw = converter.unstructure(record)
    text = json.dumps(raw)
    reloaded = json.loads(text)
    assert converter.structure(reloaded, AcquisitionRecord) == record
