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
    ExtractComponentCheck,
    ExtractReport,
    ExtractStatus,
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


@pytest.fixture(autouse=True)
def mute_extract_section(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-mute the extract section for the existing IP/cred tests.

    These tests pre-date the §8 extract section and only assert on IP +
    publisher rows; without this mute they would fail on any dev machine
    where the docling model cache isn't populated. Tests that *do*
    exercise the extract section opt out with
    ``@pytest.mark.extract_real`` so they see the unmuted module.
    """
    if request.node.get_closest_marker('extract_real') is not None:
        return

    async def _no_op(**_kwargs: object) -> ExtractReport:
        return ExtractReport(components=(), has_required_failure=False)

    monkeypatch.setattr('litspectraits.doctor._check_extract_section', _no_op)


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
# Extract section (``extract-pdf-plan.md`` §8)
# ---------------------------------------------------------------------------
#
# Two layers, mirroring the IP/cred split above:
#
# - Probe-level unit tests for ``_check_docling_extra``,
#   ``_check_docling_models``, ``_check_accelerator``,
#   ``_check_ocr_engines``, ``_check_extract_section``: each one stubs
#   the underlying docling/torch surface so the test does not depend on
#   real model weights being downloaded.
# - End-to-end exit-code policy: a full ``run_doctor`` invocation with a
#   stubbed ``_check_extract_section`` asserts that
#   ``DoctorReport.ok`` reflects required-failure semantics correctly.
#
# A handful of tests bypass the autouse ``mute_extract_section`` fixture
# by re-monkeypatching ``_check_extract_section`` (or the lower-level
# probes) — the autouse mute is a default, not a hard floor.


def _ok_extract_components(*, with_smoke: bool = False) -> tuple[ExtractComponentCheck, ...]:
    components = [
        ExtractComponentCheck(
            component='docling[extract]',
            required='yes',
            is_required=True,
            status=ExtractStatus.OK,
            detail='docling 2.93.0',
        ),
        ExtractComponentCheck(
            component='layout model',
            required='yes',
            is_required=True,
            status=ExtractStatus.OK,
            detail='cached at /tmp/models/layout',
        ),
        ExtractComponentCheck(
            component='TableFormer',
            required='yes',
            is_required=True,
            status=ExtractStatus.OK,
            detail='accurate mode loaded',
        ),
        ExtractComponentCheck(
            component='code-formula',
            required='yes',
            is_required=True,
            status=ExtractStatus.OK,
            detail='formula enrichment loaded',
        ),
        ExtractComponentCheck(
            component='accelerator',
            required='auto',
            is_required=False,
            status=ExtractStatus.CUDA,
            detail='NVIDIA A100',
        ),
        ExtractComponentCheck(
            component='OCR engines',
            required='no',
            is_required=False,
            status=ExtractStatus.OFF,
            detail='do_ocr=False',
        ),
    ]
    if with_smoke:
        components.append(
            ExtractComponentCheck(
                component='smoke convert',
                required='no',
                is_required=False,
                status=ExtractStatus.OK,
                detail='318 ms (synthetic.pdf)',
            )
        )
    return tuple(components)


def test_check_docling_extra_returns_ok_when_importable() -> None:
    """The dev venv has ``[extract]`` installed; probe must report OK."""
    from litspectraits.doctor import _check_docling_extra

    row = _check_docling_extra()
    assert row.status is ExtractStatus.OK
    assert row.is_required is True
    assert row.detail.startswith('docling ')


def test_check_docling_extra_returns_not_installed_when_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting ``sys.modules['docling'] = None`` triggers the ImportError path.

    Same idiom used in ``test_pdf.py``'s docling-not-installed test —
    the only way to reach the import-error branch without uninstalling
    the package from the venv.
    """
    import sys

    from litspectraits.doctor import _check_docling_extra

    monkeypatch.setitem(sys.modules, 'docling', None)
    row = _check_docling_extra()
    assert row.status is ExtractStatus.NOT_INSTALLED
    assert row.is_required is False  # extra is opt-in; missing extra ≠ failure
    assert 'uv sync --extra extract' in row.detail


def test_check_docling_models_missing_when_cache_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty model cache surfaces all three required rows as MISSING."""
    from litspectraits.doctor import _check_docling_models

    monkeypatch.setattr(
        'litspectraits.doctor._docling_model_dirs',
        lambda **_: (
            tmp_path / 'models',
            'layout-folder',
            'tableformer-folder',
            'codeformula-folder',
        ),
    )
    rows = _check_docling_models(model_cache_dir=None)
    assert len(rows) == 3
    assert all(row.status is ExtractStatus.MISSING for row in rows)
    assert all(row.is_required for row in rows)
    assert any('--download-models' in row.detail for row in rows)


def test_check_docling_models_ok_when_cache_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each model dir with at least one file → OK row.

    Doesn't assert specific weight filenames — the contract is
    "directory exists and is non-empty," matching the docstring on
    ``_docling_model_dirs``.
    """
    from litspectraits.doctor import _check_docling_models

    models_root = tmp_path / 'models'
    layout_dir = models_root / 'layout-folder'
    tableformer_dir = models_root / 'tableformer-folder'
    code_formula_dir = models_root / 'codeformula-folder'
    for d in (layout_dir, tableformer_dir, code_formula_dir):
        d.mkdir(parents=True)
        (d / 'weights').write_bytes(b'fake')

    monkeypatch.setattr(
        'litspectraits.doctor._docling_model_dirs',
        lambda **_: (models_root, 'layout-folder', 'tableformer-folder', 'codeformula-folder'),
    )
    rows = _check_docling_models(model_cache_dir=None)
    assert {row.component for row in rows} == {'layout model', 'TableFormer', 'code-formula'}
    assert all(row.status is ExtractStatus.OK for row in rows)


def test_check_accelerator_reports_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``torch.cuda.is_available`` is True we record CUDA + device name."""
    import sys
    import types

    from litspectraits.doctor import _check_accelerator

    fake_torch = types.ModuleType('torch')
    fake_cuda = types.ModuleType('torch.cuda')
    fake_cuda.is_available = lambda: True  # type: ignore[attr-defined]
    fake_cuda.get_device_name = lambda _idx: 'NVIDIA Test GPU'  # type: ignore[attr-defined]
    fake_torch.cuda = fake_cuda  # type: ignore[attr-defined]
    fake_backends = types.ModuleType('torch.backends')
    fake_torch.backends = fake_backends  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    monkeypatch.setitem(sys.modules, 'torch.cuda', fake_cuda)
    monkeypatch.setitem(sys.modules, 'torch.backends', fake_backends)

    row = _check_accelerator()
    assert row.status is ExtractStatus.CUDA
    assert row.detail == 'NVIDIA Test GPU'
    assert row.is_required is False  # accelerator never fails the gate


def test_check_accelerator_falls_back_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CUDA + no MPS → CPU row; the gate stays green either way."""
    import sys
    import types

    from litspectraits.doctor import _check_accelerator

    fake_torch = types.ModuleType('torch')
    fake_cuda = types.ModuleType('torch.cuda')
    fake_cuda.is_available = lambda: False  # type: ignore[attr-defined]
    fake_backends = types.ModuleType('torch.backends')
    # No ``mps`` attribute → ``hasattr(torch.backends, 'mps')`` is False
    # and the CPU fallback fires.
    fake_torch.cuda = fake_cuda  # type: ignore[attr-defined]
    fake_torch.backends = fake_backends  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    monkeypatch.setitem(sys.modules, 'torch.cuda', fake_cuda)
    monkeypatch.setitem(sys.modules, 'torch.backends', fake_backends)

    row = _check_accelerator()
    assert row.status is ExtractStatus.CPU
    assert row.is_required is False
    assert 'cpu-only' in row.detail


def test_check_ocr_engines_is_always_off() -> None:
    """OCR is permanently off in v3; row is informational only."""
    from litspectraits.doctor import _check_ocr_engines

    row = _check_ocr_engines()
    assert row.status is ExtractStatus.OFF
    assert row.is_required is False
    assert row.required == 'no'


@pytest.mark.extract_real
async def test_check_extract_section_short_circuits_when_extra_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``[extract]`` extra → single row; ``has_required_failure`` stays False.

    Pins the §8 exit-code policy verbatim: "exit 0 if the extra is not
    installed; the section is rendered as a single greyed row noting
    the install hint, exit code unaffected."
    """
    import sys

    from litspectraits.doctor import _check_extract_section

    monkeypatch.setitem(sys.modules, 'docling', None)
    report = await _check_extract_section(
        settings=settings,
        download_models=False,
        smoke_extract=False,
        fixture_path=None,
    )
    assert report.has_required_failure is False
    assert len(report.components) == 1
    assert report.components[0].component == 'docling[extract]'
    assert report.components[0].status is ExtractStatus.NOT_INSTALLED


@pytest.mark.extract_real
async def test_check_extract_section_flags_required_failure_when_model_missing(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extra installed + required model missing + no ``--download-models`` ⇒ fail.

    Pins the second leg of the §8 exit-code policy: "exit 1 if the
    extra is installed AND a required model is missing AND
    ``--download-models`` was not requested."
    """
    from litspectraits.doctor import _check_extract_section

    # Empty model cache.
    monkeypatch.setattr(
        'litspectraits.doctor._docling_model_dirs',
        lambda **_: (
            tmp_path / 'models',
            'layout-folder',
            'tableformer-folder',
            'codeformula-folder',
        ),
    )
    report = await _check_extract_section(
        settings=settings,
        download_models=False,
        smoke_extract=False,
        fixture_path=None,
    )
    assert report.has_required_failure is True
    components = {row.component: row for row in report.components}
    assert components['layout model'].status is ExtractStatus.MISSING
    assert components['TableFormer'].status is ExtractStatus.MISSING
    assert components['code-formula'].status is ExtractStatus.MISSING


@pytest.mark.extract_real
async def test_check_extract_section_clears_when_models_present(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Populating the cache flips all required rows to OK; ``has_required_failure`` False."""
    from litspectraits.doctor import _check_extract_section

    models_root = tmp_path / 'models'
    for folder in ('layout-folder', 'tableformer-folder', 'codeformula-folder'):
        d = models_root / folder
        d.mkdir(parents=True)
        (d / 'weights').write_bytes(b'fake')

    monkeypatch.setattr(
        'litspectraits.doctor._docling_model_dirs',
        lambda **_: (models_root, 'layout-folder', 'tableformer-folder', 'codeformula-folder'),
    )
    report = await _check_extract_section(
        settings=settings,
        download_models=False,
        smoke_extract=False,
        fixture_path=None,
    )
    assert report.has_required_failure is False
    components = {row.component: row for row in report.components}
    assert components['layout model'].status is ExtractStatus.OK
    assert components['TableFormer'].status is ExtractStatus.OK
    assert components['code-formula'].status is ExtractStatus.OK


@pytest.mark.extract_real
async def test_check_extract_section_runs_download_when_flagged(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``download_models=True`` invokes the downloader once, then re-probes.

    Stubs ``_maybe_download_models`` to populate the cache so the
    post-download presence check flips to OK without actually pulling
    docling weights.
    """
    from litspectraits.doctor import _check_extract_section

    models_root = tmp_path / 'models'
    model_folders = ('layout-folder', 'tableformer-folder', 'codeformula-folder')
    monkeypatch.setattr(
        'litspectraits.doctor._docling_model_dirs',
        lambda **_: (models_root, *model_folders),
    )

    download_calls: list[bool] = []

    def _fake_download(*, force: bool, model_cache_dir: Path | None = None) -> None:
        del model_cache_dir
        download_calls.append(force)
        for folder in model_folders:
            d = models_root / folder
            d.mkdir(parents=True)
            (d / 'weights').write_bytes(b'fake')

    monkeypatch.setattr('litspectraits.doctor._maybe_download_models', _fake_download)

    report = await _check_extract_section(
        settings=settings,
        download_models=True,
        smoke_extract=False,
        fixture_path=None,
    )
    assert download_calls == [False]  # force=False per §8
    assert report.has_required_failure is False


@pytest.mark.extract_real
async def test_check_extract_section_runs_smoke_when_flagged(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``smoke_extract=True`` calls ``extract_pdf`` against the staged fixture.

    Stubs ``extract_pdf`` so we don't depend on docling models being
    downloaded; asserts that the fixture flowed through ``ArtifactStore``
    and that the resulting row is OK.
    """
    from litspectraits.doctor import _check_extract_section

    # Make models present so the required gate stays green.
    models_root = tmp_path / 'models'
    for folder in ('layout-folder', 'tableformer-folder', 'codeformula-folder'):
        d = models_root / folder
        d.mkdir(parents=True)
        (d / 'weights').write_bytes(b'fake')
    monkeypatch.setattr(
        'litspectraits.doctor._docling_model_dirs',
        lambda **_: (models_root, 'layout-folder', 'tableformer-folder', 'codeformula-folder'),
    )

    fixture_path = tmp_path / 'synthetic.pdf'
    fixture_path.write_bytes(b'%PDF-1.4\n%fake bytes for smoke wiring')

    called: list[str] = []

    async def _fake_extract_pdf(
        record, _store, *, reextract: bool, model_cache_dir: Path | None = None
    ) -> None:
        called.append(record.doi)
        del reextract, model_cache_dir

    monkeypatch.setattr('litspectraits.extract.pdf.extract_pdf', _fake_extract_pdf)

    report = await _check_extract_section(
        settings=settings,
        download_models=False,
        smoke_extract=True,
        fixture_path=fixture_path,
    )
    assert called == ['10.0/doctor-smoke']
    smoke_row = next(
        (row for row in report.components if row.component == 'smoke convert'), None
    )
    assert smoke_row is not None
    assert smoke_row.status is ExtractStatus.OK
    assert smoke_row.detail.endswith('(synthetic.pdf)')


@pytest.mark.extract_real
async def test_check_extract_section_smoke_missing_fixture_returns_missing_row(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing ``--smoke-extract`` at a non-existent path surfaces a MISSING row.

    Stays in the optional/informational bucket — does not flip the
    required-failure gate (smoke is opt-in).
    """
    from litspectraits.doctor import _check_extract_section

    models_root = tmp_path / 'models'
    for folder in ('layout-folder', 'tableformer-folder', 'codeformula-folder'):
        d = models_root / folder
        d.mkdir(parents=True)
        (d / 'x').write_bytes(b'.')
    monkeypatch.setattr(
        'litspectraits.doctor._docling_model_dirs',
        lambda **_: (models_root, 'layout-folder', 'tableformer-folder', 'codeformula-folder'),
    )

    report = await _check_extract_section(
        settings=settings,
        download_models=False,
        smoke_extract=True,
        fixture_path=tmp_path / 'does-not-exist.pdf',
    )
    smoke_row = next(
        (row for row in report.components if row.component == 'smoke convert'), None
    )
    assert smoke_row is not None
    assert smoke_row.status is ExtractStatus.MISSING
    assert 'fixture not found' in smoke_row.detail
    assert report.has_required_failure is False


def test_doctor_report_ok_false_when_extract_required_failure() -> None:
    """``ok`` folds extract required-failure into the boolean."""
    base_ip = IPCheck(
        status=IPStatus.UNCONFIGURED,
        ip='203.0.113.1',
        expected_cidrs=(),
        error=None,
    )
    cred_ok = CredCheck(
        publisher=Publisher.WILEY,
        status=CredStatus.NOT_CONFIGURED,
        smoke_doi=SMOKE_DOI[Publisher.WILEY],
        detail='no credential set',
    )
    extract_failed = ExtractReport(
        components=(
            ExtractComponentCheck(
                component='layout model',
                required='yes',
                is_required=True,
                status=ExtractStatus.MISSING,
                detail='missing',
            ),
        ),
        has_required_failure=True,
    )
    report = DoctorReport(
        ip_check=base_ip,
        cred_checks=(cred_ok,),
        extract_check=extract_failed,
    )
    assert report.ok is False


def test_doctor_report_ok_true_when_extract_required_failure_false() -> None:
    """``ok`` stays True when extract has only optional issues."""
    base_ip = IPCheck(
        status=IPStatus.UNCONFIGURED,
        ip='203.0.113.1',
        expected_cidrs=(),
        error=None,
    )
    cred_ok = CredCheck(
        publisher=Publisher.WILEY,
        status=CredStatus.NOT_CONFIGURED,
        smoke_doi=SMOKE_DOI[Publisher.WILEY],
        detail='no credential set',
    )
    extract = ExtractReport(
        components=_ok_extract_components(),
        has_required_failure=False,
    )
    report = DoctorReport(
        ip_check=base_ip,
        cred_checks=(cred_ok,),
        extract_check=extract,
    )
    assert report.ok is True


def test_render_doctor_report_with_extract_section() -> None:
    """Golden-output rows for the third table.

    Pins: the table title, every column header, each component label,
    each ``required`` cell, each rendered status, and the hint snippets
    in the detail column. Width-stable (``width=120``, no color).
    """
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
        ),
        extract_check=ExtractReport(
            components=_ok_extract_components(with_smoke=True),
            has_required_failure=False,
        ),
    )
    output = _capture(report)
    # Section title + columns
    assert 'Extract components' in output
    assert 'Component' in output
    assert 'Required' in output
    assert 'Hint' in output
    # Every component row renders
    for component in (
        'docling[extract]',
        'layout model',
        'TableFormer',
        'code-formula',
        'accelerator',
        'OCR engines',
        'smoke convert',
    ):
        assert component in output, component
    # Status labels per StrEnum value
    for label in ('ok', 'cuda', 'off'):
        assert label in output, label
    # ``Required`` cell variants
    assert 'auto' in output  # accelerator row
    # Hint snippets
    assert 'docling 2.93.0' in output
    assert 'accurate mode loaded' in output
    assert 'formula enrichment loaded' in output
    assert 'do_ocr=False' in output


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
