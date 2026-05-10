"""Publisher-specific TDM retrievers (``docs/overview-v3.md`` §7).

One retriever per publisher. Each takes a normalized DOI plus its
:class:`~litspectraits.manifest.CrossRefMetadata` and returns a
:class:`~litspectraits.manifest.RetrievePayload` on success, or raises an
:class:`~litspectraits.errors.IngestError` subclass on any failure (no
fall-through). Dispatch from :class:`~litspectraits.manifest.Publisher`
to a retriever instance lives in :mod:`litspectraits.retrievers.dispatch`.
"""

from litspectraits.retrievers.base import Retriever

__all__ = ['Retriever']
