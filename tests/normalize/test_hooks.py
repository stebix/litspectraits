"""cattrs (un)structure round-trip + error cases for the Block union."""

import json

import pytest

from litspectraits.normalize import (
    BBox,
    CharRange,
    Completeness,
    Document,
    EquationBlock,
    FigureBlock,
    InlineRef,
    ParsedReference,
    Provenance,
    Reference,
    TableBlock,
    TableCell,
    TextBlock,
    converter,
)
from litspectraits.normalize.models import Block


def _flatten_messages(exc: BaseException) -> list[str]:
    """Walk a (possibly nested) :class:`BaseExceptionGroup` and collect leaf ``str``.

    cattrs wraps every structure-time failure in
    :class:`cattrs.errors.ClassValidationError` (a ``BaseExceptionGroup``),
    so the validation message produced by our union hook is nested under
    one or two layers. ``pytest.raises(..., match=)`` checks only the
    top-level message, which would just be ``'While structuring …'``.
    Tests use this helper to assert on the actual leaf error.
    """
    if isinstance(exc, BaseExceptionGroup):
        out: list[str] = []
        for sub in exc.exceptions:
            out.extend(_flatten_messages(sub))
        return out
    return [str(exc)]


def _xml_document() -> Document:
    completeness = Completeness(
        has_structured_refs=True,
        has_inline_ref_ids=True,
        has_equations=False,
        table_source='publisher',
    )
    cells = (
        (
            TableCell(text='Region', kind='th'),
            TableCell(text='T2 (ms)', kind='th'),
        ),
        (
            TableCell(text='Cortex', kind='td'),
            TableCell(text='75', kind='td'),
        ),
    )
    blocks: tuple[Block, ...] = (
        TextBlock(
            text='We measured T2 in the cortex [1].',
            provenance=Provenance(route='jats', xpath='/article/body/sec[1]/p[1]'),
            section_path=('intro',),
            inline_refs=(
                InlineRef(
                    char_range=CharRange(start=29, end=32),
                    surface_form='[1]',
                    ref_id='R1',
                    ref_id_source='jats',
                ),
            ),
        ),
        TableBlock(
            id='T1',
            label='Table 1',
            caption='Cortical T2 values.',
            n_rows=2,
            n_cols=2,
            cells=cells,
            provenance=Provenance(route='jats', xpath='/article/body/table-wrap[1]'),
            section_path=('results',),
        ),
        FigureBlock(
            id='F1',
            label='Figure 1',
            caption='Sample images.',
            provenance=Provenance(route='jats', xpath='/article/body/fig[1]'),
            section_path=('results',),
        ),
    )
    references = (
        Reference(
            id='R1',
            raw_text='Smith J. (2019). T2 mapping. JMRI.',
            parsed=ParsedReference(
                authors=('Smith, J.',),
                title='T2 mapping',
                source='JMRI',
                year=2019,
            ),
        ),
    )
    return Document(
        route='jats',
        blocks=blocks,
        references=references,
        completeness=completeness,
        title='A study',
        abstract='Some prose.',
    )


