"""Tests for :mod:`litspectraits.extract.mineru` (``docs/mineru-backend-spec.md`` §3).

MinerU's ``do_parse`` is faked via a monkeypatched ``_load_mineru`` that
returns a callable writing a scripted ``*_middle.json`` into the scratch
dir — the real MinerU library (and its multi-GB weights) is never touched
here. The real-parse smoke story lives in the gated end-to-end run, not in
these unit tests. The import-error path patches ``sys.modules['mineru']``
and asserts the typed translation.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import litspectraits.extract.mineru as mineru_mod
from litspectraits.errors import (
    BackendNotApplicableError,
    EmptyDocumentError,
    ExtractIntegrityError,
    MineruConversionError,
    MineruImportError,
    MissingArtifactError,
    ParseDegradedError,
    WrongFormatForExtractorError,
)
from litspectraits.extract.backend_ids import MINERU
from litspectraits.extract.mineru import extract_mineru
from litspectraits.manifest import AcquisitionRecord, Extractor, Format
from litspectraits.store import ArtifactStore

# Helpers ---------------------------------------------------------------------


def _text_block(content: str) -> dict[str, Any]:
    return {
        'type': 'text',
        'bbox': [10, 10, 500, 40],
        'lines': [{'spans': [{'type': 'text', 'content': content}]}],
    }


def _title_block(content: str, level: int = 1) -> dict[str, Any]:
    return {
        'type': 'title',
        'level': level,
        'bbox': [10, 5, 500, 10],
        'lines': [{'spans': [{'type': 'text', 'content': content}]}],
    }


def _table_block() -> dict[str, Any]:
    html = '<table><tr><td>Tissue</td><td>T1</td></tr><tr><td>Liver</td><td>812</td></tr></table>'
    return {
        'type': 'table',
        'bbox': [10, 50, 300, 120],
        'blocks': [
            {
                'type': 'table_body',
                'bbox': [10, 50, 300, 120],
                'lines': [{'spans': [{'type': 'table', 'html': html}]}],
            }
        ],
    }


def _middle(pages: list[dict[str, Any]], *, backend: str = 'pipeline') -> dict[str, Any]:
    return {'_backend': backend, '_version_name': '3.4.0', 'pdf_info': pages}


def _page(blocks: list[dict[str, Any]], page_idx: int = 0) -> dict[str, Any]:
    return {'page_idx': page_idx, 'page_size': [612, 792], 'para_blocks': blocks}


def _rich_middle() -> dict[str, Any]:
    """A middle.json that comfortably clears every structural-sanity floor."""
    body = 'Quantitative magnetic resonance measurement of T1, T2, and PD. ' * 12
    return _middle([_page([_title_block('Methods'), _text_block(body), _table_block()])])


def _fake_do_parse(
    middle_json: dict[str, Any] | None,
    *,
    raises: BaseException | None = None,
    write: bool = True,
    markdown: str | None = None,
    images: dict[str, bytes] | None = None,
) -> Callable[..., None]:
    """Build a stand-in ``do_parse`` that writes ``middle_json`` to the scratch dir.

    Mirrors MinerU's file-output contract: writes
    ``<output_dir>/<stem>/auto/<stem>_middle.json``. ``raises`` simulates a
    ``do_parse`` blow-up; ``write=False`` simulates a run that produces no
    middle.json at all. ``markdown`` / ``images`` simulate the ``f_dump_md``
    render — the ``<stem>.md`` and sibling ``images/`` dir the extractor reads
    back as auxiliary outputs (``docs/mineru-primary-promotion.md`` §5).
    """

    def _do_parse(
        output_dir: str,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
        **kwargs: Any,
    ) -> None:
        if raises is not None:
            raise raises
        if not write or middle_json is None:
            return
        stem = pdf_file_names[0]
        out = Path(output_dir) / stem / 'auto'
        out.mkdir(parents=True, exist_ok=True)
        (out / f'{stem}_middle.json').write_text(json.dumps(middle_json), encoding='utf-8')
        if markdown is not None:
            (out / f'{stem}.md').write_text(markdown, encoding='utf-8')
        if images:
            images_dir = out / 'images'
            images_dir.mkdir(parents=True, exist_ok=True)
            for name, data in images.items():
                (images_dir / name).write_bytes(data)

    return _do_parse


@pytest.fixture
def patch_mineru(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Install a fake ``do_parse`` via ``_load_mineru`` and return the installer."""

    def _install(do_parse: Callable[..., None]) -> None:
        monkeypatch.setattr(mineru_mod, '_load_mineru', lambda *, doi: do_parse)

    return _install


