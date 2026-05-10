"""Operator preflight diagnostic (``docs/overview-v3.md`` §12).

Two checks, both read-only:

1. **Egress IP** — GET ``https://api.ipify.org/?format=json``. Compared
   against ``Settings.expected_egress_cidrs``: empty allow-list → warn
   only; non-empty → fail when the IP is outside every CIDR. Catches
   "VPN not engaged" before a corpus build silently produces 200
   abstract-only manifests on Elsevier (§12).
2. **Per-publisher creds smoke test** — for each publisher whose
   credential is configured, dispatch the publisher's smoke DOI
   (:mod:`litspectraits._smoke_dois`) through its retriever in a
   *temporary* directory. The retriever's own loud-failure semantics
   surface as :class:`MissingCredentialError`,
   :class:`AuthRejectedError`, :class:`EntitlementDowngradeError`, etc.
   The staged file is discarded on success — doctor never touches the
   real artifact store.

Doctor is split into a pure :func:`run_doctor` returning a
:class:`DoctorReport` value object and a separate :func:`render` that
emits the Rich table. The split keeps the table render trivially
golden-testable and makes ``doctor`` re-usable from non-CLI contexts
(e.g. a future scheduled health-check).

Exit-code semantics live in :mod:`litspectraits.cli` per §12: exit 0
when every configured credential smoke-tested green (or no credentials
configured at all); exit 1 when any configured credential failed its
smoke test. The IP allow-list is observability-only — even a definite
mismatch never flips the exit code on its own. Rationale: an operator
running doctor from a laptop with the VPN down still wants to see the
per-publisher rows.
"""

import asyncio
import ipaddress
import tempfile
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Final

import httpx
import structlog
from attrs import frozen
from rich.console import Console
from rich.table import Table

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    EntitlementDowngradeError,
    IngestError,
    MissingCredentialError,
)
from litspectraits.manifest import CrossRefMetadata, Publisher
from litspectraits.retrievers.dispatch import retriever_for

_IPIFY_URL: Final = 'https://api.ipify.org/'

_logger: Final = structlog.get_logger('litspectraits.doctor')


class IPStatus(StrEnum):
    """Coarse classification of the egress-IP check result.

    ``UNCONFIGURED`` is its own state (rather than rolled into ``OK``)
    so the rendered table can spell out "no allow-list configured" —
    operators frequently misread a green row as "the allow-list passed",
    not "there was no allow-list to compare against".
    """

    UNCONFIGURED = 'unconfigured'
    OK = 'ok'
    OUTSIDE_ALLOWLIST = 'outside_allowlist'
    LOOKUP_FAILED = 'lookup_failed'


class CredStatus(StrEnum):
    """Per-publisher credential smoke-test result."""

    NOT_CONFIGURED = 'not_configured'
    OK = 'ok'
    MISSING_CREDENTIAL = 'missing_credential'
    AUTH_REJECTED = 'auth_rejected'
    ENTITLEMENT_DOWNGRADE = 'entitlement_downgrade'
    OTHER_FAILURE = 'other_failure'


@frozen
class IPCheck:
    """Outcome of the egress-IP probe."""

    status: IPStatus
    ip: str | None
    expected_cidrs: tuple[str, ...]
    error: str | None  # populated when ``status == LOOKUP_FAILED``


@frozen
class CredCheck:
    """Outcome of one publisher's smoke-test row."""

    publisher: Publisher
    status: CredStatus
    smoke_doi: str
    detail: str  # human-readable; folded into the Rich table cell


