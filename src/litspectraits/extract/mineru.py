"""MinerU PDF extractor (``docs/mineru-backend-spec.md`` §3).

The **default** PDF backend alongside :mod:`litspectraits.extract.pdf`
(docling, now the opt-in fallback — ``docs/mineru-primary-promotion.md``).
Same place in the pipeline — ``Format.PDF`` artifact → verbatim native dict
on disk at ``documents/sha256/<aa>/<sha>/document.json`` — parsed by MinerU
(``docs/mineru-backend-spec.md`` §1, §8), plus an auxiliary human-QA
``document.md`` + ``images/`` render beside it (§5 of the promotion doc). The
downstream :func:`litspectraits.normalize.normalize_mineru_document` turns the
canonical dict (only) into a :class:`~litspectraits.normalize.Document` with
``route='mineru'``.

Six stages mirroring :func:`litspectraits.extract.pdf.extract_pdf`:

1. preflight — format + on-disk existence of the artifact, plus the
   ``engine`` / ``effort`` choice (:data:`MINERU_ENGINES` /
   :data:`MINERU_EFFORTS`);
2. lazy-import ``mineru`` inside the function body, guarded →
   :class:`~litspectraits.errors.MineruImportError`;
3. run ``mineru.cli.common.do_parse`` into a scratch dir under
   ``store.tmp_dir`` via :func:`asyncio.to_thread`. MinerU is
   file-output-oriented — ``do_parse`` writes ``*_middle.json`` rather than
   returning the parse in memory;
4. read back ``*_middle.json`` (points-scale, hierarchical — preferred over
   ``content_list.json``) as the **verbatim** native dict, and run the
   structural-floor checks shared with the docling path
   (:class:`~litspectraits.errors.EmptyDocumentError` /
   :class:`~litspectraits.errors.ParseDegradedError`); an empty / missing
   ``pdf_info`` is a :class:`~litspectraits.errors.MineruConversionError`
   (MinerU has no ``ConversionStatus`` enum to key on);
5. serialize the middle.json dict unchanged (same discipline as the docling
   path's ``_serialize_document``);
6. atomic commit (:func:`litspectraits._io.atomic_write`,
   integrity-check-on-rerun, ``--reextract`` to overwrite) with
   ``backend_id='mineru'`` + a ``config_view`` recording the output-affecting
   knobs (``engine``, ``effort``, ``parse_method``, ``formula`` / ``table``
   toggles, ``lang``, ``mineru_version``) in ``meta.json``, plus the
   auxiliary ``document.md`` / ``images/`` companions and an ``aux_outputs``
   pointer to them in the meta. The integrity check keys on ``document.json``
   only; the aux companions ride along on the write path.

``engine`` selects which of MinerU's three local backends parses the PDF
(``docs/mineru-backend-spec.md`` §1, §11 Q-D/Q-E) — ``'vlm-engine'``
(**default** since the promotion; VLM parse, model-predicted geometry →
``geometry_fidelity='approximate'``), ``'pipeline'`` (text-layer, exact
geometry), or ``'hybrid-engine'`` (text-layer + VLM formula/table, tunable
via ``effort``). The adapter grades ``Provenance.geometry_fidelity`` off the
``_backend`` tag MinerU itself stamps into ``middle.json`` — see
:func:`litspectraits.normalize.mineru_adapter._engine_fidelity` — so no
extra plumbing is needed here beyond forwarding the choice to ``do_parse``.
``effort`` (``'medium'`` default / ``'high'``) only affects the
``hybrid-engine`` backend; MinerU itself ignores it for ``pipeline`` and
``vlm-engine`` (mirrors its own CLI, which accepts ``--effort``
unconditionally), so it is forwarded unconditionally too and recorded in
``meta.json`` only when it was actually consulted.

Model weights are resolved through MinerU's own mechanism
(``MINERU_MODEL_SOURCE`` env + ``~/mineru.json``), not through the docling
``model_cache_dir`` — the two backends keep independent caches. The
``model_cache_dir`` parameter is accepted only so the PDF-backend dispatch
can call both extractors with one signature; it is the docling weight cache
and is deliberately not consulted here. To relocate MinerU's own cache, set
``LITSPECTRAITS_MINERU_MODEL_CACHE_DIR`` — the CLI startup callback exports it
as ``HF_HOME`` before this module's lazy ``import mineru`` runs, so both the
``doctor`` download and the parse below read/write weights there
(:func:`litspectraits.config.apply_mineru_model_cache_env`).
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

import structlog

from litspectraits._io import atomic_write, file_sha256
from litspectraits.errors import (
    BackendNotApplicableError,
    EmptyDocumentError,
    ExtractIntegrityError,
    MineruConversionError,
    MineruImportError,
    MissingArtifactError,
    ParseDegradedError,
    SerializationError,
    WrongFormatForExtractorError,
)
from litspectraits.extract.backend_ids import MINERU
from litspectraits.extract.pdf import FLOOR_CHARS, MIN_TEXT_BLOCKS
from litspectraits.manifest import AcquisitionRecord, Extractor, ExtractRecord, Format
from litspectraits.store import ArtifactStore

_EXTRACTOR_NAME: Final = 'mineru'
_DIST_NAME: Final = 'mineru'
_DOCUMENT_FILENAME: Final = 'document.json'
_META_FILENAME: Final = 'meta.json'
# Auxiliary, human-QA outputs (``docs/mineru-primary-promotion.md`` §5).
# ``document.md`` is MinerU's markdown render; ``images/`` holds the figures
# it references. Neither is read by ``normalize`` — they sit beside the
# canonical ``document.json`` for eyeballing extraction quality.
_MARKDOWN_FILENAME: Final = 'document.md'
_IMAGES_DIRNAME: Final = 'images'

MineruEngine = Literal['pipeline', 'vlm-engine', 'hybrid-engine']
"""MinerU's local (non-http-client) backends — the ``--mineru-engine`` vocabulary."""

