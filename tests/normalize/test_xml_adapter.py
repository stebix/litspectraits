"""Unit + integration coverage for the XML → Document adapter (E0.5a).

Two layers:

- **Unit tests** drive hand-built publisher dicts through
  :func:`litspectraits.normalize.normalize_xml_document` and assert on
  the resulting :class:`Document` structure. They cover the route
  parameterisation, offset propagation onto ``InlineRef.char_range``,
  ``surface_form == text[start:end]``, cell-kind normalisation, and
  ``Completeness`` derivation.
- **Integration smoke** runs the real extractor on the in-repo
  fixtures (the same ``_RICH_JATS`` / ``_RICH_ELSEVIER`` blobs the
  extractor tests use), then feeds the resulting dict to the adapter.
  Validates the contract end-to-end without touching the network or
  any real publisher artifacts.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import litspectraits.extract.elsevier as elsevier_mod
import litspectraits.extract.jats as jats_mod
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    Publisher,
)
from litspectraits.normalize import (
    Document,
    EquationBlock,
    FigureBlock,
    TableBlock,
    TextBlock,
    XmlRoute,
    converter,
    normalize_xml_document,
)
from litspectraits.store import ArtifactStore

# Re-use the fixture bytes from the extractor test modules — they are
# the canonical "structurally rich" sample for each XML route, sized so
# the structural counts in the happy-path test are easy to read.
from tests.extract.test_elsevier import _RICH_ELSEVIER
from tests.extract.test_jats import _RICH_JATS

# ---------------------------------------------------------------------------
# Hand-built unit fixtures
# ---------------------------------------------------------------------------


def _publisher_dict_minimal(*, route: XmlRoute) -> dict[str, Any]:
    """Minimal valid publisher dict — exercise the offset propagation
    path with deterministic offsets the test can pin exactly."""
    return {
        'front': {'title': 'Sample paper', 'abstract': 'Short abstract.'},
        'sections': [
            {
                'id': 's1',
                'title': 'Intro',
                'level': 1,
                'path': ['s1'],
                'blocks': [
                    {
                        'type': 'paragraph',
                        'text': 'See [1] for details.',
                        'xrefs': [
                            {
                                'rid': 'R1',
                                'ref_type': 'bibr' if route == 'jats' else None,
                                'label': '[1]',
                                'start': 4,
                                'end': 7,
                            }
                        ],
                    }
                ],
            }
        ],
        'tables': [
            {
                'id': 'T1',
                'label': 'Table 1',
                'caption': 'Sample table.',
                'section_path': ['s1'],
                'n_rows': 2,
                'n_cols': 2,
                'cells': [
                    [
                        {'text': 'A', 'type': 'th', 'rowspan': 1, 'colspan': 1},
                        {'text': 'B', 'type': 'th', 'rowspan': 1, 'colspan': 1},
                    ],
                    [
                        {'text': 'one', 'type': 'td', 'rowspan': 1, 'colspan': 1},
                        {'text': 'two', 'type': 'td', 'rowspan': 1, 'colspan': 1},
                    ],
                ],
            }
        ],
        'figures': [
            {
                'id': 'F1',
                'label': 'Figure 1',
                'caption': 'Sample figure.',
                'section_path': ['s1'],
            }
        ],
        'references': [
            {
                'id': 'R1',
                'raw_text': 'Smith J. 2019. A study. JMRI.',
                'authors': ['Smith, J.'],
                'title': 'A study',
                'source': 'JMRI',
                'year': '2019',
                'doi': '10.1002/abc.123',
            }
        ],
    }


# ---------------------------------------------------------------------------
# Unit tests — hand-built dicts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_route_propagates_to_provenance(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    assert doc.route == route
    assert all(block.provenance.route == route for block in doc.blocks)


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_block_kinds_count(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    kinds = [b.type for b in doc.blocks]
    # One paragraph, one table, one figure — grouped, in that order.
    assert kinds == ['text', 'table', 'figure']


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_inline_ref_char_range_indexes_block_text(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    text_block = next(b for b in doc.blocks if isinstance(b, TextBlock))
    assert len(text_block.inline_refs) == 1
    ref = text_block.inline_refs[0]
    # The verbatim-anchor gate's load-bearing property: surface_form is
    # exactly what the block text holds at the claimed offset range.
    assert text_block.text[ref.char_range.start : ref.char_range.end] == ref.surface_form
    assert ref.surface_form == '[1]'


@pytest.mark.parametrize(
    ('route', 'expected_source'),
    [('jats', 'jats'), ('elsevier', 'elsevier')],
)
def test_inline_ref_id_source_matches_route(route: XmlRoute, expected_source: str) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    text_block = next(b for b in doc.blocks if isinstance(b, TextBlock))
    ref = text_block.inline_refs[0]
    assert ref.ref_id == 'R1'
    assert ref.ref_id_source == expected_source


def test_inline_ref_with_no_rid_has_none_source() -> None:
    raw = _publisher_dict_minimal(route='elsevier')
    raw['sections'][0]['blocks'][0]['xrefs'][0]['rid'] = None
    doc = normalize_xml_document(raw, route='elsevier')
    text_block = next(b for b in doc.blocks if isinstance(b, TextBlock))
    ref = text_block.inline_refs[0]
    assert ref.ref_id is None
    assert ref.ref_id_source is None


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_table_block_carries_cells_and_kinds(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    assert table.id == 'T1'
    assert table.n_rows == 2 and table.n_cols == 2
    # Header row is normalised to ``kind='th'``; body row to ``'td'``.
    assert [c.kind for c in table.cells[0]] == ['th', 'th']
    assert [c.kind for c in table.cells[1]] == ['td', 'td']
    assert table.cells[1][0].text == 'one'


def test_cals_entry_cell_normalises_to_td() -> None:
    # Elsevier CALS tables emit ``type='entry'`` for every cell. The
    # adapter must map that to a non-header ``'td'`` rather than
    # mis-tagging as header.
    raw = _publisher_dict_minimal(route='elsevier')
    for row in raw['tables'][0]['cells']:
        for cell in row:
            cell['type'] = 'entry'
    doc = normalize_xml_document(raw, route='elsevier')
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    assert all(c.kind == 'td' for row in table.cells for c in row)


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_figure_block_caption_and_path(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    figure = next(b for b in doc.blocks if isinstance(b, FigureBlock))
    assert figure.id == 'F1'
    assert figure.caption == 'Sample figure.'
    assert figure.section_path == ('s1',)


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_reference_parsed_populated(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    assert len(doc.references) == 1
    ref = doc.references[0]
    assert ref.id == 'R1'
    assert ref.parsed is not None
    assert ref.parsed.authors == ('Smith, J.',)
    assert ref.parsed.year == 2019


def test_reference_with_no_structured_fields_has_no_parsed() -> None:
    raw = _publisher_dict_minimal(route='jats')
    raw['references'][0].update(
        {'authors': [], 'title': None, 'source': None, 'year': None, 'doi': None}
    )
    doc = normalize_xml_document(raw, route='jats')
    assert doc.references[0].parsed is None


def test_reference_with_unparseable_year_drops_year() -> None:
    raw = _publisher_dict_minimal(route='jats')
    raw['references'][0]['year'] = '2019a'
    doc = normalize_xml_document(raw, route='jats')
    parsed = doc.references[0].parsed
    assert parsed is not None
    assert parsed.year is None


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_completeness_reflects_dict_content(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route=route), route=route)
    assert doc.completeness.has_structured_refs is True
    assert doc.completeness.has_inline_ref_ids is True
    assert doc.completeness.has_equations is False
    assert doc.completeness.table_source == 'publisher'


def test_completeness_flags_off_when_dict_lacks_them() -> None:
    raw = _publisher_dict_minimal(route='jats')
    # Strip everything that would set a completeness flag.
    raw['references'] = []
    raw['sections'][0]['blocks'][0]['xrefs'] = []
    doc = normalize_xml_document(raw, route='jats')
    assert doc.completeness.has_structured_refs is False
    assert doc.completeness.has_inline_ref_ids is False


def test_normalized_document_round_trips_through_converter() -> None:
    doc = normalize_xml_document(_publisher_dict_minimal(route='jats'), route='jats')
    raw = converter.unstructure(doc)
    # Survives JSON encode/decode (the on-disk shape will be JSON).
    restored = converter.structure(json.loads(json.dumps(raw)), Document)
    assert restored == doc


# ---------------------------------------------------------------------------
# Display-mode equations
# ---------------------------------------------------------------------------


def _publisher_dict_with_equation(*, route: XmlRoute) -> dict[str, Any]:
    """Minimal publisher dict whose section carries a paragraph then an equation.

    Block order is preserved through the adapter, so the two should land
    adjacent in :attr:`Document.blocks` with the equation following the
    text block. ``mathml`` carries a verbatim ``<math>`` envelope of the
    sort the JATS / Elsevier extractors serialise via ``serialize_mathml``.
    """
    return {
        'front': {'title': None, 'abstract': None},
        'sections': [
            {
                'id': 's1',
                'title': 'Theory',
                'level': 1,
                'path': ['s1'],
                'blocks': [
                    {'type': 'paragraph', 'text': 'See below.', 'xrefs': []},
                    {
                        'type': 'equation',
                        'id': 'eq1',
                        'text': 'T1 = a + b',
                        'mathml': (
                            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
                            '<mi>T1</mi><mo>=</mo><mi>a</mi><mo>+</mo><mi>b</mi>'
                            '</math>'
                        ),
                    },
                ],
            }
        ],
        'tables': [],
        'figures': [],
        'references': [],
    }


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_equation_block_emitted_with_mathml(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_with_equation(route=route), route=route)
    equations = [b for b in doc.blocks if isinstance(b, EquationBlock)]
    assert len(equations) == 1
    eq = equations[0]
    assert eq.id == 'eq1'
    assert eq.text == 'T1 = a + b'
    assert eq.mathml is not None and '<mi>T1</mi>' in eq.mathml
    # XML routes never populate ``latex`` — that's the docling field.
    assert eq.latex is None
    assert eq.section_path == ('s1',)
    assert eq.provenance.route == route


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_equation_preserves_section_block_order(route: XmlRoute) -> None:
    # Paragraph comes first, equation second — the adapter must respect
    # the publisher-dict's reading order so the verbatim-anchor gate's
    # block-index references stay stable.
    doc = normalize_xml_document(_publisher_dict_with_equation(route=route), route=route)
    kinds = [b.type for b in doc.blocks]
    assert kinds[:2] == ['text', 'equation']


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_completeness_has_equations_when_equation_present(route: XmlRoute) -> None:
    doc = normalize_xml_document(_publisher_dict_with_equation(route=route), route=route)
    assert doc.completeness.has_equations is True


def test_empty_publisher_dict_yields_empty_document() -> None:
    doc = normalize_xml_document(
        {'front': {}, 'sections': [], 'tables': [], 'figures': [], 'references': []},
        route='jats',
    )
    assert doc.blocks == ()
    assert doc.references == ()
    assert doc.completeness.has_structured_refs is False
    assert doc.title is None


# ---------------------------------------------------------------------------
# Integration smoke — run the real extractor end-to-end into the adapter
# ---------------------------------------------------------------------------


def _stage_record(
    *,
    tmp_path: Path,
    body: bytes,
    fmt: Format,
    publisher: Publisher,
    sha256: str,
    doi: str,
) -> tuple[ArtifactStore, AcquisitionRecord]:
    """Write ``body`` into a fresh store and return a record pointing at it.

    Stripped-down version of the ``tests/extract/conftest.py`` helper —
    duplicated here so the normalize tests stay self-contained. If we
    grow a third caller, lift the helper to ``tests/conftest.py``.
    """
    store = ArtifactStore(data_dir=tmp_path)
    relpath = store.artifact_relpath(sha256, fmt)
    artifact_path = store.data_dir / relpath
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(body)
    publisher_str = 'Springer Nature' if publisher is Publisher.SPRINGER_NATURE else 'Elsevier BV'
    metadata = CrossRefMetadata(
        doi=doi,
        publisher_str=publisher_str,
        title='Smoke',
        authors=('Doe, J.',),
        year=2024,
        type='journal-article',
        license=None,
    )
    record = AcquisitionRecord(
        doi=doi,
        sha256=sha256,
        artifact_path=relpath,
        format=fmt,
        publisher=publisher,
        metadata=metadata,
        fetched_url='https://example.org/' + doi,
        fetched_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='smoke 0.0',
        byte_size=len(body),
        origin='auto',
        manual_provenance=None,
    )
    return store, record


async def test_end_to_end_jats_extract_then_normalize(tmp_path: Path) -> None:
    store, record = _stage_record(
        tmp_path=tmp_path,
        body=_RICH_JATS,
        fmt=Format.JATS_XML,
        publisher=Publisher.SPRINGER_NATURE,
        sha256='1' * 64,
        doi='10.1000/jats.smoke',
    )
    await jats_mod.extract_jats(record, store=store)
    document_path = store.document_dir(record.sha256) / 'document.json'
    raw = json.loads(document_path.read_text())
    doc = normalize_xml_document(raw, route='jats')

    assert doc.route == 'jats'
    assert doc.title == 'Quantitative T1 mapping at 7 T'
    # One paragraph in s1, one in s1-1, two in s2 → four text blocks.
    text_blocks = [b for b in doc.blocks if isinstance(b, TextBlock)]
    assert len(text_blocks) == 4
    # The intro paragraph holds the bibr xref; its inline ref points
    # at R1 and surface_form matches the block text byte-for-byte.
    intro = next(b for b in text_blocks if b.section_path == ('s1',))
    assert len(intro.inline_refs) == 1
    ref = intro.inline_refs[0]
    assert ref.ref_id == 'R1'
    assert intro.text[ref.char_range.start : ref.char_range.end] == ref.surface_form == '[1]'
    # Table + figure surface as their own blocks.
    assert any(isinstance(b, TableBlock) and b.id == 't1' for b in doc.blocks)
    assert any(isinstance(b, FigureBlock) and b.id == 'f1' for b in doc.blocks)
    # References parsed; Completeness reflects the route.
    assert doc.references[0].parsed is not None
    assert doc.completeness.table_source == 'publisher'


async def test_end_to_end_elsevier_extract_then_normalize(tmp_path: Path) -> None:
    store, record = _stage_record(
        tmp_path=tmp_path,
        body=_RICH_ELSEVIER,
        fmt=Format.ELSEVIER_XML,
        publisher=Publisher.ELSEVIER,
        sha256='2' * 64,
        doi='10.1000/elsevier.smoke',
    )
    await elsevier_mod.extract_elsevier(record, store=store)
    document_path = store.document_dir(record.sha256) / 'document.json'
    raw = json.loads(document_path.read_text())
    doc = normalize_xml_document(raw, route='elsevier')

    assert doc.route == 'elsevier'
    intro_text = next(
        b for b in doc.blocks if isinstance(b, TextBlock) and b.section_path == ('s1',)
    )
    assert len(intro_text.inline_refs) == 1
    ref = intro_text.inline_refs[0]
    assert ref.ref_id == 'b1'
    assert ref.ref_id_source == 'elsevier'
    assert intro_text.text[ref.char_range.start : ref.char_range.end] == '[1]'
    # All cells normalise to 'th' / 'td' (Elsevier CALS 'entry' lands as 'td').
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    assert all(c.kind in ('th', 'td') for row in table.cells for c in row)
