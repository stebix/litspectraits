"""Command-line interface (``docs/overview-v3.md`` §10, §17.9).

Nine commands ship today: ``ingest``, ``sideload``, ``doctor``,
``show``, ``list``, ``extract``, ``normalize``, ``show-document``,
``diff-routes`` (plus the convenience ``smoke`` ephemeral-tempdir
wrapper around ``ingest``). ``list`` is the discovery counterpart to
``show``: it enumerates every DOI in the local store so the operator
does not need the exact DOI in hand. ``extract``, ``normalize``, and ``diff-routes`` are
deliberately separate composable steps rather than folded into one
command — each stage stays independently re-runnable and
operator-introspectable (``docs/dual-route-comparison-overview.md`` §9).
``show-document`` renders a normalised document as HTML for human
inspection (``docs/rendering-mvp-plan.md``).

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
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

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
    BackendNotApplicableError,
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
    MineruConversionError,
    MineruImportError,
    MissingArtifactError,
    MissingCredentialError,
    MissingModelWeightsError,
    NormalizeError,
    NormalizeIntegrityError,
    NotOpenAccessError,
    ParseDegradedError,
    PublisherAPIError,
    RateLimitExhaustedError,
    SerializationError,
    UnknownBackendError,
    UnsupportedPublisherError,
    WrongFormatForExtractorError,
)
from litspectraits.extract import extract as run_extract
from litspectraits.extract.backend_ids import DEFAULT_PDF_BACKEND, DOCLING_STANDARD, MINERU
from litspectraits.http import http_client
from litspectraits.ingest import ingest as run_ingest
from litspectraits.manifest import AcquisitionRecord, ExtractRecord, Format, converter
from litspectraits.normalize import (
    Document,
    DualFormatResult,
    NormalizedMeta,
    RenderContext,
    commit_normalized_document,
    compare_dual_format_dois,
    compare_reports,
    format_dual_format_report,
    load_normalized_document,
    normalize_docling_document,
    normalize_mineru_document,
    normalize_xml_document,
    render_html,
)
from litspectraits.normalize import converter as normalize_converter
from litspectraits.sideload import sideload as run_sideload
from litspectraits.store import ArtifactStore, DOIIndexEntry

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
    # 8 is the next free slot; reserved for the Springer-OA "DOI is real
    # but not open-access" failure. Distinct from AuthRejectedError (key
    # is valid, just too narrow in scope) and from PublisherAPIError
    # (no fault on Springer's side — the response was well-formed).
    NotOpenAccessError: 8,
}


_EXTRACT_EXIT_CODES: Final[dict[type[ExtractError], int]] = {
    # Configuration / dispatch errors (operator must change inputs or env).
    DoclingImportError: 2,
    MineruImportError: 2,
    BackendNotApplicableError: 2,
    WrongFormatForExtractorError: 2,
    MissingArtifactError: 2,
    MissingModelWeightsError: 2,
    # Conversion / parse-time failures.
    DoclingConversionError: 4,
    DoclingDegradedError: 4,
    MineruConversionError: 4,
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
    NotOpenAccessError: (
        'DOI exists but is not open-access; '
        'set SPRINGER_TDM_API_KEY (premium TDM licence) for the full Springer corpus, '
        'or `litspectraits sideload <doi> <pdf>` an institutionally-licensed copy'
    ),
    RateLimitExhaustedError: 'publisher 429d after retries; back off and retry later',
    PublisherAPIError: 'publisher 5xx or malformed response; rerun and escalate if persistent',
    MalformedArtifactError: 'artifact failed magic-byte sniff (paywall HTML, error page, etc.)',
    IntegrityError: (
        'sha256 collision against an existing artifact with different bytes; '
        'investigate before overwriting'
    ),
}


_NORMALIZE_EXIT_CODES: Final[dict[type[NormalizeError], int]] = {
    # Refused to overwrite a divergent on-disk normalisation.
    NormalizeIntegrityError: 7,
    # Upstream extract meta's backend_id resolves to no adapter — an
    # operator/config error, same exit code as extract's BackendNotApplicableError.
    UnknownBackendError: 2,
}


_NORMALIZE_HINTS: Final[dict[type[NormalizeError], str]] = {
    NormalizeIntegrityError: (
        're-normalised document differs from the existing one; '
        'pass `--renormalize` to overwrite if the change is intentional'
    ),
    UnknownBackendError: (
        'the upstream extraction recorded no backend this normalizer serves; '
        're-run `litspectraits extract <doi> --backend docling-standard|mineru`'
    ),
}


_EXTRACT_HINTS: Final[dict[type[ExtractError], str]] = {
    DoclingImportError: (
        '`docling` is not installed; run `uv sync --extra extract` to install it '
        '(or extract only Springer/Elsevier artifacts, which use lxml)'
    ),
    MineruImportError: (
        '`mineru` is not installed; run `uv sync --extra mineru` to install it '
        '(or select `--backend docling-standard`)'
    ),
    BackendNotApplicableError: (
        'the requested --backend cannot serve this artifact (non-PDF format, or unknown id); '
        'drop --backend for XML artifacts, or pick an installed PDF backend'
    ),
    MineruConversionError: 'MinerU failed to parse the PDF; inspect the artifact bytes',
    WrongFormatForExtractorError: (
        'dispatch routed this record to the wrong extractor; '
        'should be unreachable in production — file a bug'
    ),
    MissingArtifactError: 'artifact file is gone; re-run `litspectraits ingest <doi>` to refetch',
    MissingModelWeightsError: (
        'docling model weights are missing from the configured cache; run '
        '`litspectraits doctor --download-models` to populate it '
        '(or check `LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR`)'
    ),
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
        _render_record_panel(record, store=store, console=out_console)


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
        _render_record_panel(record, store=store, console=out_console)


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
            _render_record_panel(record, store=store, console=out_console)
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
        _render_record_panel(record, store=store, console=out_console)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


# Aware sentinel sorted *before* every real ``added_at`` so DOIs whose
# index lines carried no parseable timestamp sink to the bottom of the
# newest-first listing rather than crashing the mixed-aware comparison.
_MIN_ADDED_AT: Final = datetime(1, 1, 1, tzinfo=UTC)


@app.command(name='list')
def cmd_list(
    quiet: bool = typer.Option(
        False,
        '--quiet',
        '-q',
        help='Print bare DOIs, one per line (copy/pipe-friendly); suppresses the table.',
    ),
    json_output: bool = typer.Option(
        False, '--json', help='Emit the catalog as a JSON array on stdout.'
    ),
) -> None:
    """List every DOI in the local store, newest first (no network call).

    Reads ``index/by_doi.jsonl`` and renders one row per DOI with its
    title, year, publisher, stored format(s), and extract / normalize
    status — the discovery counterpart to ``show <doi>``, which needs the
    exact DOI up front.

    ``--quiet`` prints just the bare DOIs (one per line) so a DOI can be
    copied or piped without retyping it, e.g.::

        litspectraits list -q | fzf
        litspectraits show "$(litspectraits list -q | head -1)"

    ``--json`` emits a structured array carrying full per-artifact detail
    (format, sha256, extract / normalize flags). When both flags are
    given, ``--quiet`` wins.

    Exits 0 even when the store is empty — an empty catalog is a valid
    state, not an error.
    """
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    out_console = _stdout_console()
    err_console = _stderr_console()

    entries = store.iter_index()
    entries.sort(key=lambda entry: entry.latest_added_at or _MIN_ADDED_AT, reverse=True)

    if quiet:
        for entry in entries:
            print(entry.doi)
        return

    rows = [_catalog_row(entry, store=store) for entry in entries]

    if json_output:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return

    if not rows:
        err_console.print(
            '[dim]local store is empty — run `litspectraits ingest <doi>` to add one[/dim]'
        )
        return
    _render_catalog_table(rows, console=out_console)


class _CatalogArtifact(TypedDict):
    """One stored artifact's row in the ``list`` catalog (also the JSON shape)."""

    format: str
    sha256: str
    extracted: bool
    normalized: bool