DEFAULT_MINERU_ENGINE: Final[MineruEngine] = 'vlm-engine'
"""VLM parse — the promoted default (``docs/mineru-primary-promotion.md`` §1).

Recovers inline/display formulae and table values docling's text-layer route
loses on Wiley PDFs, at the cost of model-predicted (``'approximate'``)
geometry. The exact-geometry ``'pipeline'`` engine is still available via
``--mineru-engine pipeline``; ``'hybrid-engine'`` keeps exact geometry while
using the VLM for formula/table only.
"""

MINERU_ENGINES: Final[tuple[MineruEngine, ...]] = ('pipeline', 'vlm-engine', 'hybrid-engine')
"""All wired MinerU engines, for CLI choice validation.

Deliberately excludes MinerU's ``vlm-http-client`` / ``hybrid-http-client``
backends — those parse against a remote ``server_url`` this project has no
credentialed access to; only the three backends that run against the local
weight cache are wired.
"""

HYBRID_ENGINE: Final = 'hybrid-engine'
"""The one :data:`MINERU_ENGINES` member that consults :data:`MINERU_EFFORTS`."""

MineruEffort = Literal['medium', 'high']
"""MinerU's hybrid-engine effort levels — the ``--mineru-effort`` vocabulary."""

DEFAULT_MINERU_EFFORT: Final[MineruEffort] = 'medium'

MINERU_EFFORTS: Final[tuple[MineruEffort, ...]] = ('medium', 'high')
"""Effort levels, for CLI choice validation. Meaningful only for :data:`HYBRID_ENGINE`."""

# ``parse_method='auto'`` lets MinerU pick OCR vs text-layer per page; the
# text-layer path is what yields exact geometry on born-digital PDFs.
_PARSE_METHOD: Final = 'auto'
# Default OCR/layout language. MR literature is overwhelmingly English; a
# per-DOI language override is a follow-on if the corpus ever needs one.
_LANG: Final = 'en'

# middle.json block types that carry running prose (mirrors MinerU's own
# ``make_blocks_to_markdown`` text bucket). Used for the char-count density
# floor; kept in sync with the adapter's text-block mapping.
_TEXT_BLOCK_TYPES: Final = frozenset({'text', 'list', 'index', 'abstract', 'ref_text'})
_TITLE_BLOCK_TYPE: Final = 'title'
_TABLE_BLOCK_TYPE: Final = 'table'
_FIGURE_BLOCK_TYPES: Final = frozenset({'image', 'chart'})

_logger: Final = structlog.get_logger('litspectraits.extract.mineru')


class _ParseOutputs(NamedTuple):
    """The MinerU parse products this backend persists.

    ``middle_json`` is the canonical, verbatim payload (serialized to
    ``document.json`` and the sole input to ``normalize``). ``markdown`` and
    ``images`` are the auxiliary human-QA render
    (``docs/mineru-primary-promotion.md`` §5): ``markdown`` is ``None`` when
    MinerU emitted no ``.md``; ``images`` is the ``(filename, bytes)`` pairs
    from the sibling ``images/`` dir, empty when the parse yielded no figures.
    """

    middle_json: dict[str, Any]
    markdown: bytes | None
    images: tuple[tuple[str, bytes], ...]


