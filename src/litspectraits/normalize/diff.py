"""Cross-format differential comparison for normalised :class:`Document`s.

Implements the §2.4(a) cross-format differential harness from
``docs/normalized-documents-discussion.md`` at the only level we can
implement it pre-E1: structural proxies on the :class:`Document` itself.

What this is, and what it deliberately is **not**
-------------------------------------------------
The discussion doc is explicit that the harness's primary mode is a
**measurement-space comparison**: the XML route's :class:`Measurement`
set is the trusted reference, the PDF route's deviations are the
docling error catalogue *denominated in relaxometric values*. That mode
cannot be built until E1 lands (no :class:`Measurement` exists yet).

What this module builds is the **regression tripwire** that operates
*before* E1 — a small set of :class:`Document`-level proxies:

- table count + per-table cell-token coverage (tables are where the
  values live, so this is the highest-signal proxy)
- section-path outline overlap
- reference count

Two normalised documents for the same paper will *never* be byte-
identical by design (XML carries ``xpath`` and resolved ``ref_id``s;
docling carries ``page``/``bbox`` and only raw reference text; XML lacks
``EquationBlock`` in E0.5a; docling tables come from TableFormer rather
than publisher markup). Each proxy here is a *number*, not a strict
equality assertion, and is meant to flag regressions ("table cell
coverage dropped from 0.92 to 0.71 after the docling bump") rather than
correctness.

Module layout
-------------
- :func:`compare_documents` and the comparison data classes
  (:class:`TableComparison`, :class:`DocumentComparison`) are the pure
  primitives: two normalised documents in, one comparison record out.
- :func:`compare_dual_format_dois` is the data-store integration:
  walks ``index/by_doi.jsonl``, filters to DOIs with both PDF and XML
  manifests, loads each side's persisted normalised document via
  :mod:`litspectraits.normalize.persistence`, calls
  :func:`compare_documents`, and yields per-DOI results. DOIs that
  cannot be compared yield a :class:`DualFormatSkip` with one of three
  typed reasons (:class:`SkipReason`).
- :func:`format_dual_format_report` renders a per-DOI text summary;
  :func:`compare_reports` is the temporal-regression view across two
  persisted reports (``--out`` / ``--compare-to`` in the CLI).

See ``docs/dual-route-comparison-overview.md`` for the operational
mental model and the relationship to the E1 measurement-space
follow-on.
"""

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

import structlog
from attrs import frozen

from litspectraits.manifest import Format
from litspectraits.normalize.hooks import converter as _normalize_converter
from litspectraits.normalize.models import (
    Block,
    Document,
    TableBlock,
    TableCell,
)
from litspectraits.normalize.persistence import (
    DOCUMENT_FILENAME as _NORMALIZED_DOCUMENT_FILENAME,
)
from litspectraits.normalize.persistence import (
    load_normalized_document,
)
from litspectraits.store import ArtifactStore

_logger: Final = structlog.get_logger('litspectraits.normalize.diff')

_XML_FORMATS: Final[frozenset[Format]] = frozenset({Format.JATS_XML, Format.ELSEVIER_XML})
"""The XML-side manifest formats the dual-format harness considers
paired against a :attr:`Format.PDF` artifact. A new XML publisher
landing in :class:`Format` needs an entry here too — keeping the set
explicit means the harness fails-closed (treats the new format as
non-XML, i.e. not eligible) rather than fail-open (silently treating
some PDF-vs-PDF or XML-vs-XML pairing as dual-format)."""

XmlRoute = Literal['jats', 'elsevier']
"""The two routes ``compare_documents`` accepts as the reference side —
narrower than :data:`Route` because docling-vs-docling diffs aren't a
use case the harness is designed for."""