def _capturing_do_parse(
    middle_json: dict[str, Any], captured: dict[str, Any]
) -> Callable[..., None]:
    """Like :func:`_fake_do_parse`, but records every kwarg ``do_parse`` received."""
    inner = _fake_do_parse(middle_json)

    def _do_parse(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        inner(*args, **kwargs)

    return _do_parse


# Stage 1 — preflight ---------------------------------------------------------


async def test_wrong_format_raises_wrong_format_for_extractor(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store, fmt=Format.JATS_XML, write_artifact=False)
    with pytest.raises(WrongFormatForExtractorError) as excinfo:
        await extract_mineru(record, store)
    assert excinfo.value.context['expected'] == Format.PDF.value
    assert excinfo.value.context['extractor'] == 'mineru'


async def test_missing_artifact_raises_missing_artifact_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store, write_artifact=False)
    with pytest.raises(MissingArtifactError) as excinfo:
        await extract_mineru(record, store)
    assert excinfo.value.context['sha256'] == record.sha256


# Stage 2 — import ------------------------------------------------------------


async def test_missing_mineru_extra_raises_mineru_import_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_acquisition_record(store=store)
    # Force ``from mineru.cli.common import do_parse`` to fail.
    monkeypatch.setitem(sys.modules, 'mineru', None)
    with pytest.raises(MineruImportError) as excinfo:
        await extract_mineru(record, store)
    assert excinfo.value.context['extractor'] == 'mineru'
    assert 'uv sync --extra mineru' in str(excinfo.value.context['hint'])


# Stage 3 — convert -----------------------------------------------------------


async def test_do_parse_raising_becomes_mineru_conversion_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(None, raises=RuntimeError('weights missing')))
    with pytest.raises(MineruConversionError) as excinfo:
        await extract_mineru(record, store)
    assert 'weights missing' in str(excinfo.value.context['error'])


async def test_no_middle_json_written_raises_conversion_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(None, write=False))
    with pytest.raises(MineruConversionError) as excinfo:
        await extract_mineru(record, store)
    assert 'middle.json' in str(excinfo.value.context['hint'])


async def test_empty_pdf_info_raises_conversion_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_middle([])))
    with pytest.raises(MineruConversionError) as excinfo:
        await extract_mineru(record, store)
    assert 'pdf_info' in str(excinfo.value.context['hint'])


# Stage 4 — structural sanity -------------------------------------------------


async def test_zero_text_blocks_raises_empty_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    # A page with only a title + table — no prose text blocks.
    patch_mineru(_fake_do_parse(_middle([_page([_title_block('Heading'), _table_block()])])))
    with pytest.raises(EmptyDocumentError):
        await extract_mineru(record, store)


async def test_char_count_below_floor_raises_parse_degraded(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_middle([_page([_text_block('too short')])])))
    with pytest.raises(ParseDegradedError) as excinfo:
        await extract_mineru(record, store)
    assert int(excinfo.value.context['char_count']) < int(excinfo.value.context['floor_chars'])  # type: ignore[arg-type]


# Stage 6 — commit ------------------------------------------------------------


async def test_happy_path_commits_document_and_meta(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_rich_middle()))

    result = await extract_mineru(record, store)

    assert result.extractor is Extractor.MINERU
    assert result.extractor_version.startswith('mineru ')
    assert result.n_text_blocks == 1
    assert result.n_section_headers == 1
    assert result.n_tables == 1
    assert result.n_pages == 1

    doc_dir = store.document_dir(record.sha256)
    document = json.loads((doc_dir / 'document.json').read_text())
    # document.json is the verbatim middle.json (carries MinerU's own keys).
    assert document['_backend'] == 'pipeline'
    assert 'pdf_info' in document

    meta = json.loads((doc_dir / 'meta.json').read_text())
    assert meta['backend_id'] == MINERU
    assert meta['extractor'] == Extractor.MINERU.value
    # Default engine is the promoted vlm-engine (docs/mineru-primary-promotion.md §1).
    assert meta['pipeline']['engine'] == 'vlm-engine'
    assert meta['pipeline']['parse_method'] == 'auto'

    # Scratch dir cleaned up.
    assert list(store.tmp_dir.glob('mineru-*')) == []