async def extract_mineru(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
    model_cache_dir: Path | None = None,
    engine: str = DEFAULT_MINERU_ENGINE,
    effort: str = DEFAULT_MINERU_EFFORT,
) -> ExtractRecord:
    """Extract a PDF artifact with MinerU into the canonical document tree.

    Signature mirrors :func:`litspectraits.extract.pdf.extract_pdf` so the
    backend dispatch (``docs/mineru-backend-spec.md`` §1) can treat the two
    PDF backends interchangeably. Commits ``document.json`` (the verbatim
    MinerU ``middle.json``) + ``meta.json`` under
    ``documents/sha256/<aa>/<sha>/`` only on full success; every failure
    raises an :class:`~litspectraits.errors.ExtractError` subclass before
    any on-disk write.

    Idempotent on identical output: if ``document.json`` already exists with
    bytes matching the freshly-parsed middle.json, the directory is left
    untouched and the returned record describes the just-completed run.

    Parameters
    ----------
    record : AcquisitionRecord
        Manifest of the committed PDF. ``record.format`` must be
        :attr:`Format.PDF`.
    store : ArtifactStore
        Resolves ``record.artifact_path`` to an absolute path, provides the
        scratch + staging ``tmp_dir``, and computes the canonical
        ``documents/sha256/<aa>/<sha>/`` directory.
    reextract : bool, default False
        Overwrite an existing ``document.json`` whose bytes differ from the
        new parse. Without this flag a divergent re-extract raises
        :class:`~litspectraits.errors.ExtractIntegrityError` — and that
        guard also catches an unintended backend swap on the same artifact
        (``docs/mineru-backend-spec.md`` §5).
    model_cache_dir : pathlib.Path | None, default None
        The docling weight cache, forwarded by the shared PDF-backend
        dispatch. MinerU resolves its own weights through
        ``MINERU_MODEL_SOURCE`` / ``~/mineru.json`` (relocatable via
        ``LITSPECTRAITS_MINERU_MODEL_CACHE_DIR`` →
        :func:`litspectraits.config.apply_mineru_model_cache_env`), so this
        value is **not** consulted here; it is accepted only for signature
        compatibility.
    engine : str, default :data:`DEFAULT_MINERU_ENGINE`
        Which MinerU local backend parses the PDF — one of
        :data:`MINERU_ENGINES`. Forwarded verbatim to ``do_parse``'s
        ``backend`` parameter.
    effort : str, default :data:`DEFAULT_MINERU_EFFORT`
        Hybrid-engine parsing effort — one of :data:`MINERU_EFFORTS`.
        Only consulted by MinerU when ``engine == 'hybrid-engine'``.

    Returns
    -------
    ExtractRecord
        In-memory description of the extraction. The persisted form is
        ``meta.json``; this struct is not separately written.

    Raises
    ------
    WrongFormatForExtractorError
        ``record.format`` is not :attr:`Format.PDF`.
    MissingArtifactError
        ``record.artifact_path`` does not resolve to a file under
        ``store.data_dir``.
    BackendNotApplicableError
        ``engine`` is not in :data:`MINERU_ENGINES` or ``effort`` is not in
        :data:`MINERU_EFFORTS` — a direct-caller guard mirroring the CLI's
        ``click.Choice`` validation.
    MineruImportError
        The ``[mineru]`` extra is not installed.
    MineruConversionError
        ``do_parse`` raised, wrote no ``*_middle.json``, or produced a
        middle.json with no ``pdf_info`` pages.
    EmptyDocumentError
        Zero non-empty text blocks after a successful parse.
    ParseDegradedError
        ``char_count`` below :data:`~litspectraits.extract.pdf.FLOOR_CHARS`.
    SerializationError
        The middle.json dict could not be re-serialized to JSON.
    ExtractIntegrityError
        Existing ``document.json`` differs and ``reextract=False``.
    """
    artifact_path = _preflight(record=record, store=store)
    _validate_engine_and_effort(doi=record.doi, engine=engine, effort=effort)
    do_parse = _load_mineru(doi=record.doi)

    parse = await asyncio.to_thread(
        _run_do_parse,
        do_parse=do_parse,
        doi=record.doi,
        artifact_path=artifact_path,
        tmp_dir=store.tmp_dir,
        engine=engine,
        effort=effort,
    )

    counts = _count_structure(parse.middle_json)
    _check_structure(doi=record.doi, counts=counts)

    body = _serialize_document(doi=record.doi, middle_json=parse.middle_json)
    return _commit(
        record=record,
        store=store,
        counts=counts,
        body=body,
        reextract=reextract,
        engine=engine,
        effort=effort,
        markdown=parse.markdown,
        images=parse.images,
    )


# Stage 1 — preflight ---------------------------------------------------------


