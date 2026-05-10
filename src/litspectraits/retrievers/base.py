"""Retriever protocol (``docs/overview-v3.md`` §7).

A retriever is a small async object that knows how to talk to one
publisher's TDM endpoint. It must:

1. Acquire its per-publisher rate-limit token before making the network
   call (see :mod:`litspectraits.retrievers._ratelimit`).
2. Validate that the credential it needs is configured; raise
   :class:`~litspectraits.errors.MissingCredentialError` early when the
   credential is absent **and** no IP-based fallback applies for that
   publisher (Wiley supports IP-only auth; Springer and Elsevier do not).
3. Issue exactly one fetch attempt per call (with bounded in-retriever
   429 retry via ``tenacity``).
4. Translate publisher-side / SDK exceptions into the
   :class:`~litspectraits.errors.IngestError` taxonomy. Anything that is
   not a typed :class:`~litspectraits.errors.IngestError` after
   :meth:`fetch` returns is a bug in the retriever.
5. Stage the bytes inside ``tmp_dir`` (a path supplied by the orchestrator,
   typically ``ArtifactStore.tmp_dir``) and return a fully-populated
   :class:`~litspectraits.manifest.RetrievePayload`.

The :class:`Retriever` Protocol is intentionally minimal — there is no
``cleanup`` / ``warmup`` / ``preflight`` step. A retriever is built once
in the dispatch table and reused across DOIs; per-DOI state lives only
in local variables inside :meth:`fetch`.
"""

from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

import httpx

from litspectraits.config import Settings
from litspectraits.manifest import CrossRefMetadata, Format, Publisher, RetrievePayload


@runtime_checkable
class Retriever(Protocol):
    """Async retriever for one publisher.

    Attributes
    ----------
    publisher : Publisher
        The publisher this retriever handles. Must match its dispatch-table
        key in :mod:`litspectraits.retrievers.dispatch`.
    format : Format
        The single format this retriever produces. Wiley → PDF, Springer
        Nature → JATS XML, Elsevier → Elsevier XML. The orchestrator
        cross-checks this against the magic-byte sniff before commit.
    rate_per_second : float
        Conservative ceiling enforced by the per-publisher token bucket.
        Sourced from ``Settings.rate_limit_*`` at construction time so an
        operator override is honored without rebuilding the retriever.

    Notes
    -----
    ``format`` shadows the builtin to mirror the field name on
    :class:`~litspectraits.manifest.RetrievePayload` /
    :class:`~litspectraits.manifest.AcquisitionRecord`. Same trade-off
    documented there: the spec uses ``format`` consistently and renaming
    to ``fmt`` would be the larger sin.
    """

    publisher: ClassVar[Publisher]
    format: ClassVar[Format]
    rate_per_second: float

    async def fetch(
        self,
        doi: str,
        meta: CrossRefMetadata,
        *,
        client: httpx.AsyncClient,
        tmp_dir: Path,
        settings: Settings,
    ) -> RetrievePayload:
        """Retrieve the artifact for ``doi``.

        Parameters
        ----------
        doi : str
            Normalized DOI to fetch.
        meta : CrossRefMetadata
            Pre-fetched CrossRef record. The retriever may use it for
            logging cross-checks but should not re-derive routing from it
            — dispatch already happened upstream.
        client : httpx.AsyncClient
            Shared client built by :func:`litspectraits.http.http_client`.
            The Elsevier retriever uses it directly; Wiley / Springer
            shims may ignore it (their SDKs manage their own connections).
        tmp_dir : pathlib.Path
            Staging directory for the downloaded file. Typically
            :attr:`ArtifactStore.tmp_dir`. The retriever must write its
            bytes here under a unique filename; the orchestrator's
            atomic commit relocates them.
        settings : Settings
            Process settings — credential lookups, rate-limit overrides.

        Returns
        -------
        RetrievePayload
            Populated success payload.

        Raises
        ------
        litspectraits.errors.IngestError
            Any failure mode. Subclasses chosen per §5.
        """
        ...
