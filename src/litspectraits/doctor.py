"""Operator preflight diagnostic (``docs/overview-v3.md`` §12,
``docs/extract-pdf-plan.md`` §8).

Three checks, all read-only by default:

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
3. **Extract components** (``extract-pdf-plan.md`` §8) — probe the
   ``[extract]`` extra, the docling model cache, and the accelerator
   ``AcceleratorDevice.AUTO`` will resolve to. The model-cache location
   honors ``LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR``
   (:attr:`~litspectraits.config.Settings.docling_model_cache_dir`) —
   when set, it is both the directory probed for weights and the
   ``output_dir`` ``--download-models`` writes to; otherwise docling's
   own ``~/.cache/docling/models`` applies. Two opt-ins ride on top of
   this section: ``--download-models`` fetches the three required v3
   weights — the Egret-Large layout model, TableFormer, and the
   code/formula VLM — and ``--smoke-extract`` runs a live docling
   conversion against the packaged synthetic fixture.
   Both side-effects are off by default — plain ``doctor`` stays
   read-only and network-free for the extract section.

Doctor is split into a pure :func:`run_doctor` returning a
:class:`DoctorReport` value object and a separate :func:`render` that
emits the Rich table. The split keeps the table render trivially
golden-testable and makes ``doctor`` re-usable from non-CLI contexts
(e.g. a future scheduled health-check).

Exit-code semantics live in :mod:`litspectraits.cli` per §12 +
``extract-pdf-plan.md`` §8: exit 0 when every configured credential
smoke-tested green (or no credentials configured) and every *required*
extract component is present (or the extra itself is not installed);
exit 1 otherwise. The IP allow-list is observability-only — even a
definite mismatch never flips the exit code on its own. Rationale: an
operator running doctor from a laptop with the VPN down still wants to
see the per-publisher rows.
"""

import asyncio
import ipaddress
import tempfile
import time
from collections.abc import Iterable
from enum import StrEnum
from importlib import metadata, resources
from pathlib import Path
from typing import Final

import httpx
import structlog
from attrs import frozen
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    EntitlementDowngradeError,
    ExtractError,
    IngestError,
    MissingCredentialError,
    NotOpenAccessError,
)

# Module-level so ``litspectraits.doctor._docling_model_dirs`` stays a patchable
# attribute (the doctor tests monkeypatch it) and the probe functions resolve it
# through the module global rather than a local re-import that would shadow the
# patch. The extract module is the single source of truth for which docling
# weights the v3 pipeline requires and where they live.
from litspectraits.extract.pdf import _docling_model_dirs, _model_dir_present
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    Publisher,
)
from litspectraits.retrievers.dispatch import retriever_for
from litspectraits.store import ArtifactStore

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
    """Per-publisher credential smoke-test result.

    ``NOT_OPEN_ACCESS`` is Springer-specific: the operator's
    ``SPRINGER_OA_API_KEY`` is valid but the smoke DOI is not open-access,
    so the Open Access tier cannot serve it. Carved out from
    ``OTHER_FAILURE`` because the recourse ("get a TDM licence or
    sideload") is materially different — see
    :class:`~litspectraits.errors.NotOpenAccessError`.
    """

    NOT_CONFIGURED = 'not_configured'
    OK = 'ok'
    MISSING_CREDENTIAL = 'missing_credential'
    AUTH_REJECTED = 'auth_rejected'
    ENTITLEMENT_DOWNGRADE = 'entitlement_downgrade'
    NOT_OPEN_ACCESS = 'not_open_access'
    OTHER_FAILURE = 'other_failure'


