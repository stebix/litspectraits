"""Tests for :mod:`litspectraits.extract.jats` (step 10c).

The fixtures are inline byte literals in the spirit of
``tests/test_sniff.py``: tiny, hermetic, and explicit about which JATS
shape they exercise. Real Springer Nature samples belong in the gated
end-to-end smoke tests (§22), not here.
"""

import json
from collections.abc import Callable
from typing import Final

import pytest

import litspectraits.extract.jats as jats_mod
from litspectraits.errors import (
    EmptyDocumentError,
    ExtractIntegrityError,
    MalformedDocumentError,
    MissingArtifactError,
    SerializationError,
    WrongFormatForExtractorError,
)
from litspectraits.extract.jats import SCHEMA_NAME, SCHEMA_VERSION, extract_jats
from litspectraits.manifest import (
    AcquisitionRecord,
    Extractor,
    Format,
    Publisher,
)
from litspectraits.store import ArtifactStore

# A complete-but-minimal JATS fixture: one nested section pair, one
# paragraph with an inline citation, one 2x2 table with caption, one
# figure with caption, one reference. Sized so the structural counts in
# the happy-path test are easy to read.
_RICH_JATS: Final[bytes] = b"""<?xml version="1.0" encoding="UTF-8"?>
<article article-type="research-article">
  <front>
    <article-meta>
      <title-group>
        <article-title>Quantitative T1 mapping at 7 T</article-title>
      </title-group>
      <abstract>
        <p>We measured T1 in white matter and grey matter.</p>
        <p>Results agree with prior literature within 5%.</p>
      </abstract>
    </article-meta>
  </front>
  <body>
    <sec id="s1">
      <title>Introduction</title>
      <p>Quantitative T1 measurements anchor MR fingerprinting
        <xref ref-type="bibr" rid="R1">[1]</xref>.</p>
      <sec id="s1-1">
        <title>Background</title>
        <p>Prior work spans 1.5 T to 7 T.</p>
      </sec>
    </sec>
    <sec id="s2">
      <title>Methods</title>
      <p>We acquired single-slice IR-EPI data.</p>
      <p>Reconstruction used a 256x256 matrix.</p>
      <table-wrap id="t1">
        <label>Table 1</label>
        <caption><p>Acquisition parameters.</p></caption>
        <table>
          <thead>
            <tr><th>Parameter</th><th>Value</th></tr>
          </thead>
          <tbody>
            <tr><td>TR</td><td>5000 ms</td></tr>
          </tbody>
        </table>
      </table-wrap>
      <fig id="f1">
        <label>Figure 1</label>
        <caption><p>Inversion recovery curves for white matter.</p></caption>
        <graphic xlink:href="fig1.png" xmlns:xlink="http://www.w3.org/1999/xlink"/>
      </fig>
    </sec>
  </body>
  <back>
    <ref-list>
      <ref id="R1">
        <element-citation publication-type="journal">
          <person-group person-group-type="author">
            <name><surname>Ma</surname><given-names>D</given-names></name>
            <name><surname>Gulani</surname><given-names>V</given-names></name>
          </person-group>
          <article-title>Magnetic resonance fingerprinting</article-title>
          <source>Nature</source>
          <year>2013</year>
          <pub-id pub-id-type="doi">10.1038/nature11971</pub-id>
        </element-citation>
      </ref>
    </ref-list>
  </back>
</article>
"""

_BARE_JATS: Final[bytes] = b"""<?xml version="1.0" encoding="UTF-8"?>
<article>
  <body>
    <sec id="s1">
      <title>Solo</title>
      <p>One paragraph is enough to clear EmptyDocumentError.</p>
    </sec>
  </body>
</article>
"""

_NAMESPACED_JATS: Final[bytes] = b"""<?xml version="1.0" encoding="UTF-8"?>
<jats:article xmlns:jats="https://jats.nlm.nih.gov/archiving/1.3/">
  <jats:body>
    <jats:sec id="s1">
      <jats:title>Namespaced</jats:title>
      <jats:p>Content under a JATS namespace prefix.</jats:p>
    </jats:sec>
  </jats:body>
</jats:article>
"""

