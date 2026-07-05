"""Tests for :mod:`litspectraits.normalize.mineru_adapter` (spec §4).

Adversarial ``middle.json`` payloads with planted rowspan/colspan tables,
inline + display equations, missing-bbox blocks, and the pipeline-vs-vlm
geometry grade — the same faithfulness bar the docling adapter is held to.
"""

from typing import Any

import pytest

from litspectraits.normalize import converter
from litspectraits.normalize.mineru_adapter import normalize_mineru_document
from litspectraits.normalize.models import (
    Document,
    EquationBlock,
    FigureBlock,
    TableBlock,
    TextBlock,
)

# Helpers ---------------------------------------------------------------------


def _middle(blocks: list[dict[str, Any]], *, backend: str = 'pipeline') -> dict[str, Any]:
    return {
        '_backend': backend,
        'pdf_info': [{'page_idx': 0, 'page_size': [612, 792], 'para_blocks': blocks}],
    }


def _text(content: str, bbox: list[float] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        'type': 'text',
        'lines': [{'spans': [{'type': 'text', 'content': content}]}],
    }
    if bbox is not None:
        block['bbox'] = bbox
    return block


# Text + inline math ----------------------------------------------------------


def test_text_block_basic_provenance() -> None:
    doc = normalize_mineru_document(_middle([_text('Hello world.', [10, 10, 200, 30])]))
    assert len(doc.blocks) == 1
    block = doc.blocks[0]
    assert isinstance(block, TextBlock)
    assert block.text == 'Hello world.'
    assert block.provenance.route == 'mineru'
    assert block.provenance.page == 0
    assert block.provenance.geometry_fidelity == 'exact'
    assert block.provenance.bbox is not None


def test_inline_equation_offsets_slice_back_to_latex() -> None:
    block = {
        'type': 'text',
        'bbox': [10, 10, 400, 30],
        'lines': [
            {
                'spans': [
                    {'type': 'text', 'content': 'The rate '},
                    {'type': 'inline_equation', 'content': 'R_1 = 1/T_1'},
                    {'type': 'text', 'content': ' is reported.'},
                ]
            }
        ],
    }
    doc = normalize_mineru_document(_middle([block]))
    tb = doc.blocks[0]
    assert isinstance(tb, TextBlock)
    assert tb.text == 'The rate R_1 = 1/T_1 is reported.'
    assert len(tb.inline_math) == 1
    im = tb.inline_math[0]
    # The char range must slice the text back to exactly the LaTeX source.
    assert tb.text[im.char_range.start : im.char_range.end] == im.latex == 'R_1 = 1/T_1'


def test_inline_refs_are_anchored() -> None:
    doc = normalize_mineru_document(_middle([_text('Prior work [12, 14] showed.', [0, 0, 9, 9])]))
    tb = doc.blocks[0]
    assert isinstance(tb, TextBlock)
    assert len(tb.inline_refs) == 1
    ref = tb.inline_refs[0]
    assert tb.text[ref.char_range.start : ref.char_range.end] == '[12, 14]'
    assert ref.ref_id is None and ref.ref_id_source is None


def test_multiline_text_joined_with_space_keeps_offsets() -> None:
    block = {
        'type': 'text',
        'bbox': [0, 0, 9, 9],
        'lines': [
            {'spans': [{'type': 'text', 'content': 'first line'}]},
            {
                'spans': [
                    {'type': 'inline_equation', 'content': 'x^2'},
                    {'type': 'text', 'content': ' tail'},
                ]
            },
        ],
    }
    doc = normalize_mineru_document(_middle([block]))
    tb = doc.blocks[0]
    assert isinstance(tb, TextBlock)
    assert tb.text == 'first line x^2 tail'
    im = tb.inline_math[0]
    assert tb.text[im.char_range.start : im.char_range.end] == 'x^2'


# Display equations -----------------------------------------------------------


def test_interline_equation_becomes_equation_block() -> None:
    block = {
        'type': 'interline_equation',
        'bbox': [10, 40, 200, 70],
        'lines': [{'spans': [{'type': 'interline_equation', 'content': 'S = S_0 e^{-TE/T_2}'}]}],
    }
    doc = normalize_mineru_document(_middle([block]))
    eq = doc.blocks[0]
    assert isinstance(eq, EquationBlock)
    assert eq.latex == 'S = S_0 e^{-TE/T_2}'
    assert eq.mathml is None
    assert doc.completeness.has_equations is True


