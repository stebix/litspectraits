"""Tests for :mod:`litspectraits.extract.elsevier` (step 10d).

Same fixture discipline as ``tests/extract/test_jats.py``: inline byte
literals, hermetic, explicit about which Elsevier shape they exercise.
Real ScienceDirect samples belong in the gated end-to-end smoke tests
(§22), not here.
"""

import json
from collections.abc import Callable
from typing import Final

import pytest

import litspectraits.extract.elsevier as elsevier_mod
from litspectraits.errors import (
    EmptyDocumentError,
    ExtractIntegrityError,
    MalformedDocumentError,
    MissingArtifactError,
    SerializationError,
    WrongFormatForExtractorError,
)
from litspectraits.extract.elsevier import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
    extract_elsevier,
)
from litspectraits.manifest import (
    AcquisitionRecord,
    Extractor,
    Format,
    Publisher,
)
from litspectraits.store import ArtifactStore

# A complete-but-minimal Elsevier CEP envelope: ``<full-text-retrieval-
# response>`` wrapping ``<originalText>/<xocs:doc>``, three sections (one
# nested), one paragraph with an inline ``<ce:cross-ref>``, one CALS
# ``<ce:table>``, one ``<ce:figure>``, one ``<ce:bib-reference>``. Sized so
# the structural counts in the happy-path test are easy to read.
_RICH_ELSEVIER: Final[bytes] = b"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response xmlns="http://www.elsevier.com/xml/svapi/article/dtd"
                              xmlns:dc="http://purl.org/dc/elements/1.1/"
                              xmlns:ce="http://www.elsevier.com/xml/common/dtd"
                              xmlns:sb="http://www.elsevier.com/xml/common/struct-bib/dtd"
                              xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
  <coredata>
    <dc:title>Quantitative T2 mapping at 3 T</dc:title>
    <dc:description>We measured T2 in white matter.</dc:description>
  </coredata>
  <originalText>
    <xocs:doc>
      <xocs:serial-item>
        <ce:sections>
          <ce:section id="s1">
            <ce:section-title>Introduction</ce:section-title>
            <ce:para>T2 mapping is foundational
              <ce:cross-ref refid="b1">[1]</ce:cross-ref>.</ce:para>
            <ce:section id="s1-1">
              <ce:section-title>Background</ce:section-title>
              <ce:para>Prior work spans clinical fields.</ce:para>
            </ce:section>
          </ce:section>
          <ce:section id="s2">
            <ce:section-title>Methods</ce:section-title>
            <ce:para>We acquired multi-echo spin-echo data.</ce:para>
            <ce:table id="t1">
              <ce:label>Table 1</ce:label>
              <ce:caption><ce:simple-para>Acquisition parameters.</ce:simple-para></ce:caption>
              <tgroup cols="2">
                <thead>
                  <row><entry>Parameter</entry><entry>Value</entry></row>
                </thead>
                <tbody>
                  <row><entry>TE</entry><entry>10 ms</entry></row>
                </tbody>
              </tgroup>
            </ce:table>
            <ce:figure id="f1">
              <ce:label>Figure 1</ce:label>
              <ce:caption><ce:simple-para>T2 decay curves.</ce:simple-para></ce:caption>
            </ce:figure>
          </ce:section>
        </ce:sections>
        <ce:bibliography>
          <ce:bibliography-sec>
            <ce:bib-reference id="b1">
              <sb:reference>
                <sb:contribution>
                  <sb:authors>
                    <sb:author>
                      <ce:surname>Carr</ce:surname>
                      <ce:given-name>H Y</ce:given-name>
                    </sb:author>
                  </sb:authors>
                  <sb:title>
                    <sb:maintitle>Effects of diffusion on free precession</sb:maintitle>
                  </sb:title>
                </sb:contribution>
                <sb:host>
                  <sb:issue>
                    <sb:series>
                      <sb:title><sb:maintitle>Physical Review</sb:maintitle></sb:title>
                    </sb:series>
                    <sb:date>1954</sb:date>
                  </sb:issue>
                </sb:host>
              </sb:reference>
              <ce:source-text>Carr H Y, Purcell E M. Phys Rev. 1954;94:630.</ce:source-text>
              <ce:doi>10.1103/PhysRev.94.630</ce:doi>
            </ce:bib-reference>
          </ce:bibliography-sec>
        </ce:bibliography>
      </xocs:serial-item>
    </xocs:doc>
  </originalText>
