# Dual-route comparison — what it is, what it isn't

A working note on the cross-format differential test (the "diff harness")
that compares PDF→docling→`Document` against XML→`Document` for the same
paper. Subordinate to `normalized-documents-discussion.md` §2.4(a); where
the two disagree, the discussion doc wins and this one should be updated
to match.

Audience: future-self picking this up cold, and anyone curating the
dual-format corpus that the harness depends on.

---

## 1. What the harness actually is

Two distinct questions live near each other and are easy to conflate.
Pinning them apart is the most important thing in this document:

- **Q1 — transform faithfulness.** Given a (possibly pathological)
  `DoclingDocument`, does our normalizer preserve its structure into a
  `Document` faithfully? This is a property of *our code*. It is tested
  by adversarial synthetic fixtures (`docs/normalized-documents-discussion.md`
  §2.2 / §2.3): we fabricate weird inputs (cell transpositions, skipped
  heading levels, out-of-order body items) and assert the transform
  preserves them.
- **Q2 — extraction quality.** Given the *real* world (a real paper,
  acquired via two different publisher routes), does docling extract
  enough of the same content that two routes' normalised `Document`s
  agree on the things we care about? This is a property of *docling*,
  not our normalizer. It is the diff harness's job.

The dual-route diff harness exists to answer **Q2 only.** Conflating it
with Q1 is the standard category error.

---

## 2. The publisher / format matrix

The harness needs a paper that exists in our store in **both** the PDF
format and one of the XML formats. The v3 publisher set is fixed and so
is the format each publisher hands us:

| Publisher       | Native format                | XML route value |
| --------------- | ---------------------------- | --------------- |
| Wiley           | PDF                          | (none)          |
| Springer Nature | JATS XML                     | `'jats'`        |
| Elsevier        | Elsevier-flavored XML (CEP)  | `'elsevier'`    |

JATS is **Springer's** format, not a generic XML synonym. Elsevier
returns its own CEP-flavored XML when called with `view=FULL`. A working
mental model of "JATS = the Springer side, Elsevier-XML = the Elsevier
side" prevents a recurring slip when planning the curation.

The two viable dual-format setups are therefore:

1. **Springer DOI**: JATS XML acquired natively → operator sideloads a
   PDF for the same DOI → docling normalises the PDF, the JATS adapter
   normalises the XML, harness compares.
2. **Elsevier DOI**: Elsevier CEP XML acquired natively → operator
   sideloads a PDF for the same DOI → docling normalises the PDF, the
   Elsevier adapter normalises the XML, harness compares.

**Wiley DOIs cannot easily participate.** Wiley hands us a PDF natively
and there is no comparable, clean route to obtain a JATS or
Elsevier-style XML for the same DOI. (PubMed Central can sometimes
provide JATS for Wiley papers but is a different pipeline.) The
practical effect: when curating the dual-format corpus, you pick from
your Springer/Elsevier ingests and sideload PDFs *for those* — never the
reverse.

---

## 3. The workflow, end to end

For a Springer or Elsevier DOI:

1. Normal v3 ingest acquires the publisher XML. Manifest + artifact land
   in the store under the publisher's format directory (`jats/` or
   `elsevier/`).
2. Operator obtains a PDF for the same DOI through an out-of-band route
   (publisher OA download, institutional-access copy, etc.) and commits
   it via `litspectraits sideload <doi> <pdf-path>` (PDF-only sideload,
   per v3 §10). Manifest + artifact land under `artifacts/pdf/`.
3. `litspectraits extract <doi>` runs against both artifacts; each
   produces a `documents/sha256/<aa>/<sha>/document.json` (the publisher-dict /
   docling-dict shape).
4. A normalize step (planned: `litspectraits normalize <doi>`) routes
   each extractor output through the matching adapter
   (`normalize_xml_document` with `route=` matching the publisher,
   `normalize_docling_document` for the PDF) and writes the resulting
   `Document` to `normalized/sha256/<aa>/<sha>/document.json`.
5. The diff harness (`litspectraits diff-routes`) auto-discovers
   dual-format DOIs from `index/by_doi.jsonl`, loads both normdocs, and
   feeds them to `compare_documents(xml_doc, docling_doc)`.

The harness's per-DOI output is a `DocumentComparison` carrying the
proxy numbers (table counts, per-table cell-token recall, section-path
overlap, reference counts, equation presence). Aggregating across
dual-format DOIs gives a corpus-level number, stratifiable by
`xml_route` so the Springer/JATS half and the Elsevier half can be read
separately (they stress different parts of the docling pipeline).

---

## 4. What "agreement" means — and what it doesn't

The two pipelines will **never** produce byte-identical normalised
`Document`s, by design:

- XML carries `xpath` plus resolved `ref_id`s; docling carries
  `page` + `bbox` and only raw reference text (until a later citation-
  parsing pass runs).
- XML route lacks `EquationBlock` in E0.5a; docling has them via formula
  enrichment.
- XML tables come from publisher markup with explicit `th`/`td` kinds;
  docling tables come from TableFormer with its own cell-typing heuristic.
- XML provenance routes are constrained by the Provenance invariants in
  `models.py`; docling provenance routes are constrained differently.

Equality assertions on the two documents are the wrong shape. The
harness's outputs are *numbers* (recall, overlap counts) that measure
agreement on the things that matter for downstream measurement
extraction:

- Table-cell **token recall** (case-folded whitespace tokens; a multiset,
  not a set — repeated rows count with multiplicity). This is the
  highest-signal proxy because tables are where the numeric values live.
