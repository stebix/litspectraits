"""On-disk persistence for the normalised :class:`Document` (E0.5b).

See ``docs/normalized-documents-discussion.md`` §3 for the design and
``docs/dual-route-comparison-overview.md`` §9 for how this layer feeds
the diff harness loader.

Layout under :attr:`ArtifactStore.data_dir`::

    normalized/sha256/<aa>/<sha>/document.json    # cattrs-unstructured Document
    normalized/sha256/<aa>/<sha>/meta.json        # the NormalizedMeta sidecar

Mirrors :meth:`ArtifactStore.document_dir`'s sharded layout one layer
downstream (one normalisation per artifact, append-only). The
``<aa>/<sha>/`` segments come from
:meth:`ArtifactStore.normalized_dir` — never construct paths by
hand here.

The commit discipline is the same as ingest and extract:

- atomic ``os.replace`` from ``<data_dir>/tmp/`` to the canonical path
  via :func:`litspectraits._io.atomic_write`;
- never silently overwrite — bytes that differ on a re-normalisation
  surface as :class:`~litspectraits.errors.NormalizeIntegrityError`
  unless ``renormalize=True``.

The :class:`NormalizedMeta` sidecar records everything that can change
the on-disk ``document.json`` bytes, so a future "is this normalisation
stale?" check is one ``meta.json`` read away. The fields follow the
discussion doc §3.5 enumeration verbatim:

- ``normaliser_version`` — bump when adapter behaviour changes;
- ``route`` — the discriminator on :class:`Document`, mirrored here so
  the meta is self-describing without parsing ``document.json``;
- ``whitespace_rule`` — id of the canonicalisation rule baked into the
  adapters (today: passthrough; verbatim source text);
- ``source_extractor_meta_sha`` — sha256 of the upstream
  ``documents/sha256/<aa>/<sha>/meta.json``, so an extractor ``pipeline_view``
  change correctly invalidates this normalisation;
- ``completeness`` — copy of :attr:`Document.completeness` so the
  measurement layer's confidence gate can branch without re-parsing
  the document body.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Final

import structlog
from attrs import field, frozen

from litspectraits._io import atomic_write, file_sha256
from litspectraits.errors import NormalizeIntegrityError
from litspectraits.normalize.hooks import converter
from litspectraits.normalize.models import Completeness, Document, Route
from litspectraits.store import ArtifactStore

# ---------------------------------------------------------------------------
# Constants — bump deliberately, never silently
# ---------------------------------------------------------------------------

NORMALIZER_VERSION: Final = '1.0'
"""Semver-shaped version of the normalize subsystem.

Bump on any change that can alter ``document.json`` bytes for an
unchanged input: adapter behaviour change, whitespace rule change,
Completeness derivation change. The :class:`NormalizedMeta` records
this so a stale-check is a cheap dict comparison.
"""

WHITESPACE_RULE: Final = 'passthrough-v1'
"""Id of the whitespace canonicalisation rule baked into the adapters.

