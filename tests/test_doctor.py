"""Tests for :mod:`litspectraits.doctor` (``docs/overview-v3.md`` §12).

Two layers:

- :func:`run_doctor` — pure async function returning a
  :class:`DoctorReport`. Tested by stubbing ``ipify`` via respx and
  monkeypatching ``litspectraits.doctor.retriever_for`` with fake
  retrievers (same idiom as ``test_ingest.py``).
- :func:`render` — pure renderer over a constructed report. Tested by
  capturing into an in-memory :class:`rich.console.Console` and asserting
  the plain-text rendering as a golden output.
"""

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from respx import MockRouter
from rich.console import Console

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.config import Settings
from litspectraits.doctor import (
    CredCheck,
    CredStatus,
    DoctorReport,
    IPCheck,
    IPStatus,
    render,
    run_doctor,
)
from litspectraits.errors import (
    AuthRejectedError,
    EntitlementDowngradeError,
    MissingCredentialError,
    PublisherAPIError,
)
from litspectraits.http import http_client
from litspectraits.manifest import CrossRefMetadata, Format, Publisher, RetrievePayload

# ---------------------------------------------------------------------------
# Fake retriever (same shape as test_ingest._FakeRetriever)
# ---------------------------------------------------------------------------


FetchHook = Callable[[str, CrossRefMetadata, Path], RetrievePayload]


class _FakeRetriever:
    """Doctor-flavored fake retriever.

    Doctor never reads the returned payload — it only cares whether
    ``fetch`` raised — so the hook can return a placeholder. Hooks that
    *should* fail raise an :class:`IngestError` subclass instead.
    """

    rate_per_second: ClassVar[float] = 0.0

    def __init__(self, *, publisher: Publisher, fmt: Format, hook: FetchHook) -> None:
        self.publisher = publisher  # type: ignore[misc]
        self.format = fmt  # type: ignore[misc]
        self._hook = hook
        self.calls: list[str] = []

    async def fetch(
        self,
        doi: str,
        meta: CrossRefMetadata,
        *,
        client: httpx.AsyncClient,
        tmp_dir: Path,
        settings: Settings,
    ) -> RetrievePayload:
        del client, settings
        self.calls.append(doi)
        return self._hook(doi, meta, tmp_dir)


def _ok_hook(publisher: Publisher, fmt: Format) -> FetchHook:
    """Hook that writes a tiny placeholder file inside tmp_dir."""

    def _hook(doi: str, _meta: CrossRefMetadata, tmp_dir: Path) -> RetrievePayload:
        path = tmp_dir / 'fake.part'
        path.write_bytes(b'fake')
        return RetrievePayload(
            sha256='0' * 64,
            byte_size=4,
            tmp_path=path,
            format=fmt,
            fetched_url=f'https://example.test/{doi}',
            sdk_version='fake 0.0.1',
        )

    return _hook


def _raise(exc: Exception) -> FetchHook:
    def _hook(_doi: str, _meta: CrossRefMetadata, _tmp_dir: Path) -> RetrievePayload:
        raise exc

    return _hook


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def install_retriever(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Publisher, _FakeRetriever], None]]:
    registry: dict[Publisher, _FakeRetriever] = {}

    def _fake_retriever_for(publisher: Publisher) -> _FakeRetriever:
        if publisher not in registry:
            raise AssertionError(
                f'no fake retriever registered for {publisher.value!r}'
            )
        return registry[publisher]

    monkeypatch.setattr('litspectraits.doctor.retriever_for', _fake_retriever_for)

    def _register(publisher: Publisher, retriever: _FakeRetriever) -> None:
        registry[publisher] = retriever

    yield _register


def _mock_ipify(respx_mock: MockRouter, *, ip: str = '203.0.113.5') -> None:
    respx_mock.get('https://api.ipify.org/').mock(
        return_value=httpx.Response(200, json={'ip': ip})
    )


# ---------------------------------------------------------------------------
# Smoke-DOI registry
# ---------------------------------------------------------------------------


