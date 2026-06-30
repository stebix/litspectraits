"""Ingest, extract, and normalize error taxonomies.

Three parallel error trees rooted at :class:`IngestError`,
:class:`ExtractError`, and :class:`NormalizeError`. The v3 happy path
raises exactly one subclass when it cannot make forward progress;
nothing is materialized on failure (``docs/overview-v3.md`` §0, §5, §14,
plus ``docs/extract-pdf-plan.md`` §5 for the extract side and
``docs/normalized-documents-discussion.md`` §3.7 for the normalize side).
The class itself is the contract — operator hint and CLI exit code
dispatch off ``type(exc)``.

Each instance carries the DOI plus an open-ended ``context`` dict
(publisher, fetched URL, raw error string, SDK version, …). Both
attributes are populated by the base ``__init__``; subclasses add no
behavior.

The three trees are kept structurally identical but inheritance-disjoint
so ``except IngestError`` at the ingest boundary never catches an extract
or normalize failure, and vice versa. The seven-line ``__init__`` is
mirrored verbatim across the three bases rather than inherited from a
shared private parent — explicit duplication keeps the catch boundaries
grep-able and the three contracts independent.
"""


class IngestError(RuntimeError):
    """Base class for all v3 happy-path failures.

    Parameters
    ----------
    message : str, optional
        Human-readable summary. When omitted, a default
        ``ClassName doi=<doi> key=value …`` rendering is synthesized so
        ``str(exc)`` and bare ``logger.exception`` calls remain useful
        before the CLI's Rich panel is in place.
    doi : str
        DOI under which the failure occurred. Always present, even when
        the failure is configuration-shaped, so callers can correlate
        across structured logs.
    **context : object
        Free-form structured fields attached to the failure (e.g.
        ``publisher='wiley'``, ``http_status=403``, ``sdk_version='1.0.0'``).
    """

    doi: str
    context: dict[str, object]

    def __init__(self, message: str = '', *, doi: str, **context: object) -> None:
        if not message:
            extras = ' '.join(f'{key}={value!r}' for key, value in context.items())
            message = f'{type(self).__name__} doi={doi!r}'
            if extras:
                message = f'{message} {extras}'
        super().__init__(message)
        self.doi = doi
        self.context = dict(context)


# Validation / dispatch -------------------------------------------------------


class DOINotFoundError(IngestError):
    """CrossRef returned 404 for the DOI."""


class UnsupportedPublisherError(IngestError):
    """DOI prefix is not in the publisher dispatch table (§6)."""


# Configuration ---------------------------------------------------------------


class MissingCredentialError(IngestError):
    """Required publisher credential is unset and no IP fallback applies.

    Distinct from :class:`AuthRejectedError`: this is a "you forgot to
    configure something" failure detected before any network call. Doctor
    (``litspectraits doctor``) is meant to surface this.
    """


# Publisher-side --------------------------------------------------------------


class AuthRejectedError(IngestError):
    """Publisher returned 401 / 403 — token rejected or IP not allowlisted."""


class EntitlementDowngradeError(IngestError):
    """Elsevier returned a ``META_ABS`` (abstract-only) envelope.

    Raised by the Elsevier retriever when a ``view=FULL`` request comes
    back without an ``<originalText>`` / ``<xocs:doc>`` subtree. Abstracts
    are corpus poison for downstream value extraction; never silently
    accept them (§5).
    """


class NotOpenAccessError(IngestError):
    """Springer Open Access tier returned zero records for a real DOI.

    Raised by the Springer retriever's OA path when
    ``api.springernature.com/openaccess/jats`` answers 200 with a body
    containing no ``<article>`` element for a DOI we know exists (it
    passed the CrossRef metadata lookup upstream). The DOI is real but
    not open-access — the operator needs the premium TDM tier
    (``SPRINGER_TDM_API_KEY``) to retrieve it, or must sideload an
    institutionally-licensed PDF.

    Distinct from :class:`PublisherAPIError` so the CLI panel hint can
    point at the *recourse* (acquire TDM tier, or sideload) rather than
    leaving the operator wondering whether their key is broken.
    Distinct from :class:`AuthRejectedError` because the credential is
    valid — only its content scope is too narrow.
    """


