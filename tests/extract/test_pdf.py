"""Tests for :mod:`litspectraits.extract.pdf` (step 10b).

The unit tests fake docling via a monkeypatched ``_load_docling`` (see
``tests/extract/conftest.py``). The real docling library is intentionally
**not** exercised here — that smoke story belongs in the docling-aware
``doctor --smoke-extract`` flow landing in step 10f. The single test that
covers the real-import path patches ``sys.modules['docling'] = None`` and
asserts the import-error translation.
"""

import json
import sys
from collections.abc import Callable

import pytest
from structlog.testing import capture_logs

import litspectraits.extract.pdf as pdf_mod
from litspectraits.errors import (
    DoclingConversionError,
    DoclingDegradedError,
    DoclingImportError,
    EmptyDocumentError,
    ExtractIntegrityError,
    MissingArtifactError,
    ParseDegradedError,
    SerializationError,
    WrongFormatForExtractorError,
)
from litspectraits.extract.pdf import FLOOR_CHARS, extract_pdf
from litspectraits.manifest import (
    AcquisitionRecord,
    Extractor,
    Format,
    Publisher,
)
from litspectraits.store import ArtifactStore
from tests.extract.conftest import (
    FakeConvertResult,
    FakeDocling,
    FakeDocument,
    FakePictureItem,
    FakeStatus,
    FakeTableItem,
    FakeTextItem,
    make_paragraph_text,
)

# Helpers ---------------------------------------------------------------------


def _rich_document(*, n_pages: int = 3) -> FakeDocument:
    """A document that comfortably clears every structural-sanity floor."""
    return FakeDocument(
        texts=[
            FakeTextItem(text='Introduction', label='section_header'),
            FakeTextItem(text=make_paragraph_text(length=600), label='text'),
            FakeTextItem(text='Methods', label='section_header'),
            FakeTextItem(text=make_paragraph_text(length=400), label='text'),
        ],
        tables=[FakeTableItem(), FakeTableItem()],
        pictures=[FakePictureItem()],
        n_pages=n_pages,
    )


def _register_success(
    fake_docling: FakeDocling,
    record: AcquisitionRecord,
    store: ArtifactStore,
    document: FakeDocument | None = None,
) -> FakeDocument:
    document = document if document is not None else _rich_document()
    artifact_path = store.data_dir / record.artifact_path
    fake_docling.converter.register(
        artifact_path,
        FakeConvertResult(status=FakeStatus.SUCCESS, document=document),
    )
    return document


# Stage 1 — preflight ---------------------------------------------------------


async def test_wrong_format_raises_wrong_format_for_extractor(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store, fmt=Format.JATS_XML, write_artifact=False)
    with pytest.raises(WrongFormatForExtractorError) as excinfo:
        await extract_pdf(record, store)
    assert excinfo.value.doi == record.doi
    assert excinfo.value.context['expected'] == Format.PDF.value
    assert excinfo.value.context['actual'] == Format.JATS_XML.value
    # No docling load attempted — preflight rejects before stage 2.
    assert fake_docling.load_calls == []


async def test_missing_artifact_raises_missing_artifact_error(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store, write_artifact=False)
    with pytest.raises(MissingArtifactError) as excinfo:
        await extract_pdf(record, store)
    assert excinfo.value.context['sha256'] == record.sha256
    assert 'litspectraits ingest' in str(excinfo.value.context['hint'])
    assert fake_docling.load_calls == []


# Stage 2 — convert -----------------------------------------------------------