def _preflight(*, record: AcquisitionRecord, store: ArtifactStore) -> Path:
    """Resolve the artifact path and assert format + existence.

    The magic-byte sniff at ingest already validated ``%PDF-``; we do not
    re-sniff. This is purely "the dispatcher routed a PDF here and the file
    is still on disk."
    """
    if record.format is not Format.PDF:
        raise WrongFormatForExtractorError(
            doi=record.doi,
            expected=Format.PDF.value,
            actual=record.format.value,
            extractor=_EXTRACTOR_NAME,
        )
    artifact_path = store.data_dir / record.artifact_path
    if not artifact_path.is_file():
        raise MissingArtifactError(
            doi=record.doi,
            sha256=record.sha256,
            artifact_path=str(artifact_path),
            hint='re-run `litspectraits ingest <doi>` to refetch the artifact',
        )
    return artifact_path


def _validate_engine_and_effort(*, doi: str, engine: str, effort: str) -> None:
    """Reject an unwired ``engine`` / ``effort`` before any conversion work.

    The CLI already gates these via ``click.Choice`` (``docs/mineru-backend-
    spec.md`` §8), but :func:`extract_mineru` is also called directly (tests,
    ``doctor``, future non-CLI callers), so this is a defense-in-depth guard
    rather than the only line of defense.
    """
    if engine not in MINERU_ENGINES:
        raise BackendNotApplicableError(
            doi=doi,
            backend=MINERU,
            mineru_engine=engine,
            hint=f'unknown --mineru-engine {engine!r}; valid: {", ".join(MINERU_ENGINES)}',
        )
    if effort not in MINERU_EFFORTS:
        raise BackendNotApplicableError(
            doi=doi,
            backend=MINERU,
            mineru_effort=effort,
            hint=f'unknown --mineru-effort {effort!r}; valid: {", ".join(MINERU_EFFORTS)}',
        )


# Stage 2 — load -------------------------------------------------------------


def _load_mineru(*, doi: str) -> Any:
    """Lazy-import MinerU's in-process ``do_parse`` entrypoint.

    Kept lazy so the ``[mineru]`` extra stays opt-in — a docling-only corpus
    never imports MinerU (mirrors ``extract.pdf._load_docling``). An
    :class:`ImportError` becomes a typed
    :class:`~litspectraits.errors.MineruImportError`.

    Tests bypass this by monkeypatching the symbol; the import-error path is
    exercised by making the import fail.
    """
    try:
        from mineru.cli.common import do_parse  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise MineruImportError(
            doi=doi,
            extractor=_EXTRACTOR_NAME,
            hint='install the [mineru] extra (uv sync --extra mineru)',
            import_error=str(exc),
        ) from exc
    _bridge_mineru_loguru()
    _silence_mineru_tqdm()
    return do_parse


# Set once we have patched tqdm's ``__init__``; guards against re-wrapping on a
# second extraction in the same process.
_tqdm_patched: bool = False


def _silence_mineru_tqdm() -> None:
    """Force MinerU's tqdm progress bars off when the console is quiet.

    MinerU and its VLM client (``mineru_vl_utils``) draw tqdm bars straight to
    ``stderr``, and the worst offenders pass ``disable=`` **explicitly** — e.g.
    ``tqdm(..., desc='Predict', disable=not self.use_tqdm)`` with ``use_tqdm``
    defaulting to ``True``. tqdm's ``TQDM_DISABLE`` env var only fills the
    ``disable`` argument when the caller *omits* it, so it can never reach those
    bars; :func:`litspectraits._logging.tame_third_party_logging` sets the env
    var (which handles the implicit-``disable`` bars and is our quiet/verbose
    signal), and we close the remaining gap here.

    The fix patches the tqdm class ``__init__`` to force ``disable=True``,
    overriding the explicit argument. Every ``from tqdm import tqdm`` reference
    holds the class object, so mutating the class reaches all of them — and
    subclasses that do not override ``__init__`` (``tqdm.auto`` in a terminal,
    the transformers path) inherit the patch too. Applied here, after MinerU's
    import, because the patch only bites once tqdm's class exists.

    Gated on the ``TQDM_DISABLE`` signal so ``-v`` (which clears it) keeps the
    bars for a debugging run, and idempotent via the module ``_tqdm_patched``
    sentinel.
    """
    global _tqdm_patched
    if _tqdm_patched or not os.environ.get('TQDM_DISABLE'):
        return
    from tqdm import std as tqdm_std  # pyright: ignore[reportMissingImports]

    original_init = tqdm_std.tqdm.__init__

    def _silenced_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs['disable'] = True
        original_init(self, *args, **kwargs)

    tqdm_std.tqdm.__init__ = _silenced_init
    _tqdm_patched = True


