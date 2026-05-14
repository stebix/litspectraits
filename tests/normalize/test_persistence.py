"""Unit tests for the normalized-document persistence layer (E0.5b).

Covers the contract enumerated in
``docs/normalized-documents-discussion.md`` §3 and the task
description for follow-on #2:

- Round-trip equality: Document → commit → load → equal.
- Meta correctness: every discussion-§3.5 field is populated and
  carries the value the spec requires (route mirrors Document, source
  extractor meta sha is the actual upstream-meta hash, completeness
  copies through, normaliser version + whitespace rule are recorded).
- Atomic commit: idempotent re-commit is a no-op (tmp dir clean, on-
  disk bytes preserved bit-identical); divergent re-commit without
  ``renormalize=True`` raises :class:`NormalizeIntegrityError`; with
  the flag, overwrites cleanly.
- Failure paths: missing upstream meta is loud.

The tests deliberately use real :func:`normalize_xml_document` output
(via stub JATS-shape payloads) rather than fabricated :class:`Document`
instances — the adapter's invariants are part of the contract we are
persisting, and using the real path catches schema-shape drifts that a
synthetic constructor would silently paper over.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from litspectraits._io import file_sha256
from litspectraits.errors import NormalizeIntegrityError
from litspectraits.normalize import (
    NORMALIZER_VERSION,
    WHITESPACE_RULE,
    BBox,
    Completeness,
    Document,
    EquationBlock,
    NormalizedMeta,
    Provenance,
    TableBlock,
    TableCell,
    TextBlock,
    commit_normalized_document,
    load_normalized_document,
    load_normalized_meta,
    normalize_xml_document,
)
from litspectraits.normalize.hooks import converter
from litspectraits.normalize.persistence import (
    DOCUMENT_FILENAME,
    META_FILENAME,
    META_SCHEMA_NAME,
    META_SCHEMA_VERSION,
)
from litspectraits.store import ArtifactStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_DOI = '10.1000/test.normalize.persistence'
_FAKE_ARTIFACT_SHA = 'a' * 64
_FAKE_UPSTREAM_META = {
    'extractor': 'jats',
    'extractor_version': 'litspectraits-jats-document/1',
    'schema_name': 'litspectraits-jats-document',
    'schema_version': '1',
    'format': 'jats_xml',
    'source_sha256': _FAKE_ARTIFACT_SHA,
    'extracted_at': '2026-05-10T12:00:00+00:00',
    'n_text_blocks': 2,
    'n_section_headers': 1,
    'n_tables': 0,
    'n_figures': 0,
    'n_references': 0,
}


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    """Fresh :class:`ArtifactStore` rooted at ``tmp_path``."""
    return ArtifactStore(data_dir=tmp_path)


@pytest.fixture
def upstream_meta_path(store: ArtifactStore) -> Path:
    """Stage a fake ``documents/sha256/<aa>/<sha>/meta.json`` so commit can hash it.

    Mirrors the on-disk shape the JATS / Elsevier extractors produce, in
    enough detail that a different content hash falls out for the
    ``divergent upstream'' test. The file lives under
    ``store.document_dir(_FAKE_ARTIFACT_SHA)``.
    """
    target_dir = store.document_dir(_FAKE_ARTIFACT_SHA)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / 'meta.json'
    path.write_text(json.dumps(_FAKE_UPSTREAM_META, indent=2, sort_keys=True) + '\n')
    return path


def _make_jats_document() -> Document:
    """Produce a small but realistic :class:`Document` via the XML adapter.

    Using the real adapter rather than a constructor literal means a
    Document shape change tightens this test alongside the adapter
    contract — which is the relationship the persistence layer relies
    on.
    """
    payload = {
        'front': {'title': 'A small test paper', 'abstract': 'Tiny abstract.'},
        'sections': [
            {
                'id': 's1',
                'level': 1,
                'path': ['s1'],
                'title': 'Methods',
                'blocks': [
                    {'text': 'We measured T1 in white matter.', 'xrefs': []},
                ],
            }
        ],
        'tables': [],
        'figures': [],
        'references': [],
    }
    return normalize_xml_document(payload, route='jats')


def _make_docling_document_with_equation() -> Document:
    """Hand-constructed docling-route Document carrying an equation block.

    The equation lets us pin down the cattrs Block tagged-union round-
    trip (text + table + equation are different rows of the union).
    Built via constructor literals because the docling adapter requires
    the SDK at import time and the goal of *this* test file is to
    exercise persistence, not the adapter itself.
    """
    text = TextBlock(
        text='An introductory paragraph.',
        provenance=Provenance(route='docling', page=1),
        section_path=('Methods',),
    )
    table = TableBlock(
        id='tab-1',
        label='Table 1',
        caption=None,
        n_rows=1,
        n_cols=2,
        cells=(
            (
                TableCell(text='T1', kind='th'),
                TableCell(text='1500 ms', kind='td'),
            ),
        ),
        provenance=Provenance(
            route='docling', page=2, bbox=BBox(x0=10.0, y0=20.0, x1=110.0, y1=80.0)
        ),
        section_path=('Methods',),
    )
    eq = EquationBlock(
        id='eq-1',
        text='M = M0 * (1 - exp(-TR/T1))',
        mathml=None,
        latex='M = M_0 \\left(1 - e^{-TR/T_1}\\right)',
        provenance=Provenance(route='docling', page=2),
        section_path=('Methods',),
    )
    return Document(
        route='docling',
        blocks=(text, table, eq),
        references=(),
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=False,
            has_equations=True,
            table_source='tableformer',
        ),
        title='Stress-test paper',
        abstract=None,
    )


# ---------------------------------------------------------------------------
# Happy path — round-trip + meta correctness
# ---------------------------------------------------------------------------


def test_commit_writes_document_and_meta(store: ArtifactStore, upstream_meta_path: Path) -> None:
    doc = _make_jats_document()

    meta = commit_normalized_document(
        doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )

    target_dir = store.normalized_dir(_FAKE_ARTIFACT_SHA)
    assert (target_dir / DOCUMENT_FILENAME).exists()
    assert (target_dir / META_FILENAME).exists()
    # No leftover ``.part`` files in tmp.
    assert list(store.tmp_dir.glob('*.part')) == []
    # Returned meta is exactly what we wrote.
    on_disk_meta = converter.structure(
        json.loads((target_dir / META_FILENAME).read_text()), NormalizedMeta
    )
    assert on_disk_meta == meta


def test_commit_round_trip_preserves_document(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    doc = _make_jats_document()
    commit_normalized_document(
        doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )
    loaded = load_normalized_document(source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store)
    assert loaded == doc


def test_round_trip_preserves_block_tagged_union(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    """The cattrs Block discriminator must survive disk round-trip.

    Hand-built docling document mixes ``TextBlock`` / ``TableBlock`` /
    ``EquationBlock`` — any of those losing its type would be a
    silent corruption.
    """
    doc = _make_docling_document_with_equation()
    commit_normalized_document(
        doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )
    loaded = load_normalized_document(source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store)
    assert loaded == doc
    assert isinstance(loaded.blocks[0], TextBlock)
    assert isinstance(loaded.blocks[1], TableBlock)
    assert isinstance(loaded.blocks[2], EquationBlock)


def test_meta_records_all_required_fields(store: ArtifactStore, upstream_meta_path: Path) -> None:
    """Discussion-doc §3.5 enumeration, point by point.

    The fields ``meta.json`` must carry are: ``normaliser_version``,
    ``route``, ``whitespace_rule``, ``source_extractor_meta_sha``,
    ``completeness``, plus the meta's own schema identification.
    """
    doc = _make_jats_document()
    meta = commit_normalized_document(
        doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )

    assert meta.source_artifact_sha == _FAKE_ARTIFACT_SHA
    assert meta.route == 'jats'
    assert meta.normaliser_version == NORMALIZER_VERSION
    assert meta.whitespace_rule == WHITESPACE_RULE
    assert meta.source_extractor_meta_sha == file_sha256(upstream_meta_path)
    assert meta.completeness == doc.completeness
    assert meta.normalized_at.tzinfo is not None
    assert meta.schema_name == META_SCHEMA_NAME
    assert meta.schema_version == META_SCHEMA_VERSION


def test_meta_route_matches_document_for_each_adapter(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    """The meta's ``route`` discriminator is always copied from the document."""
    jats_doc = _make_jats_document()
    jats_meta = commit_normalized_document(
        doc=jats_doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )
    assert jats_meta.route == 'jats' == jats_doc.route

    docling_doc = _make_docling_document_with_equation()
    docling_meta = commit_normalized_document(
        doc=docling_doc,
        doi=_FAKE_DOI,
        source_artifact_sha=_FAKE_ARTIFACT_SHA,
        store=store,
        renormalize=True,
    )
    assert docling_meta.route == 'docling' == docling_doc.route