def test_interline_equation_without_latex_still_emits_block() -> None:
    # MinerU could not OCR the formula (image-only span) — block still emitted
    # so the display equation's page/bbox provenance survives.
    block = {
        'type': 'interline_equation',
        'bbox': [10, 40, 200, 70],
        'lines': [{'spans': [{'type': 'interline_equation', 'image_path': 'eq.jpg'}]}],
    }
    doc = normalize_mineru_document(_middle([block]))
    eq = doc.blocks[0]
    assert isinstance(eq, EquationBlock)
    assert eq.latex == ''


# Tables — rowspan/colspan ----------------------------------------------------


def _table(html: str, caption: str | None = None) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {
            'type': 'table_body',
            'bbox': [10, 50, 300, 200],
            'lines': [{'spans': [{'type': 'table', 'html': html}]}],
        }
    ]
    if caption is not None:
        blocks.append(
            {
                'type': 'table_caption',
                'bbox': [10, 45, 300, 50],
                'lines': [{'spans': [{'type': 'text', 'content': caption}]}],
            }
        )
    return {'type': 'table', 'bbox': [10, 45, 300, 200], 'blocks': blocks}


def test_simple_table_values_and_dimensions() -> None:
    html = '<table><tr><td>Tissue</td><td>T1</td></tr><tr><td>Liver</td><td>812</td></tr></table>'
    doc = normalize_mineru_document(_middle([_table(html, caption='Table 1. Relaxometry.')]))
    tb = doc.blocks[0]
    assert isinstance(tb, TableBlock)
    assert tb.n_rows == 2
    assert tb.n_cols == 2
    assert tb.caption == 'Table 1. Relaxometry.'
    assert [c.text for c in tb.cells[0]] == ['Tissue', 'T1']
    assert [c.text for c in tb.cells[1]] == ['Liver', '812']
    assert doc.completeness.table_source == 'mineru'


def test_rowspan_colspan_grid_anchoring() -> None:
    html = (
        '<table>'
        '<tr><td rowspan=2>A</td><td colspan=2>B</td></tr>'
        '<tr><td>C</td><td>D</td></tr>'
        '<tr><td>E</td><td>F</td><td>G</td></tr>'
        '</table>'
    )
    doc = normalize_mineru_document(_middle([_table(html)]))
    tb = doc.blocks[0]
    assert isinstance(tb, TableBlock)
    assert tb.n_rows == 3
    assert tb.n_cols == 3
    # Row 0: A spans 2 rows, B spans 2 cols.
    assert [(c.text, c.rowspan, c.colspan) for c in tb.cells[0]] == [('A', 2, 1), ('B', 1, 2)]
    # Row 1: A's rowspan occupies col 0, so C/D anchor at cols 1 and 2 (not duplicated).
    assert [c.text for c in tb.cells[1]] == ['C', 'D']
    # Row 2: full width.
    assert [c.text for c in tb.cells[2]] == ['E', 'F', 'G']


def test_header_cells_tagged_th() -> None:
    html = '<table><tr><th>Tissue</th><th>T1</th></tr><tr><td>Liver</td><td>812</td></tr></table>'
    doc = normalize_mineru_document(_middle([_table(html)]))
    tb = doc.blocks[0]
    assert isinstance(tb, TableBlock)
    assert [c.kind for c in tb.cells[0]] == ['th', 'th']
    assert [c.kind for c in tb.cells[1]] == ['td', 'td']


def test_empty_table_html_yields_empty_grid() -> None:
    doc = normalize_mineru_document(_middle([_table('')]))
    tb = doc.blocks[0]
    assert isinstance(tb, TableBlock)
    assert tb.n_rows == 0
    assert tb.n_cols == 0
    assert tb.cells == ()


# Figures ---------------------------------------------------------------------


def test_image_block_becomes_figure_with_caption() -> None:
    block = {
        'type': 'image',
        'bbox': [10, 10, 100, 100],
        'blocks': [
            {
                'type': 'image_body',
                'bbox': [10, 10, 100, 90],
                'lines': [{'spans': [{'type': 'image', 'image_path': 'fig.jpg'}]}],
            },
            {
                'type': 'image_caption',
                'bbox': [10, 90, 100, 100],
                'lines': [{'spans': [{'type': 'text', 'content': 'Figure 2. A map.'}]}],
            },
        ],
    }
    doc = normalize_mineru_document(_middle([block]))
    fb = doc.blocks[0]
    assert isinstance(fb, FigureBlock)
    assert fb.caption == 'Figure 2. A map.'