async def test_missing_extract_extra_raises_docling_import_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting ``sys.modules['docling'] = None`` makes ``import docling`` fail.

    Exercises the real :func:`_load_docling` import-error path without
    touching the patched fake adapter — one of the two tests in this
    module that run the unmocked import code (the other being
    :func:`test_load_docling_records_buildout_knobs`).
    """
    monkeypatch.setitem(sys.modules, 'docling', None)
    monkeypatch.setitem(sys.modules, 'docling.datamodel', None)
    monkeypatch.setitem(sys.modules, 'docling.datamodel.base_models', None)
    monkeypatch.setitem(sys.modules, 'docling.datamodel.pipeline_options', None)
    monkeypatch.setitem(sys.modules, 'docling.document_converter', None)

    record = make_acquisition_record(store=store)
    with pytest.raises(DoclingImportError) as excinfo:
        await extract_pdf(record, store)
    hint = excinfo.value.context['hint']
    assert isinstance(hint, str)
    assert 'uv sync --extra extract' in hint


@pytest.mark.extract_real
def test_load_docling_records_buildout_knobs() -> None:
    """The real :func:`_load_docling` builds the ``docling-settings-buildout.md`` §2 config.

    Builds the converter for real (no model download — ``DocumentConverter``
    loads weights lazily on first ``.convert()``), then asserts on the
    recorded ``pipeline_view``: a misspelled ``PdfPipelineOptions`` kwarg
    (``do_formula_enrichment``, ``layout_options``, ``document_timeout``)
    would already have raised here, and ``layout_model`` is read straight
    off the docling ``DOCLING_LAYOUT_EGRET_LARGE`` symbol so it pins the
    spec-name coupling too.
    """
    adapter = pdf_mod._load_docling(doi='10.1234/buildout-knobs')
    pv = adapter.pipeline_view
    assert pv['layout_model'] == 'docling_layout_egret_large'
    assert pv['do_formula_enrichment'] is True
    assert pv['document_timeout'] == pdf_mod.DOCUMENT_TIMEOUT_S == 120.0
    assert pv['table_structure_kind'] == 'docling_tableformer'
    assert pv['table_mode'] == 'accurate'
    assert pv['do_cell_matching'] is True
    assert pv['force_backend_text'] is False
    # ``LAYOUT_MODEL_REPO_FOLDER`` (which ``doctor`` probes) must match the
    # repo folder of the spec the converter is actually configured with.
    from docling.datamodel.layout_model_specs import (  # pyright: ignore[reportMissingImports]
        DOCLING_LAYOUT_EGRET_LARGE,
    )

    assert DOCLING_LAYOUT_EGRET_LARGE.model_repo_folder == pdf_mod.LAYOUT_MODEL_REPO_FOLDER


async def test_failure_status_raises_docling_conversion_error(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    artifact_path = store.data_dir / record.artifact_path
    fake_docling.converter.register(
        artifact_path,
        FakeConvertResult(
            status=FakeStatus.FAILURE,
            errors=['layout model crashed', 'page 4 unreadable'],
        ),
    )
    with pytest.raises(DoclingConversionError) as excinfo:
        await extract_pdf(record, store)
    assert excinfo.value.context['errors'] == [
        'layout model crashed',
        'page 4 unreadable',
    ]
    # Nothing committed.
    assert not store.document_dir(record.sha256).exists()


async def test_partial_success_raises_docling_degraded_error(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    """``PARTIAL_SUCCESS`` is loud — the spec calls table-failure-as-warning corpus poison."""
    record = make_acquisition_record(store=store)
    artifact_path = store.data_dir / record.artifact_path
    fake_docling.converter.register(
        artifact_path,
        FakeConvertResult(
            status=FakeStatus.PARTIAL_SUCCESS,
            document=_rich_document(),
            errors=['TableFormer failed on table 3'],
        ),
    )
    with pytest.raises(DoclingDegradedError) as excinfo:
        await extract_pdf(record, store)
    assert excinfo.value.context['errors'] == ['TableFormer failed on table 3']
    assert not store.document_dir(record.sha256).exists()


# Stage 3 — structural sanity -------------------------------------------------


async def test_zero_text_blocks_raises_empty_document_error(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    document = FakeDocument(texts=[], n_pages=2)
    _register_success(fake_docling, record, store, document=document)
    with pytest.raises(EmptyDocumentError) as excinfo:
        await extract_pdf(record, store)
    assert excinfo.value.context['n_text_blocks'] == 0
    assert excinfo.value.context['n_pages'] == 2
    assert not store.document_dir(record.sha256).exists()


async def test_char_count_below_floor_raises_parse_degraded_error(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    """One block of well-below-floor content triggers ParseDegradedError."""
    record = make_acquisition_record(store=store)
    document = FakeDocument(
        texts=[FakeTextItem(text='abc', label='text')],
        n_pages=2,
    )
    _register_success(fake_docling, record, store, document=document)
    with pytest.raises(ParseDegradedError) as excinfo:
        await extract_pdf(record, store)
    assert excinfo.value.context['char_count'] == 3
    assert excinfo.value.context['floor_chars'] == FLOOR_CHARS


async def test_zero_section_headers_warns_but_succeeds(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    """Flat-structure documents (review articles) are warned about, not rejected."""
    record = make_acquisition_record(store=store)
    document = FakeDocument(
        texts=[FakeTextItem(text=make_paragraph_text(length=800), label='text')],
        n_pages=2,
    )
    _register_success(fake_docling, record, store, document=document)

    with capture_logs() as logs:
        extract_record = await extract_pdf(record, store)

    assert extract_record.n_section_headers == 0
    warnings = [
        r for r in logs if r.get('log_level') == 'warning' and 'flat structure' in r['event']
    ]
    assert len(warnings) == 1
    assert warnings[0]['doi'] == record.doi


# Stage 4 — serialize ---------------------------------------------------------


async def test_export_to_dict_failure_raises_serialization_error(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    document = _rich_document()
    document.export_exc = RuntimeError('export blew up')
    _register_success(fake_docling, record, store, document=document)
    with pytest.raises(SerializationError) as excinfo:
        await extract_pdf(record, store)
    assert excinfo.value.context['error'] == 'export blew up'
    assert not store.document_dir(record.sha256).exists()


async def test_unjsonable_payload_raises_serialization_error(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    """A docling payload carrying a non-JSON-encodable value surfaces cleanly."""
    record = make_acquisition_record(store=store)
    document = _rich_document()
    document.export_payload = {'set_field': {1, 2, 3}}  # set is unencodable
    _register_success(fake_docling, record, store, document=document)
    with pytest.raises(SerializationError) as excinfo:
        await extract_pdf(record, store)
    assert 'json.dumps' in str(excinfo.value.context['hint'])


# Stage 5 — commit ------------------------------------------------------------


async def test_happy_path_writes_document_and_meta_with_expected_counts(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    document = _rich_document(n_pages=4)
    _register_success(fake_docling, record, store, document=document)

    extract_record = await extract_pdf(record, store)

    # Returned record reflects the freshly-computed stats.
    assert extract_record.sha256 == record.sha256
    assert extract_record.extractor is Extractor.DOCLING
    assert extract_record.extractor_version.startswith('docling ')
    assert extract_record.n_pages == 4
    assert extract_record.n_section_headers == 2
    assert extract_record.n_text_blocks == 2
    assert extract_record.n_tables == 2
    assert extract_record.n_figures == 1
    assert extract_record.char_count > FLOOR_CHARS

    # Both files written into documents/sha256/<aa>/<sha>/.
    doc_path = store.document_dir(record.sha256) / 'document.json'
    meta_path = store.document_dir(record.sha256) / 'meta.json'
    assert doc_path.is_file()
    assert meta_path.is_file()

    # document.json is the verbatim docling export.
    on_disk_doc = json.loads(doc_path.read_text())
    assert on_disk_doc == document.export_to_dict()

    # meta.json carries the extraction stats + pipeline knobs.
    meta = json.loads(meta_path.read_text())
    assert meta['extractor'] == Extractor.DOCLING.value
    assert meta['source_sha256'] == record.sha256
    assert meta['format'] == Format.PDF.value
    assert meta['n_pages'] == 4
    assert meta['n_text_blocks'] == 2
    assert meta['n_section_headers'] == 2
    assert meta['n_tables'] == 2
    assert meta['n_figures'] == 1
    assert meta['pipeline']['table_mode'] == 'accurate'
    assert meta['pipeline']['table_structure_kind'] == 'docling_tableformer'
    assert meta['pipeline']['do_cell_matching'] is True
    assert meta['pipeline']['do_formula_enrichment'] is True
    assert meta['pipeline']['layout_model'] == 'docling_layout_egret_large'
    assert meta['pipeline']['document_timeout'] == 120.0
    assert meta['pipeline']['force_backend_text'] is False

    # No leftover ``.part`` files in tmp/.
    assert list(store.tmp_dir.glob('*.part')) == []
    # The fake adapter was loaded exactly once.
    assert fake_docling.load_calls == [record.doi]


async def test_idempotent_reextract_with_identical_bytes_is_a_noop(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    document = _rich_document()
    _register_success(fake_docling, record, store, document=document)

    first = await extract_pdf(record, store)

    doc_path = store.document_dir(record.sha256) / 'document.json'
    meta_path = store.document_dir(record.sha256) / 'meta.json'
    doc_mtime_before = doc_path.stat().st_mtime_ns
    meta_mtime_before = meta_path.stat().st_mtime_ns
    meta_before = meta_path.read_text()

    second = await extract_pdf(record, store)

    # Same struct shape (extracted_at differs by sub-microsecond on a
    # second pass, so we compare the structural payload separately).
    assert second.sha256 == first.sha256
    assert second.n_pages == first.n_pages
    assert second.n_text_blocks == first.n_text_blocks
    # No on-disk write happened — both files are byte-identical.
    assert doc_path.stat().st_mtime_ns == doc_mtime_before
    assert meta_path.stat().st_mtime_ns == meta_mtime_before
    assert meta_path.read_text() == meta_before


async def test_reextract_with_diverging_bytes_without_flag_raises(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    document = _rich_document()
    _register_success(fake_docling, record, store, document=document)
    await extract_pdf(record, store)

    # Re-register with a different document on the same path.
    diverging = _rich_document(n_pages=10)
    diverging.export_payload = {'fake': 'diverging-payload'}
    fake_docling.converter.register(
        store.data_dir / record.artifact_path,
        FakeConvertResult(status=FakeStatus.SUCCESS, document=diverging),
    )

    with pytest.raises(ExtractIntegrityError) as excinfo:
        await extract_pdf(record, store)
    assert (
        excinfo.value.context['existing_document_sha256']
        != excinfo.value.context['incoming_document_sha256']
    )
    # On-disk files are unchanged.
    on_disk = json.loads((store.document_dir(record.sha256) / 'document.json').read_text())
    assert on_disk == document.export_to_dict()


async def test_reextract_flag_overwrites_diverging_bytes(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    first_doc = _rich_document()
    _register_success(fake_docling, record, store, document=first_doc)
    await extract_pdf(record, store)

    new_doc = _rich_document(n_pages=10)
    new_doc.export_payload = {'fake': 'overwritten'}
    fake_docling.converter.register(
        store.data_dir / record.artifact_path,
        FakeConvertResult(status=FakeStatus.SUCCESS, document=new_doc),
    )
    extract_record = await extract_pdf(record, store, reextract=True)
    assert extract_record.n_pages == 10

    on_disk = json.loads((store.document_dir(record.sha256) / 'document.json').read_text())
    assert on_disk == {'fake': 'overwritten'}


# Module surface --------------------------------------------------------------


def test_module_constants_match_spec() -> None:
    """Pin the spec values so a future tweak surfaces in code review."""
    assert pdf_mod.FLOOR_CHARS == 500
    assert pdf_mod.MIN_TEXT_BLOCKS == 1
    assert pdf_mod.MIN_PAGES == 1


async def test_extractor_routes_off_format_not_publisher(
    store: ArtifactStore,
    fake_docling: FakeDocling,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    """``Publisher.SPRINGER_NATURE`` + ``Format.PDF`` is unusual but legal.

    The PDF extractor routes off ``record.format`` only — the publisher
    is preserved verbatim from the manifest. Pin it so a future
    publisher-aware branch in pdf.py surfaces in code review.
    """
    record = make_acquisition_record(store=store, publisher=Publisher.SPRINGER_NATURE)
    _register_success(fake_docling, record, store)
    extract_record = await extract_pdf(record, store)
    assert extract_record.extractor is Extractor.DOCLING