_BODYLESS_JATS: Final[bytes] = b"""<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front>
    <article-meta>
      <title-group><article-title>Just front matter</article-title></title-group>
    </article-meta>
  </front>
</article>
"""

_FLOATING_PARA_JATS: Final[bytes] = b"""<?xml version="1.0" encoding="UTF-8"?>
<article>
  <body>
    <p>A floating paragraph with no enclosing section.</p>
  </body>
</article>
"""

_BROKEN_XML: Final[bytes] = b'<?xml version="1.0"?><article><body><sec><p>unclosed'

_HTML_ROOT: Final[bytes] = b'<?xml version="1.0"?><html><body><p>not JATS</p></body></html>'


# Helpers ---------------------------------------------------------------------


def _jats_record(
    *,
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    body: bytes = _RICH_JATS,
    write_artifact: bool = True,
) -> AcquisitionRecord:
    return make_acquisition_record(
        store=store,
        body=body,
        fmt=Format.JATS_XML,
        publisher=Publisher.SPRINGER_NATURE,
        sha256='b' * 64,
        write_artifact=write_artifact,
    )


# Stage 1 — preflight ---------------------------------------------------------


async def test_wrong_format_raises_wrong_format_for_extractor(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = make_acquisition_record(store=store, fmt=Format.PDF, write_artifact=False)
    with pytest.raises(WrongFormatForExtractorError) as excinfo:
        await extract_jats(record, store)
    assert excinfo.value.context['expected'] == Format.JATS_XML.value
    assert excinfo.value.context['actual'] == Format.PDF.value


async def test_missing_artifact_raises_missing_artifact_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _jats_record(
        store=store, make_acquisition_record=make_acquisition_record, write_artifact=False
    )
    with pytest.raises(MissingArtifactError) as excinfo:
        await extract_jats(record, store)
    assert 'litspectraits ingest' in str(excinfo.value.context['hint'])


# Stage 2 — parse -------------------------------------------------------------


async def test_xml_syntax_error_raises_malformed_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _jats_record(
        store=store, make_acquisition_record=make_acquisition_record, body=_BROKEN_XML
    )
    with pytest.raises(MalformedDocumentError) as excinfo:
        await extract_jats(record, store)
    assert 'lxml could not parse' in str(excinfo.value.context['hint'])
    assert not store.document_dir(record.sha256).exists()


async def test_non_article_root_raises_malformed_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _jats_record(
        store=store, make_acquisition_record=make_acquisition_record, body=_HTML_ROOT
    )
    with pytest.raises(MalformedDocumentError) as excinfo:
        await extract_jats(record, store)
    assert excinfo.value.context['actual_root'] == 'html'


# Stage 3 — walk + structural sanity ------------------------------------------


async def test_bodyless_jats_raises_empty_document_error(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _jats_record(
        store=store, make_acquisition_record=make_acquisition_record, body=_BODYLESS_JATS
    )
    with pytest.raises(EmptyDocumentError) as excinfo:
        await extract_jats(record, store)
    assert excinfo.value.context['n_text_blocks'] == 0
    assert not store.document_dir(record.sha256).exists()


async def test_namespaced_jats_parses_via_local_name(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _jats_record(
        store=store, make_acquisition_record=make_acquisition_record, body=_NAMESPACED_JATS
    )
    extract_record = await extract_jats(record, store)
    assert extract_record.n_text_blocks == 1
    assert extract_record.n_section_headers == 1


async def test_floating_paragraph_outside_section_surfaces_as_synthetic_section(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    """JATS bodies sometimes carry top-level <p> outside any <sec>.

    The walker wraps these in a synthetic section so the dict shape stays
    uniform (one bucket of blocks ↔ one section entry) — pin that
    behavior so a future "drop floating paragraphs" refactor surfaces.
    """
    record = _jats_record(
        store=store,
        make_acquisition_record=make_acquisition_record,
        body=_FLOATING_PARA_JATS,
    )
    extract_record = await extract_jats(record, store)
    assert extract_record.n_text_blocks == 1
    assert extract_record.n_section_headers == 0  # no <title> means no header counted

    document = json.loads((store.document_dir(record.sha256) / 'document.json').read_text())
    assert len(document['sections']) == 1
    synthetic = document['sections'][0]
    assert synthetic['id'] is None
    assert synthetic['title'] is None
    assert synthetic['blocks'][0]['text'].strip().startswith('A floating paragraph')


# Stage 4 — serialize ---------------------------------------------------------


async def test_serialization_error_surfaces_when_payload_is_unjsonable(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A walker that produced a non-JSON-encodable value must surface cleanly."""
    record = _jats_record(store=store, make_acquisition_record=make_acquisition_record)

    real_walk = jats_mod._walk

    def _broken_walk(root):  # type: ignore[no-untyped-def]
        document, counts = real_walk(root)
        document['front']['set_field'] = {1, 2, 3}
        return document, counts

    monkeypatch.setattr(jats_mod, '_walk', _broken_walk)
    with pytest.raises(SerializationError) as excinfo:
        await extract_jats(record, store)
    assert 'json.dumps' in str(excinfo.value.context['hint'])


# Happy path ------------------------------------------------------------------


async def test_happy_path_writes_document_and_meta(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _jats_record(store=store, make_acquisition_record=make_acquisition_record)

    extract_record = await extract_jats(record, store)

    # ExtractRecord shape.
    assert extract_record.sha256 == record.sha256
    assert extract_record.extractor is Extractor.JATS
    assert extract_record.extractor_version == f'{SCHEMA_NAME}/{SCHEMA_VERSION}'
    assert extract_record.n_pages is None
    # 4 paragraphs in body sections (s1 + s1-1 + 2 in s2) — abstract <p>s
    # are tallied as char_count contributions but not as text blocks.
    assert extract_record.n_text_blocks == 4
    # 3 section <title>s (Introduction, Background, Methods).
    assert extract_record.n_section_headers == 3
    assert extract_record.n_tables == 1
    assert extract_record.n_figures == 1
    assert extract_record.char_count > 0

    # On-disk document.json: structural spot-checks.
    document = json.loads((store.document_dir(record.sha256) / 'document.json').read_text())
    assert document['schema_name'] == SCHEMA_NAME
    assert document['schema_version'] == SCHEMA_VERSION
    assert document['front']['title'] == 'Quantitative T1 mapping at 7 T'
    assert 'white matter' in document['front']['abstract']

    # Section paths: nested s1-1 should report path == ['s1', 's1-1'] / level == 2.
    by_id = {sec['id']: sec for sec in document['sections']}
    assert by_id['s1']['path'] == ['s1']
    assert by_id['s1']['level'] == 1
    assert by_id['s1-1']['path'] == ['s1', 's1-1']
    assert by_id['s1-1']['level'] == 2
    assert by_id['s2']['title'] == 'Methods'

    # Inline xref preservation, including the byte offsets the normaliser
    # depends on (E0.5a). The label is the stripped surface form; the
    # offsets index the un-stripped block text so ``text[start:end]``
    # round-trips byte-for-byte through the verbatim-anchor gate.
    intro_blocks = by_id['s1']['blocks']
    assert len(intro_blocks[0]['xrefs']) == 1
    (xref,) = intro_blocks[0]['xrefs']
    assert xref['rid'] == 'R1'
    assert xref['ref_type'] == 'bibr'
    assert xref['label'] == '[1]'
    assert intro_blocks[0]['text'][xref['start'] : xref['end']] == '[1]'

    # Table cell projection.
    table = document['tables'][0]
    assert table['id'] == 't1'
    assert table['label'] == 'Table 1'
    assert table['caption'] == 'Acquisition parameters.'
    assert table['section_path'] == ['s2']
    assert table['n_rows'] == 2
    assert table['n_cols'] == 2
    assert table['cells'][0][0] == {'text': 'Parameter', 'type': 'th', 'rowspan': 1, 'colspan': 1}
    assert table['cells'][1][1]['text'] == '5000 ms'

    # Figure surfaces caption + section path; no pixel data.
    figure = document['figures'][0]
    assert figure['id'] == 'f1'
    assert figure['caption'] == 'Inversion recovery curves for white matter.'
    assert figure['section_path'] == ['s2']

    # References: structured fields when recoverable.
    ref = document['references'][0]
    assert ref['id'] == 'R1'
    assert ref['authors'] == ['Ma, D', 'Gulani, V']
    assert ref['title'] == 'Magnetic resonance fingerprinting'
    assert ref['source'] == 'Nature'
    assert ref['year'] == '2013'
    assert ref['doi'] == '10.1038/nature11971'

    # meta.json mirrors the ExtractRecord stats + carries the schema version.
    meta = json.loads((store.document_dir(record.sha256) / 'meta.json').read_text())
    assert meta['extractor'] == Extractor.JATS.value
    assert meta['schema_name'] == SCHEMA_NAME
    assert meta['schema_version'] == SCHEMA_VERSION
    assert meta['source_sha256'] == record.sha256
    assert meta['n_text_blocks'] == 4
    assert meta['n_section_headers'] == 3
    assert meta['n_tables'] == 1
    assert meta['n_figures'] == 1
    assert meta['n_references'] == 1
    assert meta['format'] == Format.JATS_XML.value
    assert 'pipeline' not in meta
    assert 'n_pages' not in meta

    # No leftover .part files.
    assert list(store.tmp_dir.glob('*.part')) == []


# Stage 5 — commit ------------------------------------------------------------


async def test_idempotent_reextract_with_identical_bytes_is_a_noop(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
) -> None:
    record = _jats_record(store=store, make_acquisition_record=make_acquisition_record)
    await extract_jats(record, store)

    doc_path = store.document_dir(record.sha256) / 'document.json'
    meta_path = store.document_dir(record.sha256) / 'meta.json'
    doc_mtime = doc_path.stat().st_mtime_ns
    meta_mtime = meta_path.stat().st_mtime_ns
    meta_before = meta_path.read_text()

    await extract_jats(record, store)

    assert doc_path.stat().st_mtime_ns == doc_mtime
    assert meta_path.stat().st_mtime_ns == meta_mtime
    assert meta_path.read_text() == meta_before


async def test_reextract_with_diverging_bytes_without_flag_raises(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _jats_record(store=store, make_acquisition_record=make_acquisition_record)
    await extract_jats(record, store)

    # Force the second extraction to produce a different document by
    # patching SCHEMA_VERSION at the module level — emits divergent bytes
    # without us having to forge a different on-disk artifact.
    monkeypatch.setattr(jats_mod, 'SCHEMA_VERSION', '1-test-bumped')
    with pytest.raises(ExtractIntegrityError) as excinfo:
        await extract_jats(record, store)
    assert (
        excinfo.value.context['existing_document_sha256']
        != excinfo.value.context['incoming_document_sha256']
    )


async def test_reextract_flag_overwrites_diverging_bytes(
    store: ArtifactStore,
    make_acquisition_record: Callable[..., AcquisitionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _jats_record(store=store, make_acquisition_record=make_acquisition_record)
    await extract_jats(record, store)

    monkeypatch.setattr(jats_mod, 'SCHEMA_VERSION', '1-test-bumped')
    extract_record = await extract_jats(record, store, reextract=True)
    assert extract_record.extractor_version.endswith('1-test-bumped')

    document = json.loads((store.document_dir(record.sha256) / 'document.json').read_text())
    assert document['schema_version'] == '1-test-bumped'