class _CatalogRow(TypedDict):
    """One DOI's row in the ``list`` catalog (also the JSON shape)."""

    doi: str
    title: str | None
    year: int | None
    publisher: str
    latest_added_at: str | None
    artifacts: list[_CatalogArtifact]


def _catalog_row(entry: DOIIndexEntry, *, store: ArtifactStore) -> _CatalogRow:
    """Build one DOI's display/JSON payload from its index roll-up.

    Reads a single manifest for the shared bibliographic fields (title /
    year / publisher are identical across a DOI's formats — they all come
    from the same CrossRef record) and probes the document trees for each
    artifact's extract / normalize status. A manifest that the index
    references but that is missing on disk is surfaced as a loud in-row
    marker rather than aborting the whole listing — ``list`` is the tool
    an operator reaches for to *diagnose* a damaged store.
    """
    sample_sha = next(iter(entry.formats.values()))
    try:
        record = store.read_manifest(sample_sha)
    except FileNotFoundError:
        title: str | None = '⚠ manifest missing'
        year: int | None = None
        publisher = '?'
    else:
        title = record.metadata.title
        year = record.metadata.year
        publisher = record.publisher.value

    artifacts: list[_CatalogArtifact] = [
        {
            'format': fmt.value,
            'sha256': sha,
            'extracted': (store.document_dir(sha) / 'document.json').is_file(),
            'normalized': (store.normalized_dir(sha) / 'document.json').is_file(),
        }
        for fmt, sha in sorted(entry.formats.items(), key=lambda item: item[0].value)
    ]
    return {
        'doi': entry.doi,
        'title': title,
        'year': year,
        'publisher': publisher,
        'latest_added_at': (
            entry.latest_added_at.isoformat() if entry.latest_added_at is not None else None
        ),
        'artifacts': artifacts,
    }


