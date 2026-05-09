"""Top-level resolver orchestration.

Fetches the CrossRef metadata once, fans probes out concurrently, then
ranks the union of their :class:`Availability` results under the active
:class:`RankingPolicy`. Probe errors never abort the resolve — each probe
runs through :func:`safe_run` which converts exceptions into
:class:`ProbeOutcome.error`.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
import structlog

from litspectraits.acquisition.store import ArtifactStore
from litspectraits.config import Settings
from litspectraits.doi import normalize as normalize_doi
from litspectraits.http import http_client
from litspectraits.resolver.policy import (
    PUBLISHED_FIRST,
    RankingPolicy,
    credentials_from_settings,
)
from litspectraits.resolver.probes.arxiv import ArXivProbe
from litspectraits.resolver.probes.base import Probe, ProbeContext, safe_run
from litspectraits.resolver.probes.biorxiv import BioRxivProbe
from litspectraits.resolver.probes.crossref import CrossRefRecord, fetch_crossref
from litspectraits.resolver.probes.crossref_tdm import CrossRefTDMProbe
from litspectraits.resolver.probes.europepmc import EuropePMCProbe
from litspectraits.resolver.probes.local import LocalStoreProbe
from litspectraits.resolver.probes.pmc import PMCProbe
from litspectraits.resolver.probes.unpaywall import UnpaywallProbe
from litspectraits.resolver.types import (
    Availability,
    ProbeOutcome,
    ResolveResult,
)

_PROBE_CONCURRENCY = 8

log = structlog.get_logger('litspectraits.resolver')


def default_probes() -> tuple[Probe, ...]:
    """Return the standard probe set in declaration order.

    Order is informational only — concurrent execution and policy ranking
    decide selection.
    """
    return (
        LocalStoreProbe(),
        PMCProbe(),
        EuropePMCProbe(),
        BioRxivProbe(),
        ArXivProbe(),
        UnpaywallProbe(),
        CrossRefTDMProbe(),
    )


async def resolve(
    doi: str,
    *,
    settings: Settings,
    store: ArtifactStore | None = None,
    policy: RankingPolicy = PUBLISHED_FIRST,
    probes: Sequence[Probe] | None = None,
) -> ResolveResult:
    """Resolve a DOI to a ranked list of acquisition availabilities.

    Parameters
    ----------
    doi : str
        Raw or normalized DOI.
    settings : Settings
        Process configuration.
    store : ArtifactStore, optional
        Local artifact store. When ``None``, the local probe surfaces no
        candidates.
    policy : RankingPolicy, optional
        Ranking policy. Defaults to :data:`PUBLISHED_FIRST`.
    probes : Sequence[Probe], optional
        Override the probe set (for tests / specialized callers).

    Returns
    -------
    ResolveResult
        With ``chosen`` set to the top permitted candidate, or ``None`` if
        nothing was both available and fetchable.
    """
    normalized = normalize_doi(doi)
    bound = log.bind(doi=normalized, policy=policy.name)
    bound.info('resolve.start')

    selected_probes: tuple[Probe, ...] = tuple(probes) if probes is not None else default_probes()

    async with http_client(settings) as client:
        crossref_record = await _fetch_crossref_safe(client, settings, normalized)
        ctx = ProbeContext(
            doi=normalized,
            settings=settings,
            client=client,
            crossref_metadata=crossref_record,
            store=store,
        )
        outcomes = await _run_probes(selected_probes, ctx)

    found: list[Availability] = []
    for outcome in outcomes:
        found.extend(outcome.availabilities)

    creds = credentials_from_settings(settings.crossref_tdm_token)
    permitted = sorted(
        (a for a in found if policy.permits(a, available_credentials=creds)),
        key=policy.sort_key,
    )
    excluded: list[tuple[Availability, str]] = []
    for a in found:
        if not policy.permits(a, available_credentials=creds):
            reason = policy.reason_excluded(a, available_credentials=creds) or 'unknown'
            excluded.append((a, reason))

    chosen = permitted[0] if permitted else None
    candidates = tuple(permitted) + tuple(a for a, _ in excluded)

    bound.info(
        'resolve.done',
        chosen=chosen.source_kind if chosen else None,
        candidates=len(candidates),
        permitted=len(permitted),
        excluded=len(excluded),
    )
    return ResolveResult(
        doi=normalized,
        chosen=chosen,
        candidates=candidates,
        excluded=tuple(excluded),
        probes=outcomes,
        policy_name=policy.name,
        resolved_at=datetime.now(UTC),
    )


async def _fetch_crossref_safe(
    client: httpx.AsyncClient, settings: Settings, doi: str
) -> CrossRefRecord | None:
    """Fetch the CrossRef record, swallowing any error so probes still run."""
    try:
        return await fetch_crossref(client, settings, doi)
    except Exception as exc:
        log.warning('crossref.fetch_error', doi=doi, error=str(exc))
        return None


async def _run_probes(probes: Sequence[Probe], ctx: ProbeContext) -> tuple[ProbeOutcome, ...]:
    """Run probes concurrently with a small semaphore cap."""
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _one(probe: Probe) -> ProbeOutcome:
        async with sem:
            return await safe_run(probe, ctx)

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_one(p)) for p in probes]
    return tuple(t.result() for t in tasks)
