"""MinerU PDF extractor (contract stub; ``docs/mineru-backend-spec.md`` §3).

The second PDF backend alongside :mod:`litspectraits.extract.pdf`
(docling). Same place in the pipeline — ``Format.PDF`` artifact → verbatim
native dict on disk at ``documents/sha256/<aa>/<sha>/document.json`` — but
parsed by MinerU instead of docling, selected via ``--backend mineru``
(``docs/mineru-backend-spec.md`` §1, §8). The downstream
:func:`litspectraits.normalize.normalize_mineru_document` turns that dict
into a :class:`~litspectraits.normalize.Document` with ``route='mineru'``.

**This is a stub.** It is not yet wired into
:mod:`litspectraits.extract._dispatch` (the canonical PDF leg still goes
straight to docling); wiring the ``--backend`` selector + ``backend_id``
into the extract ``meta.json`` is the next step
(``docs/mineru-backend-spec.md`` §10 steps 2, 6). The error taxonomy it
will raise (:class:`~litspectraits.errors.MineruImportError`,
:class:`~litspectraits.errors.MineruConversionError`) and the exit-code /
hint wiring in :mod:`litspectraits.cli` are already in place.

Planned shape (mirrors :func:`litspectraits.extract.pdf.extract_pdf`'s
six stages):

1. lazy-import ``mineru`` inside the function body, guarded →
   :class:`~litspectraits.errors.MineruImportError` (mirror
   ``extract.pdf._load_docling``);
2. run ``mineru.cli.common.do_parse`` into a scratch dir under
   ``store.tmp_dir`` via ``asyncio.to_thread`` (MinerU is file-output
   oriented — it writes ``*_middle.json`` rather than returning in
   memory);
3. read back ``*_middle.json`` (points-scale, hierarchical — preferred
   over ``content_list.json``) as the **verbatim** native dict and
   persist it unchanged (same discipline as ``_serialize_document``);
4. structural-floor checks reused from the docling path
   (:class:`~litspectraits.errors.EmptyDocumentError` /
   :class:`~litspectraits.errors.ParseDegradedError`); MinerU has no
   ``ConversionStatus`` enum, so "empty ``pdf_info``" →
   :class:`~litspectraits.errors.MineruConversionError`;
5. atomic commit (``_io.atomic_write``, integrity-check-on-rerun,
   ``--reextract`` to overwrite) with ``backend_id='mineru'`` +
   ``config_view`` (``engine``, ``formula``, ``table``, ``lang``,
   ``mineru_version``) in ``meta.json``.
"""

from pathlib import Path

from litspectraits.manifest import AcquisitionRecord, ExtractRecord
from litspectraits.store import ArtifactStore


async def extract_mineru(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
    model_cache_dir: Path | None = None,
) -> ExtractRecord:
    """Extract a PDF artifact with MinerU into the canonical document tree.

    Signature mirrors :func:`litspectraits.extract.pdf.extract_pdf` so the
    backend dispatch (``docs/mineru-backend-spec.md`` §1) can treat the two
    PDF backends interchangeably.

    Raises
    ------
    NotImplementedError
        Always, for now — see the module docstring. The contract is
        fixed; the ``do_parse`` integration is the remaining work
        (``docs/mineru-backend-spec.md`` §10 step 3).
    """
    raise NotImplementedError(
        'extract_mineru is a contract stub; the do_parse integration is pending a '
        'resolvable [mineru] extra under the project torch pin '
        '(docs/mineru-backend-spec.md §3, §9, §10 step 3)'
    )


__all__ = ['extract_mineru']