def _render_catalog_table(rows: list[_CatalogRow], *, console: Console) -> None:
    """Render the catalog rows produced by :func:`_catalog_row` as a table."""
    table = Table(title=f'local store — {len(rows)} DOI(s)', show_header=True, expand=False)
    table.add_column('doi', no_wrap=True, style='bold')
    table.add_column('title')
    table.add_column('year', justify='right')
    table.add_column('publisher')
    table.add_column('formats')
    table.add_column('extracted')
    table.add_column('normalized')
    for row in rows:
        artifacts = row['artifacts']
        title = row['title']
        year = row['year']
        table.add_row(
            row['doi'],
            title if title else '-',
            str(year) if year is not None else '-',
            row['publisher'],
            ', '.join(artifact['format'] for artifact in artifacts),
            _aggregate_status(artifacts, key='extracted'),
            _aggregate_status(artifacts, key='normalized'),
        )
    console.print(table)


def _aggregate_status(
    artifacts: list[_CatalogArtifact], *, key: Literal['extracted', 'normalized']
) -> str:
    """Collapse per-artifact booleans into one scannable cell.

    ``✓`` when every artifact has the stage, ``✗`` when none does, and
    ``n/total`` for the partial dual-format case (e.g. the PDF is
    normalised but the JATS sibling is not). The exact per-artifact
    breakdown lives in ``--json``.
    """
    total = len(artifacts)
    if total == 0:
        return '-'
    done = sum(1 for artifact in artifacts if artifact[key])
    if done == total:
        return '[green]✓[/green]'
    if done == 0:
        return '[red]✗[/red]'
    return f'{done}/{total}'


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
    backend: str = typer.Option(
        DEFAULT_PDF_BACKEND,
        '--backend',
        envvar='LITSPECTRAITS_PDF_BACKEND',
        help=(
            'PDF parsing backend: `docling-standard` (default; text-layer, exact '
            'geometry) or `mineru`. Ignored on XML artifacts — a non-default value '
            'there is a loud error. Overridable via LITSPECTRAITS_PDF_BACKEND.'
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
    format-specific extractor, and — for PDF — the ``--backend``-selected
    parser (docling or MinerU; ``docs/mineru-backend-spec.md`` §1, §8). The
    artifact must already be in the local store; run ``litspectraits ingest
    <doi>`` first if it isn't. No network calls.

    Exit codes (``docs/extract-pdf-plan.md`` §5): 1 = record not in local
    store; 2 = invalid input / extractor preflight / unusable ``--backend``
    (``BackendNotApplicableError``); 4 = conversion failure; 6 = malformed
    output / parse degraded / empty document / serialization; 7 = integrity
    (re-extracted document differs and ``--reextract`` was not passed).
    """
    asyncio.run(
        _run_extract(
            target=target, backend=backend, reextract=reextract, json_output=json_output
        )
    )


async def _run_extract(
    *, target: str, backend: str, reextract: bool, json_output: bool
) -> None:
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()

    record = _resolve_extract_target(target=target, store=store, err_console=err_console)

    try:
        extract_record = await run_extract(
            record,
            store,
            backend=backend,
            reextract=reextract,
            model_cache_dir=settings.docling_model_cache_dir,
        )
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
    ``documents/sha256/<aa>/<sha>/``); injecting ``doi`` at the top level keeps
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
# normalize
# ---------------------------------------------------------------------------


@app.command(name='normalize')
def cmd_normalize(
    target: str = typer.Argument(
        ...,
        help=(
            'DOI or sha256 of the artifact to normalise. '
            'Sha is a 64-char lowercase hex string; anything else is parsed as a DOI.'
        ),
    ),
    renormalize: bool = typer.Option(
        False,
        '--renormalize',
        help=(
            'Overwrite an existing `normalized/sha256/<aa>/<sha>/document.json` whose bytes '
            'differ from the freshly-normalised output.'
        ),
    ),
    json_output: bool = typer.Option(
        False, '--json', help='Emit the resulting NormalizedMeta as JSON on stdout.'
    ),
) -> None:
    """Build the normalised :class:`Document` for a previously-extracted artifact.

    Reads ``documents/sha256/<aa>/<sha>/document.json`` and routes through
    the adapter that matches the parser which produced it: for PDF, the
    ``backend_id`` recorded in the extract ``meta.json`` (docling or MinerU;
    ``docs/mineru-backend-spec.md`` §1, §8); for JATS / Elsevier, the XML
    adapter keyed on format. The result is atomically committed to
    ``normalized/sha256/<aa>/<sha>/{document.json,meta.json}``
    (``docs/normalized-documents-discussion.md`` §3,
    ``docs/dual-route-comparison-overview.md`` §9). There is no
    ``--backend`` flag here by design — using it would let you normalize a
    document with the wrong adapter; the backend is read, never chosen.

    The artifact must already be ingested *and* extracted — run
    ``litspectraits ingest`` then ``litspectraits extract`` first. The
    composable three-step (``ingest`` → ``extract`` → ``normalize``) is
    a deliberate choice over folding ``normalize`` into ``extract``:
    each stage stays independently re-runnable and operator-introspectable.

    Exit codes:

    - 1: artifact not in local store, or upstream extraction missing.
    - 2: invalid input shape; docling SDK missing for a PDF target; or the
      extract meta records no backend this normalizer serves
      (:class:`~litspectraits.errors.UnknownBackendError`).
    - 7: re-normalisation diverges from existing bytes and ``--renormalize``
      was not passed (:class:`~litspectraits.errors.NormalizeIntegrityError`).
    """
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()

    record = _resolve_extract_target(target=target, store=store, err_console=err_console)
    try:
        meta = _run_normalize(record=record, store=store, renormalize=renormalize)
    except FileNotFoundError as exc:
        err_console.print(
            f'[bold yellow]upstream extraction missing:[/bold yellow] {exc}. '
            f'Run `litspectraits extract {record.doi}` first.'
        )
        raise typer.Exit(1) from exc
    except DoclingImportError as exc:
        _render_error_panel(exc, console=err_console, hints=_EXTRACT_HINTS)
        raise typer.Exit(2) from exc
    except NormalizeError as exc:
        _render_error_panel(exc, console=err_console, hints=_NORMALIZE_HINTS)
        raise typer.Exit(_NORMALIZE_EXIT_CODES.get(type(exc), 1)) from exc

    if json_output:
        _emit_normalize_meta_json(record=record, meta=meta)
    else:
        _render_normalize_meta_panel(record=record, meta=meta, console=out_console)


def _normalize_pdf(
    payload: dict[str, Any], *, store: ArtifactStore, record: AcquisitionRecord
) -> Document:
    """Dispatch a PDF ``document.json`` to the adapter matching its backend.

    A PDF can be docling- or MinerU-parsed, so the adapter is selected on
    the ``backend_id`` the extractor recorded in ``meta.json``
    (``docs/mineru-backend-spec.md`` §1, §8) — not inferred from
    :class:`~litspectraits.manifest.Format`. This keeps the normalize
    adapter in lockstep with the extractor by construction: there is no way
    to normalize a MinerU document with the docling adapter.

    Raises
    ------
    litspectraits.errors.UnknownBackendError
        The upstream meta carries no ``backend_id`` this normalizer serves
        (an unknown id, or a pre-``backend_id`` extraction).
    """
    backend_id = _read_extract_backend_id(store=store, sha256=record.sha256)
    if backend_id == DOCLING_STANDARD:
        return normalize_docling_document(payload)
    if backend_id == MINERU:
        return normalize_mineru_document(payload)
    raise UnknownBackendError(
        doi=record.doi,
        sha256=record.sha256,
        backend_id=backend_id,
        hint=(
            f'extract meta.json carries no usable backend_id (got {backend_id!r}); '
            f're-run `litspectraits extract {record.doi} --backend '
            f'docling-standard|mineru` so normalize can match the parser'
        ),
    )


def _read_extract_backend_id(*, store: ArtifactStore, sha256: str) -> str | None:
    """Read ``backend_id`` from the upstream extract ``meta.json``.

    Returns ``None`` when the field is absent (an extraction predating the
    pluggable-backend seam) or non-string, so the caller fails loud with a
    precise :class:`~litspectraits.errors.UnknownBackendError` rather than a
    ``KeyError``. The meta's existence is already guaranteed by the
    ``document.json`` check upstream; a genuinely missing meta surfaces as
    the same :class:`FileNotFoundError` the commit path raises.
    """
    meta_path = store.document_dir(sha256) / 'meta.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    backend_id = meta.get('backend_id')
    return backend_id if isinstance(backend_id, str) else None


def _run_normalize(
    *, record: AcquisitionRecord, store: ArtifactStore, renormalize: bool
) -> NormalizedMeta:
    """Glue: load the extractor output, dispatch by format, commit.

    Kept synchronous because none of the adapters are I/O bound (cattrs
    structuring against an in-memory dict). If a future adapter grows
    real I/O — e.g. a network-fetched ontology lookup — switch the
    helper to async and ``asyncio.run`` from ``cmd_normalize`` to match
    the ingest/extract pattern.
    """
    document_path = store.document_dir(record.sha256) / 'document.json'
    if not document_path.exists():
        raise FileNotFoundError(
            f'missing {document_path.relative_to(store.data_dir)} (sha256={record.sha256!r})'
        )
    payload = json.loads(document_path.read_text(encoding='utf-8'))

    if record.format is Format.PDF:
        doc = _normalize_pdf(payload, store=store, record=record)
    elif record.format is Format.JATS_XML:
        doc = normalize_xml_document(payload, route='jats')
    elif record.format is Format.ELSEVIER_XML:
        doc = normalize_xml_document(payload, route='elsevier')
    else:
        # New format added without updating dispatch — defensive guard.
        raise RuntimeError(f'no normalize adapter for format {record.format!r}')

    return commit_normalized_document(
        doc=doc,
        doi=record.doi,
        source_artifact_sha=record.sha256,
        store=store,
        renormalize=renormalize,
    )


def _emit_normalize_meta_json(*, record: AcquisitionRecord, meta: NormalizedMeta) -> None:
    """Print the :class:`NormalizedMeta` as JSON on stdout, DOI injected.

    DOI is added at the top level for the same reason as the extract
    JSON emitter: keeps ``--json | jq`` workflows self-contained when
    the operator pipes multiple normalisations into a single stream.
    """
    payload = {'doi': record.doi, **normalize_converter.unstructure(meta)}
    print(json.dumps(payload, indent=2, sort_keys=True))


def _render_normalize_meta_panel(
    *,
    record: AcquisitionRecord,
    meta: NormalizedMeta,
    console: Console,
) -> None:
    table = Table(title=f'normalised — {record.doi}', show_header=False, expand=False)
    table.add_column('field', no_wrap=True, style='bold')
    table.add_column('value')
    table.add_row('doi', record.doi)
    table.add_row('source_artifact_sha', meta.source_artifact_sha)
    table.add_row('route', meta.route)
    table.add_row('normaliser_version', meta.normaliser_version)
    table.add_row('whitespace_rule', meta.whitespace_rule)
    table.add_row('source_extractor_meta_sha', meta.source_extractor_meta_sha)
    table.add_row('normalized_at', meta.normalized_at.isoformat())
    table.add_row('has_structured_refs', str(meta.completeness.has_structured_refs))
    table.add_row('has_inline_ref_ids', str(meta.completeness.has_inline_ref_ids))
    table.add_row('has_equations', str(meta.completeness.has_equations))
    table.add_row('table_source', meta.completeness.table_source)
    console.print(table)


# ---------------------------------------------------------------------------
# show-document
# ---------------------------------------------------------------------------


@app.command(name='show-document')
def cmd_show_document(
    target: str = typer.Argument(
        ...,
        help=(
            'DOI or sha256 of the artifact to render. '
            'Sha is a 64-char lowercase hex string; anything else is parsed as a DOI.'
        ),
    ),
    out: Path | None = typer.Option(
        None,
        '--out',
        help='Write the HTML here. Defaults to `<sha256>.html` in the current directory.',
    ),
    open_browser: bool = typer.Option(
        False,
        '--open',
        help='Open the rendered HTML in the default web browser after writing it.',
    ),
) -> None:
    """Render a normalised :class:`Document` as a self-contained HTML page.

    Loads ``normalized/sha256/<aa>/<sha>/document.json`` and renders it
    via :func:`litspectraits.normalize.render_html` — a linear,
    route-faithful reading view for human inspection
    (``docs/rendering-mvp-plan.md``). The artifact must already be
    ingested, extracted, *and* normalised; run ``litspectraits normalize
    <doi>`` first if it isn't. No network calls.

    The output path is printed to stdout (pipe-friendly); ``--open``
    additionally launches it in a browser.

    Exit codes:

    - 1: artifact not in local store, or it has not been normalised yet.
    - 2: invalid DOI shape.
    """
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()

    record = _resolve_extract_target(target=target, store=store, err_console=err_console)
    try:
        doc = load_normalized_document(source_artifact_sha=record.sha256, store=store)
    except FileNotFoundError as exc:
        err_console.print(
            f'[bold yellow]not normalised:[/bold yellow] sha256={record.sha256}. '
            f'Run `litspectraits normalize {record.doi}` first.'
        )
        raise typer.Exit(1) from exc

    context = RenderContext(doi=record.doi, source_artifact_sha=record.sha256)
    html_body = render_html(doc, context=context)

    out_path = out if out is not None else Path(f'{record.sha256}.html')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_body, encoding='utf-8')
    print(out_path)
    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())


# ---------------------------------------------------------------------------
# diff-routes
# ---------------------------------------------------------------------------


@app.command(name='diff-routes')
def cmd_diff_routes(
    doi: list[str] = typer.Option(
        [],
        '--doi',
        help=(
            'Restrict the harness to specific DOIs (repeatable). '
            'Omit for auto-discovery across the entire index.'
        ),
    ),
    out: Path | None = typer.Option(
        None,
        '--out',
        help=(
            'Write the cattrs-shaped JSON report to this path. The file is '
            'the input format `--compare-to` reads.'
        ),
    ),
    compare_to: Path | None = typer.Option(
        None,
        '--compare-to',
        help=(
            'Path to a previous report (written by a prior `--out` run). '
            'When set, the command renders a temporal regression view '
            'instead of the single-snapshot text report.'
        ),
    ),
    json_output: bool = typer.Option(
        False, '--json', help='Emit the report as JSON on stdout instead of text.'
    ),
) -> None:
    """Run the cross-format differential harness on dual-format DOIs.

    Auto-discovers candidate DOIs from ``index/by_doi.jsonl`` (those
    with both a PDF and an XML manifest), loads their persisted
    normalised documents (``normalize`` must have been run first),
    and feeds each pair into the structural diff. Yields one record
    per DOI — either a :class:`DualFormatComparison` (the harness
    produced numbers) or a :class:`DualFormatSkip` (the DOI was not
    eligible for one of three operator-actionable reasons).

    Use ``--out report.json`` to persist; pair with
    ``--compare-to prev-report.json`` on a later run for temporal
    regression detection (``docs/dual-route-comparison-overview.md``
    §5). The single-snapshot text report and the temporal-diff text
    are both written to stdout; ``--json`` emits cattrs-unstructured
    JSON instead.

    Exit codes:

    - 1: operator-input error (unknown DOI passed via ``--doi``, or
      ``--compare-to`` file missing / malformed).
    """
    settings = _load_settings()
    store = ArtifactStore(settings.data_dir)
    err_console = _stderr_console()
    out_console = _stdout_console()

    try:
        results = list(compare_dual_format_dois(store, dois=doi or None))
    except ValueError as exc:
        err_console.print(f'[bold yellow]diff-routes:[/bold yellow] {exc}')
        raise typer.Exit(1) from exc

    if out is not None:
        unstructured = [normalize_converter.unstructure(r) for r in results]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(unstructured, indent=2, sort_keys=True) + '\n')

    if compare_to is not None:
        try:
            previous = _load_report(compare_to)
        except (OSError, ValueError) as exc:
            err_console.print(
                f'[bold yellow]diff-routes --compare-to:[/bold yellow] '
                f'cannot read {compare_to}: {exc}'
            )
            raise typer.Exit(1) from exc
        text = compare_reports(previous=previous, current=results)
        out_console.print(text)
        return

    if json_output:
        unstructured = [normalize_converter.unstructure(r) for r in results]
        print(json.dumps(unstructured, indent=2, sort_keys=True))
        return

    out_console.print(format_dual_format_report(results))


def _load_report(path: Path) -> list[DualFormatResult]:
    """Read a cattrs-shaped harness report from disk.

    Counterpart to ``--out``'s writer. Wraps the cattrs structuring
    in a typed surface so the CLI catches malformed reports as a
    clean exit 1 rather than letting a raw cattrs exception escape.
    """
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, list):
        raise ValueError(f'expected a JSON array of harness results; got {type(raw).__name__}')
    # cattrs supports type-alias unions via the structure hook registered
    # in :mod:`litspectraits.normalize.diff`; pyright sees ``DualFormatResult``
    # as a UnionType, not a ``type[T]``, and flags the call.
    return [
        normalize_converter.structure(item, DualFormatResult)  # pyright: ignore[reportArgumentType]
        for item in raw
    ]


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command(name='doctor')
def cmd_doctor(
    download_models: bool = typer.Option(
        False,
        '--download-models/--no-download-models',
        help=(
            'Pull docling layout + TableFormer weights if missing. '
            'Multi-GB; off by default — operator must opt in explicitly.'
        ),
    ),
    smoke_extract: bool = typer.Option(
        False,
        '--smoke-extract/--no-smoke-extract',
        help=(
            'Run a live docling conversion against the packaged synthetic PDF. '
            'Off by default to keep `doctor` from spinning up the layout model.'
        ),
    ),
) -> None:
    """Operator preflight — egress IP, publisher creds, extract components.

    Plain ``doctor`` is read-only and network-free for the extract
    section (``extract-pdf-plan.md`` §8). The two flags above are the
    only paths that trigger model downloads or actual docling work; both
    are off by default so a routine doctor run stays cheap.
    """
    settings = _load_settings()
    out_console = _stdout_console()
    report = asyncio.run(
        _run_doctor(
            settings=settings,
            download_models=download_models,
            smoke_extract=smoke_extract,
        )
    )
    render_doctor_report(report, console=out_console)
    if not report.ok:
        raise typer.Exit(1)


async def _run_doctor(*, settings: Settings, download_models: bool, smoke_extract: bool):
    async with http_client(settings) as client:
        return await run_doctor(
            settings=settings,
            client=client,
            download_models=download_models,
            smoke_extract=smoke_extract,
        )


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
    record: AcquisitionRecord, *, store: ArtifactStore, console: Console
) -> None:
    """Render a record summary as a Rich table.

    Includes an "extraction" / "normalization" row that probes the
    sharded ``documents/`` / ``normalized/`` trees via ``store`` so the
    same renderer serves ``ingest`` (where both read "not …" on a fresh
    fetch) and ``show`` against a record that has since been extracted /
    normalised.
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
    table.add_row('manifest', _manifest_relpath(record, store=store))
    table.add_row('fetched_at', record.fetched_at.isoformat())
    table.add_row('fetcher', f'litspectraits {record.fetcher_version}')
    table.add_row('sdk', record.sdk_version)
    table.add_row('extraction', _extraction_status(record, store=store))
    table.add_row('normalization', _normalization_status(record, store=store))
    console.print(table)


def _manifest_relpath(record: AcquisitionRecord, *, store: ArtifactStore) -> str:
    # ``store.manifest_path`` is the source of truth for the shard rule;
    # we render it relative-to-data_dir for portability across relocations.
    return store.manifest_path(record.sha256).relative_to(store.data_dir).as_posix()


def _extraction_status(record: AcquisitionRecord, *, store: ArtifactStore) -> str:
    """Probe for ``documents/sha256/<aa>/<sha>/document.json``; report textually.

    Routes through :meth:`ArtifactStore.document_dir` so the probe uses
    the same one-level-sharded path the extractor writes to.
    """
    document_dir = store.document_dir(record.sha256)
    if (document_dir / 'document.json').is_file():
        meta_path = document_dir / 'meta.json'
        if meta_path.is_file():
            return f'extracted ({_brief_extractor(meta_path)})'
        return 'extracted'
    return 'not extracted'


def _normalization_status(record: AcquisitionRecord, *, store: ArtifactStore) -> str:
    """Probe for ``normalized/sha256/<aa>/<sha>/document.json``; report textually.

    Mirror of :func:`_extraction_status` for the layer downstream, routed
    through :meth:`ArtifactStore.normalized_dir`. Operators need this to
    know whether the diff harness can run for a dual-format DOI without
    first invoking ``litspectraits normalize``
    (``docs/dual-route-comparison-overview.md`` §3).
    """
    normalized_dir = store.normalized_dir(record.sha256)
    if (normalized_dir / 'document.json').is_file():
        meta_path = normalized_dir / 'meta.json'
        if meta_path.is_file():
            return f'normalised ({_brief_normalizer(meta_path)})'
        return 'normalised'
    return 'not normalised'


def _brief_normalizer(meta_path: Path) -> str:
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except OSError, ValueError:
        return 'meta unreadable'
    route = meta.get('route', '?')
    version = meta.get('normaliser_version', '?')
    return f'{route} v{version}'


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
    exc: IngestError | ExtractError | NormalizeError,
    *,
    console: Console,
    hints: dict[type[IngestError], str]
    | dict[type[ExtractError], str]
    | dict[type[NormalizeError], str],
) -> None:
    """Render a typed pipeline-stage error as a Rich panel on stderr.

    Accepts any of :class:`IngestError`, :class:`ExtractError`, or
    :class:`NormalizeError`. Includes class name, DOI, every key/value
    in ``exc.context``, and an operator hint (per-call from
    ``context['hint']`` when present; otherwise the class default from
    ``hints``).

    The three error trees are inheritance-disjoint and share the same
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