@frozen
class DoctorReport:
    """Aggregate report (one IP probe + one row per publisher).

    ``ok`` is the contract for the CLI's exit-code logic: True when no
    *configured* credential failed (status in ``OK`` or
    ``NOT_CONFIGURED``); False otherwise. The IP check is excluded from
    ``ok`` per the §12 rationale (allow-list mismatches are
    observability-only).
    """

    ip_check: IPCheck
    cred_checks: tuple[CredCheck, ...]

    @property
    def ok(self) -> bool:
        return all(
            check.status in (CredStatus.OK, CredStatus.NOT_CONFIGURED)
            for check in self.cred_checks
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def run_doctor(*, settings: Settings, client: httpx.AsyncClient) -> DoctorReport:
    """Execute both preflight checks and return a :class:`DoctorReport`.

    Network calls happen here (ipify + one publisher smoke fetch per
    configured credential). Pure-data return value; rendering is
    :func:`render`'s job.
    """
    ip_check, cred_checks = await asyncio.gather(
        _check_egress_ip(client=client, settings=settings),
        _check_all_publishers(client=client, settings=settings),
    )
    return DoctorReport(ip_check=ip_check, cred_checks=cred_checks)


async def _check_egress_ip(*, client: httpx.AsyncClient, settings: Settings) -> IPCheck:
    cidrs = settings.expected_egress_cidrs
    try:
        response = await client.get(_IPIFY_URL, params={'format': 'json'})
        response.raise_for_status()
        ip = str(response.json()['ip'])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        _logger.warning('egress-ip lookup failed', error=str(exc))
        return IPCheck(
            status=IPStatus.LOOKUP_FAILED,
            ip=None,
            expected_cidrs=cidrs,
            error=str(exc),
        )
    if not cidrs:
        return IPCheck(status=IPStatus.UNCONFIGURED, ip=ip, expected_cidrs=(), error=None)
    if _ip_in_any(ip, cidrs):
        return IPCheck(status=IPStatus.OK, ip=ip, expected_cidrs=cidrs, error=None)
    return IPCheck(
        status=IPStatus.OUTSIDE_ALLOWLIST, ip=ip, expected_cidrs=cidrs, error=None
    )


def _ip_in_any(ip: str, cidrs: Iterable[str]) -> bool:
    address = ipaddress.ip_address(ip)
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            # A bad CIDR string is config sloppiness; warn and skip — the
            # allow-list parser already validated shape on env load, so
            # this branch is defensive.
            _logger.warning('ignoring invalid CIDR in allow-list', cidr=cidr)
            continue
        if address in network:
            return True
    return False


async def _check_all_publishers(
    *, client: httpx.AsyncClient, settings: Settings
) -> tuple[CredCheck, ...]:
    rows = await asyncio.gather(
        *(_check_one_publisher(p, client=client, settings=settings) for p in Publisher)
    )
    return tuple(rows)


async def _check_one_publisher(
    publisher: Publisher, *, client: httpx.AsyncClient, settings: Settings
) -> CredCheck:
    smoke_doi = SMOKE_DOI[publisher]
    if not _has_credential(publisher, settings):
        return CredCheck(
            publisher=publisher,
            status=CredStatus.NOT_CONFIGURED,
            smoke_doi=smoke_doi,
            detail='no credential set',
        )
    try:
        await _smoke_fetch(publisher, smoke_doi=smoke_doi, client=client, settings=settings)
    except MissingCredentialError as exc:
        return CredCheck(
            publisher=publisher,
            status=CredStatus.MISSING_CREDENTIAL,
            smoke_doi=smoke_doi,
            detail=_summarize(exc),
        )
    except AuthRejectedError as exc:
        return CredCheck(
            publisher=publisher,
            status=CredStatus.AUTH_REJECTED,
            smoke_doi=smoke_doi,
            detail=_summarize(exc),
        )
    except EntitlementDowngradeError as exc:
        return CredCheck(
            publisher=publisher,
            status=CredStatus.ENTITLEMENT_DOWNGRADE,
            smoke_doi=smoke_doi,
            detail=_summarize(exc),
        )
    except IngestError as exc:
        return CredCheck(
            publisher=publisher,
            status=CredStatus.OTHER_FAILURE,
            smoke_doi=smoke_doi,
            detail=_summarize(exc),
        )
    return CredCheck(
        publisher=publisher,
        status=CredStatus.OK,
        smoke_doi=smoke_doi,
        detail='fetched ok',
    )


def _has_credential(publisher: Publisher, settings: Settings) -> bool:
    match publisher:
        case Publisher.WILEY:
            return bool(settings.wiley_tdm_token)
        case Publisher.SPRINGER_NATURE:
            return bool(settings.springer_api_key)
        case Publisher.ELSEVIER:
            return bool(settings.elsevier_api_key)


async def _smoke_fetch(
    publisher: Publisher,
    *,
    smoke_doi: str,
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    """Run the publisher's retriever once into a *throwaway* tempdir.

    Doctor must be read-only (§12), so the staged ``*.part`` file is
    discarded automatically when the tempdir context exits. We never
    call :meth:`ArtifactStore.commit` from here.

    ``CrossRefMetadata`` is synthesized inline rather than fetched from
    CrossRef. None of the three retrievers actually consume the meta
    fields beyond protocol uniformity (Wiley + Springer ``del meta``
    explicitly; Elsevier has ``del meta`` too), so adding a CrossRef
    round-trip would only widen the failure surface without diagnostic
    value: doctor exists to test publisher-side credentials, not the
    polite pool.
    """
    retriever = retriever_for(publisher)
    stub_meta = CrossRefMetadata(
        doi=smoke_doi,
        publisher_str='',
        title=None,
        authors=(),
        year=None,
        type=None,
        license=None,
    )
    with tempfile.TemporaryDirectory(prefix='litspectraits-doctor-') as td:
        await retriever.fetch(
            smoke_doi,
            stub_meta,
            client=client,
            tmp_dir=Path(td),
            settings=settings,
        )


def _summarize(exc: IngestError) -> str:
    """Compact one-line error summary for the table cell.

    Renders the most useful context fields if present; otherwise falls
    back to the exception's class name. Avoids dumping the full
    ``context`` dict because the table cell needs to stay narrow enough
    that an 80-col terminal does not wrap.
    """
    for key in ('http_status', 'sdk_status', 'hint'):
        value = exc.context.get(key)
        if value is not None:
            return f'{type(exc).__name__}: {key}={value}'
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_IP_STATUS_LABEL: Final[dict[IPStatus, str]] = {
    IPStatus.OK: 'ok',
    IPStatus.UNCONFIGURED: 'no allow-list configured',
    IPStatus.OUTSIDE_ALLOWLIST: 'outside allow-list',
    IPStatus.LOOKUP_FAILED: 'lookup failed',
}

_CRED_STATUS_LABEL: Final[dict[CredStatus, str]] = {
    CredStatus.OK: 'ok',
    CredStatus.NOT_CONFIGURED: 'not configured',
    CredStatus.MISSING_CREDENTIAL: 'missing credential',
    CredStatus.AUTH_REJECTED: 'auth rejected',
    CredStatus.ENTITLEMENT_DOWNGRADE: 'entitlement downgrade',
    CredStatus.OTHER_FAILURE: 'other failure',
}


def render(report: DoctorReport, *, console: Console) -> None:
    """Render ``report`` as two Rich tables (IP, then per-publisher).

    No styling beyond column / header — keeping the rendering plain so
    the golden-output test in ``test_doctor.py`` does not have to model
    ANSI escape sequences. The CLI layer applies colour separately if
    desired (e.g. coloured row backgrounds keyed off ``status``).
    """
    console.print(_render_ip_table(report.ip_check))
    console.print(_render_cred_table(report.cred_checks))


def _render_ip_table(check: IPCheck) -> Table:
    table = Table(title='Egress IP', show_header=True, header_style='bold')
    table.add_column('IP', no_wrap=True)
    table.add_column('Allow-list')
    table.add_column('Status', no_wrap=True)
    table.add_column('Detail')
    cidrs = ', '.join(check.expected_cidrs) if check.expected_cidrs else '-'
    table.add_row(
        check.ip or '-',
        cidrs,
        _IP_STATUS_LABEL[check.status],
        check.error or '',
    )
    return table


def _render_cred_table(checks: tuple[CredCheck, ...]) -> Table:
    table = Table(title='Publisher credentials', show_header=True, header_style='bold')
    table.add_column('Publisher', no_wrap=True)
    table.add_column('Smoke DOI', no_wrap=True)
    table.add_column('Status', no_wrap=True)
    table.add_column('Detail')
    for check in checks:
        table.add_row(
            check.publisher.value,
            check.smoke_doi,
            _CRED_STATUS_LABEL[check.status],
            check.detail,
        )
    return table
