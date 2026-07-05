"""Format → backend → extractor dispatch (``docs/overview-v3.md`` §11).

A ``match`` on :attr:`AcquisitionRecord.format`. All three legs are wired
(PDF step 10b, JATS step 10c, Elsevier step 10d). The PDF leg is further
*backend-dispatched* (``docs/mineru-backend-spec.md`` §1): one
:attr:`Format.PDF` artifact can be parsed by docling or MinerU, selected
by ``backend`` (CLI ``--backend`` / ``LITSPECTRAITS_PDF_BACKEND``). The XML
legs carry no pluggable backend — a non-default ``backend`` on an XML
artifact is a loud :class:`~litspectraits.errors.BackendNotApplicableError`,
never a silent ignore.

When ``backend == 'mineru'``, two further knobs select which MinerU local
backend runs and at what effort (``--mineru-engine`` / ``--mineru-effort``,
``docs/mineru-backend-spec.md`` §1, §8, §11 Q-D/Q-E). Same non-default-
means-explicit rule as ``backend`` itself: their defaults are tolerated on
any backend, but an explicit non-default value combined with a non-mineru
backend is rejected before any conversion work — see
:func:`_reject_mineru_knobs_if_inapplicable`.

The :class:`~litspectraits.errors.ExtractError` taxonomy is the
load-bearing piece of step 10a — every leaf extractor plugs into a
dispatcher that already speaks the right error language.
"""

from pathlib import Path

from litspectraits.errors import BackendNotApplicableError
from litspectraits.extract.backend_ids import (
    DEFAULT_PDF_BACKEND,
    DOCLING_STANDARD,
    MINERU,
    PDF_BACKEND_IDS,
)
from litspectraits.extract.elsevier import extract_elsevier
from litspectraits.extract.jats import extract_jats
from litspectraits.extract.mineru import (
    DEFAULT_MINERU_EFFORT,
    DEFAULT_MINERU_ENGINE,
    extract_mineru,
)
from litspectraits.extract.pdf import extract_pdf
from litspectraits.manifest import AcquisitionRecord, ExtractRecord, Format
from litspectraits.store import ArtifactStore


async def extract(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    backend: str = DEFAULT_PDF_BACKEND,
    mineru_engine: str = DEFAULT_MINERU_ENGINE,
    mineru_effort: str = DEFAULT_MINERU_EFFORT,
    reextract: bool = False,
    model_cache_dir: Path | None = None,
) -> ExtractRecord:
    """Dispatch ``record`` to the format- and backend-specific extractor.

    Parameters
    ----------
    record : AcquisitionRecord
        Manifest of the committed artifact. ``record.format`` selects the
        leaf extractor; ``record.artifact_path`` (relative to
        ``store.data_dir``) is where the leaf reads bytes from.
    store : ArtifactStore
        Used by leaf extractors to read the artifact and stage
        ``documents/sha256/<aa>/<sha>/`` outputs atomically (same tmp → rename pattern
        as ingest).
    backend : str, default :data:`DEFAULT_PDF_BACKEND`
        PDF parsing backend id (``'docling-standard'`` / ``'mineru'``).
        Consulted **only** on the :attr:`Format.PDF` leg. A non-default
        value on an XML artifact, or an unknown id on a PDF, raises
        :class:`~litspectraits.errors.BackendNotApplicableError` before any
        conversion work. The default means "no explicit choice" and so is
        tolerated (ignored) on the XML legs.
    mineru_engine : str, default :data:`~litspectraits.extract.mineru.DEFAULT_MINERU_ENGINE`
        Which MinerU local backend parses the PDF — one of
        :data:`~litspectraits.extract.mineru.MINERU_ENGINES`. Consulted
        **only** when ``backend == 'mineru'``; an explicit non-default value
        combined with any other ``backend`` raises
        :class:`~litspectraits.errors.BackendNotApplicableError`.
    mineru_effort : str, default :data:`~litspectraits.extract.mineru.DEFAULT_MINERU_EFFORT`
        MinerU hybrid-engine effort — one of
        :data:`~litspectraits.extract.mineru.MINERU_EFFORTS`. Same
        applicability rule as ``mineru_engine``.
    reextract : bool, default False
        Whether to overwrite an existing extraction whose serialized
        ``document.json`` differs from the one we are about to write.
        Surfaces as :class:`~litspectraits.errors.ExtractIntegrityError`
        when ``False`` and bytes differ.
    model_cache_dir : pathlib.Path | None, default None
        Model-weights directory; only consulted on the
        :attr:`Format.PDF` leg (the XML extractors carry no model). See
        :func:`litspectraits.extract.pdf.extract_pdf`.

    Returns
    -------
    ExtractRecord
        Returned by the leaf extractor.

    Raises
    ------
    litspectraits.errors.ExtractError
        Any extractor-side failure. The class itself is the contract.
    litspectraits.errors.BackendNotApplicableError
        ``backend`` cannot serve ``record`` (non-default on XML, or an
        unknown PDF backend id), or ``mineru_engine`` / ``mineru_effort``
        was set explicitly while ``backend != 'mineru'``.
    """
    _reject_mineru_knobs_if_inapplicable(
        record=record, backend=backend, engine=mineru_engine, effort=mineru_effort
    )
    match record.format:
        case Format.PDF:
            return await _dispatch_pdf_backend(
                record,
                store,
                backend=backend,
                mineru_engine=mineru_engine,
                mineru_effort=mineru_effort,
                reextract=reextract,
                model_cache_dir=model_cache_dir,
            )
        case Format.JATS_XML:
            _reject_backend_on_non_pdf(record=record, backend=backend)
            return await extract_jats(record, store, reextract=reextract)
        case Format.ELSEVIER_XML:
            _reject_backend_on_non_pdf(record=record, backend=backend)
            return await extract_elsevier(record, store, reextract=reextract)


