"""Construction-time invariants for the normalized Document attrs models."""

import pytest

from litspectraits.normalize import (
    BBox,
    CharRange,
    Completeness,
    Document,
    EquationBlock,
    FigureBlock,
    InlineMath,
    InlineRef,
    Provenance,
    Reference,
    TableBlock,
    TableCell,
    TextBlock,
)

# ---------------------------------------------------------------------------
# Provenance route-conditional invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_xml_route_rejects_page_fields(route: str) -> None:
    with pytest.raises(ValueError, match='XML provenance'):
        Provenance(route=route, xpath='/article/body/sec[1]/p[1]', page=3)  # type: ignore[arg-type]


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_xml_route_rejects_bbox(route: str) -> None:
    with pytest.raises(ValueError, match='XML provenance'):
        Provenance(
            route=route,  # type: ignore[arg-type]
            xpath='/article/body',
            bbox=BBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0),
        )


@pytest.mark.parametrize('route', ['jats', 'elsevier'])
def test_xml_route_rejects_page_char_range(route: str) -> None:
    with pytest.raises(ValueError, match='XML provenance'):
        Provenance(
            route=route,  # type: ignore[arg-type]
            xpath='/article/body',
            page_char_range=CharRange(start=0, end=10),
        )


def test_xml_route_xpath_only_is_valid() -> None:
    prov = Provenance(route='jats', xpath='/article/body/sec[1]/p[3]')
    assert prov.xpath == '/article/body/sec[1]/p[3]'
    assert prov.page is None
    assert prov.bbox is None
    assert prov.page_char_range is None


def test_docling_route_rejects_xpath() -> None:
    with pytest.raises(ValueError, match='xpath must be None'):
        Provenance(route='docling', xpath='/article/body', page=1)


def test_docling_route_requires_page() -> None:
    with pytest.raises(ValueError, match='requires `page`'):
        Provenance(route='docling')


def test_docling_route_page_only_is_valid() -> None:
    prov = Provenance(route='docling', page=4)
    assert prov.page == 4
    assert prov.xpath is None


def test_docling_route_full_geometry_is_valid() -> None:
    prov = Provenance(
        route='docling',
        page=4,
        bbox=BBox(x0=72.0, y0=120.0, x1=540.0, y1=180.0),
        page_char_range=CharRange(start=1024, end=1287),
        geometry_fidelity='exact',
    )
    assert prov.bbox is not None
    assert prov.page_char_range is not None


# ---------------------------------------------------------------------------
# InlineRef honesty invariant
# ---------------------------------------------------------------------------


def test_inline_ref_id_without_source_raises() -> None:
    with pytest.raises(ValueError, match='must be both None or both set'):
        InlineRef(
            char_range=CharRange(start=10, end=14),
            surface_form='[12]',
            ref_id='R12',
            ref_id_source=None,
        )


def test_inline_ref_source_without_id_raises() -> None:
    with pytest.raises(ValueError, match='must be both None or both set'):
        InlineRef(
            char_range=CharRange(start=10, end=14),
            surface_form='[12]',
            ref_id=None,
            ref_id_source='jats',
        )


def test_inline_ref_both_none_is_valid() -> None:
    ref = InlineRef(
        char_range=CharRange(start=10, end=14),
        surface_form='[12]',
    )
    assert ref.ref_id is None
    assert ref.ref_id_source is None


def test_inline_ref_both_set_is_valid() -> None:
    ref = InlineRef(
        char_range=CharRange(start=10, end=14),
        surface_form='[12]',
        ref_id='R12',
        ref_id_source='jats',
    )
    assert ref.ref_id == 'R12'
    assert ref.ref_id_source == 'jats'


# ---------------------------------------------------------------------------
# EquationBlock route-conditional invariants
# ---------------------------------------------------------------------------


def test_equation_block_both_mathml_and_latex_raises() -> None:
    with pytest.raises(ValueError, match='exactly one of'):
        EquationBlock(
            id='E1',
            text='E = mc^2',
            mathml='<math/>',
            latex=r'E = mc^2',
            provenance=Provenance(route='docling', page=1),
        )


def test_equation_block_neither_mathml_nor_latex_raises() -> None:
    with pytest.raises(ValueError, match='exactly one of'):
        EquationBlock(
            id='E1',
            text='E = mc^2',
            mathml=None,
            latex=None,
            provenance=Provenance(route='docling', page=1),
        )


