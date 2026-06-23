"""Coverage for the static-HTML Document renderer (``render.py``).

Two layers:

- **Unit tests** drive small hand-built :class:`Document`s through
  :func:`litspectraits.normalize.render_html` and assert on the rendered
  HTML — inline-ref splicing + escaping (the one place offsets and
  escaping interact), table spans / cell kinds, equation raw-source,
  reference anchoring, completeness badges, the XML by-kind banner,
  route-honest provenance, and determinism.
- **Golden snapshots** (``docs/normalized-documents-discussion.md``
  §2.4(b)) freeze one rendered HTML per route. They are the regression
  tripwire: a normaliser change, a schema bump, or a render tweak
  surfaces as a reviewable HTML diff. Regenerate intentionally with
  ``LITSPECTRAITS_REGEN_GOLDEN=1 uv run pytest tests/normalize/test_render.py``.
"""

import os
from pathlib import Path

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
    RenderContext,
    TableBlock,
    TableCell,
    TextBlock,
    normalize_xml_document,
    render_html,
)

GOLDEN_DIR = Path(__file__).parent / 'golden'
_REGEN_ENV = 'LITSPECTRAITS_REGEN_GOLDEN'


# ---------------------------------------------------------------------------
# Small constructors
# ---------------------------------------------------------------------------


def _docling_prov(page: int = 1) -> Provenance:
    return Provenance(
        route='docling',
        page=page,
        bbox=BBox(x0=50.0, y0=60.0, x1=500.0, y1=80.0),
        page_char_range=CharRange(start=0, end=10),
    )


def _minimal_docling_doc(*, blocks: tuple = (), references: tuple = ()) -> Document:
    return Document(
        route='docling',
        blocks=blocks,
        references=references,
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=False,
            has_equations=any(isinstance(b, EquationBlock) for b in blocks),
            table_source='tableformer',
        ),
        title='Test paper',
        abstract=None,
    )


# ---------------------------------------------------------------------------
# Inline-ref splicing + escaping — the P0 surface
# ---------------------------------------------------------------------------


def test_escaping_around_inline_ref_does_not_corrupt_offsets() -> None:
    # 'a & b ' is 6 chars (indices 0-5); '[1]' sits at [6, 9); ' c < d' follows.
    text = 'a & b [1] c < d'
    block = TextBlock(
        text=text,
        provenance=_docling_prov(),
        inline_refs=(InlineRef(char_range=CharRange(6, 9), surface_form='[1]'),),
    )
    out = render_html(_minimal_docling_doc(blocks=(block,)))
    # Special chars on both sides of the splice are escaped...
    assert 'a &amp; b ' in out
    assert ' c &lt; d' in out
    # ...and the marker lands exactly on the bracketed reference.
    assert '<mark class="inline-ref">[1]</mark>' in out


def test_resolved_inline_ref_links_to_reference_anchor() -> None:
    block = TextBlock(
        text='See [1].',
        provenance=Provenance(route='jats'),
        inline_refs=(
            InlineRef(
                char_range=CharRange(4, 7),
                surface_form='[1]',
                ref_id='R1',
                ref_id_source='jats',
            ),
        ),
    )
    doc = Document(
        route='jats',
        blocks=(block,),
        references=(Reference(id='R1', raw_text='Doe 2020.'),),
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=True,
            has_equations=False,
            table_source='publisher',
        ),
    )
    out = render_html(doc)
    assert '<a class="inline-ref" href="#ref-R1">[1]</a>' in out
    assert 'id="ref-R1"' in out


def test_unresolved_inline_ref_is_mark_not_link() -> None:
    block = TextBlock(
        text='See [9].',
        provenance=_docling_prov(),
        inline_refs=(InlineRef(char_range=CharRange(4, 7), surface_form='[9]'),),
    )
    out = render_html(_minimal_docling_doc(blocks=(block,)))
    assert '<mark class="inline-ref">[9]</mark>' in out
    assert 'href="#ref-' not in out


