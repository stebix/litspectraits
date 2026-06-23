"""Static-HTML renderer for the normalised :class:`Document` (human inspection).

A single pure function, :func:`render_html`, turns a :class:`Document`
into one self-contained HTML string — inline ``<style>``, zero external
assets, zero JavaScript — so it opens offline and diffs cleanly. The
function is the renderer ``docs/normalized-documents-discussion.md``
§2.4(b) reserves for golden-HTML snapshot tests; the same output backs
both the ``litspectraits show-document`` command and that regression
tripwire.

Design decisions (``docs/rendering-mvp-plan.md`` §0)
----------------------------------------------------
- **Faithful projection.** Blocks render in stored order — including the
  XML route's by-kind grouping (paragraphs → tables → figures from
  :mod:`litspectraits.normalize.xml_adapter`). The render never
  reconstructs reading order; a one-line banner flags the by-kind order
  on XML documents so it does not read as a bug.
- **Raw math.** ``mathml`` / ``latex`` render as escaped source text in a
  ``<pre>``; no client-side typesetting, so the output stays deterministic.
- **Honest about route asymmetry.** The route + :class:`Completeness`
  flags render as badges with a legend: a dim badge means "absent because
  this route cannot produce it", never "bug".

Determinism: the output is a total function of ``(doc, context)``. No
wall-clock, no randomness, no dict-iteration order — every loop walks an
ordered tuple. This is load-bearing for the golden snapshots.
"""

import html
from typing import Final

from attrs import frozen

from litspectraits.normalize.models import (
    Block,
    Document,
    EquationBlock,
    FigureBlock,
    InlineRef,
    Provenance,
    Reference,
    TableBlock,
    TableCell,
    TextBlock,
)


@frozen
class RenderContext:
    """Optional stable identifiers shown in the document header band.

    Both fields are deterministic given the artifact, so including them
    in the rendered HTML keeps the golden snapshot stable. Omit the
    context entirely (the :func:`render_html` default) for a doc-only
    render.

    Attributes
    ----------
    doi : str | None
        The DOI the document was ingested under.
    source_artifact_sha : str | None
        SHA-256 of the source artifact (the ``normalized/<sha>/`` key).
    """

    doi: str | None = None
    source_artifact_sha: str | None = None


_STYLE: Final = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, system-ui, sans-serif; line-height: 1.5;
  max-width: 50rem; margin: 2rem auto; padding: 0 1rem; }
header { border-bottom: 2px solid currentColor; margin-bottom: 1.5rem;
  padding-bottom: 1rem; }
h1 { font-size: 1.5rem; margin: 0 0 .5rem; }
.abstract { font-style: italic; opacity: .85; }
.badges { display: flex; flex-wrap: wrap; gap: .4rem; margin: .6rem 0; }
.badge { font-size: .75rem; padding: .12rem .5rem; border-radius: .8rem;
  border: 1px solid currentColor; white-space: nowrap; }