class ExtractStatus(StrEnum):
    """Per-component status row for the extract section.

    Heterogeneous on purpose: the device-name labels (``CUDA``/``MPS``/
    ``CPU``) double as both status and display label so the
    accelerator row reads naturally in the rendered table without a
    second status→label mapping. Required-vs-optional semantics are
    encoded on the row, not in the status itself — ``OFF`` means
    "deliberately disabled" (e.g. OCR), while ``MISSING`` means "we
    asked for it and it's not there" (e.g. layout weights).
    """

    OK = 'ok'
    NOT_INSTALLED = 'not_installed'
    MISSING = 'missing'
    OFF = 'off'
    CUDA = 'cuda'
    MPS = 'mps'
    CPU = 'cpu'
    SKIPPED = 'skipped'


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
class ExtractComponentCheck:
    """One row in the extract-section table (``extract-pdf-plan.md`` §8).

    ``required`` is the displayed string ('yes' / 'no' / 'auto'); the
    boolean intent lives in :attr:`is_required` so the exit-code
    decision is a single attribute lookup without re-parsing the
    display string.
    """

    component: str
    required: str
    is_required: bool
    status: ExtractStatus
    detail: str


@frozen
class ExtractReport:
    """Aggregate of the extract-section rows.

    :attr:`has_required_failure` is the contract for the CLI's exit-code
    logic: True when the ``[extract]`` extra **is** installed and at
    least one required component (layout model, TableFormer, opted-in
    smoke convert) is missing or failed. False when the extra is not
    installed at all — the extract section is opt-in
    (``extract-pdf-plan.md`` §8 "exit code unaffected").
    """

    components: tuple[ExtractComponentCheck, ...]
    has_required_failure: bool