class RateLimitExhaustedError(IngestError):
    """Publisher returned 429 after the retriever's bounded retry chain."""


class PublisherAPIError(IngestError):
    """Publisher 5xx, malformed response, or unexpected SDK exception."""


# Local validation ------------------------------------------------------------


class MalformedArtifactError(IngestError):
    """Magic-byte sniff rejected a downloaded or sideloaded artifact.

    Catches paywall-HTML-as-PDF, error-page-as-XML, and operator sideload
    of a non-PDF file.
    """


class IntegrityError(IngestError):
    """Hash collision against an existing artifact whose bytes differ.

    Raised on commit rather than silently overwriting.
    """


# Extract errors ==============================================================
#
# Step 10 (``docs/overview-v3.md`` §21, ``docs/extract-pdf-plan.md`` §5).
# Same DOI + context-dict shape as :class:`IngestError`. The trees are
# deliberately disjoint so CLI panels and pytest-style ``except`` blocks at
# each pipeline stage pattern-match cleanly. The ``__init__`` is mirrored
# verbatim rather than inherited from a shared private base — explicit
# duplication of seven lines keeps the contracts independent and the
# class taxonomies grep-able. Same rationale applies to
# :class:`NormalizeError` below.


class ExtractError(RuntimeError):
    """Base class for all v3 extraction failures.

    Parameters
    ----------
    message : str, optional
        Human-readable summary. When omitted, a default
        ``ClassName doi=<doi> key=value …`` rendering is synthesized so
        ``str(exc)`` and bare ``logger.exception`` calls remain useful
        before the CLI's Rich panel is in place.
    doi : str
        DOI under which the failure occurred. Always present so structured
        logs and per-DOI retry queues can correlate ingest and extract
        failures against the same paper.
    **context : object
        Free-form structured fields attached to the failure (e.g.
        ``extractor='docling'``, ``status='PARTIAL_SUCCESS'``,
        ``char_count=42``).
    """

    doi: str
    context: dict[str, object]

    def __init__(self, message: str = '', *, doi: str, **context: object) -> None:
        if not message:
            extras = ' '.join(f'{key}={value!r}' for key, value in context.items())
            message = f'{type(self).__name__} doi={doi!r}'
            if extras:
                message = f'{message} {extras}'
        super().__init__(message)
        self.doi = doi
        self.context = dict(context)


# Configuration / dispatch ----------------------------------------------------


class DoclingImportError(ExtractError):
    """The ``[extract]`` extra is not installed; ``import docling`` failed.

    Operator hint: ``uv sync --extra extract`` (``extract-pdf-plan.md`` §2.4).
    The extract extra is opt-in — an Elsevier+Springer-only corpus can run
    extraction without ever installing docling.
    """


class WrongFormatForExtractorError(ExtractError):
    """Dispatcher routed a record to an extractor that does not handle its format.

    Defensive guard: ``extract/_dispatch.py`` already routes on
    :attr:`AcquisitionRecord.format`, so this should be unreachable in
    production. Raising loudly here catches future refactors that bypass
    the dispatcher (e.g. direct ``extract_pdf`` calls in tests).
    """


class MineruImportError(ExtractError):
    """The ``[mineru]`` extra is not installed; ``import mineru`` failed.

    Mirror of :class:`DoclingImportError` for the MinerU PDF backend
    (``docs/mineru-backend-spec.md`` §6). The MinerU extra is opt-in and
    kept separate from ``[extract]`` because the two pull overlapping but
    independently-pinned model stacks; a docling-only corpus never needs
    it. Operator hint: ``uv sync --extra mineru``.
    """