def _bridge_mineru_loguru() -> None:
    """Route MinerU's ``loguru`` output into stdlib ``logging`` under ``mineru``.

    MinerU logs through ``loguru``, which owns its own ``stderr`` sink and
    ignores the stdlib root logger our structlog handler lives on — so an
    ``INFO``-level MinerU narrates over any console level we set. loguru also
    *reconfigures itself at import time*, which is why this runs here, right
    after :func:`_load_mineru` imports ``do_parse``, rather than at CLI
    startup (a startup bridge would be clobbered by that import).

    We drop MinerU's default sink and add one that re-emits each record to
    ``logging.getLogger('mineru')`` at the *same* numeric level (loguru and
    stdlib share level numbers). :func:`litspectraits._logging.tame_third_party_logging`
    pins that logger to the resolved console level, so MinerU output now
    obeys the one knob: silent at the ``warning`` default, visible under
    ``-v``. loguru is a hard dependency of ``mineru``; importing it here is
    safe because we only reach this line once the ``mineru`` import succeeded.
    """
    from loguru import logger as loguru_logger  # pyright: ignore[reportMissingImports]

    def _forward(message: Any) -> None:
        record = message.record
        logging.getLogger('mineru').log(record['level'].no, record['message'])

    loguru_logger.remove()
    loguru_logger.add(_forward, level=0, format='{message}')


# Stage 3 — convert ----------------------------------------------------------


def _run_do_parse(
    *,
    do_parse: Any,
    doi: str,
    artifact_path: Path,
    tmp_dir: Path,
    engine: str,
    effort: str,
) -> _ParseOutputs:
    """Run MinerU into a scratch dir under ``tmp_dir`` and read back its outputs.

    MinerU writes its outputs to disk rather than returning them, so we hand
    it a per-run scratch directory inside the store's ``tmp/`` (never the
    canonical tree), then read back the single ``*_middle.json`` it produced
    (the canonical payload) plus the auxiliary markdown render + ``images/``
    (``f_dump_md=True``; ``docs/mineru-primary-promotion.md`` §5) — both while
    the scratch tree still exists. The scratch tree is removed afterwards
    regardless of outcome — ``tmp/`` is also cleared at store startup as a
    backstop.

    Runs in a worker thread (called via :func:`asyncio.to_thread`); MinerU's
    own image-render pool is process-based and managed internally.

    ``effort`` is forwarded unconditionally — MinerU's own ``do_parse`` only
    consults it on the ``hybrid-engine`` branch and silently ignores it
    otherwise (confirmed against ``mineru.cli.common.do_parse``'s source: the
    ``pipeline`` and plain ``vlm-`` branches never read the parameter), so
    there is nothing to special-case here.
    """
    scratch_dir = tmp_dir / f'mineru-{secrets.token_hex(8)}'
    scratch_dir.mkdir(parents=True, exist_ok=True)
    pdf_bytes = artifact_path.read_bytes()
    # Stable, filesystem-safe stem; the read-back globs for ``*_middle.json``
    # so the exact stem does not matter, but a deterministic name keeps the
    # scratch tree legible if a run is inspected mid-flight.
    stem = 'document'
    try:
        try:
            do_parse(
                str(scratch_dir),
                [stem],
                [pdf_bytes],
                [_LANG],
                backend=engine,
                parse_method=_PARSE_METHOD,
                formula_enable=True,
                table_enable=True,
                effort=effort,
                f_draw_layout_bbox=False,
                f_draw_span_bbox=False,
                f_dump_md=True,
                f_dump_middle_json=True,
                f_dump_model_output=False,
                f_dump_orig_pdf=False,
                f_dump_content_list=False,
            )
        except Exception as exc:  # do_parse has many failure modes; re-type as one
            raise MineruConversionError(
                doi=doi,
                extractor=_EXTRACTOR_NAME,
                error=str(exc),
                hint='MinerU do_parse raised; check model weights (litspectraits doctor)',
            ) from exc

        middle_paths = sorted(scratch_dir.rglob('*_middle.json'))
        if not middle_paths:
            raise MineruConversionError(
                doi=doi,
                extractor=_EXTRACTOR_NAME,
                hint='do_parse wrote no *_middle.json — the parse produced nothing',
            )
        middle_json = _read_middle_json(doi=doi, path=middle_paths[0])
        markdown, images = _read_aux_outputs(scratch_dir)
        return _ParseOutputs(middle_json=middle_json, markdown=markdown, images=images)
    finally:
        _rmtree(scratch_dir)