.badge.on { background: currentColor; }
.badge.on > span { mix-blend-mode: difference; color: #fff; }
.badge.off { opacity: .4; }
.legend { font-size: .75rem; opacity: .7; margin-top: .4rem; }
.ctx { font-size: .8rem; opacity: .7; font-family: ui-monospace, monospace; }
.banner { font-size: .8rem; border-left: 3px solid currentColor;
  padding: .3rem .6rem; opacity: .8; margin: 1rem 0; }
.block { margin: 1.1rem 0; }
.section-path { font-size: .7rem; text-transform: uppercase;
  letter-spacing: .04em; opacity: .55; margin-bottom: .15rem; }
.prov { font-size: .7rem; opacity: .55; font-family: ui-monospace, monospace;
  margin-top: .15rem; }
.inline-ref { text-decoration: none; }
mark.inline-ref { background: rgba(255, 220, 0, .35); padding: 0 .1rem; }
a.inline-ref { background: rgba(80, 160, 255, .25); padding: 0 .1rem; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
caption { caption-side: top; text-align: left; font-weight: 600;
  margin-bottom: .3rem; }
th, td { border: 1px solid currentColor; padding: .25rem .5rem;
  text-align: left; vertical-align: top; }
th { background: rgba(127, 127, 127, .15); }
figure.placeholder { border: 1px dashed currentColor; padding: 1rem;
  margin: 0; opacity: .8; }
.noimg { font-size: .75rem; opacity: .6; }
pre.math { background: rgba(127, 127, 127, .12); padding: .6rem;
  overflow-x: auto; font-size: .85rem; }
.math-tag { font-size: .7rem; opacity: .6; }
ol.references { padding-left: 1.4rem; font-size: .85rem; }
ol.references li { margin: .4rem 0; }
.ref-raw { opacity: .9; }
.ref-parsed { font-size: .78rem; opacity: .65; margin-top: .1rem; }
.empty { opacity: .5; font-style: italic; }
""".strip()


def render_html(doc: Document, *, context: RenderContext | None = None) -> str:
    """Render a normalised :class:`Document` as one self-contained HTML string.

    Parameters
    ----------
    doc : Document
        The normalised document to render, from either adapter.
    context : RenderContext | None, optional
        Stable identifiers (DOI, source artifact sha) shown in the header
        band. ``None`` renders the document alone.

    Returns
    -------
    str
        A complete ``<!DOCTYPE html>`` document. Deterministic: the same
        ``(doc, context)`` always produces byte-identical output.

    Raises
    ------
    ValueError
        A :class:`TextBlock` carries overlapping :class:`InlineRef`
        char ranges — the splicer cannot place both and refuses to
        silently mis-render (see :func:`_render_inline_text`).
    """
    parts: list[str] = [
        '<!DOCTYPE html>',
        '<html lang="en">',
        '<head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{_esc(doc.title or "untitled document")}</title>',
        f'<style>{_STYLE}</style>',
        '</head>',
        '<body>',
        _render_header(doc, context),
        _render_route_banner(doc),
    ]
    parts.extend(_render_block(block) for block in doc.blocks)
    parts.append(_render_references(doc.references))
    parts.append('</body>')
    parts.append('</html>')
    return '\n'.join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def _render_header(doc: Document, context: RenderContext | None) -> str:
    rows: list[str] = ['<header>']
    title_html = _esc(doc.title) if doc.title else '<span class="empty">untitled</span>'
    rows.append(f'<h1>{title_html}</h1>')
    if context is not None and (context.doi or context.source_artifact_sha):
        ctx_bits = []
        if context.doi:
            ctx_bits.append(f'doi: {_esc(context.doi)}')
        if context.source_artifact_sha:
            ctx_bits.append(f'sha256: {_esc(context.source_artifact_sha)}')
        rows.append(f'<div class="ctx">{" · ".join(ctx_bits)}</div>')
    rows.append(_render_badges(doc))
    if doc.abstract:
        rows.append(f'<p class="abstract">{_esc(doc.abstract)}</p>')
    else:
        rows.append('<p class="abstract empty">no abstract recorded for this route</p>')
    rows.append(_render_summary(doc))
    rows.append('</header>')
    return '\n'.join(rows)


def _render_badges(doc: Document) -> str:
    """Render the route + :class:`Completeness` flags as pill badges.

    A populated flag renders solid (``on``); an absent one renders dim
    (``off``). The legend spells out that dim means "absent by route",
    so a docling document's ``refs: raw`` does not read as a defect.
    """
    c = doc.completeness
    badges = [
        _badge(f'route: {doc.route}', on=True),
        _badge('structured refs', on=c.has_structured_refs),
        _badge('inline ref ids', on=c.has_inline_ref_ids),
        _badge('equations', on=c.has_equations),
        _badge(f'tables: {c.table_source}', on=True),
    ]
    return (
        '<div class="badges">'
        + ''.join(badges)
        + '</div>'
        + '<div class="legend">Solid = present in this document. '
        'Dim = absent because this route does not produce it (not a bug).</div>'
    )


def _badge(label: str, *, on: bool) -> str:
    cls = 'on' if on else 'off'
    return f'<span class="badge {cls}"><span>{_esc(label)}</span></span>'


def _render_summary(doc: Document) -> str:
    counts = {'text': 0, 'table': 0, 'figure': 0, 'equation': 0}
    for block in doc.blocks:
        counts[block.type] += 1
    summary = (
        f'{counts["text"]} text · {counts["table"]} tables · '
        f'{counts["figure"]} figures · {counts["equation"]} equations · '
        f'{len(doc.references)} references'
    )
    return f'<div class="legend">{_esc(summary)}</div>'


def _render_route_banner(doc: Document) -> str:
    """Flag the XML route's by-kind block order so it does not read as a bug."""
    if doc.route in ('jats', 'elsevier'):
        return (
            '<div class="banner">XML route: blocks are ordered by kind '
            '(all paragraphs in section order, then all tables, then all '
            'figures), not source reading order.</div>'
        )
    return ''


# ---------------------------------------------------------------------------
# Block dispatch
# ---------------------------------------------------------------------------


def _render_block(block: Block) -> str:
    if isinstance(block, TextBlock):
        body = _render_text_block(block)
    elif isinstance(block, TableBlock):
        body = _render_table_block(block)
    elif isinstance(block, FigureBlock):
        body = _render_figure_block(block)
    else:
        # Exhaustive over the Block union — pyright narrows to EquationBlock.
        body = _render_equation_block(block)
    return (
        '<div class="block">'
        + _render_section_path(block.section_path)
        + body
        + _render_provenance(block.provenance)
        + '</div>'
    )


def _render_section_path(section_path: tuple[str | None, ...]) -> str:
    crumbs = ' › '.join(_esc(part) for part in section_path if part)  # noqa: RUF001
    if not crumbs:
        return ''
    return f'<div class="section-path">{crumbs}</div>'


def _render_text_block(block: TextBlock) -> str:
    return f'<p>{_render_inline_text(block.text, block.inline_refs)}</p>'


def _render_table_block(block: TableBlock) -> str:
    head_bits = []
    if block.label:
        head_bits.append(_esc(block.label))
    if block.caption:
        head_bits.append(_esc(block.caption))
    caption = f'<caption>{" — ".join(head_bits)}</caption>' if head_bits else ''
    rows = []
    for row in block.cells:
        cells = ''.join(_render_cell(cell) for cell in row)
        rows.append(f'<tr>{cells}</tr>')
    dims = f'<div class="prov">{block.n_rows}×{block.n_cols} grid</div>'  # noqa: RUF001
    return f'<table>{caption}{"".join(rows)}</table>{dims}'


def _render_cell(cell: TableCell) -> str:
    tag = 'th' if cell.kind == 'th' else 'td'
    attrs = ''
    if cell.rowspan != 1:
        attrs += f' rowspan="{cell.rowspan}"'
    if cell.colspan != 1:
        attrs += f' colspan="{cell.colspan}"'
    return f'<{tag}{attrs}>{_esc(cell.text)}</{tag}>'


def _render_figure_block(block: FigureBlock) -> str:
    head_bits = []
    if block.label:
        head_bits.append(_esc(block.label))
    if block.caption:
        head_bits.append(_esc(block.caption))
    caption = ' — '.join(head_bits) if head_bits else '<span class="empty">untitled figure</span>'
    return (
        '<figure class="placeholder">'
        f'<figcaption>{caption}</figcaption>'
        '<div class="noimg">no image data (figures never carry pixels — by design)</div>'
        '</figure>'
    )


def _render_equation_block(block: EquationBlock) -> str:
    if block.mathml is not None:
        tag, source = 'mathml', block.mathml
    else:
        # EquationBlock guarantees exactly one of mathml / latex.
        tag, source = 'latex', block.latex or ''
    label = f'{_esc(block.id)} · ' if block.id else ''
    return (
        f'<div class="math-tag">{label}equation ({tag} source)</div>'
        f'<pre class="math">{_esc(source)}</pre>'
    )


# ---------------------------------------------------------------------------
# Inline-ref splicing — the one place offsets and escaping interact
# ---------------------------------------------------------------------------


def _render_inline_text(text: str, inline_refs: tuple[InlineRef, ...]) -> str:
    """Splice :class:`InlineRef` highlights into ``text``, escaping as we go.

    The char ranges index into the *raw* ``text``, so the splice walks
    the raw string and HTML-escapes each segment lazily. Escaping the
    whole string first would shift every offset (``&`` → ``&amp;``),
    breaking the anchor the verbatim gate depends on.

    Resolved refs (``ref_id`` set) render as an in-page ``<a>`` link to
    the reference entry; unresolved refs (``ref_id is None``, normal on
    the docling route) render as a plain ``<mark>`` highlight.

    Raises
    ------
    ValueError
        Two refs overlap. The splicer processes them in ascending
        ``start`` order and refuses any whose ``start`` precedes the
        previous ``end`` — placing both would corrupt the output, so we
        fail loudly rather than silently drop or nest one.
    """
    if not inline_refs:
        return _esc(text)
    ordered = sorted(inline_refs, key=lambda r: (r.char_range.start, r.char_range.end))
    out: list[str] = []
    cursor = 0
    for ref in ordered:
        start, end = ref.char_range.start, ref.char_range.end
        if start < cursor:
            raise ValueError(
                f'overlapping inline refs at char {start} (previous ref ended at '
                f'{cursor}); cannot render {ref.surface_form!r} unambiguously'
            )
        out.append(_esc(text[cursor:start]))
        out.append(_render_one_ref(text[start:end], ref))
        cursor = end
    out.append(_esc(text[cursor:]))
    return ''.join(out)


def _render_one_ref(surface: str, ref: InlineRef) -> str:
    inner = _esc(surface)
    if ref.ref_id is not None:
        href = _esc(f'ref-{ref.ref_id}')
        return f'<a class="inline-ref" href="#{href}">{inner}</a>'
    return f'<mark class="inline-ref">{inner}</mark>'


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def _render_references(refs: tuple[Reference, ...]) -> str:
    if not refs:
        return '<h2>References</h2><p class="empty">no references recorded</p>'
    items = []
    for ref in refs:
        anchor = _esc(f'ref-{ref.id}')
        raw = (
            f'<div class="ref-raw">{_esc(ref.raw_text)}</div>'
            if ref.raw_text
            else '<div class="ref-raw empty">(no raw text)</div>'
        )
        parsed = _render_parsed_reference(ref)
        items.append(f'<li id="{anchor}">{raw}{parsed}</li>')
    return f'<h2>References</h2><ol class="references">{"".join(items)}</ol>'


def _render_parsed_reference(ref: Reference) -> str:
    """Render the structured fields of a parsed reference, when present."""
    if ref.parsed is None:
        return ''
    p = ref.parsed
    bits: list[str] = []
    if p.authors:
        bits.append(_esc('; '.join(p.authors)))
    if p.year is not None:
        bits.append(f'({p.year})')
    if p.title:
        bits.append(_esc(p.title))
    if p.source:
        bits.append(_esc(p.source))
    if p.doi:
        bits.append(f'doi:{_esc(p.doi)}')
    if not bits:
        return ''
    return f'<div class="ref-parsed">{" · ".join(bits)}</div>'


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _render_provenance(prov: Provenance) -> str:
    """Render a block's source provenance, honest about route asymmetry."""
    if prov.route == 'docling':
        bits = [f'p.{prov.page}']
        if prov.bbox is not None:
            b = prov.bbox
            bits.append(f'bbox({b.x0:.0f},{b.y0:.0f},{b.x1:.0f},{b.y1:.0f})')
        return f'<div class="prov">docling · {" · ".join(bits)}</div>'
    # XML routes — no page geometry; surface xpath if it ever lands.
    detail = _esc(prov.xpath) if prov.xpath else 'no page geometry'
    return f'<div class="prov">{_esc(prov.route)} · {detail}</div>'


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def _esc(value: str) -> str:
    """HTML-escape text for both element and attribute contexts."""
    return html.escape(value, quote=True)


__all__ = ['RenderContext', 'render_html']