</full-text-retrieval-response>
"""

# Same shape as the retriever-side fixture: a META_ABS abstract-only
# envelope that lacks ``<originalText>``. The retriever raises
# :class:`EntitlementDowngradeError` on this; the extractor must raise
# :class:`MalformedDocumentError` as defence-in-depth (the artifact passed
# sniff at ingest but has no full-text body).
_META_ABS_BODY: Final[bytes] = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<full-text-retrieval-response '
    b'xmlns="http://www.elsevier.com/xml/svapi/article/dtd">\n'
    b'  <coredata>\n'
    b'    <dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Demo</dc:title>\n'
    b'    <dc:description xmlns:dc="http://purl.org/dc/elements/1.1/">'
    b'A short abstract.</dc:description>\n'
    b'  </coredata>\n'
    b'</full-text-retrieval-response>\n'
)

# Originaltext present (so the META_ABS guard accepts the envelope) but
# the body has no sections / paragraphs / tables / figures / references.
# Drives the walker into ``n_text_blocks == 0`` → EmptyDocumentError.
_EMPTY_FULL_TEXT_BODY: Final[bytes] = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<full-text-retrieval-response '
    b'xmlns="http://www.elsevier.com/xml/svapi/article/dtd">\n'
    b'  <originalText>\n'
    b'    <doc xmlns="http://www.elsevier.com/xml/xocs/dtd"/>\n'
    b'  </originalText>\n'
    b'</full-text-retrieval-response>\n'
)

_BROKEN_XML: Final[bytes] = (
    b'<?xml version="1.0"?><full-text-retrieval-response><coredata><dc:title>unclosed'
)

_HTML_ROOT: Final[bytes] = b'<?xml version="1.0"?><html><body><p>not Elsevier</p></body></html>'


# Helpers ---------------------------------------------------------------------


def _elsevier_record(
    *,
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    body: bytes = _RICH_ELSEVIER,
    write_artifact: bool = True,
) -> AcquisitionRecord:
    return make_acquisition_record(
        store=store,
        body=body,
        fmt=Format.ELSEVIER_XML,
        publisher=Publisher.ELSEVIER,
        sha256='c' * 64,
        write_artifact=write_artifact,
    )


# Stage 1 — preflight ---------------------------------------------------------


async def test_wrong_format_raises_wrong_format_for_extractor(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store, fmt=Format.PDF, write_artifact=False)
    with pytest.raises(WrongFormatForExtractorError) as excinfo:
        await extract_elsevier(record, store)
    assert excinfo.value.context['expected'] == Format.ELSEVIER_XML.value
    assert excinfo.value.context['actual'] == Format.PDF.value


async def test_missing_artifact_raises_missing_artifact_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _elsevier_record(
        store=store, make_acquisition_record=make_acquisition_record, write_artifact=False
    )
    with pytest.raises(MissingArtifactError) as excinfo:
        await extract_elsevier(record, store)
    assert 'litspectraits ingest' in str(excinfo.value.context['hint'])


# Stage 2 — parse + META_ABS guard --------------------------------------------


async def test_xml_syntax_error_raises_malformed_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _elsevier_record(
        store=store, make_acquisition_record=make_acquisition_record, body=_BROKEN_XML
    )
    with pytest.raises(MalformedDocumentError) as excinfo:
        await extract_elsevier(record, store)
    assert 'lxml could not parse' in str(excinfo.value.context['hint'])
    assert not store.document_dir(record.sha256).exists()


async def test_non_full_text_root_raises_malformed_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _elsevier_record(
        store=store, make_acquisition_record=make_acquisition_record, body=_HTML_ROOT
    )
    with pytest.raises(MalformedDocumentError) as excinfo:
        await extract_elsevier(record, store)
    assert excinfo.value.context['actual_root'] == 'html'


async def test_meta_abs_envelope_raises_malformed_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    """Defense in depth against an entitlement downgrade reaching extraction.

    The retriever raises :class:`EntitlementDowngradeError` on this shape
    upstream; pinning the same rejection at the extract layer protects
    against a stale-on-disk artifact (or a future sideload-bypass) that
    skipped the retriever's check.
    """
    record = _elsevier_record(
        store=store, make_acquisition_record=make_acquisition_record, body=_META_ABS_BODY
    )
    with pytest.raises(MalformedDocumentError) as excinfo:
        await extract_elsevier(record, store)
    assert 'META_ABS' in str(excinfo.value.context['hint'])
    assert not store.document_dir(record.sha256).exists()


# Stage 3 — walk + structural sanity ------------------------------------------


async def test_empty_full_text_raises_empty_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _elsevier_record(
        store=store,
        make_acquisition_record=make_acquisition_record,
        body=_EMPTY_FULL_TEXT_BODY,
    )
    with pytest.raises(EmptyDocumentError) as excinfo:
        await extract_elsevier(record, store)
    assert excinfo.value.context['n_text_blocks'] == 0
    assert not store.document_dir(record.sha256).exists()


# Stage 4 — serialize ---------------------------------------------------------


async def test_serialization_error_surfaces_when_payload_is_unjsonable(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A walker that produced a non-JSON-encodable value must surface cleanly."""
    record = _elsevier_record(store=store, make_acquisition_record=make_acquisition_record)

    real_walk = elsevier_mod._walk

    def _broken_walk(root):  # type: ignore[no-untyped-def]
        document, counts = real_walk(root)
        document['front']['set_field'] = {1, 2, 3}
        return document, counts

    monkeypatch.setattr(elsevier_mod, '_walk', _broken_walk)
    with pytest.raises(SerializationError) as excinfo:
        await extract_elsevier(record, store)
    assert 'json.dumps' in str(excinfo.value.context['hint'])


