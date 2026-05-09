"""Probe protocol and shared context.

A probe is any object with a ``name: str`` attribute and an ``async run``
method. Probes are stateless and idempotent — they read from the shared
:class:`ProbeContext` and never mutate global state. Errors must be caught
internally and reported via :class:`ProbeOutcome.error`; uncaught exceptions
will propagate through the orchestrator's :class:`asyncio.TaskGroup` and
cancel siblings.
"""

import time
from typing import Protocol

import httpx
import structlog
from attrs import frozen

from litspectraits.acquisition.store import ArtifactStore
from litspectraits.config import Settings
from litspectraits.resolver.probes.crossref import CrossRefRecord
from litspectraits.resolver.types import ProbeOutcome


@frozen
class ProbeContext:
    """Shared per-resolve context passed to every probe.

    Attributes
    ----------
    doi : str
        Normalized DOI under resolution.
    settings : Settings
        Process-wide configuration.
    client : httpx.AsyncClient
        Shared HTTP client.
    crossref_metadata : CrossRefRecord | None
        Metadata fetched once at the start of the resolve. ``None`` when
        CrossRef returned 404 or errored.
    store : ArtifactStore | None
        Local artifact store handle. Only populated when the local probe
        is enabled.
    """

    doi: str
    settings: Settings
    client: httpx.AsyncClient
    crossref_metadata: CrossRefRecord | None
    store: ArtifactStore | None


class Probe(Protocol):
    """Structural protocol implemented by every concrete probe."""

    name: str

    async def run(self, ctx: ProbeContext) -> ProbeOutcome: ...


def measure(start: float) -> int:
    """Return milliseconds elapsed since ``start`` (a :func:`time.monotonic` reading)."""
    return int((time.monotonic() - start) * 1000)


async def safe_run(probe: Probe, ctx: ProbeContext) -> ProbeOutcome:
    """Run a probe and convert any exception into a :class:`ProbeOutcome` with ``error`` set.

    Probes are expected to handle their own errors; this is a defensive
    safety net so that a single misbehaving probe never aborts the resolve.
    """
    log = structlog.get_logger(f'litspectraits.resolver.{probe.name}').bind(doi=ctx.doi)
    start = time.monotonic()
    try:
        outcome = await probe.run(ctx)
    except Exception as exc:
        log.warning('probe.error', error=str(exc), error_type=type(exc).__name__)
        return ProbeOutcome(probe=probe.name, error=str(exc), duration_ms=measure(start))
    log.debug(
        'probe.done',
        availabilities=len(outcome.availabilities),
        error=outcome.error,
        duration_ms=outcome.duration_ms,
    )
    return outcome
