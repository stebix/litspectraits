# Measurement extraction — buildout sketch

Design-space map for the next large stage: **parsed `document.json` → citable
`Measurement` records** (T1, T2, T2\*, T1ρ, PD, χ, ADC, MTR, …) with the
context needed to interpret them and provenance back to a specific block /
sentence / table cell.

This is a route survey, not an implementation plan. It is subordinate to
`overview.md` (whole-project goals, the `Measurement` schema sketch, the agent
triad) and `overview-v3.md` (current ingest/extract state). Where this file
disagrees with those, they win and this file should be updated to match.

Status as of writing: the ingest path (`DOI → CrossRef → publisher dispatch →
credentialed retrieve → magic-byte validate → atomic commit`) and the
format-dispatched extractors (`extract/{pdf,jats,elsevier}.py`) are in. The
measurement stage is greenfield.

---

## 1. What we're starting from (and the gap)

Each extracted artifact yields `documents/<sha>/document.json` in **one of
three shapes**:

- **`extract/jats.py`** — `{front, sections[], tables[], figures[],
  references[]}`. Sections flattened with `path` (root-to-leaf section ids);
  `blocks` are paragraphs carrying `text` + `xrefs` (`rid` / `ref_type` /
  `label`). Tables are 2-D `cells` grids with `rowspan` / `colspan` preserved
  verbatim (not expanded), plus `caption`, `label`, `section_path`. Figures
  are caption-only. References carry `raw_text` + easy structured fields
  (authors, title, source, year, doi).
- **`extract/elsevier.py`** — same JATS-flavored dict shape, deliberately, so
  downstream stays publisher-agnostic.
- **`extract/pdf.py`** — `docling`'s native `export_to_dict()` (a
  `DoclingDocument` tree: `SectionHeaderItem` / `TextItem` / `TableItem` /
  `PictureItem` with `prov` = page + bbox + charspan). **A different shape
  from the XML extractors.**