@frozen
class TableComparison:
    """Per-table comparison between an XML reference and the docling output.

    Tables are matched **by document order**: ``xml_index=k`` is paired
    with ``docling_index=k`` whenever both routes recovered at least
    that many tables. This is the simplest defensible matching and is
    fine for the regression-tripwire use case (a coverage drop on the
    *Nth* table is detectable regardless of which physical table that
    is). Smarter matching — by caption Jaccard, by label string —
    properly belongs in the measurement layer's table-binding pass.

    The token counts are denominated in **case-folded whitespace-split
    tokens** drawn from every cell's ``text`` field. We deliberately do
    not strip punctuation or numerics: the values we care about
    (``"42.3"``, ``"1.5T"``) survive unchanged through case-folding-only
    normalisation, and number-as-token is the right unit for a quick
    coverage proxy.

    Attributes
    ----------
    xml_index, docling_index : int
        Position of the matched tables in their respective documents'
        ``blocks`` order, restricted to :class:`TableBlock` entries.
    xml_n_rows, xml_n_cols, docling_n_rows, docling_n_cols : int
        Source-emitted dimensions; spans are *not* expanded.
    xml_cell_tokens, docling_cell_tokens : int
        Total tokens in each side's cells (a multiset cardinality —
        repeated tokens are counted with multiplicity).
    shared_cell_tokens : int
        Multiset intersection size. Two ratios fall out trivially:
        ``shared / xml_cell_tokens`` (recall against the trusted XML
        reference, the headline number) and ``shared / docling_cell_tokens``
        (precision; mostly diagnostic).
    """

    xml_index: int
    docling_index: int
    xml_n_rows: int
    xml_n_cols: int
    docling_n_rows: int
    docling_n_cols: int
    xml_cell_tokens: int
    docling_cell_tokens: int
    shared_cell_tokens: int


@frozen
class DocumentComparison:
    """Summary of a single (XML reference, docling output) pair.

    The xml-route field captures *which* XML route produced the reference
    (``'jats'`` or ``'elsevier'``) so an aggregated report can stratify —
    Elsevier and Springer/JATS papers stress different parts of the docling
    pipeline, and a regression that only shows up on one route is the kind
    of signal we want to keep visible.
    """

    xml_route: XmlRoute
    n_tables_xml: int
    n_tables_docling: int
    table_comparisons: tuple[TableComparison, ...]
    section_paths_xml: tuple[tuple[str | None, ...], ...]
    section_paths_docling: tuple[tuple[str | None, ...], ...]
    shared_section_paths: int
    n_references_xml: int
    n_references_docling: int
    has_equations_xml: bool
    has_equations_docling: bool


def compare_documents(
    xml_doc: Document,
    docling_doc: Document,
) -> DocumentComparison:
    """Run the slice-2 proxy diffs against an XML reference + docling output.

    Parameters
    ----------
    xml_doc : Document
        :class:`Document` produced by
        :func:`litspectraits.normalize.normalize_xml_document` — the
        trusted reference side.
    docling_doc : Document
        :class:`Document` produced by
        :func:`litspectraits.normalize.normalize_docling_document` —
        the side under test.

    Returns
    -------
    DocumentComparison
        The proxy diffs. None of the individual numbers is a
        correctness assertion; this is a regression-tripwire shape that
        a caller aggregates into a per-corpus summary
        (see :func:`format_comparison_report` for the canonical
        formatting).

    Raises
    ------
    ValueError
        ``xml_doc.route`` is not an XML route, or ``docling_doc.route``
        is not ``'docling'``. The comparison's directionality matters
        (XML is the trusted reference); a silently-swapped call would
        produce numbers whose sign was wrong and lie to the regression
        gate.
    """
    if xml_doc.route not in ('jats', 'elsevier'):
        raise ValueError(
            "compare_documents requires xml_doc.route in {'jats', 'elsevier'}; "
            f'got {xml_doc.route!r}'
        )
    if docling_doc.route != 'docling':
        raise ValueError(
            f"compare_documents requires docling_doc.route == 'docling'; got {docling_doc.route!r}"
        )

    xml_tables = _tables(xml_doc.blocks)
    docling_tables = _tables(docling_doc.blocks)
    table_comparisons = tuple(
        _compare_one_table(idx, xml_t, docling_t)
        for idx, (xml_t, docling_t) in enumerate(zip(xml_tables, docling_tables, strict=False))
    )

    xml_paths = _unique_section_paths_in_order(xml_doc.blocks)
    docling_paths = _unique_section_paths_in_order(docling_doc.blocks)
    shared_paths = len(set(xml_paths) & set(docling_paths))

    return DocumentComparison(
        xml_route=xml_doc.route,  # type: ignore[arg-type]
        n_tables_xml=len(xml_tables),
        n_tables_docling=len(docling_tables),
        table_comparisons=table_comparisons,
        section_paths_xml=xml_paths,
        section_paths_docling=docling_paths,
        shared_section_paths=shared_paths,
        n_references_xml=len(xml_doc.references),
        n_references_docling=len(docling_doc.references),
        has_equations_xml=xml_doc.completeness.has_equations,
        has_equations_docling=docling_doc.completeness.has_equations,
    )


