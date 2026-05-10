"""Publisher → :class:`Retriever` dispatch table (``docs/overview-v3.md`` §7.4).

Trivial three-entry registry. The orchestrator (``ingest.py``, Step 7)
calls :func:`retriever_for` after :func:`publisher_for_doi` resolves
which publisher owns the DOI. Adding a fourth publisher is a one-line
PR here plus a new module under :mod:`litspectraits.retrievers`.

Retrievers are constructed with their default rate-limit ceiling
(sourced from §8). Per-call rate overrides happen in
:class:`~litspectraits.retrievers._ratelimit.RateLimiter` via the
``Settings.rate_limit_*`` env values, which the orchestrator reads when
passing settings into :meth:`Retriever.fetch`.

Construction is lazy: each retriever is built on first request and
cached for the life of the process. This keeps the SDK lazy-imports
(Wiley, Springer) deferred to the first fetch for that publisher
rather than firing at module import.
"""

from collections.abc import Callable
from typing import Final

from litspectraits.errors import UnsupportedPublisherError
from litspectraits.manifest import Publisher
from litspectraits.retrievers.base import Retriever
from litspectraits.retrievers.elsevier import ElsevierRetriever
from litspectraits.retrievers.springer import SpringerRetriever
from litspectraits.retrievers.wiley import WileyRetriever

_BUILDERS: Final[dict[Publisher, Callable[[], Retriever]]] = {
    Publisher.WILEY: WileyRetriever,
    Publisher.ELSEVIER: ElsevierRetriever,
    Publisher.SPRINGER_NATURE: SpringerRetriever,
}

_CACHE: dict[Publisher, Retriever] = {}


def retriever_for(publisher: Publisher) -> Retriever:
    """Return a (cached) retriever for ``publisher``.

    Raises
    ------
    UnsupportedPublisherError
        If the dispatch table has no entry for ``publisher``. This should
        never fire for a value produced by
        :func:`litspectraits.metadata.publisher_for_doi` — the two tables
        are kept in sync — but the guard is here to make a future
        :class:`Publisher` enum extension a loud failure rather than a
        silent ``KeyError``.
    """
    cached = _CACHE.get(publisher)
    if cached is not None:
        return cached
    builder = _BUILDERS.get(publisher)
    if builder is None:
        raise UnsupportedPublisherError(
            doi='',
            publisher=publisher.value,
            hint='no retriever registered for this Publisher value',
        )
    instance = builder()
    _CACHE[publisher] = instance
    return instance


def reset_cache() -> None:
    """Drop cached retriever instances (test-only).

    The dispatch table itself is immutable; this exists so tests that
    monkeypatch a publisher's SDK can force a fresh construction in the
    next ``retriever_for`` call.
    """
    _CACHE.clear()