def _docling_document() -> Document:
    completeness = Completeness(
        has_structured_refs=False,
        has_inline_ref_ids=False,
        has_equations=True,
        table_source='tableformer',
    )
    blocks: tuple[Block, ...] = (
        TextBlock(
            text='See Table 1.',
            provenance=Provenance(
                route='docling',
                page=2,
                bbox=BBox(x0=72.0, y0=100.0, x1=540.0, y1=130.0),
                page_char_range=CharRange(start=0, end=12),
                geometry_fidelity='exact',
            ),
        ),
        EquationBlock(
            id='formula-1',
            text='S = M0 (1 - exp(-TR/T1))',
            mathml=None,
            latex=r'S = M_0 (1 - e^{-TR/T_1})',
            provenance=Provenance(route='docling', page=4),
        ),
    )
    return Document(
        route='docling',
        blocks=blocks,
        references=(Reference(id='ref-1', raw_text='Smith J. T2 mapping. JMRI 2019.'),),
        completeness=completeness,
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_xml_document() -> None:
    doc = _xml_document()
    raw = converter.unstructure(doc)
    restored = converter.structure(raw, Document)
    assert restored == doc


def test_round_trip_docling_document() -> None:
    doc = _docling_document()
    raw = converter.unstructure(doc)
    restored = converter.structure(raw, Document)
    assert restored == doc


def test_round_trip_through_json() -> None:
    doc = _xml_document()
    raw = converter.unstructure(doc)
    payload = json.dumps(raw)
    restored = converter.structure(json.loads(payload), Document)
    assert restored == doc


def test_unstructure_emits_block_type_discriminator() -> None:
    doc = _docling_document()
    raw = converter.unstructure(doc)
    assert all('type' in b for b in raw['blocks'])
    assert {b['type'] for b in raw['blocks']} == {'text', 'equation'}


# ---------------------------------------------------------------------------
# Error cases on structuring
# ---------------------------------------------------------------------------


def test_structure_block_missing_type_discriminator() -> None:
    raw = {
        'route': 'jats',
        'blocks': [
            {
                # 'type' deliberately missing
                'text': 'oops',
                'provenance': {'route': 'jats', 'xpath': '/x'},
                'section_path': [],
                'inline_refs': [],
            }
        ],
        'references': [],
        'completeness': {
            'has_structured_refs': True,
            'has_inline_ref_ids': True,
            'has_equations': False,
            'table_source': 'publisher',
        },
        'title': None,
        'abstract': None,
        'schema_name': 'litspectraits-normalized-document',
        'schema_version': '1',
    }
    with pytest.raises(Exception) as excinfo:
        converter.structure(raw, Document)
    assert any('missing the required `type`' in m for m in _flatten_messages(excinfo.value))


def test_structure_block_unknown_type_tag() -> None:
    raw = {
        'route': 'jats',
        'blocks': [
            {
                'type': 'paragraph',  # legacy / wrong tag
                'text': 'oops',
                'provenance': {'route': 'jats', 'xpath': '/x'},
                'section_path': [],
                'inline_refs': [],
            }
        ],
        'references': [],
        'completeness': {
            'has_structured_refs': True,
            'has_inline_ref_ids': True,
            'has_equations': False,
            'table_source': 'publisher',
        },
        'title': None,
        'abstract': None,
        'schema_name': 'litspectraits-normalized-document',
        'schema_version': '1',
    }
    with pytest.raises(Exception) as excinfo:
        converter.structure(raw, Document)
    assert any('unknown block `type`' in m for m in _flatten_messages(excinfo.value))


def test_structure_block_non_mapping_raises() -> None:
    raw = {
        'route': 'jats',
        'blocks': ['not-a-mapping'],
        'references': [],
        'completeness': {
            'has_structured_refs': True,
            'has_inline_ref_ids': True,
            'has_equations': False,
            'table_source': 'publisher',
        },
        'title': None,
        'abstract': None,
        'schema_name': 'litspectraits-normalized-document',
        'schema_version': '1',
    }
    with pytest.raises(Exception) as excinfo:
        converter.structure(raw, Document)
    assert any('must be a mapping' in m for m in _flatten_messages(excinfo.value))


def test_structure_route_mismatch_propagates() -> None:
    """A persisted Document with route='jats' but a docling-route block
    must fail loud at structure time — same invariant the model enforces
    at construction.
    """
    raw = {
        'route': 'jats',
        'blocks': [
            {
                'type': 'text',
                'text': 'oops',
                'provenance': {'route': 'docling', 'page': 1},
                'section_path': [],
                'inline_refs': [],
            }
        ],
        'references': [],
        'completeness': {
            'has_structured_refs': True,
            'has_inline_ref_ids': True,
            'has_equations': False,
            'table_source': 'publisher',
        },
        'title': None,
        'abstract': None,
        'schema_name': 'litspectraits-normalized-document',
        'schema_version': '1',
    }
    with pytest.raises(Exception) as excinfo:
        converter.structure(raw, Document)
    assert any('different provenance.route' in m for m in _flatten_messages(excinfo.value))