So the first architectural fact: **there is no canonical normalized `Document`
yet.** `overview-v3.md` §11 deferred it on purpose ("the structured dicts are
rich enough that re-running through a normaliser later is cheap"). The work
described here *is* that work. Either every measurement route learns three
input shapes, or step zero is the normaliser that `overview.md` already specs:
`Document → blocks: tuple[Block, ...]` tagged union (`TextBlock` / `TableBlock`
/ `FigureBlock` / `EquationBlock`), per-block `provenance` (route, page, bbox,
xpath, char range), inline references as first-class `(char_range, ref_id,
surface_form)`, `cattrs` structuring hooks for the `Block` union from day one,
round-trip test (serialize → deserialize → render to readable HTML → diff) as
the schema's first real test.

What's missing for measurement extraction, regardless of route:

1. The normalized `Document` (above).
2. Char-offset spans inside block text, canonicalised so the same offsets mean
   the same thing to a verifier and to a future highlight UI. (JATS today has
   `xref` descriptors but **not** char ranges into the paragraph text.)
3. `xref → reference → DOI → internal doc_hash` resolution (needed for
   citation chains; JATS gives `rid`, the rest is a CrossRef-match post-pass).
4. The `Measurement` schema itself — attrs `@frozen` + `cattrs`, per-field
   "closed enum vs free-text-with-canonicalisation" decided. `overview.md`
   already sketches it; §3 below extends it.

---

## 1.5 Is the normaliser actually feasible? — the honest answer

Short version: **yes — but it is two easy adapters and one lossy one, not one
uniform transform, and the schema has to admit that out loud.** Pretending all
three routes yield equivalent `Document`s is the one way this goes wrong.

### What the three shapes actually are

| Route | Source format | Shape today | Section hierarchy | Inline refs | Tables | Bibliography | Geometry |
|---|---|---|---|---|---|---|---|
| `extract/jats.py` | JATS XML (Springer Nature TDM) | our dict: `{front, sections[], tables[], figures[], references[]}` | explicit `<sec>` nesting, already flattened to a list with `path` (root-to-leaf ids) | `<xref rid ref-type label>` descriptors per `<p>` — **`rid` resolves to `references[id==rid]`**, but **no char offset into the paragraph text yet** | HTML model (`tr`/`th`/`td`), `rowspan`/`colspan` kept **verbatim, unexpanded** | structured: `raw_text` + authors/title/source/year/doi from `<element-citation>` | none — XML has no page/bbox; provenance is xpath-shaped |
| `extract/elsevier.py` | Elsevier CEP XML (`view=FULL`) | **the same dict**, deliberately (`docs/overview-v3.md` §11) | `<ce:section>` nesting, flattened identically | `<ce:cross-ref refid>` → surfaced as `rid` (publisher attr name hidden); CEP has no `ref-type` analogue (`None`); again **no char offset** | CALS/OASIS model (`row`/`entry`) projected to the same grid; `morerows`→`rowspan` exact, `namest`/`nameend`→`colspan` **heuristic** (trailing-digit parse of `col<N>`) | structured: `<ce:bib-reference>` → same fields | none |
| `extract/pdf.py` | PDF (Wiley TDM, or sideload) | **docling's `export_to_dict()` verbatim** — `DoclingDocument`: flat `texts[]` / `tables[]` / `pictures[]` + a `body` tree of `self_ref`/`parent`/`children`; each item has `label` (`section_header` / `text` / `caption` / `list_item` / …), `text`, `prov` = page + bbox + charspan | **implicit** — `section_header` items in reading order carry a `level`; the section *path* must be reconstructed by running a stack over heading levels (or walking `body`) | **none structured** — "[12]" is just characters in `text`; docling may emit `RefItem` cross-refs but does not resolve them to a bibliography | `TableItem.data`: a **dense** grid with per-cell `row/col span` + `*_offset_idx` — but it is a **vision-model reconstruction** with its own error modes (the `PARTIAL_SUCCESS` gate catches the worst, subtle misreads survive) | **none structured** — references come out as plain `text` items, maybe under a "References" heading; needs a separate parsing pass (GROBID / anystyle / refextract / LLM) | per-item page + bbox + charspan — the *best* of the three for a highlight UI |

### Reading the table

Two routes (≈ the whole corpus, given the publisher mix is Wiley+Elsevier+Springer
and only Wiley returns PDF) are already 90 % normalised — the Elsevier extractor
*deliberately* emits the JATS dict shape, so "normalise" there means:

1. rename keys into the `attrs` models (`sections[]`→`blocks[]` flattened to a
   single ordered tuple, `section_path` derived once);
2. **attach char offsets to the inline refs** — the one genuinely missing piece,
   and it is mechanical: `full_text()` already concatenates text-with-tails in
   document order, so re-walking the `<p>` and tracking cumulative length gives
   `(start, end)` for each `<xref>`/`<cross-ref>` for free;
3. wrap, run `cattrs` un/structure hooks, done.

That is **deterministic, pure, unit-testable, low-risk**. It is the E0.5 the doc
already calls for, and it can land first and unblock E2–E5 on the
Springer/Elsevier slice immediately.

The PDF route is also feasible **as a structural transform** — heading-level
stack → `section_path`, `body`-tree order → block order, `prov` → `provenance`,
`TableItem.data` → `TableBlock.cells` — but it **cannot manufacture information
docling never extracted**:

- **structured bibliography** — absent; PDF-route `Document.references` stays
  `raw`-only until a citation-parsing pass runs (this is *already* the plan —
  `overview.md` splits references into raw/parsed/resolved precisely because the
  PDF route needs GROBID-or-equivalent for `parsed`);
- **resolved inline-ref ids** — `InlineRef.ref_id` is frequently `None` for
  PDF-route blocks; you can regex the bracketed marker (char range is trivial)
  but binding "[12]" → a `Reference` needs the parsed bibliography above plus a
  marker-style match — a downstream pass, not the normaliser's job;
- **table footnote roles** — docling doesn't model them; footnote text lands as
  `text` items near the table, not as `TableBlock.footnotes`;
- **MathML/LaTeX equations** — JATS/CEP carry MathML inline; docling now runs
  `do_formula_enrichment=True` (`docling-settings-buildout.md` §1.2 →
  `extract-pdf-plan.md` §4), so equations are recovered as first-class items on
  the PDF route too rather than falling out as stray `text` / `picture` items —
  `EquationBlock` is populated for every route. (The earlier state, before that
  knob was set, was: XML routes populated, PDF degraded-to-empty.) The standing
  "pick docling settings once and freeze" decision lives in
  `docling-settings-buildout.md`.

### So the schema commitment that makes it honest

`overview.md` already says the right thing — "provenance fields tell us where
each block came from; the type system does not bifurcate." Keep that. But add:
the *type* doesn't bifurcate, the **population of optional fields does**, and the
schema must say which-by-route, not leave a reader guessing whether a `None`
means "the paper had none" or "this route can't see it":

- `Block.provenance` is a union where `page`/`bbox` are PDF-route-only and
  `xpath` is XML-route-only — both legitimately `None` on the other route;
  carry a `route: Literal["jats", "elsevier", "docling"]` discriminator so the
  meaning of every `None` is unambiguous.
- `InlineRef.ref_id: str | None` — `None` is *normal* for docling-route blocks,
  not a bug; a `resolved: bool` or `ref_id_source` field stops the citation pass
  from re-resolving what JATS already gave for free.
- `Reference` keeps the raw/parsed/resolved split from `overview.md`; PDF-route
  refs are raw-only at normalise time, by design.
- Optional: a per-`Document` `completeness` summary (`has_structured_refs`,
  `has_inline_ref_ids`, `has_equations`, `table_source ∈ {publisher, tableformer}`)
  so a downstream consumer — or the table sub-pipeline's confidence gate — can
  branch on route quality without re-deriving it.

### What the round-trip test can and can't be

`overview.md` §"order of work" calls for "serialize → deserialize → render to
readable HTML → diff" as the schema's first real test. Be precise about the
*diff target*, because you can't diff against the original XML (formatting and
namespace cruft are deliberately dropped):