# Provenance / geometry fidelity ----------------------------------------------


def test_missing_bbox_relaxes_to_absent() -> None:
    doc = normalize_mineru_document(_middle([_text('No bbox here.', bbox=None)]))
    prov = doc.blocks[0].provenance
    assert prov.bbox is None
    assert prov.geometry_fidelity == 'absent'
    assert prov.page == 0


def test_malformed_bbox_treated_as_absent() -> None:
    doc = normalize_mineru_document(_middle([_text('Bad bbox.', bbox=[1, 2, 3])]))  # len 3
    prov = doc.blocks[0].provenance
    assert prov.bbox is None
    assert prov.geometry_fidelity == 'absent'


def test_vlm_engine_grades_geometry_approximate() -> None:
    doc = normalize_mineru_document(_middle([_text('Hi.', [0, 0, 9, 9])], backend='vlm'))
    assert doc.blocks[0].provenance.geometry_fidelity == 'approximate'


def test_block_with_neither_page_nor_bbox_fails_loud() -> None:
    payload = {
        '_backend': 'pipeline',
        'pdf_info': [{'para_blocks': [_text('Orphan.', bbox=None)]}],  # no page_idx
    }
    with pytest.raises(ValueError, match='neither page'):
        normalize_mineru_document(payload)


# Titles / section path -------------------------------------------------------


def test_titles_build_section_path_and_document_title() -> None:
    blocks = [
        {
            'type': 'title',
            'level': 1,
            'bbox': [0, 0, 9, 1],
            'lines': [{'spans': [{'type': 'text', 'content': 'Methods'}]}],
        },
        _text('Body under methods.', [0, 2, 9, 9]),
        {
            'type': 'title',
            'level': 2,
            'bbox': [0, 10, 9, 11],
            'lines': [{'spans': [{'type': 'text', 'content': 'Acquisition'}]}],
        },
        _text('Body under acquisition.', [0, 12, 9, 19]),
    ]
    doc = normalize_mineru_document(_middle(blocks))
    assert doc.title == 'Methods'
    bodies = [b for b in doc.blocks if isinstance(b, TextBlock)]
    assert bodies[0].section_path == ('Methods',)
    assert bodies[1].section_path == ('Methods', 'Acquisition')


# Dropped / malformed ---------------------------------------------------------


def test_discarded_and_unknown_blocks_dropped() -> None:
    blocks = [
        {'type': 'discarded', 'bbox': [0, 0, 1, 1], 'lines': []},
        {
            'type': 'aside_text',
            'bbox': [0, 0, 1, 1],
            'lines': [{'spans': [{'type': 'text', 'content': 'aside'}]}],
        },
        _text('Real body.', [0, 0, 9, 9]),
    ]
    doc = normalize_mineru_document(_middle(blocks))
    assert len(doc.blocks) == 1
    assert isinstance(doc.blocks[0], TextBlock)


def test_pdf_info_not_a_list_raises() -> None:
    with pytest.raises(ValueError, match='no pdf_info list'):
        normalize_mineru_document({'_backend': 'pipeline', 'pdf_info': {}})


# Schema / round-trip ---------------------------------------------------------


def test_document_route_is_mineru_and_blocks_match() -> None:
    doc = normalize_mineru_document(_middle([_text('Body text here.', [0, 0, 9, 9])]))
    assert doc.route == 'mineru'
    assert all(b.provenance.route == 'mineru' for b in doc.blocks)
    assert doc.schema_version == '2'


def test_cattrs_round_trip_is_stable() -> None:
    html = '<table><tr><td>Tissue</td><td>T1</td></tr><tr><td>Liver</td><td>812</td></tr></table>'
    blocks = [
        {
            'type': 'title',
            'level': 1,
            'bbox': [0, 0, 9, 1],
            'lines': [{'spans': [{'type': 'text', 'content': 'Results'}]}],
        },
        _text('Body [3] with math.', [0, 2, 9, 9]),
        _table(html, caption='Table 1.'),
    ]
    doc = normalize_mineru_document(_middle(blocks))
    raw = converter.unstructure(doc)
    assert converter.structure(raw, Document) == doc
