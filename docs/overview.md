# MRI Literature Property Extraction Pipeline — Design

## Aim

Build a pipeline that takes a DOI as input and produces structured, queryable, provenance-tracked records of quantitative MR and electromagnetic property values reported in the MRI literature.

Target quantities include relaxometric values (T1, T2, T2\*, PD), magnetic susceptibility (χ), diffusion parameters, and related EM/tissue properties. Each value record carries the contextual parameters needed to interpret it correctly: pulse sequence and key sequence parameters, field strength, scanner vendor/model, temperature, in vivo / ex vivo / phantom status, anatomical region, subject demographics where relevant, and uncertainty (SD, IQR, CI, or N as available).

Every record cites back to the source paper at sub-document granularity (specific block, ideally specific sentence or table cell), and where the source itself cites an earlier paper as the origin of a value, the citation chain is followed and recorded so duplicates can be collapsed to their primary source.

The output is a queryable database that downstream consumers — including the `tissue-properties` package — can use as a curated, traceable knowledge base rather than a static YAML.

## Pipeline Overview

```
DOI
 │
 ▼
[Resolver] ──► chooses best available acquisition route
 │
 ├─► JATS XML  (PMC OA, Europe PMC, bioRxiv API)
 ├─► LaTeX     (arXiv source, when available)
 └─► PDF       (always available as fallback)
 │
 ▼
[Acquisition] ──► raw artifact persisted, content-addressed
 │
 ▼
[Extraction] ──► normalized Document
 │   • JATS    → parse XML directly
 │   • LaTeX   → latexml/pandoc
 │   • PDF     → GROBID (structure + bibliography) + Marker (tables)
 │
 ▼
[Reference resolution] ──► bibliography entries → DOIs / internal doc_ids
 │
 ▼
[Agent triad: Ingestor → Auditor → Corrector]
 │
 ▼
[Measurement records] ──► provenance-linked, queryable store
```

## Acquisition Routes

The resolver determines which route to use per DOI, in order of preference:

1. **JATS XML** — preferred when available. Gives ground-truth document structure: section hierarchy, parsed bibliography, structured tables, MathML equations. Sources:
   - PMC ID converter + OA Web Service for OA-licensed full text
   - Europe PMC `fullTextXML` endpoint (sometimes covers what NCBI does not)
   - bioRxiv / medRxiv API
   - CrossRef `link` field for publisher-deposited TDM XML (requires institutional access)

2. **LaTeX source** — preferred where available, primarily arXiv. Excellent for equation-heavy content; smaller slice of the corpus.

3. **PDF** — universal fallback. Always retrievable when the paper is accessible at all.

The resolver returns a structured `XMLAvailability`-style result rather than just a URL, recording the chosen source, license, and whether authentication is required.

For the MRI corpus specifically, a meaningful fraction of relevant work is published in Wiley journals (MRM, JMRI), where JATS exists but typically requires Crossref TDM tokens plus institutional subscription. PDF + GROBID will likely carry most of the workload; JATS is the bonus.

## Extraction

### Route 1: JATS XML
Parse XML directly into the normalized Document. Use JATS tables natively (publisher-structured cells are typically better than vision-model reconstruction). Fall back to Marker on the rendered PDF only when JATS tables are present-but-empty or malformed. Bibliography parsing is straightforward from `<ref>` elements.

### Route 2: PDF
- **GROBID** for document skeleton: section hierarchy, paragraphs, captions, parsed bibliography with author/year/title/journal extraction. Run with coordinate output enabled to get bbox provenance for text spans.
- **Marker** for tables: structured cells with bbox preservation. Tables are where most measurement values live; this slot must be reliable.

Nougat is explicitly rejected: hallucination on dense numeric tables and orphaned upstream maintenance make it disqualifying for a provenance-backed system.

### Route 3: LaTeX
latexml or pandoc to a structured intermediate, then to the normalized Document. Excellent equation fidelity.

### Figures
Captions are extracted in all routes. Figure-content extraction (reading axis labels or data points from pixels) is a known gap and not addressed in v1.

## Normalized Document Representation

A single canonical schema, regardless of acquisition route. Provenance fields tell us where each block came from; the type system does not bifurcate.

```python
@frozen
class Document:
    doc_id: str                        # hash of source artifact + DOI
    doi: str | None
    metadata: DocumentMetadata
    blocks: tuple[Block, ...]          # flat, ordered (linear reading order)
    references: tuple[Reference, ...]
    source: ExtractionProvenance       # route, tool versions, timestamp
    schema_version: str
```

**Blocks** are a tagged union: `TextBlock` (paragraph / heading / caption / list_item / abstract), `TableBlock` (cells with row/col spans, headers, footnotes), `FigureBlock` (caption + image reference, no pixel-level extraction), `EquationBlock` (MathML and/or LaTeX). Each block carries:

- `block_id` — stable within document
- `section_path` — e.g. `("Methods", "MR Acquisition")`, derived from heading hierarchy then flattened
- `provenance` — source kind (jats / grobid / marker / latex), page, bbox, xpath, char range, extractor confidence

**Inline references** are first-class within text blocks: `(char_range, ref_id, surface_form)`. This is what makes citation-chain queries possible and what the auditor uses to verify "value X is attributed to Smith 2010."

**References** are split into raw / parsed / resolved:
- `raw` — the bibliography entry as printed
- `parsed` — structured fields from JATS or GROBID
- `resolved` — DOI/PMID via CrossRef/PubMed lookup, plus internal `doc_hash` if we already ingested the cited paper

**Cells** carry their own provenance (cell-level bbox), enabling table-cell-level highlighting in any future review UI.

The full text bytes of a paragraph are stored as plain strings with InlineRef offsets, not as marked-up markdown — markdown round-trips lose offset precision.

### No merging across sources
When multiple extractors run on the same document (e.g. GROBID and Marker on a PDF), the canonical Document is produced by **one** route end-to-end. Other extractors' output is preserved as `AuxiliaryArtifacts` alongside the Document, queryable on demand by the agents but never silently merged in. This keeps every block's provenance to a single source.

The one narrow exception is **gap-filling**: if a block is present-but-empty in the canonical source (e.g. a JATS `<table-wrap>` with no cell content), it may be filled from the auxiliary artifact, with `source_kind="<route>_filled"` so it stays traceable. Never overwrite present-and-populated blocks.

## Storage

Three layers, separated by what they are:

### Layer 1 — Raw artifacts (immutable, content-addressed)
PDFs, JATS XML, LaTeX tarballs as fetched. Hash-addressed with two-level prefix sharding:

```
artifacts/pdf/sha256/ab/cd/abcd1234...ef.pdf
artifacts/jats/sha256/12/34/123456...ab.xml
artifacts/latex/sha256/...
```

Filesystem on the Debian server for the working set; Cloudflare R2 as canonical archive (mirroring the existing Zarr/R2 pattern).

### Layer 2 — Parsed Documents (versioned, derived)
Normalized Documents serialized as JSON (gzipped if size warrants), keyed by source-artifact hash and tagged with extractor + schema version:

```
documents/<doc_hash>/v_2026_05_a/
  document.json
  auxiliary.json
  manifest.json     # source artifact hashes, tool versions, config hash
```

Re-extraction with new tool versions writes a new version directory; old versions are retained until explicitly migrated. Disk is cheap; re-extraction is expensive in time and energy.

`cattrs` for serialization, with explicit structuring hooks for the tagged-union `Block` type from day one.

### Layer 3 — Index (mutable, queryable)
Postgres as the index over the on-disk parsed Documents. Holds:

- `documents` — DOI, doc_hash, title, year, status, version pointers
- `references_resolved` — citation chain edges (doc_hash → cited doc_hash)
- `extraction_events` — per-stage event log with tool versions and errors
- `measurements` — the extracted value records (see below), with FKs back to documents and source blocks

Postgres rows hold indexable fields only; the Document blob itself stays on disk. The index is rebuildable from the on-disk parsed corpus, so backups target Layer 2, not Postgres.

### What we do *not* build in v1
A vector store. The agents work over a known document, not over a corpus search. Embeddings become useful only when cross-corpus retrieval is needed; defer until then.

## Measurement Schema

The schema is the load-bearing decision and a sibling concept to `tissue-properties`. A measurement record represents a single reported value:

```python
@frozen
class Measurement:
    measurement_id: str
    quantity: QuantityKind              # T1, T2, T2*, PD, chi, ADC, ...
    value: Value                        # mean + uncertainty (SD/IQR/CI/N)
    unit: str                           # canonicalized (ms, s, ppm, ...)

    # Context — what's needed to slot the value correctly
    tissue: TissueRef                   # canonicalized (UBERON/FMA + free-text)
    in_vivo_status: Literal["in_vivo", "ex_vivo", "phantom", "in_silico"]
    field_strength_T: float
    temperature_C: float | None
    sequence: SequenceContext           # family + key parameters (TR, TE, TI, FA, ...)
    scanner: ScannerRef | None          # vendor, model
    subject: SubjectContext | None      # species, n, age, health status

    # Provenance
    source_doc: str                     # doc_hash
    source_block: str                   # block_id within Document
    source_span: tuple[int, int] | None # char range or cell coordinate
    citation_chain: tuple[str, ...]     # ordered doc_hashes back to primary source

    # Trust
    trust_state: Literal["proposed", "audited", "reconciled", "human_verified"]
    flags: tuple[str, ...]              # "anomalous_vs_prior", "cited_from_elsewhere", ...
    extraction_event_id: str
```