- the achievable, meaningful version is a **golden-snapshot** test — render to
  HTML, freeze one golden HTML per fixture, diff future runs against it; plus
- a **no-text-loss invariant** that needs no golden: every character of every
  `text` field in the source dict appears in the assembled `Document` (modulo a
  declared whitespace-normalisation rule), and every `xref`/`cross-ref` label
  appears at its claimed char span. That invariant is route-agnostic and is the
  thing that actually protects you against a normaliser bug silently dropping a
  sentence — which would silently drop a measurement.

### Bottom line for the build order

E0.5 splits cleanly: **E0.5a — XML→`Document`** (JATS + Elsevier; mechanical,
land it first, unblocks the bulk of the corpus) and **E0.5b — docling→`Document`**
(structural transform + the explicit nullable-field discipline above; can follow,
since the PDF slice is both the minority and the one whose lower fidelity the
schema now represents honestly). Neither is research; the only real *work* is
the char-offset re-walk (small) and resisting the temptation to paper over the
PDF route's gaps with heuristics that would let a `None` lie.

---

## 2. Where the values actually live (this shapes everything)

In rough order of yield and difficulty:

| Locus | Yield | Difficulty | Notes |
|---|---|---|---|
| **Relaxometry tables** (tissue × field strength × {T1, T2, T2\*, PD, χ, ADC}) | Highest | Highest | Multi-level headers, units in caption *or* header *or* cell, `mean ± SD` vs split columns, `—` / `n.d.` / `n/a`, footnote markers, transposed layouts, abbreviated tissue names with a legend footnote, ranges `1.5–3.0`, composite cells `1084 ± 45 (n=12)`. **The make-or-break sub-pipeline.** |
| **Results / Discussion prose** | High | Medium | "Frontal white matter T1 was 1084 ± 45 ms at 3 T (n = 12)." Finding the value is easy; *binding context* is the hard part. |
| **Abstract** | Medium | Low–Medium | Headline numbers; also where review/meta papers re-report others' values → citation-chain hazard. |
| **Methods** | (context, not values) | Medium | Field strength(s), scanner vendor/model, sequence family + TR/TE/TI/FA, temperature, in vivo / ex vivo / phantom, n, demographics. One paper commonly has **1–3 distinct acquisition setups**. |
| **Figure captions** | Low | Low | Occasionally "(T1 = 900 ms)"; pixel-level figure reading is an explicit v1 non-goal. |
| **Cited sources** | — | — | "T1 values were adopted from Wansapura et al. [12]" — must be detected and routed to `citation_chain`, not treated as a primary measurement. |