def test_equation_block_mathml_on_docling_raises() -> None:
    with pytest.raises(ValueError, match='mathml is only populated on XML routes'):
        EquationBlock(
            id='E1',
            text='E = mc^2',
            mathml='<math/>',
            latex=None,
            provenance=Provenance(route='docling', page=1),
        )


def test_equation_block_latex_on_xml_raises() -> None:
    with pytest.raises(ValueError, match='latex is only populated on the PDF routes'):
        EquationBlock(
            id='E1',
            text='E = mc^2',
            mathml=None,
            latex=r'E = mc^2',
            provenance=Provenance(route='jats', xpath='/article/body'),
        )


def test_equation_block_docling_latex_is_valid() -> None:
    block = EquationBlock(
        id='formula-3',
        text='E = mc^2',
        mathml=None,
        latex=r'E = mc^2',
        provenance=Provenance(route='docling', page=2),
    )
    assert block.type == 'equation'
    assert block.latex == r'E = mc^2'


def test_equation_block_xml_mathml_is_valid() -> None:
    block = EquationBlock(
        id='eq1',
        text='E = mc^2',
        mathml='<math><mrow><mi>E</mi></mrow></math>',
        latex=None,
        provenance=Provenance(route='jats', xpath='/article/body/disp-formula[1]'),
    )
    assert block.type == 'equation'
    assert block.mathml is not None


# ---------------------------------------------------------------------------
# Document route-purity invariant
# ---------------------------------------------------------------------------


def _completeness_xml() -> Completeness:
    return Completeness(
        has_structured_refs=True,
        has_inline_ref_ids=True,
        has_equations=False,
        table_source='publisher',
    )


def _xml_text_block(text: str = 'A paragraph.') -> TextBlock:
    return TextBlock(
        text=text,
        provenance=Provenance(route='jats', xpath='/article/body/sec[1]/p[1]'),
    )


def _docling_text_block(text: str = 'A paragraph.') -> TextBlock:
    return TextBlock(
        text=text,
        provenance=Provenance(route='docling', page=1),
    )


def test_document_uniform_route_is_valid() -> None:
    doc = Document(
        route='jats',
        blocks=(_xml_text_block(),),
        references=(),
        completeness=_completeness_xml(),
    )
    assert doc.route == 'jats'
    assert len(doc.blocks) == 1


def test_document_rejects_mixed_routes() -> None:
    with pytest.raises(ValueError, match=r'different provenance\.route'):
        Document(
            route='jats',
            blocks=(_xml_text_block(), _docling_text_block()),
            references=(),
            completeness=_completeness_xml(),
        )


def test_document_rejects_block_route_disagreeing_with_document_route() -> None:
    with pytest.raises(ValueError, match=r'different provenance\.route'):
        Document(
            route='docling',
            blocks=(_xml_text_block(),),
            references=(),
            completeness=_completeness_xml(),
        )


# ---------------------------------------------------------------------------
# Smoke: full Document with all block kinds (docling route exercises every variant)
# ---------------------------------------------------------------------------


def test_full_docling_document_constructs() -> None:
    completeness = Completeness(
        has_structured_refs=False,
        has_inline_ref_ids=False,
        has_equations=True,
        table_source='tableformer',
    )
    cells = (
        (
            TableCell(text='Tissue', kind='th'),
            TableCell(text='T1 (ms)', kind='th'),
        ),
        (
            TableCell(text='White matter', kind='td'),
            TableCell(text='1084', kind='td'),
        ),
    )
    blocks = (
        TextBlock(
            text='Relaxation times are reported in Table 1.',
            provenance=Provenance(route='docling', page=3),
            inline_refs=(
                InlineRef(
                    char_range=CharRange(start=33, end=40),
                    surface_form='Table 1',
                ),
            ),
        ),
        TableBlock(
            id='table-1',
            label='Table 1',
            caption='Relaxation times at 3 T.',
            n_rows=2,
            n_cols=2,
            cells=cells,
            provenance=Provenance(
                route='docling',
                page=3,
                bbox=BBox(x0=72.0, y0=200.0, x1=540.0, y1=320.0),
                geometry_fidelity='exact',
            ),
        ),
        FigureBlock(
            id='fig-1',
            label='Figure 1',
            caption='Sagittal slice through the brain.',
            provenance=Provenance(route='docling', page=2),
        ),
        EquationBlock(
            id='formula-1',
            text='S = M0 (1 - exp(-TR/T1))',
            mathml=None,
            latex=r'S = M_0 (1 - e^{-TR/T_1})',
            provenance=Provenance(route='docling', page=4),
        ),
    )
    references = (Reference(id='R1', raw_text='Smith J. (2019). T1 mapping. JMRI.'),)
    doc = Document(
        route='docling',
        blocks=blocks,
        references=references,
        completeness=completeness,
        title='A study of T1 in white matter',
    )
    assert {b.type for b in doc.blocks} == {'text', 'table', 'figure', 'equation'}
    assert doc.completeness.table_source == 'tableformer'