class BackendNotApplicableError(ExtractError):
    """A ``--backend`` was requested that cannot serve this artifact.

    Two cases (``docs/mineru-backend-spec.md`` §1, §6): a non-default PDF
    backend was selected for a non-PDF (JATS / Elsevier) artifact, where
    backend choice is meaningless; or an unknown backend id was passed.
    Both are operator-configuration errors caught before any conversion
    work, never a silent fall-through to the default backend.
    """


class MissingArtifactError(ExtractError):
    """``record.artifact_path`` points at a file that no longer exists.

    Usually means the store was wiped between ingest and extract, or the
    operator passed a stale manifest. Never silently re-ingest — the
    correct response is to re-run ``litspectraits ingest <doi>``.
    """


class MissingModelWeightsError(ExtractError):
    """The pinned docling model cache is missing one or more required weights.

    Raised by the PDF extractor's preflight
    (:func:`litspectraits.extract.pdf._check_model_cache`) — and, as a
    backstop, when docling itself raises ``FileNotFoundError`` mid-conversion
    — when ``LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR`` points at a directory that
    does not hold every weight the v3 ``PdfPipelineOptions`` loads (Egret-Large
    layout, TableFormer, code/formula VLM).

    docling only auto-downloads weights when ``artifacts_path is None``; with a
    pinned cache a missing model is a fatal misconfiguration, not something the
    extractor will silently fetch (multi-GB pulls stay explicit). The operator
    fix is ``litspectraits doctor --download-models`` (or correcting the env
    var). Context carries ``model_cache_dir`` and the ``missing`` component
    labels so the Rich panel and structured logs name the gap precisely —
    unlike docling's own message, which (because the Egret spec's ``model_path``
    is empty) misleadingly points at the cache *root*.
    """


class MalformedDocumentError(ExtractError):
    """Artifact passed magic-byte sniff at ingest but failed structural parse.

    Raised by the JATS / Elsevier XML extractors when ``lxml`` rejects the
    bytes (``XMLSyntaxError``) or when the parsed root carries no
    recognizable JATS / Elsevier shape (e.g. an envelope without an
    ``<article>`` body or a ``<full-text-retrieval-response>`` without
    ``<originalText>``). Distinct from the structural sanity errors below
    because the *parse step itself* failed — the sniff at ingest already
    validated the root element, so anything that breaks here points at a
    corrupted artifact, not "we couldn't extract enough content."

    Distinct from :class:`MalformedArtifactError` (the ingest-side class
    raised by the magic-byte sniff) so CLI panel dispatch can pattern-
    match each pipeline stage independently.
    """


# Conversion ------------------------------------------------------------------


class DoclingConversionError(ExtractError):
    """Docling returned ``ConversionStatus.FAILURE`` (``extract-pdf-plan.md`` §3 stage 2)."""


class DoclingDegradedError(ExtractError):
    """Docling returned ``ConversionStatus.PARTIAL_SUCCESS``.

    Treated as a hard failure rather than a warning: a partial success
    today is almost always a TableFormer failure, and tables are exactly
    where the corpus's measurement values live. Silently committing a
    half-extracted document is the kind of failure we cannot detect
    downstream (``extract-pdf-plan.md`` §2.2, §3 stage 2).
    """


class MineruConversionError(ExtractError):
    """MinerU's ``do_parse`` failed or produced an empty parse.

    The MinerU analogue of :class:`DoclingConversionError`
    (``docs/mineru-backend-spec.md`` §3). MinerU has no ``ConversionStatus``
    enum, so the failure surfaces as "``do_parse`` raised" or "the
    ``middle.json`` it wrote carries no ``pdf_info``". The downstream
    structural-floor checks (:class:`EmptyDocumentError` /
    :class:`ParseDegradedError`) are shared with the docling path and fire
    after this one passes.
    """


# Post-extraction sanity ------------------------------------------------------


class EmptyDocumentError(ExtractError):
    """Zero text blocks recovered after a successful conversion.

    Almost always a scanned PDF served without OCR by the publisher; our
    default ``do_ocr=False`` leaves us empty-handed. The right next step
    is operator-driven (re-fetch a TDM version, or rerun with a future
    ``--ocr`` flag), never a silent in-pipeline fallback
    (``extract-pdf-plan.md`` §3 stage 3).
    """