def format_comparison_report(
    items: Iterable[tuple[str, DocumentComparison]],
) -> str:
    """Render a per-DOI human-readable summary of a batch of comparisons.

    Parameters
    ----------
    items : iterable of (doi, DocumentComparison)
        The comparisons to format. The DOI string is used purely as a
        row label; nothing is parsed from it.

    Returns
    -------
    str
        Multi-line plain-text summary, one block per DOI. Cell coverage
        is reported as ``shared / xml_tokens`` (recall against the
        trusted reference); a 0-token XML cell-multiset is reported as
        ``n/a`` so a degenerate table doesn't inject ``ZeroDivisionError``.
    """
    lines: list[str] = []
    for doi, comp in items:
        lines.append(f'DOI {doi} (xml_route={comp.xml_route})')
        lines.append(
            f'  Tables: xml={comp.n_tables_xml} docling={comp.n_tables_docling}'
            f' (matched={len(comp.table_comparisons)})'
        )
        for tc in comp.table_comparisons:
            if tc.xml_cell_tokens == 0:
                coverage = 'n/a'
            else:
                coverage = f'{tc.shared_cell_tokens / tc.xml_cell_tokens:.2f}'
            lines.append(
                f'    [{tc.xml_index}] cells xml={tc.xml_n_rows}x{tc.xml_n_cols}'
                f' docling={tc.docling_n_rows}x{tc.docling_n_cols}'
                f' tokens xml={tc.xml_cell_tokens} docling={tc.docling_cell_tokens}'
                f' shared={tc.shared_cell_tokens} recall={coverage}'
            )
        n_sec_xml = len(comp.section_paths_xml)
        n_sec_docling = len(comp.section_paths_docling)
        lines.append(
            f'  Sections: xml={n_sec_xml} docling={n_sec_docling}'
            f' shared={comp.shared_section_paths}'
        )
        lines.append(
            f'  References: xml={comp.n_references_xml} docling={comp.n_references_docling}'
        )
        lines.append(
            f'  Equations: xml={comp.has_equations_xml} docling={comp.has_equations_docling}'
        )
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tables(blocks: tuple[Block, ...]) -> tuple[TableBlock, ...]:
    return tuple(b for b in blocks if isinstance(b, TableBlock))


def _unique_section_paths_in_order(
    blocks: tuple[Block, ...],
) -> tuple[tuple[str | None, ...], ...]:
    """Return each distinct ``section_path`` in first-encounter order.

    The first-encounter ordering matters because section flow is itself
    a regression signal: if docling re-orders ``Methods`` after
    ``Results`` because of a layout bug, the *set* of section paths is
    unchanged but the *sequence* has drifted. ``shared_section_paths``
    counts the set overlap; the per-side tuples preserve order for a
    caller that wants to assert on it.
    """
    seen: set[tuple[str | None, ...]] = set()
    ordered: list[tuple[str | None, ...]] = []
    for block in blocks:
        path = block.section_path
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return tuple(ordered)


def _compare_one_table(
    idx: int,
    xml_t: TableBlock,
    docling_t: TableBlock,
) -> TableComparison:
    xml_tokens = _cell_token_multiset(xml_t.cells)
    docling_tokens = _cell_token_multiset(docling_t.cells)
    shared = sum((xml_tokens & docling_tokens).values())
    return TableComparison(
        xml_index=idx,
        docling_index=idx,
        xml_n_rows=xml_t.n_rows,
        xml_n_cols=xml_t.n_cols,
        docling_n_rows=docling_t.n_rows,
        docling_n_cols=docling_t.n_cols,
        xml_cell_tokens=sum(xml_tokens.values()),
        docling_cell_tokens=sum(docling_tokens.values()),
        shared_cell_tokens=shared,
    )


def _cell_token_multiset(
    cells: tuple[tuple[TableCell, ...], ...],
) -> Counter[str]:
    """Case-folded whitespace-split tokens across every cell in the grid.

    Punctuation is preserved (``'42.3'`` is one token; we don't want to
    split decimals); ``str.split`` without args collapses runs of
    whitespace. Empty tokens are filtered to keep stray ``''`` out of
    the multiset.
    """
    counter: Counter[str] = Counter()
    for row in cells:
        for cell in row:
            for tok in cell.text.casefold().split():
                if tok:
                    counter[tok] += 1
    return counter