# ---------------------------------------------------------------------------
# geometry_fidelity relaxation (docs/mineru-backend-spec.md §2.2)
# ---------------------------------------------------------------------------


def test_xml_route_rejects_non_absent_fidelity() -> None:
    with pytest.raises(ValueError, match='geometry_fidelity must be'):
        Provenance(route='jats', xpath='/article/body', geometry_fidelity='exact')


def test_pdf_route_bbox_present_requires_grade() -> None:
    with pytest.raises(ValueError, match='grade it'):
        Provenance(
            route='docling',
            page=1,
            bbox=BBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0),
            # geometry_fidelity left at default 'absent' — inconsistent with a bbox
        )


def test_pdf_route_fidelity_without_bbox_raises() -> None:
    with pytest.raises(ValueError, match='cannot grade geometry that is not there'):
        Provenance(route='mineru', page=1, geometry_fidelity='approximate')


def test_pdf_route_bbox_absent_is_valid() -> None:
    # A synthesised/merged block with no recoverable geometry is allowed.
    prov = Provenance(route='mineru', page=2)
    assert prov.bbox is None
    assert prov.geometry_fidelity == 'absent'


@pytest.mark.parametrize('fidelity', ['exact', 'approximate'])
def test_pdf_route_graded_bbox_is_valid(fidelity: str) -> None:
    prov = Provenance(
        route='mineru',
        page=2,
        bbox=BBox(x0=72.0, y0=120.0, x1=540.0, y1=180.0),
        geometry_fidelity=fidelity,  # type: ignore[arg-type]
    )
    assert prov.geometry_fidelity == fidelity


# ---------------------------------------------------------------------------
# mineru route is a first-class PDF route (docs/mineru-backend-spec.md §2)
# ---------------------------------------------------------------------------


def test_mineru_route_requires_page() -> None:
    with pytest.raises(ValueError, match='requires `page`'):
        Provenance(route='mineru')


def test_mineru_route_rejects_xpath() -> None:
    with pytest.raises(ValueError, match='xpath must be None'):
        Provenance(route='mineru', xpath='/article/body', page=1)


def test_equation_block_latex_on_mineru_is_valid() -> None:
    block = EquationBlock(
        id='eq-7',
        text='S = M0 (1 - exp(-TR/T1))',
        mathml=None,
        latex=r'S = M_0 (1 - e^{-TR/T_1})',
        provenance=Provenance(route='mineru', page=3),
    )
    assert block.latex is not None


def test_equation_block_mathml_on_mineru_raises() -> None:
    with pytest.raises(ValueError, match='mathml is only populated on XML routes'):
        EquationBlock(
            id='eq-7',
            text='x',
            mathml='<math/>',
            latex=None,
            provenance=Provenance(route='mineru', page=3),
        )


# ---------------------------------------------------------------------------
# TextBlock.inline_math (docs/mineru-backend-spec.md §2.4)
# ---------------------------------------------------------------------------


def test_text_block_carries_inline_math() -> None:
    block = TextBlock(
        text='The signal S decays as exp(-t/T2).',
        provenance=Provenance(route='mineru', page=1),
        inline_math=(InlineMath(char_range=CharRange(start=23, end=33), latex=r'e^{-t/T_2}'),),
    )
    assert len(block.inline_math) == 1
    assert block.inline_math[0].latex == r'e^{-t/T_2}'


def test_text_block_inline_math_defaults_empty() -> None:
    block = TextBlock(text='No math here.', provenance=Provenance(route='jats'))
    assert block.inline_math == ()