def _read_middle_json(*, doi: str, path: Path) -> dict[str, Any]:
    """Load and minimally validate the MinerU middle.json payload.

    ``pdf_info`` must be a non-empty list — MinerU has no
    ``ConversionStatus`` enum, so "no pages" is the floor that distinguishes
    a degenerate parse from a usable one
    (``docs/mineru-backend-spec.md`` §3).
    """
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise MineruConversionError(
            doi=doi,
            extractor=_EXTRACTOR_NAME,
            error=str(exc),
            hint='middle.json was unreadable or not valid JSON',
        ) from exc
    if not isinstance(payload, dict):
        raise MineruConversionError(
            doi=doi,
            extractor=_EXTRACTOR_NAME,
            hint=f'middle.json is not a JSON object (got {type(payload).__name__})',
        )
    pdf_info = payload.get('pdf_info')
    if not isinstance(pdf_info, list) or not pdf_info:
        raise MineruConversionError(
            doi=doi,
            extractor=_EXTRACTOR_NAME,
            hint='middle.json carries no pdf_info pages',
        )
    return payload


def _read_aux_outputs(scratch_dir: Path) -> tuple[bytes | None, tuple[tuple[str, bytes], ...]]:
    """Read back MinerU's markdown render + extracted images from the scratch dir.

    ``f_dump_md=True`` makes MinerU write ``{stem}.md`` plus a sibling
    ``images/`` dir, which the markdown references by ``images/<name>``
    relative links (``docs/mineru-primary-promotion.md`` §5). Reading both
    here — while the scratch tree still exists — lets :func:`_commit` install
    them beside ``document.json`` so the relative links stay valid.

    Auxiliary and best-effort about *presence*: a parse that emitted no
    ``.md`` yields ``(None, ())`` rather than failing the extract (the
    canonical ``document.json`` is already in hand). It is **not** lenient
    about *read* errors — an :class:`OSError` reading a file MinerU just wrote
    propagates, since that signals a real disk problem, not an absent output.
    """
    md_paths = sorted(scratch_dir.rglob('*.md'))
    if not md_paths:
        return None, ()
    md_path = md_paths[0]
    markdown = md_path.read_bytes()
    images: list[tuple[str, bytes]] = []
    images_dir = md_path.parent / _IMAGES_DIRNAME
    if images_dir.is_dir():
        for image_path in sorted(images_dir.iterdir()):
            if image_path.is_file():
                images.append((image_path.name, image_path.read_bytes()))
    return markdown, tuple(images)


# Stage 4 — structural sanity -------------------------------------------------


class _Counts:
    """Structural counters tallied from a MinerU middle.json payload.

    A plain mutable accumulator (not frozen) because it is filled in a
    single walk; the fields mirror :class:`litspectraits.manifest.ExtractRecord`
    so the meta builder and the structural checks read one shape.
    """

    __slots__ = (
        'char_count',
        'n_figures',
        'n_pages',
        'n_section_headers',
        'n_tables',
        'n_text_blocks',
    )

    def __init__(self) -> None:
        self.n_pages = 0
        self.n_text_blocks = 0
        self.n_section_headers = 0
        self.n_tables = 0
        self.n_figures = 0
        self.char_count = 0


def _count_structure(middle_json: dict[str, Any]) -> _Counts:
    """Walk ``pdf_info[*].para_blocks`` once and tally the per-record counters.

    Section headers are counted separately from text blocks (disjoint, same
    convention as the docling path). ``char_count`` sums the running-prose
    span content plus heading text — it is a content-density measure for the
    :class:`~litspectraits.errors.ParseDegradedError` floor, not a faithful
    character total, so display-equation and table content are excluded.
    """
    counts = _Counts()
    pages = middle_json.get('pdf_info', [])
    counts.n_pages = len(pages)
    for page in pages:
        for block in page.get('para_blocks', []):
            block_type = block.get('type')
            if block_type in _TEXT_BLOCK_TYPES:
                text = _block_text(block)
                counts.char_count += len(text)
                if text:
                    counts.n_text_blocks += 1
            elif block_type == _TITLE_BLOCK_TYPE:
                counts.n_section_headers += 1
                counts.char_count += len(_block_text(block))
            elif block_type == _TABLE_BLOCK_TYPE:
                counts.n_tables += 1
            elif block_type in _FIGURE_BLOCK_TYPES:
                counts.n_figures += 1
    return counts


def _block_text(block: dict[str, Any]) -> str:
    """Concatenate the text-bearing span content of one prose/title block.

    Counts ``text`` and ``inline_equation`` span content (the spans that
    contribute characters to the rendered prose); other span types
    (display equations live in their own block type) are ignored.
    """
    parts: list[str] = []
    for line in block.get('lines', []):
        for span in line.get('spans', []):
            if span.get('type') in ('text', 'inline_equation'):
                content = span.get('content')
                if content:
                    parts.append(content)
    return ''.join(parts)


