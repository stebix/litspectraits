"""Canonical vocabulary of PDF-backend identifiers (``docs/mineru-backend-spec.md`` §1).

The *backend id* is the discriminator that flows extract → normalize
through ``meta.json``'s ``backend_id`` field — **not** a new
:class:`~litspectraits.manifest.Format`. MinerU output is still
:attr:`~litspectraits.manifest.Format.PDF`; it is a different *backend*,
not a different *format*. Keying backend identity here (and in the meta)
rather than on ``Format`` is what lets ``normalize`` pick the matching
adapter without a parser ever being inferred from the artifact bytes.

Only backends that are actually wired are listed. ``'docling-vlm'`` (the
spec's third id — docling's VLM pipeline) is an additive follow-on: add
the literal and its extractor branch together so the vocabulary never
advertises a backend the dispatcher cannot serve.
"""

from typing import Final, Literal

PdfBackendId = Literal['docling-standard', 'mineru']
"""Identifier for a wired PDF parsing backend, recorded in extract ``meta.json``."""

DOCLING_STANDARD: Final[PdfBackendId] = 'docling-standard'
"""The docling text-layer pipeline — the default, exact-geometry backend."""

MINERU: Final[PdfBackendId] = 'mineru'
"""The MinerU ``pipeline`` engine (``docs/mineru-backend-spec.md`` §3)."""

DEFAULT_PDF_BACKEND: Final[PdfBackendId] = DOCLING_STANDARD
"""Backend used when neither ``--backend`` nor ``LITSPECTRAITS_PDF_BACKEND`` is set."""

PDF_BACKEND_IDS: Final[tuple[PdfBackendId, ...]] = (DOCLING_STANDARD, MINERU)
"""All wired PDF backend ids, for CLI choice validation and dispatch."""


__all__ = [
    'DEFAULT_PDF_BACKEND',
    'DOCLING_STANDARD',
    'MINERU',
    'PDF_BACKEND_IDS',
    'PdfBackendId',
]