async def _dispatch_pdf_backend(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    backend: str,
    mineru_engine: str,
    mineru_effort: str,
    reextract: bool,
    model_cache_dir: Path | None,
) -> ExtractRecord:
    """Route a PDF artifact to the selected backend extractor.

    An ``if`` chain rather than ``match`` on purpose: ``case CONSTANT:``
    binds a capture pattern instead of matching the constant's value, so a
    typo'd backend id would silently match the first branch. Comparing to
    the :mod:`litspectraits.extract.backend_ids` constants keeps the
    vocabulary single-sourced and fails loud on anything unknown.
    """
    if backend == DOCLING_STANDARD:
        return await extract_pdf(
            record, store, reextract=reextract, model_cache_dir=model_cache_dir
        )
    if backend == MINERU:
        return await extract_mineru(
            record,
            store,
            reextract=reextract,
            model_cache_dir=model_cache_dir,
            engine=mineru_engine,
            effort=mineru_effort,
        )
    raise BackendNotApplicableError(
        doi=record.doi,
        backend=backend,
        format=record.format.value,
        valid_backends=list(PDF_BACKEND_IDS),
        hint=f'unknown PDF backend {backend!r}; valid ids: {", ".join(PDF_BACKEND_IDS)}',
    )


def _reject_backend_on_non_pdf(*, record: AcquisitionRecord, backend: str) -> None:
    """Fail loud when a backend was explicitly chosen for a non-PDF artifact.

    The default backend means "unset" — an XML artifact extracted without a
    ``--backend`` flag must not trip this. Only an *explicit* non-default
    choice is the operator-configuration error.
    """
    if backend != DEFAULT_PDF_BACKEND:
        raise BackendNotApplicableError(
            doi=record.doi,
            backend=backend,
            format=record.format.value,
            hint=(
                f'--backend selects a PDF parser, but this artifact is '
                f'{record.format.value}; drop --backend for XML formats'
            ),
        )


def _reject_mineru_knobs_if_inapplicable(
    *, record: AcquisitionRecord, backend: str, engine: str, effort: str
) -> None:
    """Fail loud when ``--mineru-engine`` / ``--mineru-effort`` cannot take effect.

    The MinerU knobs only bite on a :attr:`Format.PDF` artifact parsed by the
    ``mineru`` backend. Each knob's default means "unset" and is tolerated on
    every leg; an *explicit* non-default value is an operator-configuration
    error unless it lands on exactly that combination, caught before any
    conversion work.

    The ``Format.PDF`` clause matters now that ``mineru`` is the *default*
    backend (``docs/mineru-primary-promotion.md`` §1): without it, an
    env-wide ``--mineru-effort high`` would be silently ignored on a Springer
    XML artifact instead of loudly rejected. Keying on "mineru backend **and**
    PDF format" preserves the pre-promotion fail-loud behaviour through the
    default flip.
    """
    if backend == MINERU and record.format is Format.PDF:
        return
    offending = [
        flag
        for flag, value, default in (
            ('--mineru-engine', engine, DEFAULT_MINERU_ENGINE),
            ('--mineru-effort', effort, DEFAULT_MINERU_EFFORT),
        )
        if value != default
    ]
    if offending:
        raise BackendNotApplicableError(
            doi=record.doi,
            backend=backend,
            mineru_engine=engine,
            mineru_effort=effort,
            hint=(
                f'{", ".join(offending)} only applies with --backend mineru; '
                f'drop it, or pass --backend mineru'
            ),
        )