- **Section-path overlap** (set intersection) and ordering (preserved
  per-side as a tuple) — a docling layout bug that re-orders Methods
  after Results shows up as preserved set overlap but drifted sequence.
- **Reference count agreement** — a coarse proxy for whether docling
  recovered the bibliography at all.

Each number is meant as a *regression tripwire*, not a correctness claim
(see next section).

---

## 5. Tripwire, not unit test

A unit test asserts a fixed expected value. The diff harness reports a
coverage number whose meaning is **only revealed by trend**:

- "Table cell-token recall on this DOI is 0.87" is not a pass/fail.
- "Recall dropped from 0.87 to 0.71 after the docling settings change"
  *is* a signal.

The implication for the implementation is that single-snapshot reports
are the weaker version of the tool. Reports persist to disk
(`reports/<date>.json` via `cattrs.unstructure`), and the CLI grows a
`--compare-to <prev-report.json>` flag for temporal regression
detection. The longitudinal compare is where the real value sits; the
snapshot is the building block.

---

## 6. XML as ground truth — with one caveat

The harness treats the XML route as the trusted reference and reports
docling's deviations from it. This is defensible because:

- Publisher XML encodes structure (cell boundaries, section nesting,
  reference lists) explicitly; docling has to infer it from layout.
- The XML route is a *shorter* path through fewer ML-inferred steps, so
  its failure modes are different in kind and frequency.

But "trusted at the structural recall level" is **not** "trusted at the
value-correctness level." Both routes can have route-specific bugs: a
JATS adapter could mis-parse a nested table, an Elsevier extractor could
drop a footnote. The harness measures structural agreement, not
absolute truth. Disagreement on the value of a specific cell is, in
practice, *almost always* docling's problem — but it isn't always.

In the measurement-space version of this test (E1; see next section), a
value disagreement is treated as docling-attributable by default and
the rare XML-attributable cases are surfaced for manual review. This is
a conscious asymmetry the harness inherits, not a property of the
fundamental setup.

---

## 7. Curation is the real prerequisite

The harness's code is a small loader + the existing `compare_documents`
primitive. The *expensive* prerequisite is operator labour:

- Identify ~10–20 Springer/Elsevier DOIs that are good candidates
  (broad coverage of tissue types, field strengths, table styles).
- Obtain a PDF for each (publisher OA download, institutional access,
  whatever the licence allows).
- Sideload each PDF with a `ManualProvenance` record (operator email,
  licence assertion, source URL, free-text note — institutional-access
  PDFs are licensed but not redistributable, and the manifest is the
  only place that fact lives, per v3 §6).
- Run `extract` and `normalize` for each artifact pair so the dual
  normdocs exist on disk.

None of this is automatable from this side of the network — it lives
with the operator. The harness is unusable until that corpus exists; the
corpus is unusable without the harness. Both halves need to land before
the regression-tripwire story is real.

---

## 8. Where this sits on the trajectory toward E1

`docs/normalized-documents-discussion.md` §2.4(a) frames the *eventual*
cross-format differential test in **measurement space**: the XML route
produces a `Measurement` set, the PDF route produces another, and the
deviations between them are the docling error catalogue denominated in
relaxometric values ("the PDF route recovers 87% of the values, of the
misses N are table-cell misreads, M are dropped").

That mode requires `Measurement` to exist, which is E1+. Until then, the
**Document-space** proxies above (tables / sections / references) stand
in. The two are not redundant — they answer the same question at
different points in the pipeline — but the proxy version pays its way
*now* by catching docling regressions before E1 lands.

The function signature is designed to survive the transition:
`compare_dual_format_dois(dois, store)` yields a `DocumentComparison`
today; at E1 it gains an additional `MeasurementComparison` field on
the same yield tuple, or grows a sibling
`compare_dual_format_dois_measurements` that takes the same loader
plumbing. The plumbing is the durable investment; the comparator slot
is replaceable.

---

## 9. Implementation pointers

- Primitives (existing): `src/litspectraits/normalize/diff.py` —
  `compare_documents`, `format_comparison_report`, the `TableComparison`
  / `DocumentComparison` `@frozen` data models.
- Loader (planned, follow-on #1): `compare_dual_format_dois(dois, store)
  -> Iterable[(doi, DocumentComparison)]`. Auto-discovers dual-format
  candidates by scanning `index/by_doi.jsonl`; reads
  `normalized/sha256/<aa>/<sha>/document.json` per side.
- Persistence: `normalized/sha256/<aa>/<sha>/` with `document.json` +
  `meta.json`; atomic commit via `tmp/` + `os.replace` per `CLAUDE.md`.
- CLI: separate composable commands (`extract`, `normalize`,
  `diff-routes`) rather than folding `normalize` into `extract` — keeps
  each stage independently re-runnable and introspectable, at the cost
  of one extra command in the common path.

Failure modes the loader must distinguish in its output:

- DOI not in index → operator error, raise loudly.
- DOI in index but only one format present → "not yet a dual-format
  candidate"; skip + log, do not yield.
- Both formats present but extraction missing on one side → "operator
  forgot to run extract"; skip + log with a different tag so the
  operator can fix it directly.
- Both formats present, both extracted, but normalize missing → same
  shape as above, different tag.
- All present and normalised → yield the comparison.

The point of the tag distinction is that "skip because not curated" and
"skip because you forgot a step" are operationally different problems
even though both end in no yield.
