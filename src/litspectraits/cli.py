"""Command-line interface (``docs/overview-v3.md`` §10, §17.9).

Three commands ship in this iteration: ``ingest``, ``doctor``, ``show``.
``extract`` and ``sideload`` from the §10 surface land alongside their
backends — ``extract/`` (Step 10) and ``sideload.py`` (Step 8) — and are
intentionally absent until then. Operators who type
``litspectraits extract`` or ``litspectraits sideload`` get Typer's
"unknown command" message; we don't ship NotImplementedError stubs
because a stub would imply the surface is wired up but broken.

Failure model
-------------

Every typed :class:`~litspectraits.errors.IngestError` subclass maps to
a deterministic exit code (see :data:`_EXIT_CODES`, sourced from §14).
A Rich error panel renders to ``stderr`` so JSON mode (``--json``) can
keep ``stdout`` clean. ``InvalidDOIError`` is treated as exit 2
("operator-input shape error") even though it is not an
:class:`IngestError` — the operator-facing intent is the same as
``UnsupportedPublisherError``: their input is wrong, fix it.

Anything else escapes uncaught so the traceback is visible. Per
CLAUDE.md "fail loudly, no excessive exception catching" — masking an
unexpected error behind a generic exit code would hide bugs. The
``IngestError`` base class is a contract; everything outside that
contract is a programming error.

Output channels
---------------

- ``stdout`` is reserved for command output — ``--json`` payloads,
  doctor's tables, the per-record summary panel.
- ``stderr`` carries diagnostics: structlog output, Rich error panels.

This split matters once a batch ingest pipes ``--json`` into ``jq``;
mixing diagnostics into stdout would break that consumer silently.
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Final

import attrs
import dotenv
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from litspectraits import __version__
from litspectraits._logging import configure_logging
from litspectraits.config import MissingConfigError, Settings
from litspectraits.doctor import render as render_doctor_report
from litspectraits.doctor import run_doctor
from litspectraits.doi import InvalidDOIError, normalize
from litspectraits.errors import (
    AuthRejectedError,
    DOINotFoundError,
    EntitlementDowngradeError,
    IngestError,
    IntegrityError,
    MalformedArtifactError,
    MissingCredentialError,
    PublisherAPIError,
    RateLimitExhaustedError,
    UnsupportedPublisherError,
)
from litspectraits.http import http_client
from litspectraits.ingest import ingest as run_ingest
from litspectraits.manifest import AcquisitionRecord, converter
from litspectraits.store import ArtifactStore

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help='DOI-driven literature ingestion pipeline.',
)


# ---------------------------------------------------------------------------
# Exit-code matrix (§14)
# ---------------------------------------------------------------------------

_EXIT_CODES: Final[dict[type[IngestError], int]] = {
    DOINotFoundError: 2,
    UnsupportedPublisherError: 2,
    MissingCredentialError: 2,
    AuthRejectedError: 4,
    EntitlementDowngradeError: 4,
    RateLimitExhaustedError: 5,
    PublisherAPIError: 5,
    MalformedArtifactError: 6,
    IntegrityError: 7,
}

# Per-class operator hint shown in the Rich error panel. Falls back to
# the exception's own ``context['hint']`` when the class hint here is
# ``None`` so retrievers can override per call site (e.g. the Wiley
# missing-extra hint vs the missing-token hint share a class but differ
# in actionable advice).
_DEFAULT_HINTS: Final[dict[type[IngestError], str]] = {
    DOINotFoundError: 'CrossRef returned 404 — verify the DOI is correct',
    UnsupportedPublisherError: (
        'no retriever for this DOI prefix; widen the dispatch table in metadata.py'
    ),
    MissingCredentialError: (
        'configure the publisher credential and re-run; '
        '`litspectraits doctor` is the preflight'
    ),
    AuthRejectedError: (
        'publisher rejected the credential; check entitlement and IP allow-listing'
    ),
    EntitlementDowngradeError: (
        'Elsevier returned META_ABS — request a full-text title or run from an entitled IP'
    ),
    RateLimitExhaustedError: 'publisher 429d after retries; back off and retry later',
    PublisherAPIError: 'publisher 5xx or malformed response; rerun and escalate if persistent',
    MalformedArtifactError: 'artifact failed magic-byte sniff (paywall HTML, error page, etc.)',
    IntegrityError: (
        'sha256 collision against an existing artifact with different bytes; '
        'investigate before overwriting'
    ),
}


# ---------------------------------------------------------------------------
# Startup callback
# ---------------------------------------------------------------------------


@app.callback()
def _startup() -> None:
    """Run before every command — load ``.env`` and configure logging.

    Idempotent: ``configure_logging`` no-ops on subsequent calls and
    ``dotenv.load_dotenv()`` does not overwrite already-set vars.
    """
    dotenv.load_dotenv()
    configure_logging()


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@app.command(name='ingest')
def cmd_ingest(
    doi: str = typer.Argument(..., help='DOI to ingest (URL, doi:, or bare form).'),
    cache_hit_ok: bool = typer.Option(
        False,
        '--cache-hit-ok',
        help='If a manifest already exists for the DOI, return it without touching the network.',
    ),
    json_output: bool = typer.Option(
        False, '--json', help='Emit the manifest as JSON on stdout.'
    ),
) -> None:
    """Ingest one DOI through the v3 happy path."""
    asyncio.run(_run_ingest(doi=doi, cache_hit_ok=cache_hit_ok, json_output=json_output))


async def _run_ingest(*, doi: str, cache_hit_ok: bool, json_output: bool) -> None:
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()
    try:
        async with http_client(settings) as client:
            record = await run_ingest(
                doi,
                settings=settings,
                store=store,
                client=client,
                cache_hit_ok=cache_hit_ok,
            )
    except InvalidDOIError as exc:
        _render_invalid_doi(exc, console=err_console)
        raise typer.Exit(2) from exc
    except IngestError as exc:
        _render_error_panel(exc, console=err_console)
        raise typer.Exit(_EXIT_CODES.get(type(exc), 1)) from exc
    if json_output:
        _emit_record_json(record)
    else:
        _render_record_panel(record, settings=settings, console=out_console)


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------


@app.command(name='smoke')
def cmd_smoke(
    doi: str = typer.Argument(..., help='DOI to smoke-test (URL, doi:, or bare form).'),
    keep: bool = typer.Option(
        False,
        '--keep',
        help='Preserve the ephemeral data dir on success (always preserved on failure).',
    ),
    json_output: bool = typer.Option(
        False, '--json', help='Emit the manifest as JSON on stdout.'
    ),
) -> None:
    """End-to-end ingest in a throwaway data directory.

    Sets ``data_dir`` to a fresh ``tempfile.mkdtemp`` for the duration
    of the call. On success the directory is removed (use ``--keep`` to
    preserve it). On failure the directory is *always* preserved and its
    path is printed to stderr so the operator can inspect any partially
    staged bytes under ``tmp/``. Other settings (``contact_email``,
    publisher tokens, rate-limit overrides) are sourced from the
    operator's environment unchanged.

    Exit codes match :func:`cmd_ingest` — the smoke command is a thin
    isolation wrapper, not a different failure model.
    """
    asyncio.run(_run_smoke(doi=doi, keep=keep, json_output=json_output))


def _make_smoke_dir() -> Path:
    """Create the ephemeral smoke data dir.

    Indirection lets the test suite redirect ``mkdtemp`` under
    ``tmp_path`` without monkey-patching the stdlib ``tempfile``
    module globally (which would also catch pytest's own tempdir
    machinery). Production calls go straight to ``tempfile.mkdtemp``
    so the dir lands in the platform's normal temp root (typically
    ``/tmp``, often tmpfs).
    """
    return Path(tempfile.mkdtemp(prefix='litspectraits-smoke-'))


async def _run_smoke(*, doi: str, keep: bool, json_output: bool) -> None:
    base_settings = _load_settings()
    smoke_dir = _make_smoke_dir()
    settings = attrs.evolve(base_settings, data_dir=smoke_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()
    err_console.print(f'[dim]smoke data_dir: {smoke_dir}[/dim]')

    success = False
    try:
        store = ArtifactStore(smoke_dir)
        try:
            async with http_client(settings) as client:
                record = await run_ingest(
                    doi, settings=settings, store=store, client=client
                )
        except InvalidDOIError as exc:
            _render_invalid_doi(exc, console=err_console)
            raise typer.Exit(2) from exc
        except IngestError as exc:
            _render_error_panel(exc, console=err_console)
            raise typer.Exit(_EXIT_CODES.get(type(exc), 1)) from exc
        if json_output:
            _emit_record_json(record)
        else:
            _render_record_panel(record, settings=settings, console=out_console)
        success = True
    finally:
        if success and not keep:
            shutil.rmtree(smoke_dir, ignore_errors=True)
        elif success:
            err_console.print(
                f'[green]preserved smoke data_dir: {smoke_dir}[/green]'
            )
        else:
            err_console.print(
                f'[yellow]preserved smoke data_dir for inspection: {smoke_dir}[/yellow]'
            )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@app.command(name='show')
def cmd_show(
    doi: str = typer.Argument(..., help='DOI to look up in the local store.'),
    json_output: bool = typer.Option(
        False, '--json', help='Emit the manifest as JSON on stdout.'
    ),
) -> None:
    """Show the locally-stored manifest for ``doi`` (no network call).

    Exits 1 when the DOI has not been ingested locally; 2 when ``doi``
    is not a valid DOI shape (mirrors :class:`InvalidDOIError`'s exit
    code from ``ingest``).
    """
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()
    try:
        normalized = normalize(doi)
    except InvalidDOIError as exc:
        _render_invalid_doi(exc, console=err_console)
        raise typer.Exit(2) from exc
    record = store.find_by_doi(normalized)
    if record is None:
        if json_output:
            print(json.dumps({'doi': normalized, 'found': False}))
        else:
            err_console.print(
                f'[bold yellow]not in local store:[/bold yellow] {normalized}'
            )
        raise typer.Exit(1)
    if json_output:
        _emit_record_json(record)
    else:
        _render_record_panel(record, settings=settings, console=out_console)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command(name='doctor')
def cmd_doctor() -> None:
    """Operator preflight — egress IP + per-publisher credential smoke test."""
    settings = _load_settings()
    out_console = _stdout_console()
    report = asyncio.run(_run_doctor(settings=settings))
    render_doctor_report(report, console=out_console)
    if not report.ok:
        raise typer.Exit(1)


async def _run_doctor(*, settings: Settings):
    async with http_client(settings) as client:
        return await run_doctor(settings=settings, client=client)


# ---------------------------------------------------------------------------
# Settings + console helpers
# ---------------------------------------------------------------------------


def _load_settings() -> Settings:
    """Load :class:`Settings` from env, rendering a clean error on failure.

    Wraps :class:`MissingConfigError` so the operator sees a Rich panel
    instead of a Python traceback for the most common configuration
    mistake (forgot to set ``LITSPECTRAITS_CONTACT_EMAIL`` in ``.env``).
    """
    try:
        return Settings.from_env()
    except MissingConfigError as exc:
        console = _stderr_console()
        console.print(
            Panel(
                str(exc),
                title='[bold red]Configuration error[/bold red]',
                border_style='red',
            )
        )
        raise typer.Exit(2) from exc


def _stderr_console() -> Console:
    return Console(stderr=True, highlight=False)


def _stdout_console() -> Console:
    return Console(stderr=False, highlight=False)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _emit_record_json(record: AcquisitionRecord) -> None:
    """Print the manifest as JSON on stdout (sorted, indented)."""
    print(json.dumps(converter.unstructure(record), indent=2, sort_keys=True))


def _render_record_panel(
    record: AcquisitionRecord, *, settings: Settings, console: Console
) -> None:
    """Render a record summary as a Rich table.

    Includes an "extraction" row that probes for
    ``documents/<sha>/document.json`` so the same renderer serves
    ``ingest`` (where extraction always reads "not extracted") and a
    future ``show`` against an extracted record (Step 10).
    """
    table = Table(title=f'manifest — {record.doi}', show_header=False, expand=False)
    table.add_column('field', no_wrap=True, style='bold')
    table.add_column('value')
    table.add_row('publisher', record.publisher.value)
    table.add_row('format', record.format.value)
    table.add_row('sha256', record.sha256)
    table.add_row('byte_size', f'{record.byte_size:,}')
    table.add_row('license', record.metadata.license or '-')
    table.add_row('origin', record.origin)
    table.add_row('artifact', record.artifact_path)
    table.add_row('manifest', _manifest_relpath(record, settings=settings))
    table.add_row('fetched_at', record.fetched_at.isoformat())
    table.add_row('fetcher', f'litspectraits {record.fetcher_version}')
    table.add_row('sdk', record.sdk_version)
    table.add_row('extraction', _extraction_status(record, settings=settings))
    console.print(table)


def _manifest_relpath(record: AcquisitionRecord, *, settings: Settings) -> str:
    # Recompute via the shard rule for robustness — store.manifest_path
    # is the source of truth, but we prefer to render relative-to-
    # data_dir for portability.
    store = ArtifactStore(settings.data_dir)
    return store.manifest_path(record.sha256).relative_to(settings.data_dir).as_posix()


def _extraction_status(record: AcquisitionRecord, *, settings: Settings) -> str:
    """Probe for ``documents/<sha>/document.json``; report textually.

    Forward-compatible with Step 10 — once :mod:`litspectraits.extract`
    lands and writes the document tree, this row flips to "extracted"
    automatically. No coupling to the extractor module needed today.
    """
    document_path = settings.data_dir / 'documents' / record.sha256 / 'document.json'
    if document_path.is_file():
        meta_path = settings.data_dir / 'documents' / record.sha256 / 'meta.json'
        if meta_path.is_file():
            return f'extracted ({_brief_extractor(meta_path)})'
        return 'extracted'
    return 'not extracted'


def _brief_extractor(meta_path: Path) -> str:
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'meta unreadable'
    name = meta.get('extractor', '?')
    version = meta.get('version', '?')
    return f'{name} {version}'


def _render_invalid_doi(exc: InvalidDOIError, *, console: Console) -> None:
    console.print(
        Panel(
            str(exc),
            title='[bold red]Invalid DOI[/bold red]',
            border_style='red',
        )
    )


def _render_error_panel(exc: IngestError, *, console: Console) -> None:
    """Render a typed :class:`IngestError` as a Rich panel on stderr.

    Includes class name, DOI, every key/value in ``exc.context``, and an
    operator hint (per-call from ``context['hint']`` when present;
    otherwise the class default from :data:`_DEFAULT_HINTS`).
    """
    body = Table.grid(padding=(0, 1))
    body.add_column('key', no_wrap=True, style='bold')
    body.add_column('value')
    body.add_row('doi', exc.doi)
    for key, value in exc.context.items():
        if key == 'hint':
            continue  # rendered separately below
        body.add_row(key, str(value))
    hint = exc.context.get('hint') or _DEFAULT_HINTS.get(type(exc), '-')
    body.add_row('hint', str(hint))
    console.print(
        Panel(
            body,
            title=f'[bold red]{type(exc).__name__}[/bold red]',
            border_style='red',
        )
    )


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


@app.command(name='version')
def cmd_version() -> None:
    """Print the installed package version."""
    print(f'litspectraits {__version__}')
