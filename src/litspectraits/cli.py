"""Command-line interface (``docs/overview-v3.md`` §10, §17.9).

Five commands ship today: ``ingest``, ``sideload``, ``doctor``, ``show``,
``extract`` (plus the convenience ``smoke`` ephemeral-tempdir wrapper
around ``ingest``).

Failure model
-------------

Every typed :class:`~litspectraits.errors.IngestError` /
:class:`~litspectraits.errors.ExtractError` subclass maps to a
deterministic exit code (see :data:`_INGEST_EXIT_CODES` /
:data:`_EXTRACT_EXIT_CODES`, sourced from §14 and
``docs/extract-pdf-plan.md`` §5 respectively). A Rich error panel
renders to ``stderr`` so JSON mode (``--json``) can keep ``stdout``
clean. ``InvalidDOIError`` is treated as exit 2 ("operator-input shape
error") even though it is not an :class:`IngestError` — the operator-
facing intent is the same as :class:`UnsupportedPublisherError`: their
input is wrong, fix it.

Anything else escapes uncaught so the traceback is visible. Per
CLAUDE.md "fail loudly, no excessive exception catching" — masking an
unexpected error behind a generic exit code would hide bugs. The
:class:`IngestError` and :class:`ExtractError` base classes are
contracts; anything outside those contracts is a programming error.

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
import re
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
    DoclingConversionError,
    DoclingDegradedError,
    DoclingImportError,
    DOINotFoundError,
    EmptyDocumentError,
    EntitlementDowngradeError,
    ExtractError,
    ExtractIntegrityError,
    IngestError,
    IntegrityError,
    MalformedArtifactError,
    MalformedDocumentError,
    MissingArtifactError,
    MissingCredentialError,
    ParseDegradedError,
    PublisherAPIError,
    RateLimitExhaustedError,
    SerializationError,
    UnsupportedPublisherError,
    WrongFormatForExtractorError,
)
from litspectraits.extract import extract as run_extract
from litspectraits.http import http_client
from litspectraits.ingest import ingest as run_ingest
from litspectraits.manifest import AcquisitionRecord, ExtractRecord, converter
from litspectraits.sideload import sideload as run_sideload
from litspectraits.store import ArtifactStore

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help='DOI-driven literature ingestion pipeline.',
)


# ---------------------------------------------------------------------------
# Exit-code matrices
# ---------------------------------------------------------------------------
#
# Ingest exit codes track ``docs/overview-v3.md`` §14. Extract exit codes
# track ``docs/extract-pdf-plan.md`` §5 (config = 2, conversion = 4,
# malformed-output = 6, integrity = 7). The two trees are inheritance-
# disjoint so a single shared dict would also work, but separate per-tree
# matrices keep the type annotations crisp and make the per-pipeline
# scheme grep-able. The shared renderer below takes the right dict as a
# kwarg.

_INGEST_EXIT_CODES: Final[dict[type[IngestError], int]] = {
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


_EXTRACT_EXIT_CODES: Final[dict[type[ExtractError], int]] = {
    # Configuration / dispatch errors (operator must change inputs or env).
    DoclingImportError: 2,
    WrongFormatForExtractorError: 2,
    MissingArtifactError: 2,
    # Conversion / parse-time failures.
    DoclingConversionError: 4,
    DoclingDegradedError: 4,
    # Post-extraction "malformed output" failures. ``MalformedDocumentError``
    # was added in step 10c (post-dating ``extract-pdf-plan.md`` §5's
    # original table); it sits in the same bucket as ``EmptyDocumentError``
    # and ``ParseDegradedError`` — the artifact parsed but the structure we
    # expected wasn't there.
    EmptyDocumentError: 6,
    MalformedDocumentError: 6,
    ParseDegradedError: 6,
    SerializationError: 6,
    # Refused to overwrite a divergent on-disk extraction.
    ExtractIntegrityError: 7,
}


# Per-class operator hint shown in the Rich error panel. Falls back to
# the exception's own ``context['hint']`` when the class hint here is
# ``None`` so producers can override per call site (e.g. the Wiley
# missing-extra hint vs the missing-token hint share a class but differ
# in actionable advice).

_INGEST_HINTS: Final[dict[type[IngestError], str]] = {
    DOINotFoundError: 'CrossRef returned 404 — verify the DOI is correct',
    UnsupportedPublisherError: (
        'no retriever for this DOI prefix; widen the dispatch table in metadata.py'
    ),
    MissingCredentialError: (
        'configure the publisher credential and re-run; `litspectraits doctor` is the preflight'
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


_EXTRACT_HINTS: Final[dict[type[ExtractError], str]] = {
    DoclingImportError: (
        '`docling` is not installed; run `uv sync --extra extract` to install it '
        '(or extract only Springer/Elsevier artifacts, which use lxml)'
    ),
    WrongFormatForExtractorError: (
        'dispatch routed this record to the wrong extractor; '
        'should be unreachable in production — file a bug'
    ),
    MissingArtifactError: 'artifact file is gone; re-run `litspectraits ingest <doi>` to refetch',
    DoclingConversionError: 'docling rejected the PDF outright; inspect the artifact bytes',
    DoclingDegradedError: (
        'docling returned PARTIAL_SUCCESS; TableFormer or layout recognition failed — '
        'tables in this paper are unreliable'
    ),
    EmptyDocumentError: (
        'zero text blocks recovered; likely a scanned PDF served without OCR. '
        'Re-fetch a TDM version if available'
    ),
    MalformedDocumentError: (
        'artifact passed magic-byte sniff but failed structural parse '
        '(e.g. META_ABS envelope, broken XML); re-ingest to validate'
    ),
    ParseDegradedError: (
        'character count below the floor; layout recognition probably failed — '
        'most content was filtered as furniture or never emerged as text'
    ),
    SerializationError: (
        'json.dumps rejected the extracted document; '
        'usually a docling version mismatch or an unexpected payload shape'
    ),
    ExtractIntegrityError: (
        're-extracted document differs from the existing one; '
        'pass `--reextract` to overwrite if the change is intentional'
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
    json_output: bool = typer.Option(False, '--json', help='Emit the manifest as JSON on stdout.'),
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
        _render_error_panel(exc, console=err_console, hints=_INGEST_HINTS)
        raise typer.Exit(_INGEST_EXIT_CODES.get(type(exc), 1)) from exc
    if json_output:
        _emit_record_json(record)
    else:
        _render_record_panel(record, settings=settings, console=out_console)


# ---------------------------------------------------------------------------
# sideload
# ---------------------------------------------------------------------------


@app.command(name='sideload')
def cmd_sideload(
    doi: str = typer.Argument(..., help='DOI of the article being sideloaded.'),
    pdf_path: Path = typer.Argument(
        ...,
        help='Path to the operator-retrieved PDF.',
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    license_assertion: str = typer.Option(
        ...,
        '--license',
        help=(
            'SPDX identifier or free-form license assertion '
            '(e.g. "wiley-tdm-internal-use-only"). Mandatory: this is the legal trail.'
        ),
    ),
    source_url: str | None = typer.Option(
        None,
        '--source-url',
        help='URL the PDF was retrieved from (recorded in manual_provenance).',
    ),
    note: str = typer.Option(
        '',
        '--note',
        help='Free-form note about how the PDF was obtained.',
    ),
    json_output: bool = typer.Option(False, '--json', help='Emit the manifest as JSON on stdout.'),
) -> None:
    """Register an operator-retrieved PDF in the local store (PDF-only).

    JATS / Elsevier-XML sideload is intentionally out of scope for v3:
    the realistic operator workflow is "I downloaded the publisher PDF
    via my library proxy". Idempotent on ``(doi, sha256)``.
    """
    asyncio.run(
        _run_sideload(
            doi=doi,
            pdf_path=pdf_path,
            license_assertion=license_assertion,
            source_url=source_url,
            note=note,
            json_output=json_output,
        )
    )


async def _run_sideload(
    *,
    doi: str,
    pdf_path: Path,
    license_assertion: str,
    source_url: str | None,
    note: str,
    json_output: bool,
) -> None:
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()
    try:
        async with http_client(settings) as client:
            record = await run_sideload(
                doi,
                pdf_path,
                license_assertion=license_assertion,
                source_url=source_url,
                note=note,
                settings=settings,
                store=store,
                client=client,
            )
    except InvalidDOIError as exc:
        _render_invalid_doi(exc, console=err_console)
        raise typer.Exit(2) from exc
    except IngestError as exc:
        _render_error_panel(exc, console=err_console, hints=_INGEST_HINTS)
        raise typer.Exit(_INGEST_EXIT_CODES.get(type(exc), 1)) from exc
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
    json_output: bool = typer.Option(False, '--json', help='Emit the manifest as JSON on stdout.'),
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
                record = await run_ingest(doi, settings=settings, store=store, client=client)
        except InvalidDOIError as exc:
            _render_invalid_doi(exc, console=err_console)
            raise typer.Exit(2) from exc
        except IngestError as exc:
            _render_error_panel(exc, console=err_console, hints=_INGEST_HINTS)
            raise typer.Exit(_INGEST_EXIT_CODES.get(type(exc), 1)) from exc
        if json_output:
            _emit_record_json(record)
        else:
            _render_record_panel(record, settings=settings, console=out_console)
        success = True
    finally:
        if success and not keep:
            shutil.rmtree(smoke_dir, ignore_errors=True)
        elif success:
            err_console.print(f'[green]preserved smoke data_dir: {smoke_dir}[/green]')
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
    json_output: bool = typer.Option(False, '--json', help='Emit the manifest as JSON on stdout.'),
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
            err_console.print(f'[bold yellow]not in local store:[/bold yellow] {normalized}')
        raise typer.Exit(1)
    if json_output:
        _emit_record_json(record)
    else:
        _render_record_panel(record, settings=settings, console=out_console)


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


# A bare sha256 hex digest. Anchored so a DOI that happens to start with
# 64 hex chars (impossible in practice — DOIs start with a registrant
# prefix like ``10.NNNN/``) still routes through ``normalize``.
_SHA256_RE: Final = re.compile(r'^[0-9a-f]{64}$')


@app.command(name='extract')
def cmd_extract(
    target: str = typer.Argument(
        ...,
        help=(
            'DOI or sha256 of the artifact to extract. '
            'Sha is a 64-char lowercase hex string; anything else is parsed as a DOI.'
        ),
    ),
    reextract: bool = typer.Option(
        False,
        '--reextract',
        help='Overwrite an existing document.json whose bytes differ from the new extraction.',
    ),
    json_output: bool = typer.Option(
        False, '--json', help='Emit the resulting ExtractRecord as JSON on stdout.'
    ),
) -> None:
    """Extract structure from a previously-ingested artifact.

    Dispatches through :func:`litspectraits.extract.extract` to the
    format-specific extractor (PDF / JATS / Elsevier). The artifact must
    already be in the local store; run ``litspectraits ingest <doi>``
    first if it isn't. No network calls.

    Exit codes (``docs/extract-pdf-plan.md`` §5): 1 = record not in local
    store; 2 = invalid input or extractor preflight; 4 = conversion
    failure; 6 = malformed output / parse degraded / empty document /
    serialization; 7 = integrity (re-extracted document differs and
    ``--reextract`` was not passed).
    """
    asyncio.run(_run_extract(target=target, reextract=reextract, json_output=json_output))


async def _run_extract(*, target: str, reextract: bool, json_output: bool) -> None:
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()

    record = _resolve_extract_target(target=target, store=store, err_console=err_console)

    try:
        extract_record = await run_extract(record, store, reextract=reextract)
    except ExtractError as exc:
        _render_error_panel(exc, console=err_console, hints=_EXTRACT_HINTS)
        raise typer.Exit(_EXTRACT_EXIT_CODES.get(type(exc), 1)) from exc

    if json_output:
        _emit_extract_record_json(record=record, extract_record=extract_record)
    else:
        _render_extract_record_panel(
            record=record, extract_record=extract_record, console=out_console
        )


def _resolve_extract_target(
    *, target: str, store: ArtifactStore, err_console: Console
) -> AcquisitionRecord:
    """Resolve ``target`` (DOI or sha256) to an :class:`AcquisitionRecord`.

    Sha-not-on-disk and DOI-not-in-index both surface as exit 1 — same
    "the operator referenced something we never ingested" failure mode
    that ``litspectraits show`` uses. Invalid DOI shape exits 2 (matches
    :func:`cmd_ingest` and :func:`cmd_show`). Raises :class:`typer.Exit`
    on failure; never returns ``None``.
    """
    if _SHA256_RE.match(target):
        try:
            return store.read_manifest(target)
        except FileNotFoundError as exc:
            err_console.print(f'[bold yellow]not in local store:[/bold yellow] sha256={target}')
            raise typer.Exit(1) from exc
    try:
        normalized = normalize(target)
    except InvalidDOIError as exc:
        _render_invalid_doi(exc, console=err_console)
        raise typer.Exit(2) from exc
    record = store.find_by_doi(normalized)
    if record is None:
        err_console.print(f'[bold yellow]not in local store:[/bold yellow] {normalized}')
        raise typer.Exit(1)
    return record


def _emit_extract_record_json(*, record: AcquisitionRecord, extract_record: ExtractRecord) -> None:
    """Print the ExtractRecord as JSON on stdout, with DOI injected.

    The on-disk :class:`ExtractRecord` is keyed by sha (to align with
    ``documents/<sha>/``); injecting ``doi`` at the top level keeps
    operator workflows that pipe ``--json`` through ``jq`` self-contained.
    """
    payload = {'doi': record.doi, **converter.unstructure(extract_record)}
    print(json.dumps(payload, indent=2, sort_keys=True))


def _render_extract_record_panel(
    *,
    record: AcquisitionRecord,
    extract_record: ExtractRecord,
    console: Console,
) -> None:
    table = Table(title=f'extracted — {record.doi}', show_header=False, expand=False)
    table.add_column('field', no_wrap=True, style='bold')
    table.add_column('value')
    table.add_row('doi', record.doi)
    table.add_row('sha256', extract_record.sha256)
    table.add_row('extractor', extract_record.extractor.value)
    table.add_row('version', extract_record.extractor_version)
    table.add_row('extracted_at', extract_record.extracted_at.isoformat())
    table.add_row('n_text_blocks', f'{extract_record.n_text_blocks:,}')
    table.add_row('n_section_headers', f'{extract_record.n_section_headers:,}')
    table.add_row('n_tables', f'{extract_record.n_tables:,}')
    table.add_row('n_figures', f'{extract_record.n_figures:,}')
    table.add_row('char_count', f'{extract_record.char_count:,}')
    if extract_record.n_pages is not None:
        table.add_row('n_pages', f'{extract_record.n_pages:,}')
    console.print(table)


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
    except OSError, ValueError:
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


def _render_error_panel(
    exc: IngestError | ExtractError,
    *,
    console: Console,
    hints: dict[type[IngestError], str] | dict[type[ExtractError], str],
) -> None:
    """Render a typed :class:`IngestError` / :class:`ExtractError` as a Rich panel on stderr.

    Includes class name, DOI, every key/value in ``exc.context``, and an
    operator hint (per-call from ``context['hint']`` when present;
    otherwise the class default from ``hints``).

    The two error trees are inheritance-disjoint and share the same
    ``.doi`` + ``.context`` shape; the renderer is identical between
    them so we share one function and route the right hints dict in.
    """
    body = Table.grid(padding=(0, 1))
    body.add_column('key', no_wrap=True, style='bold')
    body.add_column('value')
    body.add_row('doi', exc.doi)
    for key, value in exc.context.items():
        if key == 'hint':
            continue  # rendered separately below
        body.add_row(key, str(value))
    # ``hints`` is keyed by per-tree class; ``type(exc)`` lookups against
    # the "wrong" tree are guaranteed to miss (disjoint hierarchy) and
    # fall through to '-', so the cast at the lookup site is sound.
    class_hint: object = hints.get(type(exc), '-')  # type: ignore[arg-type]
    hint = exc.context.get('hint') or class_hint
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
