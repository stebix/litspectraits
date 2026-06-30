"""MinerU → :class:`~litspectraits.normalize.Document` adapter (contract stub).

Sibling of :mod:`litspectraits.normalize.docling_adapter` and
:mod:`litspectraits.normalize.xml_adapter`: consumes the verbatim MinerU
``middle.json`` payload that the MinerU extractor writes to
``documents/sha256/<aa>/<sha>/document.json`` and produces a
:class:`Document` with ``route='mineru'``.

**This is a stub.** The schema relaxation it depends on has landed
(``Route`` now admits ``'mineru'``; :class:`Provenance` grades geometry;
:class:`~litspectraits.normalize.models.TextBlock` carries
``inline_math``), but the walk itself is deferred until the
``[mineru]`` extra resolves under the project torch pin and a real
``middle.json`` sample is available to pin the field names and write the
Q1-faithfulness fixtures. See ``docs/mineru-backend-spec.md`` §4.

The contract this stub will implement
-------------------------------------
Walk ``pdf_info[*].para_blocks`` in reading order, mapping MinerU block
types to :class:`~litspectraits.normalize.models.Block` variants:

==================================  ==============================================
MinerU ``middle.json`` type         → ``Block`` / action
==================================  ==============================================
``text`` / ``list`` / ``index``     ``TextBlock``; ``inline_equation`` spans →
                                    ``InlineMath`` with offsets into ``text``
``title``                           section-path push (``text_level`` → depth),
                                    mirroring docling's ``SectionHeaderItem``
``interline_equation``              ``EquationBlock(latex=…, mathml=None)``
``image`` / ``chart``               ``FigureBlock`` (caption from
                                    ``image_caption`` / ``chart_caption``)
``table``                           ``TableBlock``; parse ``table_body`` **HTML**
                                    → ``cells`` via ``lxml.html`` (expand
                                    row/colspans to a rectangular grid)
``discarded_blocks``                dropped (headers / footers / page numbers)
==================================  ==============================================

Provenance per block: ``Provenance(route='mineru', page=page_idx,
bbox=…, geometry_fidelity=…)``. ``middle.json`` bboxes are in **PDF
points** (forwarded verbatim into :class:`~litspectraits.normalize.models.BBox`);
the ``content_list.json`` 0-1000 form is NOT used here. ``geometry_fidelity``
is ``'exact'`` when MinerU ran its ``pipeline`` engine (text-layer) and
``'approximate'`` for the ``vlm`` engine — the adapter learns which from
the extractor's ``config_view`` (threaded in, or read from the
``middle.json`` top-level ``_backend`` field).

Faithfulness discipline (Q1): fail loud on a content block carrying
neither ``page_idx`` nor ``bbox`` rather than silently emit
``bbox=None``; cover the rowspan/colspan and points-vs-0-1000 traps with
adversarial fixtures, same bar as the docling adapter.
"""

from typing import Any

from litspectraits.normalize.models import Document


def normalize_mineru_document(document: dict[str, Any]) -> Document:
    """Convert a MinerU ``middle.json`` payload to a :class:`Document`.

    Parameters
    ----------
    document : dict
        The verbatim MinerU ``*_middle.json`` payload — the on-disk shape
        of ``documents/sha256/<aa>/<sha>/document.json`` produced by the
        MinerU extractor (``docs/mineru-backend-spec.md`` §3).

    Returns
    -------
    Document
        Frozen :class:`Document` with ``route='mineru'``.

    Raises
    ------
    NotImplementedError
        Always, for now — see the module docstring. The contract is
        fixed; the walk is the remaining work in
        ``docs/mineru-backend-spec.md`` §10 step 4.
    """
    raise NotImplementedError(
        'normalize_mineru_document is a contract stub; the middle.json walk is '
        'pending a resolvable [mineru] extra and a real sample to pin field names '
        '(docs/mineru-backend-spec.md §4, §10 step 4)'
    )


__all__ = ['normalize_mineru_document']