Controlled vocabularies vs free-text-with-canonicalization is decided per field: tissue uses UBERON/FMA where possible with a free-text fallback; quantity is a closed enum; sequence family is a closed enum but parameters are structured floats.

`citation_chain` is what makes "Wansapura 1999 propagation" detectable: if a measurement was found via Smith 2015 which cites Jones 2008 which cites Wansapura 1999 (which we have ingested), the chain `(smith_hash, jones_hash, wansapura_hash)` is recorded and downstream queries can collapse to the primary source.

## Agent Triad

Three agents, each with a defined role and an immutable event log of their actions.

### Ingestor
Reads a normalized Document and proposes Measurement records. Has tool access for: unit conversion, schema validation, controlled-vocabulary lookup (tissue canonicalization), table-cell retrieval by coordinate, surrounding-context retrieval. Tools are deterministic; the LLM does not do unit math or vocabulary matching itself.

Output: Measurement records in `trust_state="proposed"`, each linked to the block(s) and span(s) it was extracted from.

### Auditor
Independently re-extracts on the same Document, ideally with a different model family, and compares against the Ingestor's output. Independent re-extraction (rather than checking) catches a different class of errors and provides something close to inter-rater agreement as a free signal. Discrepancies surface as flags rather than overwrites.

Also performs citation-chain checks: when a measurement is attributed to a cited reference, verify the InlineRef exists and the cited paper is queued for ingestion if not yet present.

Output: `trust_state="audited"` (agreement) or flagged for the Corrector.

### Corrector
Resolves discrepancies. Allowed to *propose* a new candidate version, never to mutate existing records — the data model is append-only, with a derived "current best view" projection. Has an explicit human-queue exit for cases it cannot confidently reconcile.

Output: `trust_state="reconciled"` or `"human_verified"` after human review.

### Orchestration
Each agent action is idempotent and addressable by `(doc_hash, agent_version, prompt_version)`. Re-runs are safe; partial backfills work without coordination. A simple queue + worker pattern with idempotency baked into the data model is sufficient — no Temporal/Prefect required at this stage.

## Provenance and Citation Chain

Provenance is per-block at the Document level and per-measurement at the record level. For a given Measurement:

- `source_doc` + `source_block` + `source_span` → exact location in the parsed Document
- The block's own provenance → page, bbox, xpath in the original artifact
- → highlight-in-PDF (or highlight-in-XML) for any extracted value

The citation chain is built incrementally. When Reference resolution finds a DOI we have not yet ingested, it queues that DOI for ingestion. When ingestion completes, the new doc_hash is backfilled into all existing References that resolved to the same DOI — and into all Measurements whose chain includes that reference. The chain is never broken; missing links are simply unresolved until the cited paper is ingested.

## Evaluation

A hand-curated gold set — order of 50–200 papers extracted manually — is treated as a first-class deliverable, not an afterthought. Without it:

- Prompt and pipeline changes cannot be quantified as improvements
- The Auditor's value-add over the Ingestor alone cannot be measured
- Per-quantity / per-route extraction quality is invisible

The gold set is built before the agent triad goes into production use.

## Non-Goals (v1)

- Figure-pixel extraction (axis labels, plotted data points)
- Cross-corpus semantic search / embeddings
- Automated mutation of records (append-only only)
- Coverage of closed-access journals beyond what institutional subscriptions allow

## Open Decisions

- **Controlled vocabulary for sequence family** — closed enum is the goal, but the long tail of sequence variants in MRI literature is real. Start narrow (SE, GRE, IR-SE, MPRAGE, bSSFP, EPI, MRF families) and extend on demand.
- **Tissue canonicalization** — UBERON has good coverage for organs; subregions (deep brain nuclei, cortical layers) get patchier. Decide the fallback policy explicitly.
- **Wiley TDM access** — confirm whether the Würzburg library can issue a Crossref TDM token. This determines what fraction of MRM is reachable as JATS vs PDF-only.
- **Trust-state policy for downstream consumers** — `tissue-properties` should consume which trust states by default? Likely `reconciled` and `human_verified` only, but worth being explicit.

## First Build Targets

In order:

1. Resolver + Layer 1 storage. Given a DOI, fetch and persist the best raw artifact.
2. Route 1 extractor (JATS → Document) + round-trip test: serialize Document, deserialize, render to readable HTML, compare to original. The round-trip is the schema's first real test.
3. Route 2 extractor (PDF → GROBID + Marker → Document). Same round-trip test.
4. Reference resolution against CrossRef/PubMed.
5. Postgres index + extraction event log.
6. Gold set construction (in parallel with 1–5).
7. Ingestor agent against the gold set; measure extraction quality before adding the Auditor.
8. Auditor; measure value-add over Ingestor alone.
9. Corrector + human queue.