# Happy path ------------------------------------------------------------------


async def test_happy_path_writes_document_and_meta(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _elsevier_record(store=store, make_acquisition_record=make_acquisition_record)

    extract_record = await extract_elsevier(record, store)

    # ExtractRecord shape.
    assert extract_record.sha256 == record.sha256
    assert extract_record.extractor is Extractor.ELSEVIER
    assert extract_record.extractor_version == f'{SCHEMA_NAME}/{SCHEMA_VERSION}'
    assert extract_record.n_pages is None
    # 3 paragraphs: 1 in s1, 1 in s1-1, 1 in s2.
    assert extract_record.n_text_blocks == 3
    # 3 section <title>s: Introduction, Background, Methods.
    assert extract_record.n_section_headers == 3
    assert extract_record.n_tables == 1
    assert extract_record.n_figures == 1
    assert extract_record.char_count > 0

    # On-disk document.json: structural spot-checks.
    document = json.loads((store.document_dir(record.sha256) / 'document.json').read_text())
    assert document['schema_name'] == SCHEMA_NAME
    assert document['schema_version'] == SCHEMA_VERSION
    assert document['front']['title'] == 'Quantitative T2 mapping at 3 T'
    assert document['front']['abstract'] == 'We measured T2 in white matter.'

    # Section paths: nested s1-1 should report path == ['s1', 's1-1'] / level == 2.
    by_id = {sec['id']: sec for sec in document['sections']}
    assert by_id['s1']['path'] == ['s1']
    assert by_id['s1']['level'] == 1
    assert by_id['s1-1']['path'] == ['s1', 's1-1']
    assert by_id['s1-1']['level'] == 2
    assert by_id['s2']['title'] == 'Methods'

    # Inline xref preservation: CEP ``refid`` surfaces as ``rid`` to match
    # the JATS dict shape; ``ref_type`` is ``None`` because CEP doesn't
    # encode an analogue. ``start`` / ``end`` byte offsets index the
    # un-stripped block text — E0.5a normaliser depends on them.
    intro_blocks = by_id['s1']['blocks']
    assert len(intro_blocks[0]['xrefs']) == 1
    (xref,) = intro_blocks[0]['xrefs']
    assert xref['rid'] == 'b1'
    assert xref['ref_type'] is None
    assert xref['label'] == '[1]'
    assert intro_blocks[0]['text'][xref['start'] : xref['end']] == '[1]'

    # CALS table projection: 2 rows by 2 cols, spans default to 1.
    table = document['tables'][0]
    assert table['id'] == 't1'
    assert table['label'] == 'Table 1'
    assert table['caption'] == 'Acquisition parameters.'
    assert table['section_path'] == ['s2']
    assert table['n_rows'] == 2
    assert table['n_cols'] == 2
    assert table['cells'][0][0] == {
        'text': 'Parameter',
        'type': 'entry',
        'rowspan': 1,
        'colspan': 1,
    }
    assert table['cells'][1][1]['text'] == '10 ms'

    # Figure surfaces caption + section path; no pixel data.
    figure = document['figures'][0]
    assert figure['id'] == 'f1'
    assert figure['caption'] == 'T2 decay curves.'
    assert figure['section_path'] == ['s2']

    # References: structured fields when recoverable.
    ref = document['references'][0]
    assert ref['id'] == 'b1'
    assert ref['authors'] == ['Carr, H Y']
    assert ref['title'] == 'Effects of diffusion on free precession'
    assert ref['source'] == 'Physical Review'
    assert ref['year'] == '1954'
    assert ref['doi'] == '10.1103/PhysRev.94.630'
    # raw_text comes from <ce:source-text> when present, not the full
    # element body (which would also include all the structured fields).
    assert ref['raw_text'] == 'Carr H Y, Purcell E M. Phys Rev. 1954;94:630.'

    # meta.json mirrors the ExtractRecord stats + carries the schema version.
    meta = json.loads((store.document_dir(record.sha256) / 'meta.json').read_text())
    assert meta['extractor'] == Extractor.ELSEVIER.value
    assert meta['schema_name'] == SCHEMA_NAME
    assert meta['schema_version'] == SCHEMA_VERSION
    assert meta['source_sha256'] == record.sha256
    assert meta['n_text_blocks'] == 3
    assert meta['n_section_headers'] == 3
    assert meta['n_tables'] == 1
    assert meta['n_figures'] == 1
    assert meta['n_references'] == 1
    assert meta['format'] == Format.ELSEVIER_XML.value
    assert 'pipeline' not in meta
    assert 'n_pages' not in meta

    # No leftover .part files.
    assert list(store.tmp_dir.glob('*.part')) == []


# Stage 5 — commit ------------------------------------------------------------


async def test_idempotent_reextract_with_identical_bytes_is_a_noop(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _elsevier_record(store=store, make_acquisition_record=make_acquisition_record)
    await extract_elsevier(record, store)

    doc_path = store.document_dir(record.sha256) / 'document.json'
    meta_path = store.document_dir(record.sha256) / 'meta.json'
    doc_mtime = doc_path.stat().st_mtime_ns
    meta_mtime = meta_path.stat().st_mtime_ns
    meta_before = meta_path.read_text()

    await extract_elsevier(record, store)

    assert doc_path.stat().st_mtime_ns == doc_mtime
    assert meta_path.stat().st_mtime_ns == meta_mtime
    assert meta_path.read_text() == meta_before


async def test_reextract_with_diverging_bytes_without_flag_raises(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _elsevier_record(store=store, make_acquisition_record=make_acquisition_record)
    await extract_elsevier(record, store)

    # Force the second extraction to produce a different document by
    # patching SCHEMA_VERSION at the module level — emits divergent bytes
    # without us having to forge a different on-disk artifact.
    monkeypatch.setattr(elsevier_mod, 'SCHEMA_VERSION', '1-test-bumped')
    with pytest.raises(ExtractIntegrityError) as excinfo:
        await extract_elsevier(record, store)
    assert (
        excinfo.value.context['existing_document_sha256']
        != excinfo.value.context['incoming_document_sha256']
    )


async def test_reextract_flag_overwrites_diverging_bytes(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _elsevier_record(store=store, make_acquisition_record=make_acquisition_record)
    await extract_elsevier(record, store)

    monkeypatch.setattr(elsevier_mod, 'SCHEMA_VERSION', '1-test-bumped')
    extract_record = await extract_elsevier(record, store, reextract=True)
    assert extract_record.extractor_version.endswith('1-test-bumped')

    document = json.loads((store.document_dir(record.sha256) / 'document.json').read_text())
    assert document['schema_version'] == '1-test-bumped'