# ===========================================================================
# Dual-format harness — data-store integration (follow-on #1 of E0.5b)
#
# Operationalises the cross-format differential test
# (``docs/normalized-documents-discussion.md`` §2.4(a) and
# ``docs/dual-route-comparison-overview.md``) over the *real* corpus:
# discover dual-format DOIs by scanning ``index/by_doi.jsonl``, load
# their persisted normdocs (built by ``normalize/persistence.py``),
# feed each pair into :func:`compare_documents`, and yield the per-DOI
# result alongside skipped DOIs with a typed reason so the operator
# can act on each bucket distinctly.
# ===========================================================================


class SkipReason(StrEnum):
    """Why the harness did not produce a :class:`DocumentComparison` for a DOI.

    The three reasons map one-to-one to operator remediation steps —
    a corpus-curation gap, a missing extract run, or a missing
    normalize run. Surfacing them distinctly (per the mental-model
    session) matters because "you don't have a PDF for this paper
    yet" and "you forgot to run normalize" are very different problems
    even though both end in "no comparison".
    """

    NOT_DUAL_FORMAT = 'not_dual_format'
    """The DOI's manifest set in ``index/by_doi.jsonl`` does not include
    both a PDF and an XML (JATS or Elsevier) artifact. Operator
    remediation: sideload the missing format
    (``docs/dual-route-comparison-overview.md`` §7)."""

    MISSING_EXTRACTION = 'missing_extraction'
    """One or both sides of the dual-format pair has no
    ``documents/sha256/<aa>/<sha>/document.json`` — the artifact was ingested but
    extraction has not run for it. Operator remediation:
    ``litspectraits extract <doi-or-sha>``."""

    MISSING_NORMALIZATION = 'missing_normalization'
    """Both sides are extracted but one or both has no
    ``normalized/sha256/<aa>/<sha>/document.json`` — extraction is done but the
    normalize step has not run. Operator remediation:
    ``litspectraits normalize <doi-or-sha>``."""


@frozen
class DualFormatComparison:
    """A completed cross-format comparison for one DOI.

    The :attr:`comparison` field carries the proxy numbers (per-table
    cell-token recall, section-path overlap, reference counts) from
    :func:`compare_documents`. The two ``*_artifact_sha`` fields
    identify which manifest entries the harness picked when the DOI
    had multiple manifests per format (latest by index order — append
    order in ``by_doi.jsonl``).

    The ``outcome`` discriminator is the tagged-union tag for
    :data:`DualFormatResult`; ``cattrs`` dispatches on it.
    """

    doi: str
    pdf_artifact_sha: str
    xml_artifact_sha: str
    comparison: DocumentComparison
    outcome: Literal['compared'] = 'compared'


@frozen
class DualFormatSkip:
    """A DOI the harness intentionally did not compare.

    The :attr:`reason` field is the typed bucket; :attr:`detail` is a
    human-readable elaboration (e.g. "PDF normalize missing for
    sha256=abc…"). Keep ``reason`` for machine dispatch and
    ``detail`` for operator triage — both are useful.
    """

    doi: str
    reason: SkipReason
    detail: str
    outcome: Literal['skipped'] = 'skipped'


DualFormatResult = DualFormatComparison | DualFormatSkip
"""Tagged union over the per-DOI harness outcome.

cattrs structures on the ``outcome`` field via the hook registered in
:mod:`litspectraits.normalize.hooks`. Consumers pattern-match on
``isinstance(result, DualFormatComparison)`` or on
``result.outcome`` — both work.
"""