Two consequences:

- **The table path and the prose path want different machinery** — treat them
  as two sub-pipelines with separate evals.
- **A two-pass decomposition is nearly forced**: Pass 1 extracts the small set
  of `AcquisitionContext` objects from Methods (+ table headers, which can
  override); Pass 2 finds values and binds each to one context id. This stops
  the system from silently guessing "3 T" when the paper used both 1.5 T and
  3 T.

---

## 3. Representing "multiple values for the same species / scanner / modality"

A paper that reports many values for one tissue under one acquisition setup is
the normal case, not the exception. Different-looking-on-the-page,
different-as-records:

1. **Per-subject values** — T1 of WM for each of 12 subjects → 12 rows.
2. **Sub-region values** — "white matter" as frontal / parietal / splenium /
   genu → different (more specific) `tissue`, not really "the same species".
3. **Test–retest / repeated acquisitions** — same subjects, two sessions →
   same context, different `session`.
4. **Multiple post-processing methods on the same raw data** — mono- vs
   bi-exponential T2 fit → same data, different `analysis_method`.
5. **A summary stat** — mean ± SD over N → one row,
   `aggregation_level="summary"`, `Value` carries mean + (SD | IQR | CI) + n.
6. Often a paper gives **both #1 and #5** → built-in redundancy that must not
   be double-counted.

### Marking the grouping — three keys, all in the spirit of the existing design

- **`AcquisitionContext` id** (first-class, see §5.2) — *this is* the "same
  scanner, same modality, same field strength, same sequence" key. Every
  measurement points at one. Multiple values sharing a context id ⇒ same
  acquisition setup, automatically.
- **`cohort_id`** (a.k.a. `sample_id` / `measurement_group_id`) — bundles the
  records the *paper itself* treats as one population/experiment. Finer than
  the context (one context can host several cohorts); this is what a
  meta-analysis would `GROUP BY`. Assigned at extraction.
- **`aggregation_level: Literal["individual", "summary", "pooled_estimate"]`**
  — so a query can pick exactly one level and never mix per-subject rows with
  a mean-over-N row.
- **`derived_from: tuple[measurement_id, ...]`** (or a `redundancy_group_id`)
  on a summary row that is the aggregate of listed individuals. The Auditor
  gets a free check: recompute mean/SD from the individuals, flag on
  disagreement. Aggregation queries pick one level per redundancy group.

Provenance stays per-record regardless — each individual value still cites its
own cell / sentence; the group keys are *additional*, not a replacement.

### Should there be a database that facilitates post-hoc aggregation? — Yes, and it already exists in the plan

It is the index layer (`overview.md` Layer 3 / the Postgres `measurements`
table), not a new component. The design commitment that makes it work:
**append-only at ingest; aggregation is a derived projection.** Store every
reported value at the granularity the paper used, tag with `context_id` /
`cohort_id` / `aggregation_level` / `tissue` / `field_strength_T`, index those
columns, and "the T1 of frontal WM at 3 T across the corpus" is a `GROUP BY`
query — computed on demand, recomputed when new papers land, never frozen.
Pre-aggregating at ingest would (a) lose the ability to re-pool when the tissue
ontology or quality filters change, and (b) fight the append-only model. So the
only thing the *extraction* schema owes the index is the grouping / granularity
fields so `GROUP BY` has something to group on.