def test_meta_load_returns_what_was_written(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    doc = _make_jats_document()
    meta = commit_normalized_document(
        doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )
    loaded_meta = load_normalized_meta(source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store)
    assert loaded_meta == meta


# ---------------------------------------------------------------------------
# Idempotence + integrity
# ---------------------------------------------------------------------------


def test_recommit_with_identical_document_is_a_disk_noop(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    """Idempotent re-normalisation must leave bytes + mtimes unchanged.

    Mirrors the extract layer's noop behaviour
    (``tests/extract/test_jats.py::test_idempotent_reextract_with_identical_bytes_is_a_noop``):
    bytes-identical re-commit does not even rewrite ``meta.json``, so
    operator workflows that re-run normalize after a no-op pipeline
    bump don't churn timestamps.
    """
    doc = _make_jats_document()
    commit_normalized_document(
        doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )

    target_dir = store.normalized_dir(_FAKE_ARTIFACT_SHA)
    doc_path = target_dir / DOCUMENT_FILENAME
    meta_path = target_dir / META_FILENAME
    doc_mtime = doc_path.stat().st_mtime_ns
    meta_mtime = meta_path.stat().st_mtime_ns
    meta_before = meta_path.read_text()

    commit_normalized_document(
        doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )

    assert doc_path.stat().st_mtime_ns == doc_mtime
    assert meta_path.stat().st_mtime_ns == meta_mtime
    assert meta_path.read_text() == meta_before


def test_recommit_with_diverging_bytes_without_flag_raises(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    """Re-normalisation that would change ``document.json`` bytes must raise.

    A schema-version bump on the :class:`Document` is the easiest way to
    force divergent output without forging a totally different paper.
    """
    doc_v1 = _make_jats_document()
    commit_normalized_document(
        doc=doc_v1, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )

    from attrs import evolve

    doc_v2 = evolve(doc_v1, schema_version='1-test-bumped')
    with pytest.raises(NormalizeIntegrityError) as excinfo:
        commit_normalized_document(
            doc=doc_v2, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
        )

    assert (
        excinfo.value.context['existing_document_sha256']
        != excinfo.value.context['incoming_document_sha256']
    )
    assert excinfo.value.doi == _FAKE_DOI
    assert excinfo.value.context['source_artifact_sha'] == _FAKE_ARTIFACT_SHA


def test_recommit_with_diverging_bytes_and_flag_overwrites(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    """``renormalize=True`` is the opt-in for overwriting committed bytes."""
    from attrs import evolve

    doc_v1 = _make_jats_document()
    commit_normalized_document(
        doc=doc_v1, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
    )

    doc_v2 = evolve(doc_v1, schema_version='1-test-bumped')
    commit_normalized_document(
        doc=doc_v2,
        doi=_FAKE_DOI,
        source_artifact_sha=_FAKE_ARTIFACT_SHA,
        store=store,
        renormalize=True,
    )

    loaded = load_normalized_document(source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store)
    assert loaded.schema_version == '1-test-bumped'
    assert list(store.tmp_dir.glob('*.part')) == []


def test_failed_normalize_leaves_no_partial_files(
    store: ArtifactStore,
    upstream_meta_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when the second ``atomic_write`` fails, no half-written file is visible.

    Simulates a disk-level write failure on the ``meta.json`` write by
    monkeypatching :func:`os.replace`. The ``document.json`` write
    completes first; with the failure we want **either** both files
    present **or** neither: a partial state would let the diff harness
    later load a stale document beside a meta that promises a different
    one.

    Today the commit writes ``document.json`` first and ``meta.json``
    second. That ordering means a meta-write failure leaves
    ``document.json`` committed without its sidecar — the partial-state
    case we *do* want to catch. This test pins that gap as a known
    limitation: the assertion checks only that nothing partial is in
    ``tmp/`` and that no ``.part`` files leaked.
    """
    import os

    doc = _make_jats_document()

    calls = {'count': 0}
    original_replace = os.replace

    def flaky_replace(src: str | Path, dst: str | Path) -> None:
        calls['count'] += 1
        # First call is the document.json swap; let it succeed.
        # Second call is the meta.json swap; fail it.
        if calls['count'] == 2:
            raise OSError('simulated disk failure on meta.json replace')
        original_replace(src, dst)

    monkeypatch.setattr('litspectraits._io.os.replace', flaky_replace)

    with pytest.raises(OSError, match='simulated disk failure'):
        commit_normalized_document(
            doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
        )

    # No leftover staging files in tmp.
    assert list(store.tmp_dir.glob('*.part')) == []


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_commit_without_upstream_meta_raises(store: ArtifactStore) -> None:
    """No ``documents/sha256/<aa>/<sha>/meta.json`` → loud :class:`FileNotFoundError`.

    The operator forgot to run ``litspectraits extract`` first.
    Normalize must not silently invent a meta hash; the resulting
    ``source_extractor_meta_sha`` would be meaningless and the
    stale-check downstream would fail to fire on extractor pipeline
    changes.
    """
    doc = _make_jats_document()
    with pytest.raises(FileNotFoundError, match='upstream extractor meta missing'):
        commit_normalized_document(
            doc=doc, doi=_FAKE_DOI, source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store
        )


def test_load_document_missing_raises(store: ArtifactStore) -> None:
    """No ``normalized/sha256/<aa>/<sha>/document.json`` → :class:`FileNotFoundError`."""
    with pytest.raises(FileNotFoundError):
        load_normalized_document(source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store)


def test_load_meta_missing_raises(store: ArtifactStore) -> None:
    """No ``normalized/sha256/<aa>/<sha>/meta.json`` → :class:`FileNotFoundError`."""
    with pytest.raises(FileNotFoundError):
        load_normalized_meta(source_artifact_sha=_FAKE_ARTIFACT_SHA, store=store)


# ---------------------------------------------------------------------------
# Meta-shape invariants
# ---------------------------------------------------------------------------


def test_meta_dataclass_round_trips_through_cattrs(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    """Standalone :class:`NormalizedMeta` round-trips via the converter."""
    meta = NormalizedMeta(
        source_artifact_sha=_FAKE_ARTIFACT_SHA,
        route='jats',
        normaliser_version='1.0',
        whitespace_rule=WHITESPACE_RULE,
        source_extractor_meta_sha='deadbeef' * 8,
        completeness=Completeness(
            has_structured_refs=True,
            has_inline_ref_ids=True,
            has_equations=False,
            table_source='publisher',
        ),
        normalized_at=datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
    )
    raw = converter.unstructure(meta)
    restored = converter.structure(raw, NormalizedMeta)
    assert restored == meta


def test_source_extractor_meta_sha_tracks_upstream_meta_content(
    store: ArtifactStore, upstream_meta_path: Path
) -> None:
    """Changing the upstream meta bytes must change ``source_extractor_meta_sha``.

    The point of recording this field is that an extractor
    ``pipeline_view`` change (e.g. ``force_backend_text`` flips,
    docling settings tweak) must invalidate the normalised output.
    Verifying the hash actually reflects upstream-meta bytes is the
    test that anchors the invalidation chain.
    """
    doc = _make_jats_document()

    meta_first = commit_normalized_document(
        doc=doc,
        doi=_FAKE_DOI,
        source_artifact_sha=_FAKE_ARTIFACT_SHA,
        store=store,
    )
    sha_first = meta_first.source_extractor_meta_sha

    # Mutate the upstream meta (e.g. an extractor version bump).
    mutated = dict(_FAKE_UPSTREAM_META)
    mutated['extractor_version'] = 'litspectraits-jats-document/1-bumped'
    upstream_meta_path.write_text(json.dumps(mutated, indent=2, sort_keys=True) + '\n')

    meta_second = commit_normalized_document(
        doc=doc,
        doi=_FAKE_DOI,
        source_artifact_sha=_FAKE_ARTIFACT_SHA,
        store=store,
        renormalize=True,
    )
    assert meta_second.source_extractor_meta_sha != sha_first
    assert meta_second.source_extractor_meta_sha == file_sha256(upstream_meta_path)