def _check_structure(*, doi: str, counts: _Counts) -> None:
    """Hard floor checks shared in spirit with the docling path.

    Empty-document fires before the char floor so a scanned-PDF-without-text
    case surfaces as :class:`EmptyDocumentError` rather than the more
    generic :class:`ParseDegradedError`.
    """
    if counts.n_text_blocks < MIN_TEXT_BLOCKS:
        raise EmptyDocumentError(
            doi=doi,
            extractor=_EXTRACTOR_NAME,
            n_text_blocks=counts.n_text_blocks,
            n_pages=counts.n_pages,
            hint=(
                'zero text blocks recovered — likely a scanned PDF served '
                'without a text layer; refetch the publisher TDM version'
            ),
        )
    if counts.char_count < FLOOR_CHARS:
        raise ParseDegradedError(
            doi=doi,
            extractor=_EXTRACTOR_NAME,
            char_count=counts.char_count,
            floor_chars=FLOOR_CHARS,
            n_text_blocks=counts.n_text_blocks,
            hint='layout recognition probably failed; most content filtered as furniture',
        )


# Stage 5 — serialize ---------------------------------------------------------


def _serialize_document(*, doi: str, middle_json: dict[str, Any]) -> bytes:
    """Render the verbatim MinerU middle.json as canonical JSON bytes.

    The dict is persisted unchanged (no transformation, no markdown
    round-trip — ``docs/mineru-backend-spec.md`` §3). ``sort_keys`` makes
    the on-disk bytes deterministic so the integrity check on re-extract is
    meaningful. The dict came from :func:`json.loads`, so re-encoding cannot
    realistically fail; the guard keeps parity with the docling path.
    """
    try:
        text = json.dumps(middle_json, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            doi=doi,
            extractor=_EXTRACTOR_NAME,
            hint='MinerU middle.json carried a value json.dumps could not encode',
            error=str(exc),
        ) from exc
    return (text + '\n').encode('utf-8')


# Stage 6 — commit ------------------------------------------------------------


def _commit(
    *,
    record: AcquisitionRecord,
    store: ArtifactStore,
    counts: _Counts,
    body: bytes,
    reextract: bool,
    engine: str,
    effort: str,
    markdown: bytes | None,
    images: tuple[tuple[str, bytes], ...],
) -> ExtractRecord:
    """Atomically install ``document.json`` + ``meta.json`` (+ aux); enforce integrity.

    Same decision tree as the docling path's commit for the canonical
    ``document.json``: absent → write; present and bytes match → no-op;
    present and bytes differ → :class:`ExtractIntegrityError` unless
    ``reextract`` (then overwrite). The auxiliary ``document.md`` / ``images/``
    (``docs/mineru-primary-promotion.md`` §5) ride along whenever the canonical
    doc is (re)written; the byte-identical no-op path leaves them untouched.
    The integrity gate is keyed on ``document.json`` only — aux outputs are
    companions, not part of the canonical-identity check.
    """
    target_dir = store.document_dir(record.sha256)
    target_doc = target_dir / _DOCUMENT_FILENAME
    target_meta = target_dir / _META_FILENAME
    version = _mineru_version()

    new_sha = hashlib.sha256(body).hexdigest()
    if target_doc.exists():
        existing_sha = file_sha256(target_doc)
        if existing_sha == new_sha:
            _logger.info(
                'extract no-op; document.json bytes unchanged',
                doi=record.doi,
                sha256=record.sha256,
                document_sha256=new_sha,
            )
            return _build_extract_record(
                record=record,
                version=version,
                counts=counts,
                extracted_at=datetime.now(tz=UTC),
            )
        if not reextract:
            raise ExtractIntegrityError(
                doi=record.doi,
                sha256=record.sha256,
                existing_document_sha256=existing_sha,
                incoming_document_sha256=new_sha,
                hint='re-extracted document differs; pass --reextract to overwrite',
            )

    target_dir.mkdir(parents=True, exist_ok=True)
    extracted_at = datetime.now(tz=UTC)
    aux_outputs = _aux_outputs_descriptor(markdown=markdown, images=images)
    meta_payload = _build_meta(
        record=record,
        version=version,
        counts=counts,
        extracted_at=extracted_at,
        engine=engine,
        effort=effort,
        aux_outputs=aux_outputs,
    )
    meta_bytes = (json.dumps(meta_payload, indent=2, sort_keys=True) + '\n').encode('utf-8')

    atomic_write(tmp_dir=store.tmp_dir, target=target_doc, body=body)
    atomic_write(tmp_dir=store.tmp_dir, target=target_meta, body=meta_bytes)
    _write_aux_outputs(store=store, target_dir=target_dir, markdown=markdown, images=images)

    _logger.info(
        'extract committed',
        doi=record.doi,
        sha256=record.sha256,
        backend_id=MINERU,
        document_sha256=new_sha,
        n_pages=counts.n_pages,
        n_text_blocks=counts.n_text_blocks,
        n_section_headers=counts.n_section_headers,
        n_tables=counts.n_tables,
        n_figures=counts.n_figures,
        char_count=counts.char_count,
    )
    return _build_extract_record(
        record=record, version=version, counts=counts, extracted_at=extracted_at
    )