async def test_aux_markdown_and_images_committed(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    """``f_dump_md`` render lands as ``document.md`` + ``images/``, recorded in meta."""
    record = make_acquisition_record(store=store)
    markdown = '# Methods\n\n![](images/fig1.jpg)\n'
    patch_mineru(
        _fake_do_parse(
            _rich_middle(),
            markdown=markdown,
            images={'fig1.jpg': b'\xff\xd8\xff\xe0JPEGBYTES'},
        )
    )

    await extract_mineru(record, store)

    doc_dir = store.document_dir(record.sha256)
    assert (doc_dir / 'document.md').read_text(encoding='utf-8') == markdown
    assert (doc_dir / 'images' / 'fig1.jpg').read_bytes() == b'\xff\xd8\xff\xe0JPEGBYTES'
    meta = json.loads((doc_dir / 'meta.json').read_text())
    assert meta['aux_outputs'] == {'markdown': 'document.md', 'images_dir': 'images'}
    # Scratch dir (which held the render pre-commit) is still cleaned up.
    assert list(store.tmp_dir.glob('mineru-*')) == []


async def test_no_aux_outputs_when_no_markdown_emitted(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    """A parse that emits no markdown leaves no ``document.md`` and an empty descriptor."""
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_rich_middle()))  # no markdown/images

    await extract_mineru(record, store)

    doc_dir = store.document_dir(record.sha256)
    assert not (doc_dir / 'document.md').exists()
    assert not (doc_dir / 'images').exists()
    meta = json.loads((doc_dir / 'meta.json').read_text())
    assert meta['aux_outputs'] == {}


async def test_reextract_no_op_when_bytes_identical(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_rich_middle()))
    await extract_mineru(record, store)
    doc_path = store.document_dir(record.sha256) / 'document.json'
    first = doc_path.read_bytes()

    # Re-run without --reextract: identical output is a no-op, not an error.
    await extract_mineru(record, store)
    assert doc_path.read_bytes() == first


async def test_divergent_reextract_raises_integrity_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_rich_middle()))
    await extract_mineru(record, store)

    # A different parse on the same artifact without --reextract must fail loud.
    body = 'Different but still long enough prose to clear the density floor. ' * 12
    patch_mineru(_fake_do_parse(_middle([_page([_text_block(body)])])))
    with pytest.raises(ExtractIntegrityError):
        await extract_mineru(record, store)


async def test_divergent_reextract_overwrites_with_flag(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_rich_middle()))
    await extract_mineru(record, store)
    doc_path = store.document_dir(record.sha256) / 'document.json'
    first = doc_path.read_bytes()

    body = 'Different but still long enough prose to clear the density floor. ' * 12
    patch_mineru(_fake_do_parse(_middle([_page([_text_block(body)])])))
    await extract_mineru(record, store, reextract=True)
    assert doc_path.read_bytes() != first


# engine / effort --------------------------------------------------------------