Open decision: **what's the default row a downstream consumer sees?**
`tissue-properties` probably wants `summary` / `pooled_estimate` rows by default
and treats `individual` rows as raw material it can re-pool — but write that
down, same as the trust-state default.

---

## 4. The fundamental fork: deterministic vs LLM vs hybrid

### Route A — pure rule-based (regex / NER / deterministic table parsing)

Pattern-match `<number> <unit>` near a quantity keyword; deterministic
header-inference on tables; gazetteer for tissues/sequences.

- **Pros:** deterministic (append-only model loves this), fast, ~free,
  provenance trivial (you have the char span), zero hallucination.
- **Cons:** brittle against the heterogeneity of MR literature; the
  synonym/abbreviation tail is brutal ("longitudinal relaxation time",
  "spin-lattice relaxation", "T1 relaxation", "$T_1$", and T1ρ is *not* T1);
  context binding across Methods → Results is essentially unsolvable with
  rules; table layouts defeat header heuristics constantly.
- **Verdict:** not viable as the *primary* route, but valuable as **(a) a
  cheap recall net / candidate generator** and **(b) the eval baseline** any
  LLM route must beat. Build it — `ValueCandidate(block_id, char_span,
  raw_value, raw_unit, quantity_guess, context_cues)` — even if only as
  scaffolding.

### Route B — LLM-driven extraction (the agent triad in `overview.md`)

Feed the LLM the normalized document (or relevant slices) + the `Measurement`
schema as a structured-output target; it emits records with provenance
pointers; deterministic *tools* handle unit conversion, vocab
canonicalisation, table-cell retrieval by coordinate, plausibility lookup —
the LLM never does unit math or ontology matching itself (already the
`overview.md` design).

- **Pros:** handles heterogeneity; binds context across sections; can reason
  about "as previously reported [12]"; one mechanism for tables + prose +
  captions.
- **Hazards:**
  - **Hallucinated numbers** — the cardinal sin for a provenance-backed
    corpus. A model that emits `1084` when the paper said `1048` (or invents a
    plausible value) silently corrupts the knowledge base.
  - **Fabricated provenance pointers** — `source_block` / `source_span` that
    don't actually contain the value.
  - **Table serialisation fidelity** — how you flatten a 2-D spanned grid into
    the prompt determines whether the model reads column 4 against header 4.
  - Long papers blow context windows; "lost in the middle" on the ones that
    fit.
  - Non-determinism vs the append-only model (re-runs propose slightly
    different records).