class ParseDegradedError(ExtractError):
    """Recovered character count is below ``FLOOR_CHARS``.

    Layout recognition probably failed: the PDF parsed but most of the
    content was filtered as ``furniture`` (headers/footers) or never
    emerged as text blocks (``extract-pdf-plan.md`` §3 stage 3).
    """


# Commit ----------------------------------------------------------------------


class SerializationError(ExtractError):
    """``export_to_dict()`` / ``cattrs`` / ``json.dumps`` failed during the write stage.

    Extremely rare; usually indicates a docling version mismatch where the
    in-memory document graph carries a node the current ``export_to_dict``
    cannot serialize (``extract-pdf-plan.md`` §3 stage 4).
    """


class ExtractIntegrityError(ExtractError):
    """Existing ``document.json`` differs and ``--reextract`` was not set.

    Named distinctly from :class:`IntegrityError` (the ingest-side class
    raised by :mod:`litspectraits.store` on artifact-hash collision) so CLI
    panel dispatch can pattern-match each failure mode independently.
    Both classes encode the same principle — refuse to silently overwrite
    bytes — but at different layers of the pipeline.
    """


# Normalize errors ============================================================
#
# E0.5 (``docs/normalized-documents-discussion.md`` §3). The normalize layer
# sits between extract and the measurement layer; its failures need to be
# distinguishable from extract failures at the CLI boundary even though the
# shape (DOI + context dict) is identical. See the module docstring for why
# the ``__init__`` is duplicated rather than inherited.


class NormalizeError(RuntimeError):
    """Base class for all v3 normalisation failures.

    Parameters
    ----------
    message : str, optional
        Human-readable summary. When omitted, a default
        ``ClassName doi=<doi> key=value …`` rendering is synthesized so
        ``str(exc)`` and bare ``logger.exception`` calls remain useful
        before the CLI's Rich panel is in place.
    doi : str
        DOI under which the failure occurred. Always present so structured
        logs and per-DOI retry queues can correlate ingest, extract, and
        normalize failures against the same paper.
    **context : object
        Free-form structured fields attached to the failure (e.g.
        ``route='docling'``, ``source_artifact_sha='deadbeef…'``,
        ``normaliser_version='1.0'``).
    """

    doi: str
    context: dict[str, object]

    def __init__(self, message: str = '', *, doi: str, **context: object) -> None:
        if not message:
            extras = ' '.join(f'{key}={value!r}' for key, value in context.items())
            message = f'{type(self).__name__} doi={doi!r}'
            if extras:
                message = f'{message} {extras}'
        super().__init__(message)
        self.doi = doi
        self.context = dict(context)


class UnknownBackendError(NormalizeError):
    """The upstream extract meta's ``backend_id`` does not resolve to a PDF adapter.

    Raised by the normalize PDF dispatch (``docs/mineru-backend-spec.md``
    §1, §8) when ``documents/sha256/<aa>/<sha>/meta.json`` carries a
    ``backend_id`` no normalize adapter serves — or omits it entirely (a
    pre-``backend_id`` extraction). Normalize refuses to guess which parser
    produced ``document.json``; the fix is to (re-)extract with a wired
    ``--backend`` so the meta records it. The XML routes never hit this —
    they dispatch on :class:`~litspectraits.manifest.Format`, not backend.
    """


class NormalizeIntegrityError(NormalizeError):
    """Existing normalized ``document.json`` differs and ``--renormalize`` was not set.

    Raised on commit when a fresh normalisation of the same artifact would
    overwrite previously-committed bytes. Distinct from
    :class:`ExtractIntegrityError` and :class:`IntegrityError` so the CLI
    panel and the diff-harness loader can pattern-match the precise stage
    that refused to overwrite. All three encode the same principle —
    never silently rewrite committed bytes — at different layers.
    """