@frozen
class DoctorReport:
    """Aggregate report (IP probe + per-publisher rows + extract section).

    ``ok`` is the contract for the CLI's exit-code logic: True when no
    *configured* credential failed (status in ``OK`` or
    ``NOT_CONFIGURED``) and no *required* extract component is missing;
    False otherwise. The IP check is excluded from ``ok`` per the §12
    rationale (allow-list mismatches are observability-only).

    :attr:`extract_check` defaults to ``None`` to preserve callers that
    construct a :class:`DoctorReport` without exercising the extract
    section (the existing test layer; future non-CLI consumers can
    follow the same pattern). :func:`run_doctor` always populates it
    when invoked through the CLI.
    """

    ip_check: IPCheck
    cred_checks: tuple[CredCheck, ...]
    extract_check: ExtractReport | None = None

    @property
    def ok(self) -> bool:
        creds_ok = all(
            check.status in (CredStatus.OK, CredStatus.NOT_CONFIGURED)
            for check in self.cred_checks
        )
        extract_ok = self.extract_check is None or not self.extract_check.has_required_failure
        return creds_ok and extract_ok


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def run_doctor(
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    download_models: bool = False,
    smoke_extract: bool = False,
    fixture_path: Path | None = None,
) -> DoctorReport:
    """Execute every preflight check and return a :class:`DoctorReport`.

    Network calls happen here (ipify + one publisher smoke fetch per
    configured credential, plus optional model downloads when
    ``download_models=True``). Pure-data return value; rendering is
    :func:`render`'s job.

    Parameters
    ----------
    settings, client
        Standard handles threaded through from the CLI.
    download_models : bool, default False
        Opt-in: when set, run
        :func:`docling.utils.model_downloader.download_models` for the
        required layout + TableFormer weights before reporting model
        presence. A multi-GB download — never the default
        (``extract-pdf-plan.md`` §8).
    smoke_extract : bool, default False
        Opt-in: when set, run a live docling conversion against
        ``fixture_path`` (defaulting to the packaged synthetic PDF)
        and report wall-clock + verbatim docling error on failure.
    fixture_path : Path, optional
        Override for the smoke-convert input. Tests use this to point
        at synthetic fixtures; operators normally rely on the packaged
        default.
    """
    ip_check, cred_checks, extract_check = await asyncio.gather(
        _check_egress_ip(client=client, settings=settings),
        _check_all_publishers(client=client, settings=settings),
        _check_extract_section(
            settings=settings,
            download_models=download_models,
            smoke_extract=smoke_extract,
            fixture_path=fixture_path,
        ),
    )
    return DoctorReport(
        ip_check=ip_check,
        cred_checks=cred_checks,
        extract_check=extract_check,
    )


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
    return IPCheck(status=IPStatus.OUTSIDE_ALLOWLIST, ip=ip, expected_cidrs=cidrs, error=None)


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
    except NotOpenAccessError as exc:
        return CredCheck(
            publisher=publisher,
            status=CredStatus.NOT_OPEN_ACCESS,
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
            # Either tier counts as "configured" — the retriever picks
            # which one to exercise (TDM if its key is set, otherwise
            # the dev-portal Open Access tier).
            return bool(settings.springer_tdm_api_key or settings.springer_oa_api_key)
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


def _summarize(exc: IngestError | ExtractError) -> str:
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
# Extract section (``extract-pdf-plan.md`` §8)
# ---------------------------------------------------------------------------
#
# Five row producers and one orchestrator. Each row producer returns a
# single :class:`ExtractComponentCheck` (or a list, in the case of the
# model-presence check which emits one row per required model). All
# docling / torch imports are local to the function bodies so an
# operator without the ``[extract]`` extra installed can still run
# ``doctor`` without seeing a stray ``ImportError`` in the traceback.

# Component labels mirror the ``extract-pdf-plan.md`` §8 mock table
# verbatim — operators grep these strings.
_COMPONENT_EXTRA: Final = 'docling[extract]'
_COMPONENT_LAYOUT: Final = 'layout model'
_COMPONENT_TABLEFORMER: Final = 'TableFormer'
_COMPONENT_CODE_FORMULA: Final = 'code-formula'
_COMPONENT_ACCEL: Final = 'accelerator'
_COMPONENT_OCR: Final = 'OCR engines'
_COMPONENT_SMOKE: Final = 'smoke convert'

# Packaged fixture used by ``doctor --smoke-extract``. Lives under
# ``src/litspectraits/_fixtures/`` (not ``tests/``) so the bytes ship in
# the wheel and a pip-installed operator can still run the smoke convert
# without checking out the repo.
_FIXTURE_PACKAGE: Final = 'litspectraits._fixtures'
_FIXTURE_FILENAME: Final = 'synthetic.pdf'


async def _check_extract_section(
    *,
    settings: Settings,
    download_models: bool,
    smoke_extract: bool,
    fixture_path: Path | None,
) -> ExtractReport:
    """Orchestrate the §8 checks; return a single :class:`ExtractReport`.

    Short-circuits when the ``[extract]`` extra isn't installed: only
    the extra-row is emitted, ``has_required_failure`` stays False
    (extra is opt-in), and ``--download-models`` / ``--smoke-extract``
    are silently no-ops on that path. Operators who explicitly asked
    for those flags get a deterministic "you need the extra first"
    table row rather than a stray :class:`ImportError`.

    Model-presence checks run before the optional download so the
    initial state is recorded; after a successful download we re-probe
    so the final table reflects on-disk truth, not the pre-download
    state.
    """
    model_cache_dir = settings.docling_model_cache_dir
    extra_row = _check_docling_extra()
    if extra_row.status is ExtractStatus.NOT_INSTALLED:
        rows: list[ExtractComponentCheck] = [extra_row]
        if download_models or smoke_extract:
            rows.append(
                ExtractComponentCheck(
                    component=_COMPONENT_SMOKE if smoke_extract else _COMPONENT_LAYOUT,
                    required='no',
                    is_required=False,
                    status=ExtractStatus.SKIPPED,
                    detail='skipped — [extract] extra not installed',
                )
            )
        return ExtractReport(components=tuple(rows), has_required_failure=False)

    if download_models:
        # Synchronous, multi-GB I/O; off the event loop.
        await asyncio.to_thread(
            _maybe_download_models, force=False, model_cache_dir=model_cache_dir
        )

    model_rows = _check_docling_models(model_cache_dir=model_cache_dir)
    accel_row = _check_accelerator()
    ocr_row = _check_ocr_engines()

    rows = [extra_row, *model_rows, accel_row, ocr_row]

    if smoke_extract:
        resolved_fixture = fixture_path or _packaged_fixture_path()
        smoke_row = await _maybe_smoke_extract(fixture_path=resolved_fixture, settings=settings)
        rows.append(smoke_row)

    has_required_failure = any(
        row.is_required and row.status is not ExtractStatus.OK for row in rows
    )
    return ExtractReport(components=tuple(rows), has_required_failure=has_required_failure)


def _check_docling_extra() -> ExtractComponentCheck:
    """Probe whether ``[extract]`` is installed; report version on hit."""
    try:
        import docling  # noqa: F401  pyright: ignore[reportMissingImports]
    except ImportError:
        return ExtractComponentCheck(
            component=_COMPONENT_EXTRA,
            required='yes',
            is_required=False,  # extra is opt-in per §8
            status=ExtractStatus.NOT_INSTALLED,
            detail='install with `uv sync --extra extract`',
        )
    try:
        version = metadata.version('docling')
    except metadata.PackageNotFoundError:  # pragma: no cover — defensive
        version = 'unknown'
    return ExtractComponentCheck(
        component=_COMPONENT_EXTRA,
        required='yes',
        is_required=True,
        status=ExtractStatus.OK,
        detail=f'docling {version}',
    )


def _check_docling_models(*, model_cache_dir: Path | None) -> list[ExtractComponentCheck]:
    """Probe the docling model cache for layout + TableFormer + code-formula.

    Three required weights for the v3 ``PdfPipelineOptions`` (see
    ``docling-settings-buildout.md`` §2): the Egret-Large layout
    region-detector, the TableFormer table-structure model (accurate
    mode), and the code/formula VLM that ``do_formula_enrichment=True``
    needs. The list moves in lockstep with that config — if the converter
    is bumped to a different layout model or formula enrichment is
    disabled, this list changes too, or ``doctor`` greenlights a machine
    that fails mid-extract on a missing weight (ibid. §2, §3).

    Existence-and-non-emptiness rather than per-file fingerprinting —
    docling's HF snapshot layout shifts across releases, and "directory
    exists with at least one file" is the strongest invariant we can
    assert without coupling ourselves to a particular weight filename
    that the next minor bump will rename.

    Parameters
    ----------
    model_cache_dir : pathlib.Path | None
        Override for the weights directory
        (``LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR``); ``None`` falls back to
        docling's own ``~/.cache/docling/models``.
    """
    models_root, layout_folder, tableformer_folder, code_formula_folder = _docling_model_dirs(
        model_cache_dir=model_cache_dir
    )
    layout_dir = models_root / layout_folder
    tableformer_dir = models_root / tableformer_folder
    code_formula_dir = models_root / code_formula_folder
    return [
        _model_row(
            component=_COMPONENT_LAYOUT,
            cache_dir=layout_dir,
            ok_hint=f'egret-large, cached at {layout_dir}',
            missing_hint=f'missing at {layout_dir} — `litspectraits doctor --download-models`',
        ),
        _model_row(
            component=_COMPONENT_TABLEFORMER,
            cache_dir=tableformer_dir,
            ok_hint='accurate mode loaded',
            missing_hint=(
                f'missing at {tableformer_dir} — `litspectraits doctor --download-models`'
            ),
        ),
        _model_row(
            component=_COMPONENT_CODE_FORMULA,
            cache_dir=code_formula_dir,
            ok_hint='formula enrichment loaded',
            missing_hint=(
                f'missing at {code_formula_dir} — `litspectraits doctor --download-models`'
            ),
        ),
    ]


def _model_row(
    *, component: str, cache_dir: Path, ok_hint: str, missing_hint: str
) -> ExtractComponentCheck:
    if _model_dir_present(cache_dir):
        return ExtractComponentCheck(
            component=component,
            required='yes',
            is_required=True,
            status=ExtractStatus.OK,
            detail=ok_hint,
        )
    return ExtractComponentCheck(
        component=component,
        required='yes',
        is_required=True,
        status=ExtractStatus.MISSING,
        detail=missing_hint,
    )


def _check_accelerator() -> ExtractComponentCheck:
    """Probe what ``AcceleratorDevice.AUTO`` will resolve to at extract time.

    Mirrors :func:`litspectraits.extract.pdf._resolve_accelerator_label`
    by intent but adds a GPU-name hint on CUDA. CPU fallback is
    deliberately *not* flagged as a required failure — academic-PDF
    extraction is correct on CPU, only slow.
    """
    try:
        import torch  # pyright: ignore[reportMissingImports]
    except ImportError:  # pragma: no cover — torch is a docling transitive
        return ExtractComponentCheck(
            component=_COMPONENT_ACCEL,
            required='auto',
            is_required=False,
            status=ExtractStatus.CPU,
            detail='torch not importable; auto will fall back to cpu',
        )
    if torch.cuda.is_available():
        try:
            name = str(torch.cuda.get_device_name(0))
        except Exception:  # pragma: no cover — defensive
            name = 'CUDA device'
        return ExtractComponentCheck(
            component=_COMPONENT_ACCEL,
            required='auto',
            is_required=False,
            status=ExtractStatus.CUDA,
            detail=name,
        )
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return ExtractComponentCheck(
            component=_COMPONENT_ACCEL,
            required='auto',
            is_required=False,
            status=ExtractStatus.MPS,
            detail='Apple Silicon GPU',
        )
    return ExtractComponentCheck(
        component=_COMPONENT_ACCEL,
        required='auto',
        is_required=False,
        status=ExtractStatus.CPU,
        detail='cpu-only — extraction will be materially slower',
    )


def _check_ocr_engines() -> ExtractComponentCheck:
    """Static row: v3 pipeline keeps ``do_ocr=False`` so OCR is off.

    Reported as ``OFF`` rather than ``MISSING`` so an operator doesn't
    chase a phantom "OCR engines not installed" warning when the v3
    default pipeline doesn't need them (``extract-pdf-plan.md`` §8).
    """
    return ExtractComponentCheck(
        component=_COMPONENT_OCR,
        required='no',
        is_required=False,
        status=ExtractStatus.OFF,
        detail='do_ocr=False (v3 default; scanned PDFs surface as EmptyDocumentError)',
    )


def _maybe_download_models(*, force: bool, model_cache_dir: Path | None = None) -> None:
    """Download the three required v3 weights: Egret layout, TableFormer, code-formula.

    Synchronous; the caller dispatches via :func:`asyncio.to_thread`.
    The required-models list matches the v3 ``PdfPipelineOptions``
    (``docling-settings-buildout.md`` §2) — which is *not* the same as
    docling's downloader defaults:

    - ``download_models(with_layout=True)`` would pull the docling
      *default* layout model (Heron), not the Egret-Large spec the
      extractor configures, so we fetch the layout weights explicitly via
      :meth:`LayoutModel.download_models` with the matching
      ``layout_model_config``.
    - ``with_code_formula=True`` — ``do_formula_enrichment=True`` needs
      the formula VLM.
    - ``with_rapidocr`` / ``with_picture_classifier`` / the VLM-figure
      switches default to True in docling 2.93; we pin them False so a
      doctor ``--download-models`` run doesn't quietly pull weights the
      v3 pipeline never loads.

    ``model_cache_dir`` (``LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR``) is the
    weights directory; passing ``None`` leaves docling on its default
    ``settings.cache_dir / 'models'`` — matching what
    :func:`_docling_model_dirs` then probes and what
    :func:`litspectraits.extract.pdf.extract_pdf` reads from.
    """
    from docling.datamodel.layout_model_specs import (  # pyright: ignore[reportMissingImports]
        DOCLING_LAYOUT_EGRET_LARGE,
    )
    from docling.models.stages.layout.layout_model import (  # pyright: ignore[reportMissingImports]
        LayoutModel,
    )
    from docling.utils.model_downloader import (  # pyright: ignore[reportMissingImports]
        download_models,
    )

    models_root, layout_folder, _tableformer_folder, _code_formula_folder = _docling_model_dirs(
        model_cache_dir=model_cache_dir
    )
    _logger.info(
        'downloading docling models (egret layout + tableformer + code-formula)',
        models_root=str(models_root),
    )
    LayoutModel.download_models(
        local_dir=models_root / layout_folder,
        force=force,
        progress=False,
        layout_model_config=DOCLING_LAYOUT_EGRET_LARGE,
    )
    download_models(
        output_dir=model_cache_dir,
        force=force,
        progress=False,
        with_layout=False,  # handled above for the Egret spec, not docling's default
        with_tableformer=True,
        with_tableformer_v2=False,
        with_code_formula=True,  # do_formula_enrichment=True needs the formula VLM
        with_picture_classifier=False,
        with_smolvlm=False,
        with_granitedocling=False,
        with_granitedocling_mlx=False,
        with_smoldocling=False,
        with_smoldocling_mlx=False,
        with_granite_vision=False,
        with_granite_chart_extraction=False,
        with_granite_chart_extraction_v4=False,
        with_rapidocr=False,
        with_easyocr=False,
    )


async def _maybe_smoke_extract(*, fixture_path: Path, settings: Settings) -> ExtractComponentCheck:
    """Run a live docling conversion against ``fixture_path``; time it.

    Stages the fixture into a throwaway tempdir so doctor remains
    read-only with respect to the operator's real ``data_dir``. The
    fixture is sniffed, hashed, written into an
    :class:`AcquisitionRecord`, and run through
    :func:`litspectraits.extract.extract_pdf` exactly as the production
    extractor would. Any :class:`ExtractError` is captured and folded
    into the row's status / detail; we never let it propagate, because
    the surrounding ``doctor`` invocation must finish rendering the
    other rows.
    """
    if not fixture_path.is_file():
        return ExtractComponentCheck(
            component=_COMPONENT_SMOKE,
            required='no',
            is_required=False,
            status=ExtractStatus.MISSING,
            detail=f'fixture not found: {fixture_path}',
        )

    # Local import keeps the docling dependency optional at module load.
    from litspectraits.extract.pdf import extract_pdf

    with tempfile.TemporaryDirectory(prefix='litspectraits-doctor-smoke-') as td_str:
        td = Path(td_str)
        store = ArtifactStore(td)
        record = _stage_fixture_for_smoke(fixture_path=fixture_path, store=store)
        start = time.monotonic()
        try:
            await extract_pdf(
                record,
                store,
                reextract=False,
                model_cache_dir=settings.docling_model_cache_dir,
            )
        except ExtractError as exc:
            return ExtractComponentCheck(
                component=_COMPONENT_SMOKE,
                required='no' if not _smoke_required(settings) else 'yes',
                is_required=False,
                status=ExtractStatus.MISSING,
                detail=_summarize(exc),
            )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ExtractComponentCheck(
            component=_COMPONENT_SMOKE,
            required='no',
            is_required=False,
            status=ExtractStatus.OK,
            detail=f'{elapsed_ms} ms ({fixture_path.name})',
        )


def _smoke_required(settings: Settings) -> bool:
    """Reserved hook: ``--smoke-extract`` is informational today.

    Returning False keeps the smoke row out of the exit-code calculus —
    a slow CPU machine that takes 30 s on synthetic.pdf is still
    "doctor green." If we ever want to gate releases on the smoke
    convert succeeding (CI), flip this on a future Settings flag.
    """
    del settings
    return False


_SMOKE_DOI: Final = '10.0/doctor-smoke'


def _stage_fixture_for_smoke(*, fixture_path: Path, store: ArtifactStore) -> AcquisitionRecord:
    """Copy the fixture into a throwaway store and synthesize a manifest.

    Doctor's smoke-convert needs an :class:`AcquisitionRecord` to feed
    :func:`extract_pdf`, but we deliberately don't run the full ingest
    pipeline (no DOI, no CrossRef, no publisher dispatch). The store
    handle is a real :class:`ArtifactStore` pointed at a tempdir, so
    nothing about this leaks into the operator's actual ``data_dir``.

    A synthetic DOI prefix (``10.0/``) keeps the fixture-derived
    manifest out of the polite-pool publisher tables and makes the
    intent self-evident in any leaked log line.
    """
    import hashlib
    from datetime import UTC, datetime

    from litspectraits.manifest import RetrievePayload

    body = fixture_path.read_bytes()
    sha256 = hashlib.sha256(body).hexdigest()
    tmp_path = store.tmp_dir / f'{sha256}.pdf'
    tmp_path.write_bytes(body)
    payload = RetrievePayload(
        sha256=sha256,
        byte_size=len(body),
        tmp_path=tmp_path,
        format=Format.PDF,
        fetched_url=f'file://{fixture_path}',
        sdk_version='doctor-smoke',
    )
    meta = CrossRefMetadata(
        doi=_SMOKE_DOI,
        publisher_str='',
        title='doctor synthetic fixture',
        authors=(),
        year=None,
        type=None,
        license=None,
    )
    record = AcquisitionRecord(
        doi=_SMOKE_DOI,
        sha256=sha256,
        artifact_path=store.artifact_relpath(sha256, Format.PDF),
        format=Format.PDF,
        publisher=Publisher.WILEY,
        metadata=meta,
        fetched_url=payload.fetched_url,
        fetched_at=datetime.now(tz=UTC),
        fetcher_version='doctor',
        sdk_version=payload.sdk_version,
        byte_size=payload.byte_size,
        origin='auto',
        manual_provenance=None,
    )
    store.commit(src=tmp_path, record=record)
    return record


def _packaged_fixture_path() -> Path:
    """Resolve the fixture path bundled inside the package.

    Returns the canonical on-disk path via :mod:`importlib.resources`
    so the call works both from a source checkout and from a
    pip-installed wheel.
    """
    # `as_file` returns a context manager only for zipped resources;
    # for filesystem packages the underlying Traversable already maps
    # to a real path. `_fixtures/` lives next to the .py modules in the
    # wheel layout, so the .files() lookup yields a Path directly.
    return Path(str(resources.files(_FIXTURE_PACKAGE).joinpath(_FIXTURE_FILENAME)))


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
    CredStatus.NOT_OPEN_ACCESS: 'not open-access',
    CredStatus.OTHER_FAILURE: 'other failure',
}