- **Required mitigations** (non-optional given the project's fail-loud ethos):
  - **Verbatim-anchor verification gate** — every emitted value string MUST be
    a verbatim substring of its cited `source_block` (prose) or equal to
    `cells[r][c].text` after canonicalisation (table). Failure ⇒ **reject
    loudly** (`ExtractionAnchorError`), don't flag. Converts hallucination
    from silent corruption into a typed failure.
  - **Plausibility-range checks** per (quantity, field strength, tissue class)
    — WM T1 @ 3 T ≈ [700, 1200] ms; GM T2 @ 1.5 T ≈ [80, 110] ms; χ of
    deep-gray nuclei in ppm; ADC in ×10⁻³ mm²/s; etc. Out-of-range ⇒
    `flags += "implausible"`, surface to Auditor, never auto-drop (real
    outliers exist).
  - **Independent re-extraction (Auditor)**, ideally a different model family —
    something like inter-rater agreement for free; discrepancies become flags,
    never overwrites.
  - **Constrained decoding** to the JSON Schema derived from the `Measurement`
    attrs model.
- **Verdict:** this is the engine — but never standalone. It sits between a
  deterministic candidate layer (recall + grounding) and a deterministic
  verification layer (anti-hallucination).

### Route C — hybrid (recommended)

Composable flavours; combine the first two, add the third for tables:

- **C1 — deterministic spotter → LLM binder.** The spotter (Route A) finds
  *every* `<number> <unit>` near a quantity keyword and hands the LLM
  candidates. The LLM's job shrinks from "find numbers in this paper" to "for
  these N candidates, decide which are real tissue-property measurements, bind
  tissue / field strength / sequence / status / n, link any citation, or
  reject." Much smaller hallucination surface — the model chooses and labels
  rather than generating numerics. It may still *add* a few candidates the
  spotter missed; those go through the same anchor gate.
- **C2 — LLM extractor → deterministic verifier.** The anchor gate + unit
  canonicaliser + plausibility ranges + schema validation, run on *every*
  record before it leaves `proposed`.
- **C3 — table-specialised path.** Tables deserve their own pipeline:
  (1) deterministic grid normalisation — resolve spans into a dense matrix,
  classify which rows are headers (multi-level), extract footnotes/legend;
  (2) an LLM (often a *cheap* one suffices) does semantic labelling — "row =
  tissue, col-group = field strength, cell = T1 in ms, footnote a =
  'frontal'"; (3) emit one `Measurement` per data cell, each anchored to
  `(table_id, r, c)`. The JATS/Elsevier extractors already hand over clean
  cell grids with spans — a real advantage over the PDF path where docling's
  TableFormer output is itself lossy and needs a confidence gate.

Defensible decision rule: **C1 + C2 for prose/captions; C3 + C2 for tables;
Route A always-on as recall net + eval baseline; Route B's full agent triad as
the orchestration around it.**

---

## 5. Cross-cutting design choices (independent of the A/B/C fork)

### 5.1 Granularity of the extraction unit
- **Whole-paper-in-one-prompt** — simplest, full context, but token cost +
  lost-in-middle on long MRM papers.
- **Section-by-section with a shared "context card"** — extract Methods →
  `AcquisitionContext[]` first, pass that card into each Results-section call.
  Scales, small prompts, explicit context binding. **Recommended default.**
- **Block-by-block with retrieval** — each unit fetches relevant Methods
  context on demand. Most surgical, most plumbing.

### 5.2 Model `AcquisitionContext` as first-class
Don't fold field strength / sequence / scanner / status into each `Measurement`
independently — extract a small list of contexts per paper, give each an id,
**force every measurement to reference exactly one**. Under-determined values
then surface as `flags += "context_ambiguous"` instead of being silently
assigned a guess. Table column headers ("3 T") can refine/override the
doc-level default for cells under them.

### 5.3 The verification layer is the spine, not a feature
`verify(measurement, document) -> Ok | AnchorError | UnitError | RangeWarning`
runs on *100 %* of records before `proposed`. Hard fail on anchor/unit;
warn-and-flag on range. Single highest-leverage piece of code in the buildout
— pure, deterministic, testable, route-agnostic. Build it early.

### 5.4 "Is this a measurement at all?" gate
`TR = 500 ms`, `TE = 12 ms`, `slice thickness 3 mm`, `b = 1000 s/mm²` are
*acquisition parameters*, not tissue-property measurements — they belong in
`SequenceContext`, not as `Measurement` rows. The quantity enum + a
classification step keeps them out of the measurement table. Conversely, a
review/meta paper's body may be *entirely* re-reports — a per-paper "does this
present original measurements?" judgment (cheap LLM call or heuristic) routes
those papers' values straight into citation-chain mode.

### 5.5 Citation-chain hooks
Even if full chain resolution is downstream, the schema carries
`citation_chain: tuple[str, ...]` and `flags` (`"cited_from_elsewhere"`) **from
day one** (`overview.md` already does this). The extractor's job: detect the
nearby `xref` / inline-ref and the linguistic cue ("adopted from", "as
reported by", "see ref"), record the surface ref. Resolution (ref → DOI →
internal doc_hash, backfilled when the cited paper is ingested) is a separate
pass.

### 5.6 Schema-first, structured decoding
`Measurement` + `Value` (mean + SD/IQR/CI/N) + `TissueRef` + `SequenceContext`
+ `ScannerRef` + `SubjectContext` + `AcquisitionContext` as attrs `@frozen`;
`cattrs` un/structure hooks; export JSON Schema; use it for (a) constrained LLM
output and (b) post-hoc validation. Per-field vocab policy: `quantity` closed
enum (T1, T2, T2\*, T1ρ, PD, χ, ADC, MTR, …); `unit` free-text-in →
canonicalised-out (ms/s, 1/s, ppm, ×10⁻³ mm²/s); `tissue` UBERON/FMA where it
reaches + free-text fallback (deep-brain nuclei / cortical layers get patchy —
decide the fallback policy explicitly); `sequence.family` small closed enum
(SE, GRE, IR-SE, MPRAGE/MP2RAGE, bSSFP, EPI, MRF…) + structured float params +
free-text variant.

### 5.7 Cost / model routing
Most papers in a broad MRI corpus contain *no* relaxometry table. Put a **cheap
relevance gate** in front (does this doc mention T1/T2/relaxometry/quantitative
-MR + numbers?) so the expensive Opus-class extractor only runs on the ~20–40 %
that matter. Cheap model for the spotter and table-labelling; expensive model
for prose binding + the Auditor (different family). Deterministic tools for
everything quantitative.

### 5.8 No merging across sources (same discipline as the doc layer)
`overview.md` already commits: one canonical extraction route produces the
`Document`; auxiliary extractors are preserved separately, never silently
merged (sole exception: gap-filling an empty-but-present block, tagged
`source_kind="<route>_filled"`). The measurement layer inherits this: one route
produces the canonical `Measurement` set; the Auditor's independent run is
**comparison-only** (flags); the append-only event log means corrections
*propose new versions* rather than mutate. Any caching / "current best view" is
a derived projection over the event log.

### 5.9 The gold set is a step-1 deliverable, not an afterthought
You cannot choose between Route A/B/C, can't measure the Auditor's value-add,
can't see per-quantity quality — without ~50–200 hand-curated papers' worth of
records. Start it **now, in parallel** with everything. A tiny annotation tool
(human confirms/corrects LLM-proposed records — active-learning-flavoured)
bootstraps it fast. Metrics: per-quantity P/R on `(value, unit, tissue,
field_strength)` tuples; "exactly right" vs "value right / context wrong";
anchor rate; hallucination rate (values not in source); table-cell coverage.

---

## 6. A possible build order (mirrors the project's "order of work" style)

- **E0** — `Measurement` / `Value` / `TissueRef` / `SequenceContext` /
  `AcquisitionContext` attrs models + `cattrs` hooks + JSON Schema export +
  per-quantity plausibility-range tables. Pure data, no I/O. Green on
  pytest/ruff/pyright.
- **E0.5a** — XML→`Document`: jats / elsevier dicts → canonical `Document`
  with `Block` tagged union, per-block provenance, **char-offset inline refs**
  (the one missing piece — a cumulative-length re-walk of each `<p>`). Lands
  first; unblocks E2–E5 on the Springer+Elsevier slice (most of the corpus).
  No-text-loss invariant + golden-HTML snapshot as the schema's first tests
  (§1.5).
- **E0.5b** — docling→`Document`: heading-level stack → `section_path`,
  `body`-order → block order, `prov` → provenance, `TableItem.data` →
  `TableBlock.cells`; nullable-by-route fields made explicit (`route`
  discriminator, `ref_id` `None`-is-normal, `references` raw-only, equations
  degraded). Can follow E0.5a.
- **E1** — provenance/verification primitives: char-offset canonicalisation,
  `verify_anchor()`, unit canonicaliser, range checker. Route-agnostic,
  heavily unit-tested.
- **E2** — deterministic candidate spotter (Route A): regex + light NER over
  prose + table cells → `ValueCandidate`s. Doubles as eval baseline.
- **E3** — table sub-pipeline (C3): grid densification + header/footnote
  classification + (cheap-LLM-or-heuristic) semantic labelling → anchored
  `Measurement`s. Highest yield; its own eval.
- **E4** — context-resolution pass: Methods → `AcquisitionContext[]`.
- **E5** — prose extraction pass (C1 + C2): LLM binds candidates →
  measurements against contexts, through the E1 verification gate. (Postgres
  index + extraction event log lands around here.)
- **E6** — gold set + eval harness (started at E0, formalised here). Route
  bake-off: A vs C3 vs C1 + C2, per quantity.
- **E7** — Ingestor agent proper (tools wired) → measure quality → add Auditor
  (different family) → measure value-add → Corrector + human queue.
- **E8** — citation-chain resolution (ref → DOI → doc_hash, backfill on
  ingest) + collapse-to-primary-source dedup.

---

## 7. Top risks, and the design move that contains each

| Risk | Containment |
|---|---|
| Hallucinated numeric values | Verbatim-anchor gate on 100 % of records — reject, don't flag |
| Fabricated provenance pointers | Same gate (anchor must resolve to a real block/cell + span) |
| Silent context mis-binding (wrong field strength/tissue) | First-class `AcquisitionContext` + mandatory ref + `context_ambiguous` flag + plausibility ranges |
| Table-structure misreads | Separate table sub-pipeline + eval; ingest footnotes/legends; resolve spans; confidence gate on docling tables |
| Unit confusion (ms↔s, 1/s↔ms, ppm↔ppb) | Deterministic canonicaliser + range checks catch most |
| Acquisition params (TR/TE) mistaken for measurements | Quantity closed-enum + "is this a tissue-property measurement?" classifier |
| Re-reported values treated as primary | Per-paper "original measurements?" gate + citation-chain detection |
| Double-counting individuals + their summary | `derived_from` / `redundancy_group_id`; aggregation picks one level; Auditor recomputes |
| Long-paper context overflow | Two-pass (context card) + per-section/per-table chunking |
| Re-run non-determinism vs append-only model | Append-only event log (propose new versions); deterministic verifier as the stable contract; pin model+prompt versions in `extraction_event` |
| Cross-source contamination | One canonical extraction route per `Document`; Auditor comparison-only; gap-fill is the only exception, tagged |

---

## 8. Open decisions worth pinning before committing

1. **Build the canonical normalized `Document` now (E0.5), or keep extracting
   against three dict shapes?** Leaning: build it now — §1.5 argues it is
   feasible and low-risk (two mechanical XML adapters + one lossy docling
   adapter), and it is the only thing that stops every measurement route from
   re-learning three shapes. Open sub-question: do E0.5a (XML) and E0.5b
   (docling) land together, or does the XML half ship first and the PDF slice
   catch up? (Doc leans: ship E0.5a first.)
2. **Primary extraction engine** — hybrid C1 + C2 / C3 (recommended), straight
   to a full LLM agent (Route B), or invest first in the deterministic spotter
   (Route A) to set a hard baseline?
3. **Granularity** — two-pass context-card (recommended), whole-paper, or
   block-by-block-with-retrieval?
4. **Tissue canonicalisation fallback policy** — UBERON/FMA + free-text is the
   sketch; the deep-gray-nuclei / cortical-layer tail needs an explicit rule.
5. **Sequence-family enum scope** — start narrow (SE, GRE, IR-SE,
   MPRAGE/MP2RAGE, bSSFP, EPI, MRF) and extend on demand, or invest up front?
6. **Where Postgres enters** — at E5 (index over on-disk records) or later; the
   on-disk append-only event log is the source of truth either way.
7. **Default trust state / aggregation level for downstream consumers** —
   `tissue-properties` consumes which `trust_state` (likely `reconciled` +
   `human_verified`) and which `aggregation_level` (likely `summary` /
   `pooled_estimate`, with `individual` as re-poolable raw material)?
8. **Eval-first cadence** — spend the first chunk of effort on a 50–200-paper
   gold set before the bake-off, or build the spotter + table path first and
   curate against their output?