def _aux_outputs_descriptor(
    *, markdown: bytes | None, images: tuple[tuple[str, bytes], ...]
) -> dict[str, Any]:
    """Describe the auxiliary outputs that will be written, for ``meta.json``.

    Keys are present only for outputs actually produced — no ``markdown`` key
    when MinerU emitted none, no ``images_dir`` key when there were no figures
    — so the meta never advertises a companion file that is not on disk.
    """
    descriptor: dict[str, Any] = {}
    if markdown is not None:
        descriptor['markdown'] = _MARKDOWN_FILENAME
    if images:
        descriptor['images_dir'] = _IMAGES_DIRNAME
    return descriptor


def _write_aux_outputs(
    *,
    store: ArtifactStore,
    target_dir: Path,
    markdown: bytes | None,
    images: tuple[tuple[str, bytes], ...],
) -> None:
    """Install ``document.md`` + ``images/*`` beside the canonical ``document.json``.

    Each file goes through the same :func:`~litspectraits._io.atomic_write`
    tmp→rename discipline as the canonical outputs. Called only on the write
    path (never the no-op path), after ``document.json`` / ``meta.json`` land.
    """
    if markdown is not None:
        atomic_write(tmp_dir=store.tmp_dir, target=target_dir / _MARKDOWN_FILENAME, body=markdown)
    if images:
        images_dir = target_dir / _IMAGES_DIRNAME
        images_dir.mkdir(parents=True, exist_ok=True)
        for name, data in images:
            atomic_write(tmp_dir=store.tmp_dir, target=images_dir / name, body=data)


def _build_extract_record(
    *,
    record: AcquisitionRecord,
    version: str,
    counts: _Counts,
    extracted_at: datetime,
) -> ExtractRecord:
    return ExtractRecord(
        sha256=record.sha256,
        extractor=Extractor.MINERU,
        extractor_version=f'{_DIST_NAME} {version}',
        extracted_at=extracted_at,
        n_text_blocks=counts.n_text_blocks,
        n_section_headers=counts.n_section_headers,
        n_tables=counts.n_tables,
        n_figures=counts.n_figures,
        char_count=counts.char_count,
        n_pages=counts.n_pages,
    )


def _build_meta(
    *,
    record: AcquisitionRecord,
    version: str,
    counts: _Counts,
    extracted_at: datetime,
    engine: str,
    effort: str,
    aux_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Assemble ``meta.json`` with the backend discriminator + config view.

    ``backend_id='mineru'`` is the seam ``normalize`` dispatches on
    (``docs/mineru-backend-spec.md`` §1). The ``pipeline`` block records the
    output-affecting MinerU knobs — the analogue of the docling path's
    pipeline view — so a later commit can detect a stale extraction without
    re-running. ``effort`` is only recorded when it was actually consulted
    (``engine == 'hybrid-engine'``); ``None`` otherwise, so the config view
    never implies a knob mattered when MinerU silently ignored it.
    ``aux_outputs`` names the auxiliary human-QA companions actually written
    (``docs/mineru-primary-promotion.md`` §5) — ``{}`` when none were.
    """
    return {
        'extractor': Extractor.MINERU.value,
        'backend_id': MINERU,
        'extractor_version': f'{_DIST_NAME} {version}',
        'format': record.format.value,
        'source_sha256': record.sha256,
        'extracted_at': extracted_at.isoformat(),
        'n_pages': counts.n_pages,
        'n_text_blocks': counts.n_text_blocks,
        'n_section_headers': counts.n_section_headers,
        'n_tables': counts.n_tables,
        'n_figures': counts.n_figures,
        'char_count': counts.char_count,
        'pipeline': {
            'engine': engine,
            'effort': effort if engine == HYBRID_ENGINE else None,
            'parse_method': _PARSE_METHOD,
            'formula_enable': True,
            'table_enable': True,
            'lang': _LANG,
            'mineru_version': version,
        },
        'aux_outputs': aux_outputs,
    }


# Helpers ---------------------------------------------------------------------


def _mineru_version() -> str:
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:  # pragma: no cover — defensive
        return 'unknown'


def _rmtree(path: Path) -> None:
    """Best-effort recursive delete of a scratch dir (never raises)."""
    shutil.rmtree(path, ignore_errors=True)


__all__ = ['extract_mineru']