def test_overlapping_inline_refs_raise() -> None:
    block = TextBlock(
        text='abcdefghij',
        provenance=_docling_prov(),
        inline_refs=(
            InlineRef(char_range=CharRange(0, 5), surface_form='abcde'),
            InlineRef(char_range=CharRange(3, 8), surface_form='defgh'),
        ),
    )
    with pytest.raises(ValueError, match='overlapping inline refs'):
        render_html(_minimal_docling_doc(blocks=(block,)))


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_table_renders_th_td_and_spans() -> None:
    table = TableBlock(
        id='#/tables/0',
        label='Table 1',
        caption='Relaxometry',
        n_rows=2,
        n_cols=2,
        cells=(
            (TableCell('Tissue', 'th'), TableCell('T1', 'th')),
            (TableCell('Liver', 'td', rowspan=2), TableCell('800', 'td', colspan=1)),
        ),
        provenance=_docling_prov(page=4),
    )
    out = render_html(_minimal_docling_doc(blocks=(table,)))
    assert '<th>Tissue</th>' in out
    assert '<td rowspan="2">Liver</td>' in out
    assert '<caption>Table 1 — Relaxometry</caption>' in out
    # colspan == 1 is the default and must not emit an attribute.
    assert 'colspan="1"' not in out


def test_table_cell_text_is_escaped() -> None:
    table = TableBlock(
        id=None,
        label=None,
        caption=None,
        n_rows=1,
        n_cols=1,
        cells=((TableCell('a < b & c', 'td'),),),
        provenance=_docling_prov(),
    )
    out = render_html(_minimal_docling_doc(blocks=(table,)))
    assert '<td>a &lt; b &amp; c</td>' in out


# ---------------------------------------------------------------------------
# Figures + equations
# ---------------------------------------------------------------------------


def test_figure_placeholder_states_no_image() -> None:
    figure = FigureBlock(
        id='#/pictures/0',
        label='Figure 1',
        caption='A plot.',
        provenance=_docling_prov(page=6),
    )
    out = render_html(_minimal_docling_doc(blocks=(figure,)))
    assert 'Figure 1 — A plot.' in out
    assert 'no image data' in out


def test_equation_mathml_source_is_escaped_verbatim() -> None:
    eq = EquationBlock(
        id='eq1',
        text='T1 = a + b',
        mathml='<math><mi>T1</mi></math>',
        latex=None,
        provenance=Provenance(route='jats'),
    )
    doc = Document(
        route='jats',
        blocks=(eq,),
        references=(),
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=False,
            has_equations=True,
            table_source='publisher',
        ),
    )
    out = render_html(doc)
    assert 'equation (mathml source)' in out
    # Raw source shown as literal text, not interpreted as markup.
    assert '&lt;math&gt;&lt;mi&gt;T1&lt;/mi&gt;&lt;/math&gt;' in out


def test_equation_latex_source_verbatim() -> None:
    eq = EquationBlock(
        id='#/texts/9',
        text='T_1 = 1/R_1',
        mathml=None,
        latex='T_1 = \\frac{1}{R_1}',
        provenance=_docling_prov(page=5),
    )
    out = render_html(_minimal_docling_doc(blocks=(eq,)))
    assert 'equation (latex source)' in out
    assert 'T_1 = \\frac{1}{R_1}' in out


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_references_anchored_with_parsed_fields() -> None:
    ref = Reference(
        id='R1',
        raw_text='Doe J. 2020. A study. JMRI.',
        parsed=ParsedReference(
            authors=('Doe, J.',),
            title='A study',
            source='JMRI',
            year=2020,
            doi='10.1/x',
        ),
    )
    doc = Document(
        route='jats',
        blocks=(),
        references=(ref,),
        completeness=Completeness(
            has_structured_refs=True,
            has_inline_ref_ids=False,
            has_equations=False,
            table_source='publisher',
        ),
    )
    out = render_html(doc)
    assert 'id="ref-R1"' in out
    assert 'Doe J. 2020. A study. JMRI.' in out
    assert 'Doe, J.' in out
    assert '(2020)' in out
    assert 'doi:10.1/x' in out


def test_reference_raw_only_when_no_parsed() -> None:
    doc = _minimal_docling_doc(
        references=(Reference(id='#/texts/40', raw_text='Smith 2019, raw only.'),)
    )
    out = render_html(doc)
    assert 'id="ref-#/texts/40"' in out
    assert 'Smith 2019, raw only.' in out
    # No parsed-fields element is emitted (the CSS class still defines the rule).
    assert 'class="ref-parsed"' not in out


# ---------------------------------------------------------------------------
# Badges, banner, provenance, determinism
# ---------------------------------------------------------------------------