async def test_default_engine_recorded_in_meta_with_no_effort(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    """Default call: engine='vlm-engine' recorded, effort recorded as None (unused).

    ``vlm-engine`` is the promoted default; effort is ``None`` because MinerU
    only consults it on ``hybrid-engine``.
    """
    record = make_acquisition_record(store=store)
    patch_mineru(_fake_do_parse(_rich_middle()))

    await extract_mineru(record, store)

    meta = json.loads((store.document_dir(record.sha256) / 'meta.json').read_text())
    assert meta['pipeline']['engine'] == 'vlm-engine'
    assert meta['pipeline']['effort'] is None


async def test_custom_engine_forwarded_to_do_parse_backend_kwarg(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    """``engine='vlm-engine'`` reaches ``do_parse`` as ``backend='vlm-engine'``."""
    record = make_acquisition_record(store=store)
    captured: dict[str, Any] = {}
    patch_mineru(_capturing_do_parse(_rich_middle(), captured))

    await extract_mineru(record, store, engine='vlm-engine')

    assert captured['backend'] == 'vlm-engine'


async def test_hybrid_engine_effort_forwarded_and_recorded(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    """``engine='hybrid-engine', effort='high'`` reaches ``do_parse`` and meta.json."""
    record = make_acquisition_record(store=store)
    captured: dict[str, Any] = {}
    patch_mineru(_capturing_do_parse(_rich_middle(), captured))

    await extract_mineru(record, store, engine='hybrid-engine', effort='high')

    assert captured['backend'] == 'hybrid-engine'
    assert captured['effort'] == 'high'
    meta = json.loads((store.document_dir(record.sha256) / 'meta.json').read_text())
    assert meta['pipeline']['engine'] == 'hybrid-engine'
    assert meta['pipeline']['effort'] == 'high'


async def test_pipeline_engine_effort_forwarded_but_recorded_as_none(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    patch_mineru: Callable[..., None],
) -> None:
    """MinerU ignores ``effort`` outside hybrid-engine; meta.json says so honestly.

    ``effort`` is still forwarded to ``do_parse`` unconditionally (MinerU's own
    CLI does the same and lets the non-hybrid branches ignore it), but the
    config view only records it when it was actually consulted.
    """
    record = make_acquisition_record(store=store)
    captured: dict[str, Any] = {}
    patch_mineru(_capturing_do_parse(_rich_middle(), captured))

    await extract_mineru(record, store, engine='pipeline', effort='high')

    assert captured['effort'] == 'high'
    meta = json.loads((store.document_dir(record.sha256) / 'meta.json').read_text())
    assert meta['pipeline']['engine'] == 'pipeline'
    assert meta['pipeline']['effort'] is None


async def test_unknown_engine_raises_backend_not_applicable(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    with pytest.raises(BackendNotApplicableError) as excinfo:
        await extract_mineru(record, store, engine='no-such-engine')
    assert 'no-such-engine' in str(excinfo.value.context['hint'])


async def test_unknown_effort_raises_backend_not_applicable(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store)
    with pytest.raises(BackendNotApplicableError) as excinfo:
        await extract_mineru(record, store, effort='no-such-effort')
    assert 'no-such-effort' in str(excinfo.value.context['hint'])


# ---------------------------------------------------------------------------
# tqdm silencing
# ---------------------------------------------------------------------------
#
# MinerU and its VLM client draw tqdm bars, and the worst pass ``disable=``
# explicitly (``mineru_vl_utils`` — ``disable=not use_tqdm`` with use_tqdm
# defaulting True), so the ``TQDM_DISABLE`` env var cannot reach them. The
# backend closes the gap by patching the tqdm class. These tests restore the
# class ``__init__`` afterwards so the global patch does not leak.


def test_silence_mineru_tqdm_disables_explicit_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """The patch overrides an explicit ``disable=False`` (the leaking pattern)."""
    pytest.importorskip('tqdm')
    import io

    from tqdm import std as tqdm_std

    monkeypatch.setattr(mineru_mod, '_tqdm_patched', False)
    monkeypatch.setenv('TQDM_DISABLE', '1')
    original_init = tqdm_std.tqdm.__init__
    try:
        mineru_mod._silence_mineru_tqdm()
        from tqdm import tqdm

        cap = io.StringIO()
        with tqdm(total=3, desc='Predict', disable=False, file=cap) as pbar:
            for _ in range(3):
                pbar.update(1)
        assert cap.getvalue().strip() == ''
    finally:
        tqdm_std.tqdm.__init__ = original_init


def test_silence_mineru_tqdm_noop_when_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``TQDM_DISABLE`` cleared (``-v``), the tqdm class is left untouched."""
    pytest.importorskip('tqdm')
    from tqdm import std as tqdm_std

    monkeypatch.setattr(mineru_mod, '_tqdm_patched', False)
    monkeypatch.delenv('TQDM_DISABLE', raising=False)
    original_init = tqdm_std.tqdm.__init__
    mineru_mod._silence_mineru_tqdm()
    assert tqdm_std.tqdm.__init__ is original_init