def test_smoke_doi_registry_covers_every_publisher() -> None:
    # Hard contract for doctor: the registry must always carry a DOI for
    # every Publisher value, otherwise ``_check_one_publisher`` KeyErrors.
    assert set(SMOKE_DOI.keys()) == set(Publisher)


# ---------------------------------------------------------------------------
# IP check
# ---------------------------------------------------------------------------


async def test_ip_check_reports_unconfigured_when_no_allowlist(
    settings: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    _mock_ipify(respx_mock, ip='203.0.113.5')
    for publisher, fmt in (
        (Publisher.WILEY, Format.PDF),
        (Publisher.SPRINGER_NATURE, Format.JATS_XML),
        (Publisher.ELSEVIER, Format.ELSEVIER_XML),
    ):
        install_retriever(
            publisher, _FakeRetriever(publisher=publisher, fmt=fmt, hook=_ok_hook(publisher, fmt))
        )
    async with http_client(settings) as client:
        report = await run_doctor(settings=settings, client=client)
    assert report.ip_check.status is IPStatus.UNCONFIGURED
    assert report.ip_check.ip == '203.0.113.5'


async def test_ip_check_reports_ok_when_inside_allowlist(
    settings: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    settings = _settings_with_cidrs(settings, ('203.0.113.0/24',))
    _mock_ipify(respx_mock, ip='203.0.113.42')
    for publisher, fmt in (
        (Publisher.WILEY, Format.PDF),
        (Publisher.SPRINGER_NATURE, Format.JATS_XML),
        (Publisher.ELSEVIER, Format.ELSEVIER_XML),
    ):
        install_retriever(
            publisher, _FakeRetriever(publisher=publisher, fmt=fmt, hook=_ok_hook(publisher, fmt))
        )
    async with http_client(settings) as client:
        report = await run_doctor(settings=settings, client=client)
    assert report.ip_check.status is IPStatus.OK
    assert report.ip_check.ip == '203.0.113.42'


async def test_ip_check_reports_outside_allowlist(
    settings: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    settings = _settings_with_cidrs(settings, ('132.187.0.0/16',))
    _mock_ipify(respx_mock, ip='8.8.8.8')
    for publisher, fmt in (
        (Publisher.WILEY, Format.PDF),
        (Publisher.SPRINGER_NATURE, Format.JATS_XML),
        (Publisher.ELSEVIER, Format.ELSEVIER_XML),
    ):
        install_retriever(
            publisher, _FakeRetriever(publisher=publisher, fmt=fmt, hook=_ok_hook(publisher, fmt))
        )
    async with http_client(settings) as client:
        report = await run_doctor(settings=settings, client=client)
    assert report.ip_check.status is IPStatus.OUTSIDE_ALLOWLIST
    assert report.ip_check.ip == '8.8.8.8'
    # Outside-allow-list does NOT flip ``report.ok`` — that's
    # observability-only per §12.
    assert report.ok is True


async def test_ip_check_reports_lookup_failed_on_5xx(
    settings: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    respx_mock.get('https://api.ipify.org/').mock(
        return_value=httpx.Response(503, text='gateway timeout')
    )
    for publisher, fmt in (
        (Publisher.WILEY, Format.PDF),
        (Publisher.SPRINGER_NATURE, Format.JATS_XML),
        (Publisher.ELSEVIER, Format.ELSEVIER_XML),
    ):
        install_retriever(
            publisher, _FakeRetriever(publisher=publisher, fmt=fmt, hook=_ok_hook(publisher, fmt))
        )
    async with http_client(settings) as client:
        report = await run_doctor(settings=settings, client=client)
    assert report.ip_check.status is IPStatus.LOOKUP_FAILED
    assert report.ip_check.ip is None


# ---------------------------------------------------------------------------
# Per-publisher creds smoke test
# ---------------------------------------------------------------------------


async def test_no_credentials_configured_skips_smoke_per_publisher(
    settings: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    """When no creds are set, every row reports NOT_CONFIGURED and report.ok holds."""
    _mock_ipify(respx_mock)
    # Even with fakes registered, the smoke fetch shouldn't fire — but
    # we register them so a regression that *does* call the fetch would
    # be caught (the AssertionError in the fixture's lookup function).
    for publisher, fmt in (
        (Publisher.WILEY, Format.PDF),
        (Publisher.SPRINGER_NATURE, Format.JATS_XML),
        (Publisher.ELSEVIER, Format.ELSEVIER_XML),
    ):
        install_retriever(
            publisher,
            _FakeRetriever(
                publisher=publisher,
                fmt=fmt,
                hook=_raise(AssertionError('should not be called')),
            ),
        )
    async with http_client(settings) as client:
        report = await run_doctor(settings=settings, client=client)
    statuses = {check.publisher: check.status for check in report.cred_checks}
    assert statuses[Publisher.WILEY] is CredStatus.NOT_CONFIGURED
    assert statuses[Publisher.SPRINGER_NATURE] is CredStatus.NOT_CONFIGURED
    assert statuses[Publisher.ELSEVIER] is CredStatus.NOT_CONFIGURED
    assert report.ok is True


async def test_all_credentials_smoke_pass_reports_ok(
    settings_with_creds: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    _mock_ipify(respx_mock)
    install_retriever(
        Publisher.WILEY,
        _FakeRetriever(
            publisher=Publisher.WILEY, fmt=Format.PDF, hook=_ok_hook(Publisher.WILEY, Format.PDF)
        ),
    )
    install_retriever(
        Publisher.SPRINGER_NATURE,
        _FakeRetriever(
            publisher=Publisher.SPRINGER_NATURE,
            fmt=Format.JATS_XML,
            hook=_ok_hook(Publisher.SPRINGER_NATURE, Format.JATS_XML),
        ),
    )
    install_retriever(
        Publisher.ELSEVIER,
        _FakeRetriever(
            publisher=Publisher.ELSEVIER,
            fmt=Format.ELSEVIER_XML,
            hook=_ok_hook(Publisher.ELSEVIER, Format.ELSEVIER_XML),
        ),
    )
    async with http_client(settings_with_creds) as client:
        report = await run_doctor(settings=settings_with_creds, client=client)
    assert all(check.status is CredStatus.OK for check in report.cred_checks)
    assert report.ok is True


@pytest.mark.parametrize(
    ('exc_factory', 'expected_status'),
    [
        (
            lambda doi: MissingCredentialError(
                doi=doi, publisher=Publisher.WILEY.value, hint='set token'
            ),
            CredStatus.MISSING_CREDENTIAL,
        ),
        (
            lambda doi: AuthRejectedError(
                doi=doi, publisher=Publisher.WILEY.value, http_status=403
            ),
            CredStatus.AUTH_REJECTED,
        ),
        (
            lambda doi: EntitlementDowngradeError(
                doi=doi, publisher=Publisher.WILEY.value, hint='META_ABS'
            ),
            CredStatus.ENTITLEMENT_DOWNGRADE,
        ),
        (
            lambda doi: PublisherAPIError(
                doi=doi, publisher=Publisher.WILEY.value, http_status=500
            ),
            CredStatus.OTHER_FAILURE,
        ),
    ],
)
async def test_smoke_failure_classifies_correctly(
    settings_with_creds: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
    exc_factory: Callable[[str], Exception],
    expected_status: CredStatus,
) -> None:
    _mock_ipify(respx_mock)
    smoke_doi = SMOKE_DOI[Publisher.WILEY]
    install_retriever(
        Publisher.WILEY,
        _FakeRetriever(
            publisher=Publisher.WILEY,
            fmt=Format.PDF,
            hook=_raise(exc_factory(smoke_doi)),
        ),
    )
    install_retriever(
        Publisher.SPRINGER_NATURE,
        _FakeRetriever(
            publisher=Publisher.SPRINGER_NATURE,
            fmt=Format.JATS_XML,
            hook=_ok_hook(Publisher.SPRINGER_NATURE, Format.JATS_XML),
        ),
    )
    install_retriever(
        Publisher.ELSEVIER,
        _FakeRetriever(
            publisher=Publisher.ELSEVIER,
            fmt=Format.ELSEVIER_XML,
            hook=_ok_hook(Publisher.ELSEVIER, Format.ELSEVIER_XML),
        ),
    )
    async with http_client(settings_with_creds) as client:
        report = await run_doctor(settings=settings_with_creds, client=client)
    by_publisher = {check.publisher: check for check in report.cred_checks}
    assert by_publisher[Publisher.WILEY].status is expected_status
    # Any non-OK configured row flips report.ok to False.
    assert report.ok is False


async def test_smoke_uses_throwaway_tempdir_no_artifact_committed(
    settings_with_creds: Settings,
    respx_mock: MockRouter,
    install_retriever: Callable[[Publisher, _FakeRetriever], None],
) -> None:
    """Doctor must be read-only: no bytes land under ``data_dir/artifacts``."""
    _mock_ipify(respx_mock)
    captured_tmpdirs: list[Path] = []

    def _hook(doi: str, _meta: CrossRefMetadata, tmp_dir: Path) -> RetrievePayload:
        captured_tmpdirs.append(tmp_dir)
        path = tmp_dir / 'fake.part'
        path.write_bytes(b'fake')
        return RetrievePayload(
            sha256='0' * 64,
            byte_size=4,
            tmp_path=path,
            format=Format.PDF,
            fetched_url=f'https://example.test/{doi}',
            sdk_version='fake 0.0.1',
        )

    install_retriever(
        Publisher.WILEY,
        _FakeRetriever(publisher=Publisher.WILEY, fmt=Format.PDF, hook=_hook),
    )
    install_retriever(
        Publisher.SPRINGER_NATURE,
        _FakeRetriever(
            publisher=Publisher.SPRINGER_NATURE,
            fmt=Format.JATS_XML,
            hook=_ok_hook(Publisher.SPRINGER_NATURE, Format.JATS_XML),
        ),
    )
    install_retriever(
        Publisher.ELSEVIER,
        _FakeRetriever(
            publisher=Publisher.ELSEVIER,
            fmt=Format.ELSEVIER_XML,
            hook=_ok_hook(Publisher.ELSEVIER, Format.ELSEVIER_XML),
        ),
    )
    async with http_client(settings_with_creds) as client:
        await run_doctor(settings=settings_with_creds, client=client)
    # Tempdir was created outside data_dir and was cleaned up.
    assert captured_tmpdirs
    for td in captured_tmpdirs:
        try:
            td.relative_to(settings_with_creds.data_dir)
        except ValueError:
            pass  # good — outside data_dir
        else:
            raise AssertionError(f'{td} should not be inside settings.data_dir')
        assert not td.exists()
    # data_dir itself remained untouched (no artifacts/ written).
    assert not (settings_with_creds.data_dir / 'artifacts').exists()


# ---------------------------------------------------------------------------
# Render — golden-output style
# ---------------------------------------------------------------------------


def _capture(report: DoctorReport) -> str:
    """Render ``report`` into an in-memory plain-text Console.

    ``color_system=None`` strips ANSI; ``width=120`` keeps Rich from
    trying to wrap the table to the test runner's actual terminal
    width (which would make the golden output flaky on CI).
    """
    buf = io.StringIO()
    console = Console(file=buf, width=120, color_system=None, force_terminal=False)
    render(report, console=console)
    return buf.getvalue()


def test_render_all_green_report() -> None:
    report = DoctorReport(
        ip_check=IPCheck(
            status=IPStatus.OK,
            ip='132.187.7.42',
            expected_cidrs=('132.187.0.0/16',),
            error=None,
        ),
        cred_checks=(
            CredCheck(
                publisher=Publisher.WILEY,
                status=CredStatus.OK,
                smoke_doi=SMOKE_DOI[Publisher.WILEY],
                detail='fetched ok',
            ),
            CredCheck(
                publisher=Publisher.SPRINGER_NATURE,
                status=CredStatus.OK,
                smoke_doi=SMOKE_DOI[Publisher.SPRINGER_NATURE],
                detail='fetched ok',
            ),
            CredCheck(
                publisher=Publisher.ELSEVIER,
                status=CredStatus.OK,
                smoke_doi=SMOKE_DOI[Publisher.ELSEVIER],
                detail='fetched ok',
            ),
        ),
    )
    output = _capture(report)
    # Header rows present
    assert 'Egress IP' in output
    assert 'Publisher credentials' in output
    # IP row content
    assert '132.187.7.42' in output
    assert '132.187.0.0/16' in output
    assert 'ok' in output
    # Per-publisher rows
    for publisher in Publisher:
        assert publisher.value in output
        assert SMOKE_DOI[publisher] in output


def test_render_mixed_report_shows_status_labels() -> None:
    report = DoctorReport(
        ip_check=IPCheck(
            status=IPStatus.LOOKUP_FAILED,
            ip=None,
            expected_cidrs=(),
            error='503: gateway timeout',
        ),
        cred_checks=(
            CredCheck(
                publisher=Publisher.WILEY,
                status=CredStatus.AUTH_REJECTED,
                smoke_doi=SMOKE_DOI[Publisher.WILEY],
                detail='AuthRejectedError: http_status=403',
            ),
            CredCheck(
                publisher=Publisher.SPRINGER_NATURE,
                status=CredStatus.NOT_CONFIGURED,
                smoke_doi=SMOKE_DOI[Publisher.SPRINGER_NATURE],
                detail='no credential set',
            ),
            CredCheck(
                publisher=Publisher.ELSEVIER,
                status=CredStatus.ENTITLEMENT_DOWNGRADE,
                smoke_doi=SMOKE_DOI[Publisher.ELSEVIER],
                detail='EntitlementDowngradeError: hint=META_ABS',
            ),
        ),
    )
    output = _capture(report)
    assert 'lookup failed' in output
    assert '503: gateway timeout' in output
    assert 'auth rejected' in output
    assert 'not configured' in output
    assert 'entitlement downgrade' in output


def test_doctor_report_ok_property_logic() -> None:
    """``report.ok`` is False as soon as any *configured* row failed."""
    base_ip = IPCheck(
        status=IPStatus.UNCONFIGURED,
        ip='203.0.113.1',
        expected_cidrs=(),
        error=None,
    )
    not_configured = CredCheck(
        publisher=Publisher.WILEY,
        status=CredStatus.NOT_CONFIGURED,
        smoke_doi=SMOKE_DOI[Publisher.WILEY],
        detail='no credential set',
    )
    auth_rejected = CredCheck(
        publisher=Publisher.SPRINGER_NATURE,
        status=CredStatus.AUTH_REJECTED,
        smoke_doi=SMOKE_DOI[Publisher.SPRINGER_NATURE],
        detail='AuthRejectedError',
    )
    ok = CredCheck(
        publisher=Publisher.ELSEVIER,
        status=CredStatus.OK,
        smoke_doi=SMOKE_DOI[Publisher.ELSEVIER],
        detail='fetched ok',
    )
    report_all_ok = DoctorReport(ip_check=base_ip, cred_checks=(not_configured, ok))
    assert report_all_ok.ok is True
    report_with_failure = DoctorReport(
        ip_check=base_ip, cred_checks=(not_configured, auth_rejected, ok)
    )
    assert report_with_failure.ok is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_with_cidrs(base: Settings, cidrs: tuple[str, ...]) -> Settings:
    """Return a copy of ``base`` with ``expected_egress_cidrs`` overridden.

    ``Settings`` is ``attrs.frozen``; ``attrs.evolve`` does the right
    thing without exposing private constructors.
    """
    import attrs

    return attrs.evolve(base, expected_egress_cidrs=cidrs)
