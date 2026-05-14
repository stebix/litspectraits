"""Coverage for the dual-format diff harness (E0.5b follow-on #1).

The harness loader walks the on-disk store and emits per-DOI
outcomes; these tests build synthetic stores under ``tmp_path`` and
assert the harness correctly classifies each one. The pure
:func:`compare_documents` primitive already has its own coverage in
``test_diff.py`` — here we only test the data-store integration and
the temporal compare.

What's tested
-------------
- Auto-discovery yields a :class:`DualFormatComparison` for a complete
  dual-format DOI (both manifests + extractions + normalisations
  present).
- DOI with only one format yields :class:`DualFormatSkip` /
  ``NOT_DUAL_FORMAT``.
- DOI with both formats but no extraction on one side yields
  :class:`DualFormatSkip` / ``MISSING_EXTRACTION``.
- DOI with both formats + extractions but no normalisation on one
  side yields :class:`DualFormatSkip` / ``MISSING_NORMALIZATION``.
- Explicit ``dois=`` filter only emits the requested subset, and
  rejects DOIs absent from the index with a typed
  :class:`ValueError`.
- ``compare_reports`` surfaces a metric regression / improvement /
  state change.

What's not tested
-----------------
The harness's structural-comparison numbers — those are tested in
``test_diff.py``. The persistence layer's atomic-commit invariants —
those are tested in ``test_persistence.py``. This file focuses on the
glue (index scan, per-DOI dispatch, skip taxonomy, temporal compare).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    Publisher,
)
from litspectraits.normalize import (
    DualFormatComparison,
    DualFormatSkip,
    SkipReason,
    commit_normalized_document,
    compare_dual_format_dois,
    compare_reports,
    converter,
    format_dual_format_report,
    normalize_xml_document,
)
from litspectraits.store import ArtifactStore

# ---------------------------------------------------------------------------
# Test corpus helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    doi: str,
    sha: str,
    fmt: Format,
    publisher: Publisher = Publisher.SPRINGER_NATURE,
) -> AcquisitionRecord:
    """Build an :class:`AcquisitionRecord` matching the store's contract.

    Helper not extracted to a shared conftest because the harness tests
    don't need the full retriever-mock plumbing the extract tests do —
    a plain record + an index entry is enough to exercise the harness.
    """
    relpath = f'artifacts/{_format_dir(fmt)}/sha256/{sha[:2]}/{sha}{_format_ext(fmt)}'
    return AcquisitionRecord(
        doi=doi,
        sha256=sha,
        artifact_path=relpath,
        format=fmt,
        publisher=publisher,
        metadata=CrossRefMetadata(
            doi=doi,
            publisher_str='Test',
            title='Test paper',
            authors=('Doe, J.',),
            year=2024,
            type='journal-article',
            license=None,
        ),
        fetched_url=f'https://test.example/{doi}',
        fetched_at=datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        fetcher_version='0.1.0',
        sdk_version='test 0.0',
        byte_size=42,
        origin='auto',
        manual_provenance=None,
    )


def _format_dir(fmt: Format) -> str:
    return {Format.PDF: 'pdf', Format.JATS_XML: 'jats', Format.ELSEVIER_XML: 'elsevier'}[fmt]


def _format_ext(fmt: Format) -> str:
    return {Format.PDF: '.pdf', Format.JATS_XML: '.xml', Format.ELSEVIER_XML: '.xml'}[fmt]


def _write_index_entry(store: ArtifactStore, record: AcquisitionRecord) -> None:
    """Append a single (doi, sha, format) line to the index.

    Used in place of a full ``store.commit()`` for tests that just need
    the index to know about a DOI without staging real artifact bytes.
    """
    entry = {
        'doi': record.doi,
        'sha256': record.sha256,
        'format': record.format.value,
        'added_at': record.fetched_at.isoformat(),
    }
    with store.index_path.open('a', encoding='utf-8') as fp:
        fp.write(json.dumps(entry) + '\n')


def _stage_extraction(store: ArtifactStore, sha: str) -> Path:
    """Write a placeholder ``documents/<sha>/{document.json,meta.json}`` pair.

    The placeholder document.json content is irrelevant for the
    harness's existence-checks; it just needs to be on disk for the
    MISSING_EXTRACTION skip to *not* fire. Meta.json carries enough
    shape that the persistence-layer sha computation has bytes to
    hash.
    """
    target_dir = store.document_dir(sha)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / 'document.json').write_text('{"placeholder": true}\n')
    (target_dir / 'meta.json').write_text(
        json.dumps(
            {'extractor': 'test', 'source_sha256': sha, 'placeholder': True},
            indent=2,
            sort_keys=True,
        )
        + '\n'
    )
    return target_dir


def _stage_normalization(
    store: ArtifactStore,
    *,
    doi: str,
    sha: str,
    route: str,
) -> None:
    """Build and commit a tiny normalised document for the given route.

    Uses the real persistence layer so the harness's
    ``load_normalized_document`` finds an actual cattrs-shaped JSON
    on disk, exactly matching the path the production code reads.
    Routes ``'jats'`` / ``'elsevier'`` use the XML adapter against a
    minimal payload; the ``'docling'`` route is built via constructor
    literals (matching the persistence test's docling fixture pattern).
    """
    _stage_extraction(store, sha)
    if route in ('jats', 'elsevier'):
        payload = {
            'front': {'title': f'Paper {doi}', 'abstract': 'tiny.'},
            'sections': [
                {
                    'id': 's1',
                    'level': 1,
                    'path': ['s1'],
                    'title': 'Methods',
                    'blocks': [
                        {'text': 'We measured T1.', 'xrefs': []},
                    ],
                }
            ],
            'tables': [
                {
                    'id': 't1',
                    'label': 'Table 1',
                    'caption': 'Cap.',
                    'section_path': ['s1'],
                    'n_rows': 2,
                    'n_cols': 2,
                    'cells': [
                        [
                            {'text': 'Tissue', 'type': 'th', 'rowspan': 1, 'colspan': 1},
                            {'text': 'T1', 'type': 'th', 'rowspan': 1, 'colspan': 1},
                        ],
                        [
                            {'text': 'Liver', 'type': 'td', 'rowspan': 1, 'colspan': 1},
                            {'text': '589', 'type': 'td', 'rowspan': 1, 'colspan': 1},
                        ],
                    ],
                }
            ],
            'figures': [],
            'references': [],
        }
        doc = normalize_xml_document(payload, route=route)  # type: ignore[arg-type]
    elif route == 'docling':
        doc = _docling_document_with_one_table()
    else:
        raise ValueError(f'unsupported test route {route!r}')

    commit_normalized_document(doc=doc, doi=doi, source_artifact_sha=sha, store=store)


def _docling_document_with_one_table() -> Any:
    """One-table docling Document for the harness happy-path test.

    Shares the same cell tokens as the XML-side fixture in
    :func:`_stage_normalization` so the comparison hits non-zero
    recall — otherwise the happy-path test reads "compared but every
    table is a miss", which is technically a valid harness outcome
    but obscures the assertion we care about.
    """
    from litspectraits.normalize import (
        BBox,
        Completeness,
        Document,
        Provenance,
        TableBlock,
        TableCell,
    )

    table = TableBlock(
        id='t1',
        label='Table 1',
        caption='Cap.',
        n_rows=2,
        n_cols=2,
        cells=(
            (
                TableCell(text='Tissue', kind='th'),
                TableCell(text='T1', kind='th'),
            ),
            (
                TableCell(text='Liver', kind='td'),
                TableCell(text='589', kind='td'),
            ),
        ),
        provenance=Provenance(
            route='docling',
            page=1,
            bbox=BBox(x0=10.0, y0=20.0, x1=110.0, y1=80.0),
            page_char_range=None,
        ),
        section_path=('Methods',),
    )
    return Document(
        route='docling',
        blocks=(table,),
        references=(),
        completeness=Completeness(
            has_structured_refs=False,
            has_inline_ref_ids=False,
            has_equations=False,
            table_source='tableformer',
        ),
        title='Test',
    )


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    """Fresh :class:`ArtifactStore` for harness tests."""
    return ArtifactStore(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_complete_dual_format_doi_yields_comparison(store: ArtifactStore) -> None:
    """A DOI with both PDF and XML normalised → :class:`DualFormatComparison`.

    Pins the full pipe: index → load both normdocs → run
    :func:`compare_documents` → yield. Recall is non-zero because the
    XML and docling fixtures share table cell tokens by design.
    """
    doi = '10.1000/dual.happy'
    pdf_sha = 'a' * 64
    xml_sha = 'b' * 64
    _write_index_entry(store, _make_record(doi=doi, sha=pdf_sha, fmt=Format.PDF))
    _write_index_entry(store, _make_record(doi=doi, sha=xml_sha, fmt=Format.JATS_XML))
    _stage_normalization(store, doi=doi, sha=pdf_sha, route='docling')
    _stage_normalization(store, doi=doi, sha=xml_sha, route='jats')

    results = list(compare_dual_format_dois(store))

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, DualFormatComparison)
    assert result.doi == doi
    assert result.pdf_artifact_sha == pdf_sha
    assert result.xml_artifact_sha == xml_sha
    assert result.comparison.n_tables_xml == 1
    assert result.comparison.n_tables_docling == 1
    # Token recall: the table cells are intentionally identical → 1.0.
    (tc,) = result.comparison.table_comparisons
    assert tc.shared_cell_tokens == tc.xml_cell_tokens


def test_elsevier_route_also_works(store: ArtifactStore) -> None:
    """The Elsevier XML route lands the same way as JATS.

    The dispatch keys on Format, not on route, so the two XML routes
    share the harness path — this test pins it by exercising
    Elsevier's manifest format explicitly.
    """
    doi = '10.1000/dual.elsevier'
    pdf_sha = 'c' * 64
    xml_sha = 'd' * 64
    _write_index_entry(store, _make_record(doi=doi, sha=pdf_sha, fmt=Format.PDF))
    _write_index_entry(store, _make_record(doi=doi, sha=xml_sha, fmt=Format.ELSEVIER_XML))
    _stage_normalization(store, doi=doi, sha=pdf_sha, route='docling')
    _stage_normalization(store, doi=doi, sha=xml_sha, route='elsevier')

    (result,) = list(compare_dual_format_dois(store))
    assert isinstance(result, DualFormatComparison)
    assert result.comparison.xml_route == 'elsevier'


# ---------------------------------------------------------------------------
# Skip taxonomy
# ---------------------------------------------------------------------------


def test_pdf_only_doi_skipped_as_not_dual_format(store: ArtifactStore) -> None:
    """A DOI in the index with only a PDF artifact (no XML side) is skipped."""
    doi = '10.1000/single.pdf'
    sha = 'e' * 64
    _write_index_entry(store, _make_record(doi=doi, sha=sha, fmt=Format.PDF))

    (result,) = list(compare_dual_format_dois(store))
    assert isinstance(result, DualFormatSkip)
    assert result.doi == doi
    assert result.reason is SkipReason.NOT_DUAL_FORMAT
    assert 'XML' in result.detail


def test_xml_only_doi_skipped_as_not_dual_format(store: ArtifactStore) -> None:
    """A DOI with only an XML artifact (no sideloaded PDF) is skipped."""
    doi = '10.1000/single.xml'
    sha = 'f' * 64
    _write_index_entry(store, _make_record(doi=doi, sha=sha, fmt=Format.JATS_XML))

    (result,) = list(compare_dual_format_dois(store))
    assert isinstance(result, DualFormatSkip)
    assert result.reason is SkipReason.NOT_DUAL_FORMAT
    assert 'PDF' in result.detail


def test_missing_extraction_one_side_skipped(store: ArtifactStore) -> None:
    """Both formats indexed but PDF extraction missing → MISSING_EXTRACTION.

    Asserts the detail string mentions the *pdf* side specifically so
    an operator knows which `litspectraits extract` to run.
    """
    doi = '10.1000/missing.extract'
    pdf_sha = '1' * 64
    xml_sha = '2' * 64
    _write_index_entry(store, _make_record(doi=doi, sha=pdf_sha, fmt=Format.PDF))
    _write_index_entry(store, _make_record(doi=doi, sha=xml_sha, fmt=Format.JATS_XML))
    # XML side fully prepared; PDF side has only the index entry, no extract.
    _stage_normalization(store, doi=doi, sha=xml_sha, route='jats')

    (result,) = list(compare_dual_format_dois(store))
    assert isinstance(result, DualFormatSkip)
    assert result.reason is SkipReason.MISSING_EXTRACTION
    assert 'pdf' in result.detail.lower()


def test_missing_normalization_one_side_skipped(store: ArtifactStore) -> None:
    """Both formats extracted but PDF normalize missing → MISSING_NORMALIZATION."""
    doi = '10.1000/missing.normalize'
    pdf_sha = '3' * 64
    xml_sha = '4' * 64
    _write_index_entry(store, _make_record(doi=doi, sha=pdf_sha, fmt=Format.PDF))
    _write_index_entry(store, _make_record(doi=doi, sha=xml_sha, fmt=Format.JATS_XML))
    # Both sides extracted, only XML normalised.
    _stage_extraction(store, pdf_sha)
    _stage_normalization(store, doi=doi, sha=xml_sha, route='jats')

    (result,) = list(compare_dual_format_dois(store))
    assert isinstance(result, DualFormatSkip)
    assert result.reason is SkipReason.MISSING_NORMALIZATION
    assert 'pdf' in result.detail.lower()


# ---------------------------------------------------------------------------
# DOI selection
# ---------------------------------------------------------------------------


def test_explicit_dois_filter_to_requested_subset(store: ArtifactStore) -> None:
    """``dois=[...]`` restricts the harness to a named subset.

    Two DOIs in the index; explicit list contains only one; harness
    yields one result.
    """
    doi_a = '10.1000/explicit.a'
    doi_b = '10.1000/explicit.b'
    _write_index_entry(store, _make_record(doi=doi_a, sha='5' * 64, fmt=Format.PDF))
    _write_index_entry(store, _make_record(doi=doi_b, sha='6' * 64, fmt=Format.PDF))

    results = list(compare_dual_format_dois(store, dois=[doi_b]))
    assert len(results) == 1
    assert results[0].doi == doi_b


def test_explicit_doi_not_in_index_raises(store: ArtifactStore) -> None:
    """A DOI in ``dois=[...]`` that is absent from the index is an operator error.

    Per the mental-model session: surface it loudly, do not yield a
    silent skip. ``ValueError`` matches the existing harness's input-
    validation idiom and stays inheritance-disjoint from the
    pipeline-stage error trees.
    """
    _write_index_entry(store, _make_record(doi='10.1000/known', sha='7' * 64, fmt=Format.PDF))

    with pytest.raises(ValueError, match=r'not present in by_doi\.jsonl'):
        list(compare_dual_format_dois(store, dois=['10.1000/typo']))


def test_empty_index_yields_no_results(store: ArtifactStore) -> None:
    """A fresh store with no ingests is a valid input — the harness just yields nothing.

    Catches a regression where the index-not-found branch raised
    instead of returning an empty mapping (auto-discovery against a
    brand-new store would otherwise spuriously fail).
    """
    results = list(compare_dual_format_dois(store))
    assert results == []


# ---------------------------------------------------------------------------
# cattrs round-trip — the report needs to write + read JSON faithfully
# ---------------------------------------------------------------------------


def test_dual_format_result_round_trips_through_converter(
    store: ArtifactStore,
) -> None:
    """Both union variants survive unstructure → JSON → structure.

    The CLI persists harness reports as cattrs-shaped JSON so a
    later ``--compare-to`` run can read them back. A regression on
    the union's structure hook would break temporal regression
    detection.
    """
    skip = DualFormatSkip(
        doi='10.1000/x',
        reason=SkipReason.MISSING_NORMALIZATION,
        detail='pdf normalize missing',
    )
    raw = converter.unstructure(skip)
    serialized = json.dumps(raw)
    deserialized = json.loads(serialized)
    from litspectraits.normalize import DualFormatResult

    # cattrs supports type-alias unions at runtime; pyright sees the
    # union form and rejects it as not assignable to ``type[T]``. The
    # registered structure hook resolves the dispatch correctly.
    restored = converter.structure(deserialized, DualFormatResult)  # pyright: ignore[reportArgumentType]
    assert restored == skip
    assert isinstance(restored, DualFormatSkip)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_format_dual_format_report_renders_compared_and_skipped(
    store: ArtifactStore,
) -> None:
    """The text report distinguishes compared vs skipped DOIs in its summary."""
    doi_compared = '10.1000/compared'
    pdf_sha = '8' * 64
    xml_sha = '9' * 64
    _write_index_entry(store, _make_record(doi=doi_compared, sha=pdf_sha, fmt=Format.PDF))
    _write_index_entry(store, _make_record(doi=doi_compared, sha=xml_sha, fmt=Format.JATS_XML))
    _stage_normalization(store, doi=doi_compared, sha=pdf_sha, route='docling')
    _stage_normalization(store, doi=doi_compared, sha=xml_sha, route='jats')

    doi_skipped = '10.1000/skipped'
    _write_index_entry(store, _make_record(doi=doi_skipped, sha='0' * 64, fmt=Format.PDF))

    text = format_dual_format_report(compare_dual_format_dois(store))
    assert 'compared=1 skipped=1' in text
    assert doi_compared in text
    assert doi_skipped in text
    assert 'not_dual_format' in text


def test_compare_reports_detects_metric_regression() -> None:
    """A drop in cell-token recall between runs surfaces as ⬇ in the diff."""
    from litspectraits.normalize import DocumentComparison, TableComparison

    base = DocumentComparison(
        xml_route='jats',
        n_tables_xml=1,
        n_tables_docling=1,
        table_comparisons=(
            TableComparison(
                xml_index=0,
                docling_index=0,
                xml_n_rows=2,
                xml_n_cols=2,
                docling_n_rows=2,
                docling_n_cols=2,
                xml_cell_tokens=10,
                docling_cell_tokens=10,
                shared_cell_tokens=9,
            ),
        ),
        section_paths_xml=(),
        section_paths_docling=(),
        shared_section_paths=0,
        n_references_xml=0,
        n_references_docling=0,
        has_equations_xml=False,
        has_equations_docling=False,
    )
    regressed = DocumentComparison(
        xml_route='jats',
        n_tables_xml=1,
        n_tables_docling=1,
        table_comparisons=(
            TableComparison(
                xml_index=0,
                docling_index=0,
                xml_n_rows=2,
                xml_n_cols=2,
                docling_n_rows=2,
                docling_n_cols=2,
                xml_cell_tokens=10,
                docling_cell_tokens=7,
                shared_cell_tokens=6,
            ),
        ),
        section_paths_xml=(),
        section_paths_docling=(),
        shared_section_paths=0,
        n_references_xml=0,
        n_references_docling=0,
        has_equations_xml=False,
        has_equations_docling=False,
    )

    prev = [
        DualFormatComparison(
            doi='10.1000/x',
            pdf_artifact_sha='a' * 64,
            xml_artifact_sha='b' * 64,
            comparison=base,
        )
    ]
    curr = [
        DualFormatComparison(
            doi='10.1000/x',
            pdf_artifact_sha='a' * 64,
            xml_artifact_sha='b' * 64,
            comparison=regressed,
        )
    ]
    text = compare_reports(previous=prev, current=curr)
    # Recall went from 0.900 to 0.600 → Δ -0.300, marker ⬇.
    assert '0.900' in text
    assert '0.600' in text
    assert '⬇' in text
    assert 'regressed=1' in text


def test_compare_reports_surfaces_state_changes() -> None:
    """A DOI that flipped from compared to skipped (or vice versa) is highlighted."""
    from litspectraits.normalize import DocumentComparison

    empty_comparison = DocumentComparison(
        xml_route='jats',
        n_tables_xml=0,
        n_tables_docling=0,
        table_comparisons=(),
        section_paths_xml=(),
        section_paths_docling=(),
        shared_section_paths=0,
        n_references_xml=0,
        n_references_docling=0,
        has_equations_xml=False,
        has_equations_docling=False,
    )
    prev = [
        DualFormatComparison(
            doi='10.1000/x',
            pdf_artifact_sha='a' * 64,
            xml_artifact_sha='b' * 64,
            comparison=empty_comparison,
        )
    ]
    curr = [
        DualFormatSkip(
            doi='10.1000/x',
            reason=SkipReason.MISSING_NORMALIZATION,
            detail='regressed pipeline state',
        )
    ]
    text = compare_reports(previous=prev, current=curr)
    assert 'state changed' in text


def test_compare_reports_marks_new_and_gone_dois() -> None:
    """DOIs added or removed between runs are labelled distinctly, not as regressions."""
    from litspectraits.normalize import DocumentComparison

    empty = DocumentComparison(
        xml_route='jats',
        n_tables_xml=0,
        n_tables_docling=0,
        table_comparisons=(),
        section_paths_xml=(),
        section_paths_docling=(),
        shared_section_paths=0,
        n_references_xml=0,
        n_references_docling=0,
        has_equations_xml=False,
        has_equations_docling=False,
    )
    prev = [
        DualFormatComparison(
            doi='10.1000/gone',
            pdf_artifact_sha='a' * 64,
            xml_artifact_sha='b' * 64,
            comparison=empty,
        )
    ]
    curr = [
        DualFormatComparison(
            doi='10.1000/new',
            pdf_artifact_sha='c' * 64,
            xml_artifact_sha='d' * 64,
            comparison=empty,
        )
    ]
    text = compare_reports(previous=prev, current=curr)
    assert '(new)' in text
    assert '(gone)' in text