def compare_dual_format_dois(
    store: ArtifactStore,
    *,
    dois: Iterable[str] | None = None,
) -> Iterator[DualFormatResult]:
    """Yield per-DOI harness outcomes for the dual-format corpus.

    Auto-discovers when ``dois is None``: scans ``index/by_doi.jsonl``,
    groups by DOI, filters to candidates with at least one PDF
    manifest and at least one XML manifest (JATS or Elsevier). When
    multiple manifests exist for the same (DOI, Format) pair, the
    most-recently-indexed one wins — same rule
    :meth:`ArtifactStore.find_by_doi` uses.

    Explicit ``dois`` filters to just those DOIs. A DOI in the explicit
    list that does not appear in the index is an operator error (typo
    or wrong corpus): the function raises :class:`ValueError` rather
    than yielding a skip. The auto-discover path cannot trigger this
    case by construction.

    Parameters
    ----------
    store : ArtifactStore
        The store whose ``index/`` and ``normalized/`` trees are read.
    dois : iterable of str, optional
        Explicit DOI subset. ``None`` (default) means auto-discover.

    Yields
    ------
    DualFormatResult
        One :class:`DualFormatComparison` per successfully-compared
        DOI, or one :class:`DualFormatSkip` per DOI the harness
        deliberately skipped.

    Raises
    ------
    ValueError
        ``dois`` is non-None and contains a DOI not present in the
        index. Surface loudly per CLAUDE.md "fail loudly" — the
        operator's input is wrong.
    """
    per_doi = _scan_index(store.index_path)
    if dois is not None:
        wanted = list(dois)
        missing = [doi for doi in wanted if doi not in per_doi]
        if missing:
            raise ValueError(
                f'dois not present in {store.index_path.name}: {missing!r}. '
                'Verify the DOI shape (lowercase, no prefixes) or rerun ingest.'
            )
        per_doi = {doi: per_doi[doi] for doi in wanted}

    for doi, formats in per_doi.items():
        yield _evaluate_one(doi=doi, formats=formats, store=store)


def _evaluate_one(
    *,
    doi: str,
    formats: dict[Format, str],
    store: ArtifactStore,
) -> DualFormatResult:
    """Decision tree for a single DOI's manifest set.

    Walks the operator-remediation cascade in order: not-dual-format →
    missing-extraction → missing-normalization → load-and-compare.
    Each fall-through point is its own typed skip so the report makes
    "fix the corpus" vs "run extract" vs "run normalize" instantly
    grep-able.
    """
    pdf_sha = formats.get(Format.PDF)
    xml_format = next((fmt for fmt in _XML_FORMATS if fmt in formats), None)
    if pdf_sha is None or xml_format is None:
        return DualFormatSkip(
            doi=doi,
            reason=SkipReason.NOT_DUAL_FORMAT,
            detail=_not_dual_format_detail(formats),
        )
    xml_sha = formats[xml_format]

    missing_extraction = _missing_extraction_side(store=store, pdf_sha=pdf_sha, xml_sha=xml_sha)
    if missing_extraction is not None:
        return DualFormatSkip(
            doi=doi,
            reason=SkipReason.MISSING_EXTRACTION,
            detail=missing_extraction,
        )

    missing_normalization = _missing_normalization_side(
        store=store, pdf_sha=pdf_sha, xml_sha=xml_sha
    )
    if missing_normalization is not None:
        return DualFormatSkip(
            doi=doi,
            reason=SkipReason.MISSING_NORMALIZATION,
            detail=missing_normalization,
        )

    pdf_doc = load_normalized_document(source_artifact_sha=pdf_sha, store=store)
    xml_doc = load_normalized_document(source_artifact_sha=xml_sha, store=store)
    comparison = compare_documents(xml_doc=xml_doc, docling_doc=pdf_doc)

    _logger.info(
        'dual-format comparison computed',
        doi=doi,
        pdf_artifact_sha=pdf_sha,
        xml_artifact_sha=xml_sha,
        xml_route=xml_doc.route,
        n_tables=comparison.n_tables_xml,
    )
    return DualFormatComparison(
        doi=doi,
        pdf_artifact_sha=pdf_sha,
        xml_artifact_sha=xml_sha,
        comparison=comparison,
    )


