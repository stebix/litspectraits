"""Normalised ``Document`` model (E0.5).

The publisher-agnostic representation built downstream of the per-format
extractors. Two adapters populate it:

- :mod:`litspectraits.normalize.xml_adapter` (E0.5a) — converts the
  JATS / Elsevier publisher dict shape (:mod:`litspectraits.extract.jats`,
  :mod:`litspectraits.extract.elsevier`) into a :class:`Document`.
- :mod:`litspectraits.normalize.docling_adapter` (E0.5b, future) —
  converts a docling ``DoclingDocument`` into a :class:`Document`.

See ``docs/normalized-documents-discussion.md`` for the design;
``docs/agentic-buildout-sketch.md`` §1.5 for the schema commitments;
``CLAUDE.md`` for the cattrs-from-day-one rule.

Schema commitments (must read before changing this module)
----------------------------------------------------------
- One :class:`Document`, one :class:`Block` tagged union — the *fields*
  bifurcate by route, not the types.
- Every :class:`Block` carries a :class:`Provenance` with
  ``route: Route`` as a mandatory discriminator; ``None`` on a
  route-irrelevant field reads as "expected", not "bug".
- :class:`InlineRef` ``ref_id`` is ``None`` on the docling route until a
  marker-match pass runs; ``ref_id_source`` records which pass
  populated it.
- :class:`Reference` is the raw / parsed / resolved split — JATS +
  Elsevier yield ``parsed``; the docling route emits ``raw_text`` only
  until a citation-parsing pass runs.
"""

from litspectraits.normalize.diff import (
    DocumentComparison,
    DualFormatComparison,
    DualFormatResult,
    DualFormatSkip,
    SkipReason,
    TableComparison,
    compare_documents,
    compare_dual_format_dois,
    compare_reports,
    format_comparison_report,
    format_dual_format_report,
)
from litspectraits.normalize.docling_adapter import normalize_docling_document
from litspectraits.normalize.hooks import converter
from litspectraits.normalize.mineru_adapter import normalize_mineru_document
from litspectraits.normalize.models import (
    BBox,
    Block,
    CharRange,
    Completeness,
    Document,
    EquationBlock,
    FigureBlock,
    GeometryFidelity,
    InlineMath,
    InlineRef,
    ParsedReference,
    Provenance,
    Reference,
    RefIdSource,
    ResolvedReference,
    Route,
    TableBlock,
    TableCell,
    TextBlock,
)
from litspectraits.normalize.persistence import (
    NORMALIZER_VERSION,
    WHITESPACE_RULE,
    NormalizedMeta,
    commit_normalized_document,
    load_normalized_document,
    load_normalized_meta,
)
from litspectraits.normalize.render import RenderContext, render_html
from litspectraits.normalize.xml_adapter import XmlRoute, normalize_xml_document

__all__ = [
    'NORMALIZER_VERSION',
    'WHITESPACE_RULE',
    'BBox',
    'Block',
    'CharRange',
    'Completeness',
    'Document',
    'DocumentComparison',
    'DualFormatComparison',
    'DualFormatResult',
    'DualFormatSkip',
    'EquationBlock',
    'FigureBlock',
    'GeometryFidelity',
    'InlineMath',
    'InlineRef',
    'NormalizedMeta',
    'ParsedReference',
    'Provenance',
    'RefIdSource',
    'Reference',
    'RenderContext',
    'ResolvedReference',
    'Route',
    'SkipReason',
    'TableBlock',
    'TableCell',
    'TableComparison',
    'TextBlock',
    'XmlRoute',
    'commit_normalized_document',
    'compare_documents',
    'compare_dual_format_dois',
    'compare_reports',
    'converter',
    'format_comparison_report',
    'format_dual_format_report',
    'load_normalized_document',
    'load_normalized_meta',
    'normalize_docling_document',
    'normalize_mineru_document',
    'normalize_xml_document',
    'render_html',
]
