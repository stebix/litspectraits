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
    DoclingConversionError,
    DoclingDegradedError,
    DoclingImportError,
    DOINotFoundError,
    EmptyDocumentError,
    EntitlementDowngradeError,
    ExtractIntegrityError,
    IntegrityError,
    MalformedArtifactError,
    MalformedDocumentError,
    MissingArtifactError,
    MissingCredentialError,
    MissingModelWeightsError,
    ParseDegradedError,
    PublisherAPIError,
    RateLimitExhaustedError,
    SerializationError,
    UnknownBackendError,
    UnsupportedPublisherError,
    WrongFormatForExtractorError,
)
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Extractor,
    ExtractRecord,
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
        'SPRINGER_OA_API_KEY',
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


def _patch_ingest_to_raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
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
            lambda doi: MissingCredentialError(doi=doi, publisher='wiley', hint='set token'),
            2,
        ),
        (
            lambda doi: AuthRejectedError(doi=doi, publisher='wiley', http_status=403),
            4,
        ),
        (
            lambda doi: EntitlementDowngradeError(doi=doi, publisher='elsevier', hint='META_ABS'),
            4,
        ),
        (
            lambda doi: RateLimitExhaustedError(doi=doi, publisher='wiley', attempts=3),
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


def test_ingest_invalid_doi_exits_2(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _patch_sideload_to_raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
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


def test_sideload_invalid_doi_exits_2(runner: CliRunner, sideload_pdf: Path) -> None:
    """A non-DOI shape is rejected with Invalid DOI panel."""
    result = runner.invoke(
        app,
        ['sideload', 'not a doi', str(sideload_pdf), '--license', 'unknown'],
    )
    assert result.exit_code == 2
    assert 'Invalid DOI' in result.stderr


def test_sideload_missing_pdf_path_typer_exit(runner: CliRunner, tmp_path: Path) -> None:
    """Typer's path-existence check rejects nonexistent paths before us.

    The Typer/Click error message goes to stderr with exit code 2.
    """
    missing = tmp_path / 'does-not-exist.pdf'
    result = runner.invoke(
        app,
        ['sideload', '10.1002/sideload.cli', str(missing), '--license', 'unknown'],
    )
    assert result.exit_code == 2


def test_sideload_missing_license_flag_typer_exit(runner: CliRunner, sideload_pdf: Path) -> None:
    """``--license`` is mandatory; Typer rejects when omitted."""
    result = runner.invoke(
        app,
        ['sideload', '10.1002/sideload.cli', str(sideload_pdf)],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_text_mode_renders_record_when_present(runner: CliRunner, tmp_path: Path) -> None:
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


def test_show_json_mode_emits_record_payload(runner: CliRunner, tmp_path: Path) -> None:
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


def _plant_extraction(
    record: AcquisitionRecord, *, store: ArtifactStore, meta: dict[str, object]
) -> None:
    """Plant a minimal ``document.json`` + ``meta.json`` for ``record``.

    Enough for the ``show`` / ``list`` extraction probes to fire; the bytes
    are irrelevant (the CLI never parses them, only checks existence and
    reads a handful of meta keys).
    """
    doc_dir = store.document_dir(record.sha256)
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / 'document.json').write_text('{}', encoding='utf-8')
    (doc_dir / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')


def test_show_extraction_row_reflects_extractor_version(runner: CliRunner, tmp_path: Path) -> None:
    """Regression: the extraction row shows the real version, not '?'.

    The extract meta keys the version under ``extractor_version`` (it has no
    bare ``version`` key), so the old ``meta.get('version')`` lookup always
    rendered ``extracted (docling ?)``.
    """
    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    _plant_extraction(
        record,
        store=store,
        meta={'extractor': 'docling', 'extractor_version': 'docling 2.0.0'},
    )

    result = runner.invoke(app, ['show', record.doi])
    assert result.exit_code == 0
    assert 'docling 2.0.0' in result.stdout
    assert 'docling ?' not in result.stdout


def test_show_extraction_row_reflects_backend_id(runner: CliRunner, tmp_path: Path) -> None:
    """The extraction row surfaces the specific backend id, not just the tool.

    ``backend_id`` is what ``normalize`` dispatches on and (for a future
    docling-vlm) can differ from the coarse ``extractor`` enum value.
    """
    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    _plant_extraction(
        record,
        store=store,
        meta={
            'extractor': 'mineru',
            'extractor_version': 'mineru 3.4.0',
            'backend_id': 'mineru',
        },
    )

    result = runner.invoke(app, ['show', record.doi])
    assert result.exit_code == 0
    assert 'mineru 3.4.0' in result.stdout


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_all_green_exits_0(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
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
        return Path(_real_tempfile.mkdtemp(prefix='litspectraits-smoke-', dir=str(root)))

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


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def _example_extract_record(*, sha256: str = 'a' * 64) -> ExtractRecord:
    return ExtractRecord(
        sha256=sha256,
        extractor=Extractor.DOCLING,
        extractor_version='docling 2.0.0',
        extracted_at=datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC),
        n_text_blocks=217,
        n_section_headers=9,
        n_tables=4,
        n_figures=5,
        char_count=38421,
        n_pages=12,
    )


def _plant_record(
    record: AcquisitionRecord,
    *,
    store: ArtifactStore,
    added_at: str = '2026-05-10T12:00:00+00:00',
) -> None:
    """Plant the manifest + index entry so the CLI can look the record up.

    Mirrors what the show tests do: write the manifest JSON and append
    the by-doi index line. The artifact bytes themselves are not needed —
    the CLI's extract command never reads them; the (mocked) dispatcher
    does, and we mock at the dispatcher boundary.

    ``added_at`` is exposed so the ``list`` ordering tests can plant
    entries with distinct timestamps; it defaults to the value the other
    suites rely on.
    """
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
                    'added_at': added_at,
                }
            )
            + '\n'
        )