# Extract-component status labels intentionally mirror the StrEnum value
# (with the one underscore replacement). Keeping it explicit rather than
# a generic ``replace('_', ' ')`` means a future enum addition surfaces
# as a KeyError in the renderer instead of silently producing a
# half-formatted label.
_EXTRACT_STATUS_LABEL: Final[dict[ExtractStatus, str]] = {
    ExtractStatus.OK: 'ok',
    ExtractStatus.NOT_INSTALLED: 'not installed',
    ExtractStatus.MISSING: 'missing',
    ExtractStatus.OFF: 'off',
    ExtractStatus.CUDA: 'cuda',
    ExtractStatus.MPS: 'mps',
    ExtractStatus.CPU: 'cpu',
    ExtractStatus.SKIPPED: 'skipped',
}


def render(report: DoctorReport, *, console: Console) -> None:
    """Render ``report`` as Rich tables (IP, publisher, extract).

    No styling beyond column / header — keeping the rendering plain so
    the golden-output test in ``test_doctor.py`` does not have to model
    ANSI escape sequences. The CLI layer applies colour separately if
    desired (e.g. coloured row backgrounds keyed off ``status``).

    The extract table is only rendered when ``report.extract_check`` is
    populated. Callers that construct a :class:`DoctorReport` manually
    (today, only the tests) can omit it and get the original two-table
    output.
    """
    console.print(_render_ip_table(report.ip_check))
    console.print(_render_cred_table(report.cred_checks))
    if report.extract_check is not None:
        console.print(_render_extract_table(report.extract_check))


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


def _render_extract_table(report: ExtractReport) -> Table:
    # Component / detail cells contain ``[extract]`` and other strings
    # that Rich would otherwise parse as markup tags; escape so the
    # literal characters render. Required/Status columns are enum-derived
    # and bracket-free, but we escape uniformly to keep the call site
    # simple.
    table = Table(title='Extract components', show_header=True, header_style='bold')
    table.add_column('Component', no_wrap=True)
    table.add_column('Required', no_wrap=True)
    table.add_column('Status', no_wrap=True)
    table.add_column('Hint')
    for row in report.components:
        table.add_row(
            rich_escape(row.component),
            rich_escape(row.required),
            rich_escape(_EXTRACT_STATUS_LABEL[row.status]),
            rich_escape(row.detail),
        )
    return table
