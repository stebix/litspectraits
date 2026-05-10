"""Tests for :mod:`litspectraits.cli` (``docs/overview-v3.md`` §17.9).

Each Typer command is invoked via :class:`typer.testing.CliRunner`. The
backends (``litspectraits.ingest.ingest``, ``litspectraits.doctor.run_doctor``)
are mocked at the module-attribute level so we exercise the CLI's
exit-code mapping, ``--json`` plumbing, and Rich rendering without
touching CrossRef or any publisher.

The tests assert observable behaviour: process exit code, stdout
content (the JSON payload, the table), and that Rich error output goes
to stderr.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.cli import app
from litspectraits.doctor import (
    CredCheck,
    CredStatus,
    DoctorReport,
    IPCheck,
    IPStatus,
)
from litspectraits.errors import (
    AuthRejectedError,
    DOINotFoundError,
    EntitlementDowngradeError,
    IntegrityError,
    MalformedArtifactError,
    MissingCredentialError,
    PublisherAPIError,
    RateLimitExhaustedError,
    UnsupportedPublisherError,
)
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    Publisher,
    converter,
)
from litspectraits.store import ArtifactStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    # Click 8.3+ always separates ``result.stdout`` and ``result.stderr``;
    # the legacy ``mix_stderr`` kwarg was removed. We rely on the split
    # to assert that Rich error panels go to stderr while --json lands
    # on stdout.
    return CliRunner()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Minimal environment: required vars set, data_dir under tmp.

    Every CLI command calls ``Settings.from_env()`` at startup, so the
    test process needs ``LITSPECTRAITS_CONTACT_EMAIL`` plus a writable
    data directory. We unset publisher creds by default; tests that
    need them set them explicitly.

    The CLI's startup callback calls ``dotenv.load_dotenv()`` to pick
    up the operator's project ``.env``. In tests that would silently
    repopulate vars we just deleted (and inject real publisher tokens
    from the developer's ``.env`` into a test process), so we stub it
    out — the autouse env above is authoritative.
    """
    monkeypatch.setattr('litspectraits.cli.dotenv.load_dotenv', lambda *a, **kw: False)
    monkeypatch.setenv('LITSPECTRAITS_CONTACT_EMAIL', 'test@example.com')
    monkeypatch.setenv('LITSPECTRAITS_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('LITSPECTRAITS_LOG_FORMAT', 'json')
    # Rich's stdout console takes its width from the runtime terminal
    # (or COLUMNS); CliRunner has no tty so Rich falls back to 80 cols
    # and truncates 64-char sha256 values with "…". Real terminals are
    # wider — pin COLUMNS so the test sees what an operator sees.
    monkeypatch.setenv('COLUMNS', '200')
    for var in (
        'WILEY_TDM_TOKEN',
        'SPRINGER_API_KEY',
        'ELSEVIER_API_KEY',
        'ELSEVIER_INSTTOKEN',
        'LITSPECTRAITS_EXPECTED_EGRESS_CIDRS',
    ):
        monkeypatch.delenv(var, raising=False)


def _example_record(*, doi: str = '10.1002/mrm.27973') -> AcquisitionRecord:
    sha = 'a' * 64
    return AcquisitionRecord(
        doi=doi,
        sha256=sha,
        artifact_path=f'artifacts/pdf/sha256/aa/{sha}.pdf',
        format=Format.PDF,
        publisher=Publisher.WILEY,
        metadata=CrossRefMetadata(
            doi=doi,
            publisher_str='Wiley',
            title='Example paper',
            authors=('Doe, Jane',),
            year=2024,
            type='journal-article',
            license='https://creativecommons.org/licenses/by/4.0/',
        ),
        fetched_url='https://api.wiley.com/onlinelibrary/tdm/v1/articles/' + doi,
        fetched_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='wiley-tdm 1.0.0',
        byte_size=12345,
        origin='auto',
        manual_provenance=None,
    )


def _patch_ingest(
    monkeypatch: pytest.MonkeyPatch, behaviour: Callable[..., AcquisitionRecord]
) -> None:
    """Replace ``cli``-bound ``run_ingest`` with an async stub.

    The CLI imports ``ingest as run_ingest``; patching that name (rather
    than ``litspectraits.ingest.ingest``) avoids the need to also
    monkeypatch retriever dispatch and CrossRef. Tests pass a sync
    callable that builds the record; the wrapper makes it awaitable.
    """

    async def _async_stub(*args: object, **kwargs: object) -> AcquisitionRecord:
        return behaviour(*args, **kwargs)

    monkeypatch.setattr('litspectraits.cli.run_ingest', _async_stub)


def _patch_ingest_to_raise(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    async def _async_stub(*args: object, **kwargs: object) -> AcquisitionRecord:
        raise exc

    monkeypatch.setattr('litspectraits.cli.run_ingest', _async_stub)


def _patch_doctor(monkeypatch: pytest.MonkeyPatch, report: DoctorReport) -> None:
    async def _async_stub(*args: object, **kwargs: object) -> DoctorReport:
        return report

    monkeypatch.setattr('litspectraits.cli.run_doctor', _async_stub)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version_command_prints_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ['version'])
    assert result.exit_code == 0
    assert result.stdout.startswith('litspectraits ')


# ---------------------------------------------------------------------------
# ingest — happy paths
# ---------------------------------------------------------------------------


def test_ingest_text_mode_renders_record_panel(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _example_record()
    _patch_ingest(monkeypatch, lambda *a, **kw: record)
    result = runner.invoke(app, ['ingest', record.doi])
    assert result.exit_code == 0
    # Panel headers / record fields land on stdout.
    assert record.doi in result.stdout
    assert 'wiley' in result.stdout
    assert 'pdf' in result.stdout
    assert record.sha256 in result.stdout


def test_ingest_json_mode_emits_machine_readable_payload(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _example_record()
    _patch_ingest(monkeypatch, lambda *a, **kw: record)
    result = runner.invoke(app, ['ingest', '--json', record.doi])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # Round-trips through cattrs back to an equivalent record.
    assert converter.structure(payload, AcquisitionRecord) == record


def test_ingest_passes_cache_hit_ok_to_orchestrator(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_kwargs: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> AcquisitionRecord:
        del args
        captured_kwargs.update(kwargs)
        return _example_record()

    _patch_ingest(monkeypatch, _capture)
    result = runner.invoke(app, ['ingest', '--cache-hit-ok', '10.1002/mrm.27973'])
    assert result.exit_code == 0
    assert captured_kwargs['cache_hit_ok'] is True


# ---------------------------------------------------------------------------
# ingest — exit-code matrix per §14
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('exc_factory', 'expected_code'),
    [
        (
            lambda doi: DOINotFoundError(doi=doi, http_status=404),
            2,
        ),
        (
            lambda doi: UnsupportedPublisherError(doi=doi, prefix='10.9999'),
            2,
        ),
        (
            lambda doi: MissingCredentialError(
                doi=doi, publisher='wiley', hint='set token'
            ),
            2,
        ),
        (
            lambda doi: AuthRejectedError(doi=doi, publisher='wiley', http_status=403),
            4,
        ),
        (
            lambda doi: EntitlementDowngradeError(
                doi=doi, publisher='elsevier', hint='META_ABS'
            ),
            4,
        ),
        (
            lambda doi: RateLimitExhaustedError(
                doi=doi, publisher='wiley', attempts=3
            ),
            5,
        ),
        (
            lambda doi: PublisherAPIError(doi=doi, publisher='wiley', http_status=500),
            5,
        ),
        (
            lambda doi: MalformedArtifactError(
                doi=doi,
                expected=Format.PDF.value,
                detected='unrecognized',
                path='/tmp/x',
                byte_size=42,
            ),
            6,
        ),
        (
            lambda doi: IntegrityError(
                doi=doi,
                sha256='a' * 64,
                existing_size=100,
                incoming_size=200,
                artifact_path='artifacts/pdf/sha256/aa/x.pdf',
            ),
            7,
        ),
    ],
)
def test_ingest_exit_codes_per_error_class(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Callable[[str], Exception],
    expected_code: int,
) -> None:
    doi = '10.1002/mrm.27973'
    _patch_ingest_to_raise(monkeypatch, exc_factory(doi))
    result = runner.invoke(app, ['ingest', doi])
    assert result.exit_code == expected_code
    # Class name appears in the Rich error panel on stderr.
    assert exc_factory(doi).__class__.__name__ in result.stderr


def test_ingest_invalid_doi_exits_2(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Don't even patch the orchestrator — InvalidDOIError fires inside
    # ``normalize`` long before run_ingest is called.
    result = runner.invoke(app, ['ingest', 'not a doi'])
    assert result.exit_code == 2
    assert 'Invalid DOI' in result.stderr


# ---------------------------------------------------------------------------
# sideload
# ---------------------------------------------------------------------------


def _patch_sideload(
    monkeypatch: pytest.MonkeyPatch, behaviour: Callable[..., AcquisitionRecord]
) -> None:
    async def _async_stub(*args: object, **kwargs: object) -> AcquisitionRecord:
        return behaviour(*args, **kwargs)

    monkeypatch.setattr('litspectraits.cli.run_sideload', _async_stub)


def _patch_sideload_to_raise(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    async def _async_stub(*args: object, **kwargs: object) -> AcquisitionRecord:
        raise exc

    monkeypatch.setattr('litspectraits.cli.run_sideload', _async_stub)


def _example_manual_record(*, doi: str = '10.1002/sideload.cli') -> AcquisitionRecord:
    from litspectraits.manifest import ManualProvenance

    sha = 'b' * 64
    return AcquisitionRecord(
        doi=doi,
        sha256=sha,
        artifact_path=f'artifacts/pdf/sha256/bb/{sha}.pdf',
        format=Format.PDF,
        publisher=Publisher.WILEY,
        metadata=CrossRefMetadata(
            doi=doi,
            publisher_str='Wiley',
            title='A sideloaded paper',
            authors=('Doe, Jane',),
            year=2024,
            type='journal-article',
            license='https://creativecommons.org/licenses/by/4.0/',
        ),
        fetched_url='',
        fetched_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='manual',
        byte_size=12345,
        origin='manual',
        manual_provenance=ManualProvenance(
            operator='test@example.com',
            retrieved_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
            source_url='https://onlinelibrary.wiley.com/doi/pdf/' + doi,
            note='via library proxy',
            license_assertion='wiley-tdm-internal-use-only',
        ),
    )


@pytest.fixture
def sideload_pdf(tmp_path: Path) -> Path:
    """A real on-disk PDF that satisfies Typer's ``exists=True`` check.

    The CLI tests stub the sideload orchestrator, so the file's *contents*
    never get sniffed or hashed — but Typer rejects the call before our
    code runs if the path doesn't exist on disk, hence a real file.
    """
    path = tmp_path / 'operator.pdf'
    path.write_bytes(b'%PDF-1.7\nmock body\n%%EOF\n')
    return path


def test_sideload_text_mode_renders_record_panel(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, sideload_pdf: Path
) -> None:
    record = _example_manual_record()
    _patch_sideload(monkeypatch, lambda *a, **kw: record)
    result = runner.invoke(
        app,
        [
            'sideload',
            record.doi,
            str(sideload_pdf),
            '--license',
            'wiley-tdm-internal-use-only',
            '--source-url',
            'https://onlinelibrary.wiley.com/doi/pdf/' + record.doi,
            '--note',
            'via library proxy',
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert record.doi in result.stdout
    assert 'manual' in result.stdout
    assert record.sha256 in result.stdout


def test_sideload_json_mode_emits_machine_readable_payload(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, sideload_pdf: Path
) -> None:
    record = _example_manual_record()
    _patch_sideload(monkeypatch, lambda *a, **kw: record)
    result = runner.invoke(
        app,
        [
            'sideload',
            '--json',
            record.doi,
            str(sideload_pdf),
            '--license',
            'cc-by-4.0',
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert converter.structure(payload, AcquisitionRecord) == record


def test_sideload_forwards_optional_arguments_to_orchestrator(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, sideload_pdf: Path
) -> None:
    """``--license`` / ``--source-url`` / ``--note`` reach the orchestrator."""
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> AcquisitionRecord:
        del args
        captured.update(kwargs)
        return _example_manual_record()

    _patch_sideload(monkeypatch, _capture)
    result = runner.invoke(
        app,
        [
            'sideload',
            '10.1002/sideload.cli',
            str(sideload_pdf),
            '--license',
            'cc-by-4.0',
            '--source-url',
            'https://example.org/x.pdf',
            '--note',
            'retrieved 2026-05-11',
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert captured['license_assertion'] == 'cc-by-4.0'
    assert captured['source_url'] == 'https://example.org/x.pdf'
    assert captured['note'] == 'retrieved 2026-05-11'


def test_sideload_default_source_url_and_note_when_omitted(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, sideload_pdf: Path
) -> None:
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> AcquisitionRecord:
        del args
        captured.update(kwargs)
        return _example_manual_record()

    _patch_sideload(monkeypatch, _capture)
    result = runner.invoke(
        app,
        [
            'sideload',
            '10.1002/sideload.cli',
            str(sideload_pdf),
            '--license',
            'unknown',
        ],
    )
    assert result.exit_code == 0
    assert captured['source_url'] is None
    assert captured['note'] == ''


@pytest.mark.parametrize(
    ('exc_factory', 'expected_code'),
    [
        (
            lambda doi: MalformedArtifactError(
                doi=doi,
                expected=Format.PDF.value,
                detected='unrecognized',
                path='/tmp/x',
                byte_size=42,
            ),
            6,
        ),
        (
            lambda doi: UnsupportedPublisherError(doi=doi, prefix='10.9999'),
            2,
        ),
        (
            lambda doi: DOINotFoundError(doi=doi, http_status=404),
            2,
        ),
        (
            lambda doi: IntegrityError(
                doi=doi,
                sha256='a' * 64,
                existing_size=100,
                incoming_size=200,
                artifact_path='artifacts/pdf/sha256/aa/x.pdf',
            ),
            7,
        ),
    ],
)
def test_sideload_exit_codes_per_error_class(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    sideload_pdf: Path,
    exc_factory: Callable[[str], Exception],
    expected_code: int,
) -> None:
    doi = '10.1002/sideload.cli'
    _patch_sideload_to_raise(monkeypatch, exc_factory(doi))
    result = runner.invoke(
        app,
        ['sideload', doi, str(sideload_pdf), '--license', 'unknown'],
    )
    assert result.exit_code == expected_code
    assert exc_factory(doi).__class__.__name__ in result.stderr


def test_sideload_invalid_doi_exits_2(
    runner: CliRunner, sideload_pdf: Path
) -> None:
    """A non-DOI shape is rejected with Invalid DOI panel."""
    result = runner.invoke(
        app,
        ['sideload', 'not a doi', str(sideload_pdf), '--license', 'unknown'],
    )
    assert result.exit_code == 2
    assert 'Invalid DOI' in result.stderr


def test_sideload_missing_pdf_path_typer_exit(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Typer's path-existence check rejects nonexistent paths before us.

    The Typer/Click error message goes to stderr with exit code 2.
    """
    missing = tmp_path / 'does-not-exist.pdf'
    result = runner.invoke(
        app,
        ['sideload', '10.1002/sideload.cli', str(missing), '--license', 'unknown'],
    )
    assert result.exit_code == 2


def test_sideload_missing_license_flag_typer_exit(
    runner: CliRunner, sideload_pdf: Path
) -> None:
    """``--license`` is mandatory; Typer rejects when omitted."""
    result = runner.invoke(
        app,
        ['sideload', '10.1002/sideload.cli', str(sideload_pdf)],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_text_mode_renders_record_when_present(
    runner: CliRunner, tmp_path: Path
) -> None:
    record = _example_record()
    store = ArtifactStore(tmp_path)
    # Plant the manifest + index entry directly so show() finds them.
    store.manifest_path(record.sha256).parent.mkdir(parents=True, exist_ok=True)
    store.manifest_path(record.sha256).write_text(
        json.dumps(converter.unstructure(record), indent=2)
    )
    with store.index_path.open('a', encoding='utf-8') as fp:
        fp.write(
            json.dumps(
                {
                    'doi': record.doi,
                    'sha256': record.sha256,
                    'format': record.format.value,
                    'added_at': '2026-05-10T12:00:00+00:00',
                }
            )
            + '\n'
        )

    result = runner.invoke(app, ['show', record.doi])
    assert result.exit_code == 0
    assert record.doi in result.stdout
    assert record.sha256 in result.stdout


def test_show_json_mode_emits_record_payload(
    runner: CliRunner, tmp_path: Path
) -> None:
    record = _example_record()
    store = ArtifactStore(tmp_path)
    store.manifest_path(record.sha256).parent.mkdir(parents=True, exist_ok=True)
    store.manifest_path(record.sha256).write_text(
        json.dumps(converter.unstructure(record), indent=2)
    )
    with store.index_path.open('a', encoding='utf-8') as fp:
        fp.write(
            json.dumps(
                {
                    'doi': record.doi,
                    'sha256': record.sha256,
                    'format': record.format.value,
                    'added_at': '2026-05-10T12:00:00+00:00',
                }
            )
            + '\n'
        )

    result = runner.invoke(app, ['show', '--json', record.doi])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert converter.structure(payload, AcquisitionRecord) == record


def test_show_missing_doi_exits_1_text_mode(runner: CliRunner) -> None:
    result = runner.invoke(app, ['show', '10.1002/never.ingested'])
    assert result.exit_code == 1
    assert 'not in local store' in result.stderr


def test_show_missing_doi_emits_json_with_found_false(runner: CliRunner) -> None:
    result = runner.invoke(app, ['show', '--json', '10.1002/never.ingested'])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {'doi': '10.1002/never.ingested', 'found': False}


def test_show_invalid_doi_exits_2(runner: CliRunner) -> None:
    result = runner.invoke(app, ['show', 'not a doi'])
    assert result.exit_code == 2
    assert 'Invalid DOI' in result.stderr


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_all_green_exits_0(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = DoctorReport(
        ip_check=IPCheck(
            status=IPStatus.OK,
            ip='132.187.1.1',
            expected_cidrs=('132.187.0.0/16',),
            error=None,
        ),
        cred_checks=tuple(
            CredCheck(
                publisher=p,
                status=CredStatus.OK,
                smoke_doi=SMOKE_DOI[p],
                detail='fetched ok',
            )
            for p in Publisher
        ),
    )
    _patch_doctor(monkeypatch, report)
    result = runner.invoke(app, ['doctor'])
    assert result.exit_code == 0
    # Table rendered to stdout.
    assert 'Egress IP' in result.stdout
    assert 'Publisher credentials' in result.stdout


def test_doctor_no_creds_configured_exits_0(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = DoctorReport(
        ip_check=IPCheck(
            status=IPStatus.UNCONFIGURED,
            ip='203.0.113.5',
            expected_cidrs=(),
            error=None,
        ),
        cred_checks=tuple(
            CredCheck(
                publisher=p,
                status=CredStatus.NOT_CONFIGURED,
                smoke_doi=SMOKE_DOI[p],
                detail='no credential set',
            )
            for p in Publisher
        ),
    )
    _patch_doctor(monkeypatch, report)
    result = runner.invoke(app, ['doctor'])
    assert result.exit_code == 0


def test_doctor_failed_credential_exits_1(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = DoctorReport(
        ip_check=IPCheck(
            status=IPStatus.OK,
            ip='132.187.1.1',
            expected_cidrs=('132.187.0.0/16',),
            error=None,
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
    _patch_doctor(monkeypatch, report)
    result = runner.invoke(app, ['doctor'])
    assert result.exit_code == 1
    assert 'auth rejected' in result.stdout


# ---------------------------------------------------------------------------
# Configuration error path
# ---------------------------------------------------------------------------


def test_missing_contact_email_exits_2_with_panel(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.delenv('LITSPECTRAITS_CONTACT_EMAIL', raising=False)
    result = runner.invoke(app, ['ingest', '10.1002/mrm.27973'])
    assert result.exit_code == 2
    assert 'Configuration error' in result.stderr
    assert 'LITSPECTRAITS_CONTACT_EMAIL' in result.stderr


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def smoke_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Confine ``cmd_smoke``'s mkdtemp under ``tmp_path``.

    The CLI's ``_make_smoke_dir`` indirection exists for exactly this:
    we redirect it here so the smoke tempdir lives under pytest's
    tmp_path and gets auto-cleaned with the test, without monkey-
    patching the stdlib ``tempfile`` module globally (which would also
    catch pytest's own tempdir machinery and is a known footgun).
    """
    import tempfile as _real_tempfile

    root = tmp_path / 'smoke-tmpdirs'
    root.mkdir()

    def _scoped() -> Path:
        return Path(
            _real_tempfile.mkdtemp(prefix='litspectraits-smoke-', dir=str(root))
        )

    monkeypatch.setattr('litspectraits.cli._make_smoke_dir', _scoped)
    return root


def _smoke_dirs(root: Path) -> list[Path]:
    """List of currently-existing smoke tempdirs under ``root``."""
    return [p for p in root.iterdir() if p.is_dir()]


def test_smoke_success_cleans_up_tempdir(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    smoke_root: Path,
) -> None:
    record = _example_record()
    _patch_ingest(monkeypatch, lambda *a, **kw: record)
    result = runner.invoke(app, ['smoke', record.doi])
    assert result.exit_code == 0, result.stderr
    # The smoke dir was created (path printed to stderr) and then removed.
    assert 'smoke data_dir:' in result.stderr
    assert _smoke_dirs(smoke_root) == []
    # Manifest still rendered to stdout.
    assert record.doi in result.stdout
    assert record.sha256 in result.stdout


def test_smoke_keep_preserves_tempdir(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    smoke_root: Path,
) -> None:
    record = _example_record()
    _patch_ingest(monkeypatch, lambda *a, **kw: record)
    result = runner.invoke(app, ['smoke', '--keep', record.doi])
    assert result.exit_code == 0
    remaining = _smoke_dirs(smoke_root)
    assert len(remaining) == 1
    # Path of the preserved dir is announced on stderr in green.
    assert 'preserved smoke data_dir' in result.stderr
    assert str(remaining[0]) in result.stderr


def test_smoke_failure_preserves_tempdir(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    smoke_root: Path,
) -> None:
    doi = '10.1002/mrm.27973'
    _patch_ingest_to_raise(
        monkeypatch,
        MissingCredentialError(doi=doi, publisher='wiley', hint='set token'),
    )
    result = runner.invoke(app, ['smoke', doi])
    # MissingCredentialError → exit 2 (same matrix as ingest).
    assert result.exit_code == 2
    # Tempdir preserved for inspection regardless of the --keep flag.
    remaining = _smoke_dirs(smoke_root)
    assert len(remaining) == 1
    assert 'preserved smoke data_dir for inspection' in result.stderr
    assert 'MissingCredentialError' in result.stderr


def test_smoke_uses_isolated_data_dir_not_env_data_dir(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    smoke_root: Path,
    tmp_path: Path,
) -> None:
    """The orchestrator must see the smoke tempdir, not LITSPECTRAITS_DATA_DIR."""
    captured: dict[str, object] = {}

    def _capture(doi: str, **kwargs: object) -> AcquisitionRecord:
        captured['doi'] = doi
        captured['data_dir'] = kwargs['settings'].data_dir  # type: ignore[union-attr]
        return _example_record(doi=doi)

    _patch_ingest(monkeypatch, _capture)
    result = runner.invoke(app, ['smoke', '10.1002/mrm.27973'])
    assert result.exit_code == 0
    smoke_dir = captured['data_dir']
    assert isinstance(smoke_dir, Path)
    # The data_dir handed to the orchestrator lives under our smoke root,
    # NOT under LITSPECTRAITS_DATA_DIR (which the autouse _env fixture
    # already pointed at tmp_path itself).
    assert smoke_dir.is_relative_to(smoke_root)
    assert not smoke_dir.is_relative_to(tmp_path / 'data')  # any non-smoke subtree


def test_smoke_json_mode_emits_manifest(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    smoke_root: Path,
) -> None:
    record = _example_record()
    _patch_ingest(monkeypatch, lambda *a, **kw: record)
    result = runner.invoke(app, ['smoke', '--json', record.doi])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert converter.structure(payload, AcquisitionRecord) == record


def test_smoke_invalid_doi_exits_2_and_preserves_tempdir(
    runner: CliRunner,
    smoke_root: Path,
) -> None:
    # No backend stub needed — InvalidDOIError fires inside the
    # orchestrator's normalize() before any retriever runs. Smoke still
    # treats this as a failure (no orchestrator success), so the
    # tempdir is preserved.
    result = runner.invoke(app, ['smoke', 'not a doi'])
    assert result.exit_code == 2
    assert 'Invalid DOI' in result.stderr
    remaining = _smoke_dirs(smoke_root)
    assert len(remaining) == 1
    assert 'preserved smoke data_dir for inspection' in result.stderr