def test_completeness_badges_reflect_flags() -> None:
    out = render_html(_minimal_docling_doc())
    # docling baseline: structured refs + inline ref ids are off (dim).
    assert '<span class="badge off"><span>structured refs</span></span>' in out
    assert '<span class="badge off"><span>inline ref ids</span></span>' in out
    assert '<span class="badge on"><span>route: docling</span></span>' in out
    assert '<span class="badge on"><span>tables: tableformer</span></span>' in out


def test_route_banner_present_on_xml_absent_on_docling() -> None:
    xml_doc = normalize_xml_document(
        {'front': {}, 'sections': [], 'tables': [], 'figures': [], 'references': []},
        route='jats',
    )
    assert 'blocks are ordered by kind' in render_html(xml_doc)
    assert 'blocks are ordered by kind' not in render_html(_minimal_docling_doc())


def test_provenance_docling_shows_page_and_bbox() -> None:
    block = TextBlock(text='x', provenance=_docling_prov(page=7))
    out = render_html(_minimal_docling_doc(blocks=(block,)))
    assert 'docling · p.7 · bbox(50,60,500,80)' in out


def test_provenance_xml_shows_no_geometry() -> None:
    block = TextBlock(text='x', provenance=Provenance(route='elsevier'))
    doc = Document(
        route='elsevier',
        blocks=(block,),
        references=(),
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=False,
            has_equations=False,
            table_source='publisher',
        ),
    )
    out = render_html(doc)
    assert 'elsevier · no page geometry' in out


def test_context_band_includes_doi_and_sha() -> None:
    out = render_html(
        _minimal_docling_doc(),
        context=RenderContext(doi='10.1/abc', source_artifact_sha='ab' * 32),
    )
    assert 'doi: 10.1/abc' in out
    assert f'sha256: {"ab" * 32}' in out


def test_render_is_deterministic() -> None:
    block = TextBlock(
        text='Repeatable [1] content.',
        provenance=_docling_prov(),
        inline_refs=(InlineRef(char_range=CharRange(11, 14), surface_form='[1]'),),
    )
    doc = _minimal_docling_doc(blocks=(block,))
    ctx = RenderContext(doi='10.1/x', source_artifact_sha='cd' * 32)
    assert render_html(doc, context=ctx) == render_html(doc, context=ctx)


def test_empty_document_renders_without_error() -> None:
    doc = Document(
        route='docling',
        blocks=(),
        references=(),
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=False,
            has_equations=False,
            table_source='tableformer',
        ),
        title=None,
        abstract=None,
    )
    out = render_html(doc)
    assert '<!DOCTYPE html>' in out
    assert 'no references recorded' in out
    assert 'untitled' in out


# ---------------------------------------------------------------------------
# Golden snapshots — one per route
# ---------------------------------------------------------------------------


def _golden_jats() -> Document:
    payload = {
        'front': {'title': 'Quantitative T1 mapping', 'abstract': 'A short abstract.'},
        'sections': [
            {
                'id': 's1',
                'title': 'Methods',
                'level': 1,
                'path': ['Methods'],
                'blocks': [
                    {
                        'type': 'paragraph',
                        'text': 'We measured T1 [1] in white matter.',
                        'xrefs': [{'rid': 'R1', 'label': '[1]', 'start': 15, 'end': 18}],
                    },
                    {
                        'type': 'equation',
                        'id': 'eq1',
                        'text': 'T1 = a + b',
                        'mathml': '<math><mi>T1</mi><mo>=</mo><mi>a</mi></math>',
                    },
                ],
            }
        ],
        'tables': [
            {
                'id': 'T1',
                'label': 'Table 1',
                'caption': 'Relaxometry values.',
                'section_path': ['Methods'],
                'n_rows': 2,
                'n_cols': 2,
                'cells': [
                    [
                        {'text': 'Tissue', 'type': 'th', 'rowspan': 1, 'colspan': 1},
                        {'text': 'T1 (ms)', 'type': 'th', 'rowspan': 1, 'colspan': 1},
                    ],
                    [
                        {'text': 'WM', 'type': 'td', 'rowspan': 1, 'colspan': 1},
                        {'text': '800', 'type': 'td', 'rowspan': 1, 'colspan': 1},
                    ],
                ],
            }
        ],
        'figures': [
            {'id': 'F1', 'label': 'Figure 1', 'caption': 'A map.', 'section_path': ['Methods']}
        ],
        'references': [
            {
                'id': 'R1',
                'raw_text': 'Doe J. 2020. A study. JMRI.',
                'authors': ['Doe, J.'],
                'title': 'A study',
                'source': 'JMRI',
                'year': '2020',
                'doi': '10.1/x',
            }
        ],
    }
    return normalize_xml_document(payload, route='jats')