def _patch_extract(
    monkeypatch: pytest.MonkeyPatch, behaviour: Callable[..., ExtractRecord]
) -> None:
    """Replace ``cli``-bound ``run_extract`` with an async stub."""

    async def _async_stub(*args: object, **kwargs: object) -> ExtractRecord:
        return behaviour(*args, **kwargs)

    monkeypatch.setattr('litspectraits.cli.run_extract', _async_stub)


def _patch_extract_to_raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    async def _async_stub(*args: object, **kwargs: object) -> ExtractRecord:
        raise exc

    monkeypatch.setattr('litspectraits.cli.run_extract', _async_stub)


def test_extract_text_mode_renders_record_panel(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _example_record()
    extract_record = _example_extract_record(sha256=record.sha256)
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    _patch_extract(monkeypatch, lambda *a, **kw: extract_record)

    result = runner.invoke(app, ['extract', record.doi])
    assert result.exit_code == 0
    # Key fields rendered on stdout via the Rich table.
    assert record.doi in result.stdout
    assert extract_record.sha256 in result.stdout
    assert Extractor.DOCLING.value in result.stdout
    # The resolved backend id is surfaced as its own row (default here).
    assert 'docling-standard' in result.stdout
    # Comma-formatted counts (matches the `{n:,}` rendering).
    assert '38,421' in result.stdout


def test_extract_json_mode_emits_machine_readable_payload(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _example_record()
    extract_record = _example_extract_record(sha256=record.sha256)
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    _patch_extract(monkeypatch, lambda *a, **kw: extract_record)

    result = runner.invoke(app, ['extract', '--json', record.doi])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # DOI + backend_id are injected at the top level (parity with `ingest --json`);
    # backend_id is the resolved PDF backend, defaulting to docling-standard.
    assert payload['doi'] == record.doi
    assert payload['backend_id'] == 'docling-standard'
    # Stripping the injected keys must leave a clean ExtractRecord payload.
    payload.pop('doi')
    payload.pop('backend_id')
    assert converter.structure(payload, ExtractRecord) == extract_record


def test_extract_passes_reextract_flag_through_dispatch(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin --reextract → run_extract(reextract=True) wiring."""
    record = _example_record()
    extract_record = _example_extract_record(sha256=record.sha256)
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    captured: dict[str, object] = {}

    async def _async_stub(*args: object, **kwargs: object) -> ExtractRecord:
        captured['args'] = args
        captured['kwargs'] = kwargs
        return extract_record

    monkeypatch.setattr('litspectraits.cli.run_extract', _async_stub)

    result = runner.invoke(app, ['extract', '--reextract', record.doi])
    assert result.exit_code == 0
    # ``model_cache_dir`` rides along from Settings (None unless
    # LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR is set); ``backend`` defaults to
    # docling-standard when neither --backend nor LITSPECTRAITS_PDF_BACKEND is set.
    assert captured['kwargs'] == {
        'backend': 'docling-standard',
        'reextract': True,
        'model_cache_dir': None,
    }


def test_extract_backend_flag_threads_through_dispatch(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--backend mineru`` reaches run_extract(backend='mineru')."""
    record = _example_record()
    extract_record = _example_extract_record(sha256=record.sha256)
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    captured: dict[str, object] = {}

    async def _async_stub(*args: object, **kwargs: object) -> ExtractRecord:
        captured['kwargs'] = kwargs
        return extract_record

    monkeypatch.setattr('litspectraits.cli.run_extract', _async_stub)

    result = runner.invoke(app, ['extract', '--backend', 'mineru', record.doi])
    assert result.exit_code == 0
    kwargs = captured['kwargs']
    assert isinstance(kwargs, dict)
    assert kwargs['backend'] == 'mineru'
    # The chosen backend is surfaced in the panel's `backend` row.
    assert 'mineru' in result.stdout


def test_extract_invalid_backend_rejected_at_parse_time(runner: CliRunner) -> None:
    """An unwired --backend id is a click usage error, before target resolution.

    Exit 2 matches the dispatch-level ``BackendNotApplicableError`` code but
    fires earlier — no DOI/sha need exist locally — and lists the wired ids.
    """
    result = runner.invoke(app, ['extract', '--backend', 'banana', '10.1002/never.ingested'])
    assert result.exit_code == 2
    assert 'docling-standard' in result.stderr
    assert 'mineru' in result.stderr


# ---------------------------------------------------------------------------
# normalize — PDF backend dispatch (docs/mineru-backend-spec.md §1, §8)
# ---------------------------------------------------------------------------


def _plant_extract_meta(
    record: AcquisitionRecord, *, store: ArtifactStore, backend_id: str | None
) -> None:
    """Write a minimal extract ``meta.json`` carrying ``backend_id``.

    ``backend_id=None`` simulates a pre-pluggable-backend extraction (the
    field absent), which the normalize dispatch must reject loudly.
    """
    meta_dir = store.document_dir(record.sha256)
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {
        'extractor': 'docling',
        'format': 'pdf',
        'source_sha256': record.sha256,
    }
    if backend_id is not None:
        meta['backend_id'] = backend_id
    (meta_dir / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')


def _forbid_call(_payload: object) -> object:
    raise AssertionError('the wrong normalize adapter was dispatched')


def test_normalize_pdf_dispatches_docling_on_backend_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``backend_id='docling-standard'`` routes to the docling adapter."""
    from litspectraits.cli import _normalize_pdf

    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_extract_meta(record, store=store, backend_id='docling-standard')
    sentinel = object()
    monkeypatch.setattr('litspectraits.cli.normalize_docling_document', lambda _p: sentinel)
    monkeypatch.setattr('litspectraits.cli.normalize_mineru_document', _forbid_call)

    result = _normalize_pdf({'irrelevant': True}, store=store, record=record)
    assert result is sentinel


def test_normalize_pdf_dispatches_mineru_on_backend_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``backend_id='mineru'`` routes to the MinerU adapter, not docling."""
    from litspectraits.cli import _normalize_pdf

    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_extract_meta(record, store=store, backend_id='mineru')
    sentinel = object()
    monkeypatch.setattr('litspectraits.cli.normalize_mineru_document', lambda _p: sentinel)
    monkeypatch.setattr('litspectraits.cli.normalize_docling_document', _forbid_call)

    result = _normalize_pdf({'irrelevant': True}, store=store, record=record)
    assert result is sentinel


def test_normalize_pdf_missing_backend_id_raises(tmp_path: Path) -> None:
    """A pre-backend_id extraction (field absent) fails loud, not silently docling."""
    from litspectraits.cli import _normalize_pdf

    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_extract_meta(record, store=store, backend_id=None)

    with pytest.raises(UnknownBackendError):
        _normalize_pdf({'irrelevant': True}, store=store, record=record)


def test_normalize_pdf_unknown_backend_id_raises(tmp_path: Path) -> None:
    """An id no normalize adapter serves (e.g. unwired docling-vlm) fails loud."""
    from litspectraits.cli import _normalize_pdf

    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_extract_meta(record, store=store, backend_id='docling-vlm')

    with pytest.raises(UnknownBackendError):
        _normalize_pdf({'irrelevant': True}, store=store, record=record)


def test_extract_by_sha256_resolves_via_read_manifest(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 64-char hex argument routes through ``store.read_manifest``, not ``find_by_doi``.

    The fixture record has DOI ``10.1002/mrm.27973`` but we pass only the
    sha — verifying the regex-driven dispatch by checking the CLI still
    succeeds even though no DOI was given.
    """
    record = _example_record()
    extract_record = _example_extract_record(sha256=record.sha256)
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    _patch_extract(monkeypatch, lambda *a, **kw: extract_record)

    result = runner.invoke(app, ['extract', record.sha256])
    assert result.exit_code == 0
    assert record.sha256 in result.stdout


def test_extract_missing_doi_exits_1(runner: CliRunner) -> None:
    """A DOI absent from the local index must exit 1 with a friendly stderr.

    Mirrors ``show``'s contract: no traceback, no Rich error panel — just
    a one-line "not in local store" hint and the exit code that scripts
    can branch on.
    """
    result = runner.invoke(app, ['extract', '10.1002/never.ingested'])
    assert result.exit_code == 1
    assert 'not in local store' in result.stderr


def test_extract_missing_sha_exits_1(runner: CliRunner) -> None:
    """A sha with no manifest on disk surfaces ``FileNotFoundError`` cleanly."""
    result = runner.invoke(app, ['extract', 'b' * 64])
    assert result.exit_code == 1
    assert 'not in local store' in result.stderr
    assert 'sha256=' in result.stderr


def test_extract_invalid_doi_exits_2(runner: CliRunner) -> None:
    """Non-sha, non-DOI input exits 2 — same shape as ingest/show.

    Spaces in the argument disqualify it from the sha regex (anchored
    ``^[0-9a-f]{64}$``) so it falls through to ``normalize``, which
    raises :class:`InvalidDOIError`.
    """
    result = runner.invoke(app, ['extract', 'not a doi'])
    assert result.exit_code == 2
    assert 'Invalid DOI' in result.stderr


@pytest.mark.parametrize(
    ('exc_factory', 'expected_code'),
    [
        # Configuration / dispatch — exit 2.
        (
            lambda doi: DoclingImportError(doi=doi, hint='install [extract] extra'),
            2,
        ),
        (
            lambda doi: WrongFormatForExtractorError(
                doi=doi, expected='pdf', actual='jats_xml', extractor='docling'
            ),
            2,
        ),
        (
            lambda doi: MissingArtifactError(
                doi=doi, sha256='a' * 64, artifact_path='/gone', hint='re-ingest'
            ),
            2,
        ),
        (
            lambda doi: MissingModelWeightsError(
                doi=doi,
                extractor='docling',
                model_cache_dir='/cache',
                missing=['layout', 'code-formula'],
            ),
            2,
        ),
        # Conversion failures — exit 4.
        (
            lambda doi: DoclingConversionError(doi=doi, status='FAILURE'),
            4,
        ),
        (
            lambda doi: DoclingDegradedError(doi=doi, status='PARTIAL_SUCCESS'),
            4,
        ),
        # Malformed output — exit 6.
        (
            lambda doi: EmptyDocumentError(doi=doi, n_text_blocks=0, n_sections=0),
            6,
        ),
        (
            lambda doi: MalformedDocumentError(doi=doi, hint='META_ABS envelope'),
            6,
        ),
        (
            lambda doi: ParseDegradedError(doi=doi, char_count=10, floor=400),
            6,
        ),
        (
            lambda doi: SerializationError(doi=doi, error='unjsonable payload'),
            6,
        ),
        # Integrity — exit 7.
        (
            lambda doi: ExtractIntegrityError(
                doi=doi,
                sha256='a' * 64,
                existing_document_sha256='e' * 64,
                incoming_document_sha256='f' * 64,
            ),
            7,
        ),
    ],
)
def test_extract_exit_codes_per_error_class(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Callable[[str], Exception],
    expected_code: int,
) -> None:
    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    _patch_extract_to_raise(monkeypatch, exc_factory(record.doi))

    result = runner.invoke(app, ['extract', record.doi])
    assert result.exit_code == expected_code
    # Class name appears in the Rich error panel on stderr.
    assert exc_factory(record.doi).__class__.__name__ in result.stderr


def test_extract_error_panel_renders_class_name_doi_and_class_hint(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden-output check for the Rich error panel.

    Asserts the four panel invariants:

    1. The exception class name appears in the panel title.
    2. The DOI is rendered as a key/value row.
    3. Every context key/value is rendered (excluding ``hint``, which
       lives in its own row).
    4. When ``context['hint']`` is absent, the class-level fallback
       hint from ``_EXTRACT_HINTS`` is rendered instead — so the
       operator never sees an empty hint row.
    """
    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    exc = EmptyDocumentError(
        doi=record.doi,
        n_text_blocks=0,
        n_sections=0,
        extractor='docling',
    )
    _patch_extract_to_raise(monkeypatch, exc)

    result = runner.invoke(app, ['extract', record.doi])
    assert result.exit_code == 6
    # 1: class title.
    assert 'EmptyDocumentError' in result.stderr
    # 2: DOI row.
    assert record.doi in result.stderr
    # 3: every non-hint context entry.
    assert 'n_text_blocks' in result.stderr
    assert 'n_sections' in result.stderr
    assert 'extractor' in result.stderr
    # 4: class-level fallback hint (matches ``_EXTRACT_HINTS[EmptyDocumentError]``).
    assert 'scanned PDF served without OCR' in result.stderr


def test_extract_error_panel_uses_context_hint_when_present(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-call ``context['hint']`` wins over the class default.

    This is the contract that lets retrievers / extractors override the
    operator hint per call site (e.g. the JATS bodyless hint is
    different from the PDF empty-OCR hint, both of which surface as
    :class:`EmptyDocumentError`).
    """
    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    exc = EmptyDocumentError(
        doi=record.doi,
        n_text_blocks=0,
        n_sections=2,
        hint='custom per-call hint about a JATS bodyless envelope',
    )
    _patch_extract_to_raise(monkeypatch, exc)

    result = runner.invoke(app, ['extract', record.doi])
    assert result.exit_code == 6
    assert 'custom per-call hint about a JATS bodyless envelope' in result.stderr
    # The class-default hint must not also be rendered.
    assert 'scanned PDF served without OCR' not in result.stderr


# ---------------------------------------------------------------------------
# show-document
# ---------------------------------------------------------------------------


def _plant_normalized_document(record: AcquisitionRecord, *, store: ArtifactStore) -> None:
    """Stage a ``normalized/<sha>/document.json`` the renderer can load.

    Built via the XML adapter (no docling dependency) and written with
    the normalize converter — the same on-disk shape
    :func:`litspectraits.normalize.load_normalized_document` reads.
    """
    from litspectraits.normalize import converter as normalize_converter
    from litspectraits.normalize import normalize_xml_document

    doc = normalize_xml_document(
        {
            'front': {'title': 'Rendered paper', 'abstract': 'Abstract.'},
            'sections': [
                {
                    'id': 's1',
                    'title': 'Methods',
                    'level': 1,
                    'path': ['Methods'],
                    'blocks': [{'type': 'paragraph', 'text': 'Body text.', 'xrefs': []}],
                }
            ],
            'tables': [],
            'figures': [],
            'references': [],
        },
        route='jats',
    )
    target = store.normalized_dir(record.sha256) / 'document.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(normalize_converter.unstructure(doc)), encoding='utf-8')


def test_show_document_writes_html(runner: CliRunner, tmp_path: Path) -> None:
    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)
    _plant_normalized_document(record, store=store)
    out_path = tmp_path / 'out.html'

    result = runner.invoke(app, ['show-document', record.sha256, '--out', str(out_path)])

    assert result.exit_code == 0, result.stderr
    assert out_path.is_file()
    html = out_path.read_text(encoding='utf-8')
    assert '<!DOCTYPE html>' in html
    assert 'Rendered paper' in html
    # The written path is echoed to stdout for piping.
    assert str(out_path) in result.stdout


def test_show_document_not_normalised_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    record = _example_record()
    store = ArtifactStore(tmp_path)
    _plant_record(record, store=store)  # manifest present, but no normalized/ output

    result = runner.invoke(app, ['show-document', record.sha256])

    assert result.exit_code == 1
    assert 'not normalised' in result.stderr


def test_show_document_unknown_artifact_exits_1(runner: CliRunner) -> None:
    result = runner.invoke(app, ['show-document', 'f' * 64])
    assert result.exit_code == 1
    assert 'not in local store' in result.stderr


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _catalog_record(
    *,
    doi: str,
    sha: str,
    fmt: Format = Format.PDF,
    publisher: Publisher = Publisher.WILEY,
    title: str | None = 'Example paper',
    year: int | None = 2024,
) -> AcquisitionRecord:
    """Build a distinct AcquisitionRecord for the list-catalog tests.

    Varies DOI / sha / format / bibliographic fields so a catalog can be
    assembled from several of them; the fixed scalars mirror
    :func:`_example_record`.
    """
    fmt_dir = {Format.PDF: 'pdf', Format.JATS_XML: 'jats', Format.ELSEVIER_XML: 'elsevier'}[fmt]
    ext = 'pdf' if fmt is Format.PDF else 'xml'
    return AcquisitionRecord(
        doi=doi,
        sha256=sha,
        artifact_path=f'artifacts/{fmt_dir}/sha256/{sha[:2]}/{sha}.{ext}',
        format=fmt,
        publisher=publisher,
        metadata=CrossRefMetadata(
            doi=doi,
            publisher_str=publisher.value,
            title=title,
            authors=('Doe, Jane',),
            year=year,
            type='journal-article',
            license=None,
        ),
        fetched_url=f'https://example.org/{doi}',
        fetched_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='test 1.0.0',
        byte_size=123,
        origin='auto',
        manual_provenance=None,
    )


def test_list_text_mode_lists_dois(runner: CliRunner, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    _plant_record(
        _catalog_record(doi='10.1002/alpha', sha='a' * 64, title='Alpha paper'),
        store=store,
    )
    _plant_record(
        _catalog_record(
            doi='10.1016/beta', sha='b' * 64, publisher=Publisher.ELSEVIER, title='Beta paper'
        ),
        store=store,
    )

    result = runner.invoke(app, ['list'])

    assert result.exit_code == 0, result.stderr
    assert '10.1002/alpha' in result.stdout
    assert '10.1016/beta' in result.stdout
    assert 'Alpha paper' in result.stdout
    assert 'Beta paper' in result.stdout
    assert 'wiley' in result.stdout
    assert 'elsevier' in result.stdout


def test_list_quiet_emits_bare_dois_newest_first(runner: CliRunner, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    # Plant the older entry first; newest-first ordering must surface the
    # later-timestamped DOI at the top regardless of index append order.
    _plant_record(
        _catalog_record(doi='10.1002/older', sha='a' * 64),
        store=store,
        added_at='2026-05-01T00:00:00+00:00',
    )
    _plant_record(
        _catalog_record(doi='10.1016/newer', sha='b' * 64),
        store=store,
        added_at='2026-05-09T00:00:00+00:00',
    )

    result = runner.invoke(app, ['list', '-q'])

    assert result.exit_code == 0, result.stderr
    # Bare DOIs only — no table chrome, newest first.
    assert result.stdout.splitlines() == ['10.1016/newer', '10.1002/older']


def test_list_json_mode_emits_catalog_with_artifact_status(
    runner: CliRunner, tmp_path: Path
) -> None:
    store = ArtifactStore(tmp_path)
    sha = 'a' * 64
    _plant_record(_catalog_record(doi='10.1002/alpha', sha=sha, title='Alpha paper'), store=store)
    # Simulate a completed extraction (but no normalization) for this sha.
    document = store.document_dir(sha) / 'document.json'
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text('{}', encoding='utf-8')

    result = runner.invoke(app, ['list', '--json'])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert row['doi'] == '10.1002/alpha'
    assert row['title'] == 'Alpha paper'
    assert row['year'] == 2024
    assert row['publisher'] == 'wiley'
    assert row['artifacts'] == [
        {'format': 'pdf', 'sha256': sha, 'extracted': True, 'normalized': False}
    ]


def test_list_json_dual_format_reports_per_artifact(runner: CliRunner, tmp_path: Path) -> None:
    """A DOI with both a PDF and a JATS artifact yields one row, two artifacts."""
    store = ArtifactStore(tmp_path)
    doi = '10.1002/dual'
    pdf_sha = 'a' * 64
    jats_sha = 'b' * 64
    _plant_record(_catalog_record(doi=doi, sha=pdf_sha, fmt=Format.PDF), store=store)
    _plant_record(_catalog_record(doi=doi, sha=jats_sha, fmt=Format.JATS_XML), store=store)
    # Extract only the PDF side.
    pdf_doc = store.document_dir(pdf_sha) / 'document.json'
    pdf_doc.parent.mkdir(parents=True, exist_ok=True)
    pdf_doc.write_text('{}', encoding='utf-8')

    result = runner.invoke(app, ['list', '--json'])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    artifacts = {a['format']: a for a in payload[0]['artifacts']}
    assert set(artifacts) == {'pdf', 'jats_xml'}
    assert artifacts['pdf']['extracted'] is True
    assert artifacts['jats_xml']['extracted'] is False


def test_list_empty_store_text_mode_exits_0_with_hint(runner: CliRunner) -> None:
    result = runner.invoke(app, ['list'])
    assert result.exit_code == 0
    assert result.stdout == ''
    assert 'local store is empty' in result.stderr


def test_list_empty_store_json_mode_emits_empty_array(runner: CliRunner) -> None:
    result = runner.invoke(app, ['list', '--json'])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