Today the rule is literal passthrough — block text is taken verbatim
from the extractor's :attr:`document.json` payload (XML route) or the
docling :class:`TextItem.text` field. The named id is recorded in the
meta so a future canonicalisation pass (collapse-runs, strip-trailing,
etc.) can be detected as a rule bump rather than silently changing
output bytes.
"""

META_SCHEMA_NAME: Final = 'litspectraits-normalized-meta'
# '2' adds `backend_id` (docs/mineru-backend-spec.md §2.5): the meta now
# records which PDF backend produced the upstream document.json, carried
# through from the extract meta. Bump on any NormalizedMeta field-set change.
META_SCHEMA_VERSION: Final = '2'

DOCUMENT_FILENAME: Final = 'document.json'
META_FILENAME: Final = 'meta.json'

_logger: Final = structlog.get_logger('litspectraits.normalize.persistence')


# ---------------------------------------------------------------------------
# Sidecar dataclass
# ---------------------------------------------------------------------------


@frozen
class NormalizedMeta:
    """``meta.json`` sidecar for a committed normalised :class:`Document`.

    Round-trips through :data:`litspectraits.normalize.hooks.converter`.
    The discussion doc §3.5 mandates recording "anything that can change
    output bytes" — the fields below are that enumeration plus the
    structural identifiers needed to locate the upstream extractor
    output.

    Attributes
    ----------
    source_artifact_sha : str
        SHA-256 of the original artifact this normalisation derives
        from. Matches :attr:`AcquisitionRecord.sha256` and the directory
        key under ``normalized/<sha256>/``.
    route : Route
        :class:`Document` route discriminator (``'jats'``, ``'elsevier'``,
        ``'docling'``, or ``'mineru'``). Mirrors :attr:`Document.route` so a
        reader can dispatch without parsing ``document.json``.
    backend_id : str | None
        The PDF backend that produced the upstream ``document.json``
        (``'docling-standard'`` / ``'mineru'``), carried through from the
        extract ``meta.json``'s ``backend_id`` field
        (``docs/mineru-backend-spec.md`` §1, §2.5). ``None`` on XML routes,
        which have no pluggable parser — ``route`` already fully identifies
        them. This records which parser is behind a PDF-route Document
        without re-reading the extract meta.
    normaliser_version : str
        :data:`NORMALIZER_VERSION` at the moment of commit. Lets a
        future "rebuild stale normalisations" sweep filter by version
        cheaply.
    whitespace_rule : str
        :data:`WHITESPACE_RULE` at the moment of commit.
    source_extractor_meta_sha : str
        SHA-256 of ``documents/<source_artifact_sha>/meta.json`` at the
        moment of commit. An extractor ``pipeline_view`` change (e.g.
        ``force_backend_text`` flips) bumps the upstream meta sha and
        therefore invalidates this normalisation.
    completeness : Completeness
        Copy of :attr:`Document.completeness`. Persisted in the meta
        so the measurement-layer confidence gate can branch on route
        quality without re-parsing the document body.
    normalized_at : datetime
        Tz-aware UTC timestamp of the commit. Producers must pass
        ``datetime.now(tz=UTC)`` — the converter does not coerce naive
        datetimes, same invariant as
        :attr:`AcquisitionRecord.fetched_at`.
    schema_name, schema_version : str
        Identify the on-disk shape of *this meta*, not the document
        it accompanies. Bump :data:`META_SCHEMA_VERSION` when this
        dataclass's field set changes.
    """

    source_artifact_sha: str
    route: Route
    backend_id: str | None
    normaliser_version: str
    whitespace_rule: str
    source_extractor_meta_sha: str
    completeness: Completeness
    normalized_at: datetime
    schema_name: str = field(default=META_SCHEMA_NAME)
    schema_version: str = field(default=META_SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def commit_normalized_document(
    *,
    doc: Document,
    doi: str,
    source_artifact_sha: str,
    store: ArtifactStore,
    renormalize: bool = False,
) -> NormalizedMeta:
    """Atomically install ``document.json`` + ``meta.json`` for a normalised Document.

    Reads the upstream extractor meta at
    ``store.document_dir(source_artifact_sha) / 'meta.json'`` to compute
    :attr:`NormalizedMeta.source_extractor_meta_sha`; this fails loudly
    if the upstream meta is absent (the operator forgot to run
    ``litspectraits extract <doi>``).

    Decision tree on existing files (mirrors
    :func:`litspectraits.extract._lxml_helpers.commit_document`):

    - absent → write both files atomically, log success.
    - present and ``document.json`` bytes match → no-op (don't rewrite
      either file). Returns the freshly-built
      :class:`NormalizedMeta`; the on-disk ``meta.json`` is preserved
      bit-identical even though its timestamp will differ from the
      returned record. Same pattern as extract.
    - present and bytes differ + ``renormalize=False`` →
      :class:`NormalizeIntegrityError`, no writes.
    - present and bytes differ + ``renormalize=True`` → overwrite both
      atomically.

    Parameters
    ----------
    doc : Document
        Normalised document produced by either
        :func:`litspectraits.normalize.normalize_xml_document` or
        :func:`litspectraits.normalize.normalize_docling_document`.
    doi : str
        DOI for error correlation. Carried into the
        :class:`NormalizeIntegrityError` context dict and bound on the
        structured-log line so an operator can grep the failure.
    source_artifact_sha : str
        SHA-256 of the artifact this normalisation derives from. Keys
        the ``normalized/sha256/<aa>/<sha>/`` output directory and identifies which
        ``documents/sha256/<aa>/<sha>/`` to read the upstream meta from.
    store : ArtifactStore
        Provides :meth:`ArtifactStore.normalized_dir`,
        :meth:`ArtifactStore.document_dir`, and :attr:`tmp_dir`.
    renormalize : bool, default False
        When ``True``, overwrite an existing differing ``document.json``
        instead of raising. The default is the safe direction —
        explicit opt-in to overwrite committed bytes.

    Returns
    -------
    NormalizedMeta
        The meta we just wrote (or would have written on the no-op
        path). ``normalized_at`` is always freshly computed.

    Raises
    ------
    FileNotFoundError
        Upstream ``documents/sha256/<aa>/<sha>/meta.json`` is missing — the
        operator needs to run ``litspectraits extract <doi>`` first.
    NormalizeIntegrityError
        Existing ``document.json`` bytes differ from the freshly
        normalised bytes and ``renormalize=False``.
    """
    source_meta_path = store.document_dir(source_artifact_sha) / 'meta.json'
    if not source_meta_path.exists():
        raise FileNotFoundError(
            f'upstream extractor meta missing at {source_meta_path}; '
            f'run `litspectraits extract` for sha256={source_artifact_sha} first'
        )
    source_extractor_meta_sha = file_sha256(source_meta_path)
    # The PDF backend that produced the upstream document.json, recorded for
    # provenance (docs/mineru-backend-spec.md §2.5). XML extractor metas carry
    # no `backend_id` -> None; the route already identifies them. The fail-loud
    # check that a PDF route *must* have a backend_id lives at the normalize
    # dispatch seam (where backend_id is load-bearing), not here.
    upstream_meta = json.loads(source_meta_path.read_text(encoding='utf-8'))
    backend_id = upstream_meta.get('backend_id')

    normalized_at = datetime.now(tz=UTC)
    meta = NormalizedMeta(
        source_artifact_sha=source_artifact_sha,
        route=doc.route,
        backend_id=backend_id,
        normaliser_version=NORMALIZER_VERSION,
        whitespace_rule=WHITESPACE_RULE,
        source_extractor_meta_sha=source_extractor_meta_sha,
        completeness=doc.completeness,
        normalized_at=normalized_at,
    )

    document_body = _serialize(converter.unstructure(doc))
    meta_body = _serialize(converter.unstructure(meta))

    target_dir = store.normalized_dir(source_artifact_sha)
    target_doc = target_dir / DOCUMENT_FILENAME
    target_meta = target_dir / META_FILENAME

    new_sha = hashlib.sha256(document_body).hexdigest()
    if target_doc.exists():
        existing_sha = file_sha256(target_doc)
        if existing_sha == new_sha:
            _logger.info(
                'normalize no-op; document.json bytes unchanged',
                doi=doi,
                source_artifact_sha=source_artifact_sha,
                route=doc.route,
                document_sha256=new_sha,
            )
            return meta
        if not renormalize:
            raise NormalizeIntegrityError(
                doi=doi,
                source_artifact_sha=source_artifact_sha,
                route=doc.route,
                existing_document_sha256=existing_sha,
                incoming_document_sha256=new_sha,
                hint='re-normalised document differs; pass --renormalize to overwrite',
            )

    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(tmp_dir=store.tmp_dir, target=target_doc, body=document_body)
    atomic_write(tmp_dir=store.tmp_dir, target=target_meta, body=meta_body)

    _logger.info(
        'normalize committed',
        doi=doi,
        source_artifact_sha=source_artifact_sha,
        route=doc.route,
        normaliser_version=NORMALIZER_VERSION,
        whitespace_rule=WHITESPACE_RULE,
        source_extractor_meta_sha=source_extractor_meta_sha,
    )
    return meta


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def load_normalized_document(
    *,
    source_artifact_sha: str,
    store: ArtifactStore,
) -> Document:
    """Load and structure ``normalized/sha256/<aa>/<sha>/document.json``.

    The diff harness loader (follow-on #1) is the primary caller — see
    ``docs/dual-route-comparison-overview.md`` §9.

    Raises
    ------
    FileNotFoundError
        ``normalized/sha256/<aa>/<sha>/document.json`` does not exist (no
        ``normalize`` run has committed for this artifact).
    """
    path = store.normalized_dir(source_artifact_sha) / DOCUMENT_FILENAME
    raw = json.loads(path.read_text(encoding='utf-8'))
    return converter.structure(raw, Document)


def load_normalized_meta(
    *,
    source_artifact_sha: str,
    store: ArtifactStore,
) -> NormalizedMeta:
    """Load and structure ``normalized/sha256/<aa>/<sha>/meta.json``.

    Lets the diff harness filter / stratify by route or completeness
    without paying the cost of structuring the full
    :class:`Document`.

    Raises
    ------
    FileNotFoundError
        ``normalized/sha256/<aa>/<sha>/meta.json`` does not exist.
    """
    path = store.normalized_dir(source_artifact_sha) / META_FILENAME
    raw = json.loads(path.read_text(encoding='utf-8'))
    return converter.structure(raw, NormalizedMeta)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(unstructured: object) -> bytes:
    """Render an unstructured cattrs value as the canonical JSON bytes.

    Indented + sorted to mirror the extract layer
    (``extract/_lxml_helpers.py:serialize_document``) — the discussion
    doc §3.4 argues for compact JSON, but bytes-on-disk stability and
    ``jq``-ability matter more for the operator workflow and the size
    delta is negligible for the corpus sizes we target.
    """
    text = json.dumps(unstructured, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + '\n').encode('utf-8')


__all__ = [
    'DOCUMENT_FILENAME',
    'META_FILENAME',
    'META_SCHEMA_NAME',
    'META_SCHEMA_VERSION',
    'NORMALIZER_VERSION',
    'WHITESPACE_RULE',
    'NormalizedMeta',
    'commit_normalized_document',
    'load_normalized_document',
    'load_normalized_meta',
]