def _scan_index(index_path: Path) -> dict[str, dict[Format, str]]:
    """Read the JSON-lines DOI index into a per-DOI, per-format sha map.

    Parses every line in ``by_doi.jsonl`` and, for each (DOI, format)
    pair, keeps the *last* sha seen — the index is append-only and
    written in commit order, so "last seen" matches "most recent
    ingest" per :meth:`ArtifactStore.find_by_doi`'s contract. Blank
    lines are skipped, but a malformed line raises
    :class:`ValueError` rather than being silently dropped (the index
    is invariant-load-bearing; a malformed line is a corpus integrity
    issue that wants surfacing).

    Returns an empty dict when the index file does not exist — that
    is the "fresh store, nothing ingested yet" state.
    """
    per_doi: dict[str, dict[Format, str]] = {}
    if not index_path.exists():
        return per_doi
    with index_path.open(encoding='utf-8') as fp:
        for line_number, raw_line in enumerate(fp, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                doi = entry['doi']
                sha = entry['sha256']
                fmt = Format(entry['format'])
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                raise ValueError(
                    f'{index_path}: malformed entry on line {line_number}: {raw_line!r}'
                ) from exc
            per_doi.setdefault(doi, {})[fmt] = sha
    return per_doi


def _not_dual_format_detail(formats: dict[Format, str]) -> str:
    """Human-readable detail for NOT_DUAL_FORMAT — what's present, what's missing.

    Reads as "we have X, you need Y" so the operator knows which
    side to sideload.
    """
    present = sorted(fmt.value for fmt in formats)
    missing: list[str] = []
    if Format.PDF not in formats:
        missing.append('PDF (sideload required)')
    if not any(fmt in formats for fmt in _XML_FORMATS):
        missing.append('XML (publisher ingest required)')
    return f'present={present}; missing={missing}'


def _missing_extraction_side(*, store: ArtifactStore, pdf_sha: str, xml_sha: str) -> str | None:
    """Return a detail string when extract has not run on one or both sides; else ``None``."""
    pdf_extracted = (store.document_dir(pdf_sha) / 'document.json').exists()
    xml_extracted = (store.document_dir(xml_sha) / 'document.json').exists()
    if pdf_extracted and xml_extracted:
        return None
    missing: list[str] = []
    if not pdf_extracted:
        missing.append(f'pdf sha256={pdf_sha[:12]}…')
    if not xml_extracted:
        missing.append(f'xml sha256={xml_sha[:12]}…')
    return f'extract has not run on: {", ".join(missing)}'


def _missing_normalization_side(*, store: ArtifactStore, pdf_sha: str, xml_sha: str) -> str | None:
    """Return a detail string when normalize has not run on one or both sides; else ``None``."""
    pdf_normalized = (store.normalized_dir(pdf_sha) / _NORMALIZED_DOCUMENT_FILENAME).exists()
    xml_normalized = (store.normalized_dir(xml_sha) / _NORMALIZED_DOCUMENT_FILENAME).exists()
    if pdf_normalized and xml_normalized:
        return None
    missing: list[str] = []
    if not pdf_normalized:
        missing.append(f'pdf sha256={pdf_sha[:12]}…')
    if not xml_normalized:
        missing.append(f'xml sha256={xml_sha[:12]}…')
    return f'normalize has not run on: {", ".join(missing)}'


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_dual_format_report(results: Iterable[DualFormatResult]) -> str:
    """Render a per-DOI summary of dual-format harness outcomes.

    One line of header per DOI, then either the per-comparison detail
    (re-using :func:`format_comparison_report`'s body shape) or the
    skip reason + detail. Operator workflow: pipe a fresh run to
    stdout to eyeball, persist via ``--out`` for later
    ``--compare-to`` regression detection.
    """
    lines: list[str] = []
    compared = 0
    skipped = 0
    for result in results:
        if isinstance(result, DualFormatComparison):
            compared += 1
            lines.append(f'DOI {result.doi} — compared')
            lines.append(
                f'  pdf_sha={result.pdf_artifact_sha[:12]}…  '
                f'xml_sha={result.xml_artifact_sha[:12]}…'
            )
            lines.extend(
                '  ' + line
                for line in format_comparison_report(
                    [(result.doi, result.comparison)]
                ).splitlines()[1:]
                # drop the doi-header line emitted by format_comparison_report;
                # we already printed our own with the dual-format detail.
            )
        else:
            skipped += 1
            lines.append(f'DOI {result.doi} — skipped ({result.reason.value})')
            lines.append(f'  {result.detail}')
    lines.append('')
    lines.append(f'Summary: compared={compared} skipped={skipped}')
    return '\n'.join(lines)


def compare_reports(
    *,
    previous: Iterable[DualFormatResult],
    current: Iterable[DualFormatResult],
) -> str:
    """Render a temporal regression view between two harness runs.

    Goal (per ``docs/dual-route-comparison-overview.md`` §5): "table
    cell-token recall dropped from 0.87 to 0.71 after the docling
    bump" is the signal we want, not the absolute number on either
    side. So this rendering pairs DOIs across the two runs and shows
    metric *deltas* in addition to absolute values.

    Pairing is by DOI. New DOIs (in current, not in previous) and
    departed DOIs (in previous, not in current) are surfaced
    separately as "(new)" / "(gone)" so a corpus change between runs
    isn't read as a regression.
    """
    prev_by_doi = {r.doi: r for r in previous}
    curr_by_doi = {r.doi: r for r in current}

    lines: list[str] = []
    n_regressed = 0
    n_improved = 0
    n_unchanged = 0

    for doi in sorted(curr_by_doi.keys() | prev_by_doi.keys()):
        prev = prev_by_doi.get(doi)
        curr = curr_by_doi.get(doi)
        if prev is None:
            # ``doi`` is in the union of keysets; if prev side is missing,
            # the curr side must be present. ``assert`` narrows for pyright.
            assert curr is not None
            lines.append(f'DOI {doi} — (new) {_state_label(curr)}')
            continue
        if curr is None:
            lines.append(f'DOI {doi} — (gone) was {_state_label(prev)}')
            continue
        if isinstance(prev, DualFormatComparison) and isinstance(curr, DualFormatComparison):
            prev_recall = _overall_recall(prev.comparison)
            curr_recall = _overall_recall(curr.comparison)
            delta = curr_recall - prev_recall
            arrow = '⬇' if delta < -1e-6 else ('⬆' if delta > 1e-6 else '·')
            if delta < -1e-6:
                n_regressed += 1
            elif delta > 1e-6:
                n_improved += 1
            else:
                n_unchanged += 1
            lines.append(
                f'DOI {doi} — recall {prev_recall:.3f} → {curr_recall:.3f}'
                f' (Δ {delta:+.3f}) {arrow}'
            )
        else:
            # State transition (compared ↔ skipped). Always surface.
            lines.append(f'DOI {doi} — state changed: {_state_label(prev)} → {_state_label(curr)}')

    lines.append('')
    lines.append(f'Summary: regressed={n_regressed} improved={n_improved} unchanged={n_unchanged}')
    return '\n'.join(lines)


def _state_label(result: DualFormatResult) -> str:
    """One-word state label for a result, used in the temporal diff."""
    if isinstance(result, DualFormatComparison):
        return f'compared (recall {_overall_recall(result.comparison):.3f})'
    return f'skipped ({result.reason.value})'


def _overall_recall(comparison: DocumentComparison) -> float:
    """Aggregate cell-token recall across all matched tables in a comparison.

    Single number for the temporal-diff use case — the corpus-level
    summary is built by averaging *this* number across DOIs, but the
    per-DOI granularity is where regressions surface first. A 0-token
    XML side (no tables, or empty tables) collapses to 0.0; that's a
    degenerate case the report-side caller already labels in the
    formatter.
    """
    total_xml = sum(tc.xml_cell_tokens for tc in comparison.table_comparisons)
    total_shared = sum(tc.shared_cell_tokens for tc in comparison.table_comparisons)
    if total_xml == 0:
        return 0.0
    return total_shared / total_xml


# ---------------------------------------------------------------------------
# cattrs structure hook for the DualFormatResult tagged union
# ---------------------------------------------------------------------------
#
# Registered here rather than in :mod:`hooks` to avoid a circular import:
# ``hooks`` already loads ``models``; ``persistence`` depends on ``hooks``;
# this module depends on ``persistence``. Registering the hook from this
# module against the shared :data:`_normalize_converter` keeps every
# structuring path going through one converter while letting the import
# graph stay forward-only.


def _structure_dual_format_result(value: Any, _type: Any) -> DualFormatResult:
    if not isinstance(value, dict):
        raise TypeError(
            'DualFormatResult must be a mapping with an `outcome` discriminator; '
            f'got {type(value).__name__}'
        )
    outcome = value.get('outcome')
    if outcome == 'compared':
        return _normalize_converter.structure(value, DualFormatComparison)
    if outcome == 'skipped':
        return _normalize_converter.structure(value, DualFormatSkip)
    raise ValueError(
        f"unknown DualFormatResult `outcome`: {outcome!r}; expected 'compared' or 'skipped'"
    )


_normalize_converter.register_structure_hook(DualFormatResult, _structure_dual_format_result)


__all__ = [
    'DocumentComparison',
    'DualFormatComparison',
    'DualFormatResult',
    'DualFormatSkip',
    'SkipReason',
    'TableComparison',
    'compare_documents',
    'compare_dual_format_dois',
    'compare_reports',
    'format_comparison_report',
    'format_dual_format_report',
]