def _golden_elsevier() -> Document:
    payload = {
        'front': {'title': 'Elsevier T2 study', 'abstract': 'Another abstract.'},
        'sections': [
            {
                'id': 's1',
                'title': 'Results',
                'level': 1,
                'path': ['Results'],
                'blocks': [
                    {
                        'type': 'paragraph',
                        'text': 'T2 differed [1] across regions.',
                        'xrefs': [{'rid': 'b1', 'label': '[1]', 'start': 12, 'end': 15}],
                    }
                ],
            }
        ],
        'tables': [
            {
                'id': 'tbl1',
                'label': 'Table 1',
                'caption': 'CALS table.',
                'section_path': ['Results'],
                'n_rows': 1,
                'n_cols': 2,
                'cells': [
                    [
                        {'text': 'Region', 'type': 'entry', 'rowspan': 1, 'colspan': 1},
                        {'text': 'T2', 'type': 'entry', 'rowspan': 1, 'colspan': 1},
                    ]
                ],
            }
        ],
        'figures': [],
        'references': [
            {'id': 'b1', 'raw_text': 'Roe A. 2018. Prior work. MRM.', 'authors': ['Roe, A.']}
        ],
    }
    return normalize_xml_document(payload, route='elsevier')


def _golden_docling() -> Document:
    text = TextBlock(
        text='Liver T1 was 800 ms [12] & rising.',
        provenance=Provenance(
            route='docling',
            page=3,
            bbox=BBox(x0=50.0, y0=60.0, x1=500.0, y1=80.0),
            page_char_range=CharRange(start=0, end=34),
        ),
        section_path=('Methods', 'MRI'),
        inline_refs=(InlineRef(char_range=CharRange(20, 24), surface_form='[12]'),),
    )
    table = TableBlock(
        id='#/tables/0',
        label=None,
        caption='Table 1. Relaxometry',
        n_rows=2,
        n_cols=2,
        cells=(
            (TableCell('Tissue', 'th'), TableCell('T1', 'th')),
            (TableCell('Liver', 'td', rowspan=2), TableCell('800', 'td')),
        ),
        provenance=Provenance(
            route='docling',
            page=4,
            bbox=BBox(x0=40.0, y0=100.0, x1=520.0, y1=300.0),
            page_char_range=CharRange(start=0, end=10),
        ),
        section_path=('Methods',),
    )
    eq = EquationBlock(
        id='#/texts/9',
        text='T_1 = 1/R_1',
        mathml=None,
        latex='T_1 = \\frac{1}{R_1}',
        provenance=Provenance(
            route='docling',
            page=5,
            bbox=BBox(x0=60.0, y0=200.0, x1=300.0, y1=220.0),
            page_char_range=CharRange(start=0, end=11),
        ),
        section_path=('Methods',),
    )
    return Document(
        route='docling',
        blocks=(text, table, eq),
        references=(Reference(id='#/texts/40', raw_text='Smith J. 2019. A study.'),),
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=False,
            has_equations=True,
            table_source='tableformer',
        ),
        title='A T1 mapping study',
        abstract=None,
    )


_GOLDEN_CASES = {
    'jats': (_golden_jats, RenderContext(doi='10.1000/jats.x', source_artifact_sha='aa' * 32)),
    'elsevier': (
        _golden_elsevier,
        RenderContext(doi='10.1000/els.x', source_artifact_sha='bb' * 32),
    ),
    'docling': (
        _golden_docling,
        RenderContext(doi='10.1002/wiley.x', source_artifact_sha='cc' * 32),
    ),
}


@pytest.mark.parametrize('name', sorted(_GOLDEN_CASES))
def test_golden_snapshot(name: str) -> None:
    builder, context = _GOLDEN_CASES[name]
    rendered = render_html(builder(), context=context)
    golden_path = GOLDEN_DIR / f'{name}.html'

    if os.environ.get(_REGEN_ENV):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(rendered, encoding='utf-8')
        pytest.skip(f'regenerated golden {golden_path.name}')

    assert golden_path.exists(), f'missing golden {golden_path}; regenerate with {_REGEN_ENV}=1'
    assert rendered == golden_path.read_text(encoding='utf-8'), (
        f'render drifted from {golden_path.name}; if intentional, regenerate with {_REGEN_ENV}=1'
    )
