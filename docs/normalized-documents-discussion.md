# Normalized `Document` — design + testing discussion

A working note. Two questions, taken in order:

1. How can a normalized `Document` model work across the three heterogeneous
   sources `{JATS XML (Springer Nature TDM), Elsevier CEP XML, docling-dict
   from Wiley PDF}`, and is it actually needed?
2. How do we test the docling-PDF route through that normaliser when simple
   fixtures plainly miss the failure modes — and the downstream metric we care
   about is extractor truthfulness on relaxometric values, not document
   normalisation per se?

This doc is subordinate to `agentic-buildout-sketch.md` (esp. §1.5, §5.3, §5.9
and the §8 open-decision list) and `docling-settings-buildout.md` (esp. §0,
§1.2, §1.3, §3); where it disagrees with those, they win and this doc should
be updated to match.

---

## Part 1 — The normalized `Document`

### 1.1 Framing nit that actually matters

It is **not** "three heterogeneous XML sources." It is **two XML routes that
already share a shape, plus one non-XML route**:

- `extract/jats.py` — JATS XML (Springer Nature TDM) → our dict
  `{front, sections[], tables[], figures[], references[]}`.
- `extract/elsevier.py` — Elsevier CEP XML (`view=FULL`) → *the same dict*, on
  purpose (`overview-v3.md` §11), so downstream stays publisher-agnostic.
- `extract/pdf.py` — Wiley PDF → docling's `export_to_dict()` verbatim — a
  `DoclingDocument` tree (`texts[]` / `tables[]` / `pictures[]` + a `body`
  tree), **not XML**, and the only genuinely different shape.

So the real problem is "**one publisher-dict shape (2 routes) + docling's
native shape (1 route)** → one `Document`," which is materially more
tractable than "three arbitrary schemas."

### 1.2 Is it needed? — Yes, and not for the obvious reason

The DRY argument ("every measurement route otherwise re-learns the input
shapes") is the weak one. The load-bearing argument is the **verbatim-anchor
gate** (`agentic-buildout-sketch.md` §5.3, and the whole reason
`docling-settings-buildout.md` §1.3 frets over `force_backend_text`): every
emitted value string MUST be a verbatim substring of its cited `source_block`
at a claimed char span, or the record is rejected loudly. That requires:

1. block text that means the same thing to the extractor and the verifier
   (canonicalised whitespace, one assembly rule), and
2. char offsets *into that text* — which JATS gives `xref` descriptors but **no
   char ranges** today, and docling gives charspans against page text, not
   against your block text.

You cannot build the anti-hallucination spine without a normalised
block-with-canonical-offsets representation. **That is what forces E0.5,** more
than any tidiness concern. `agentic-buildout-sketch.md` §8 open-decision #1
leans the same way ("build it now").

### 1.3 Target shape

Straight from the sketch, consistent with `overview.md`:

```
Document
  blocks: tuple[Block, ...]            # ordered; Block = TextBlock | TableBlock | FigureBlock | EquationBlock
  references: tuple[Reference, ...]    # raw / parsed / resolved split
  # each Block carries:
  #   provenance: route + (page,bbox) | (xpath) + char range          ← route discriminator is mandatory
  #   section_path: tuple[str, ...]
  #   inline_refs: tuple[InlineRef, ...]
  #     where InlineRef = (char_range, ref_id|None, surface_form, ref_id_source)
```

### 1.4 Adapter A — XML → `Document` (jats + elsevier): mechanical, lands first

1. **Rename into the attrs models** — `sections[]` (already flattened with
   `path`) → one ordered `blocks` tuple; `section_path` derived once; tables'
   verbatim `cells` grid → `TableBlock.cells` (spans stay unexpanded, as
   today).
2. **Attach char offsets to the inline refs** — *the one genuinely missing
   piece*, and it is a small deterministic re-walk: `full_text()` already
   concatenates text-with-tails in document order, so walking each `<p>`
   tracking cumulative length yields `(start, end)` for every `<xref>` /
   `<ce:cross-ref>` for free. JATS `rid` resolves into
   `references[id==rid]` (so `ref_id` is populated, `ref_id_source="jats"`);
   Elsevier CEP has `refid` but no `ref-type` analogue (`None`, that is
   normal).
3. Wrap in `@frozen` attrs, register `cattrs` un/structure hooks for the
   `Block` union from day one, done.

Pure, unit-testable, low-risk. Unblocks the measurement stage on the
Springer + Elsevier slice — which, given the publisher mix, is most of the
corpus.

### 1.5 Adapter B — docling dict → `Document` (Wiley PDF): structural transform + an honesty discipline

The structural part is straightforward:

- heading-level stack over `section_header` items (or a `body`-tree walk) →
  `section_path`
- `body`-tree order → block order
- `prov` (page + bbox + charspan) → `provenance` — actually the *best*
  geometry of the three for a future highlight UI
- `TableItem.data` (dense grid, per-cell span + offsets) → `TableBlock.cells`

What it **cannot manufacture**, and the schema must not let a `None` lie about
which case it is in:

| Field | XML routes | docling route |
|---|---|---|
| structured bibliography | populated from `<element-citation>` / `<ce:bib-reference>` | **raw text only** until a citation-parsing pass (GROBID / anystyle / refextract / LLM) runs — already the plan; that is why `Reference` has raw / parsed / resolved |
| inline `ref_id` | resolved (JATS) / present (Elsevier) | **`None` is normal** — "[12]" is just characters; regex the marker for the char range, but binding it to a `Reference` is a downstream marker-match pass |
| table footnote roles | modelled (`th` / `td` + caption / label) | not modelled — footnote text lands as nearby `text` items |
| equations | MathML inline | now recovered as first-class items because `do_formula_enrichment=True` landed (`docling-settings-buildout.md` §1.2) — was the degraded-to-empty case before that knob |

The schema commitment that makes the normaliser **honest** (sketch §1.5, and
the part most likely to be done wrong if rushed):

- **`Block.provenance` carries `route: Literal["jats","elsevier","docling"]`** —
  then `page` / `bbox` being `None` on an XML block, or `xpath` being `None`
  on a docling block, is unambiguous rather than suspicious.
- **`InlineRef.ref_id: str | None` with a `ref_id_source`** — so the citation
  pass does not re-resolve what JATS already handed over for free, and
  `None`-on-docling reads as "expected," not "bug."
- **`Reference` raw-only on the docling route at normalise time, by design.**
- Optional but cheap: a per-`Document` `completeness` block
  (`has_structured_refs`, `has_inline_ref_ids`, `has_equations`,
  `table_source ∈ {publisher, tableformer}`) so the measurement layer's
  confidence gate can branch on route quality without re-deriving it.

The type system does **not** bifurcate (one `Document`, one `Block` union);
the *population of optional fields* does, and the `route` discriminator is
what keeps that legible.

### 1.6 Bottom line for Part 1

- **Needed?** Yes — primarily because the verbatim-anchor verification gate
  cannot exist without canonical block text + offsets, secondarily for not
  re-learning input shapes in every measurement route.
- **Feasible?** Yes — split as **E0.5a (XML → `Document`)**: two adapters over
  an already-shared dict shape, the only new code being the cumulative-length
  offset re-walk; lands first, unblocks the corpus majority. **E0.5b (docling
  → `Document`)**: a structural transform that follows, gated on adding the
  nullable-by-route discipline above. Neither is research.
- **The one trap:** papering over the docling route's real gaps (no
  structured refs, no resolved ref ids, no table footnote roles) with
  heuristics that make a `None` lie. Better a `route` discriminator and an
  honest `completeness` summary than a uniform-looking `Document` that is
  not.

One small follow-on if E0.5b lands: the docling-settings
`force_backend_text` decision (`docling-settings-buildout.md` §1.3) becomes
*more* pointed once E0.5b exists, because that knob determines whether
docling-route block text is faithful enough for the anchor gate — worth
deciding it on the same gold-set fixtures that bless the golden-HTML
snapshots.

---

## Part 2 — Testing the docling → `Document` route

The concern, sharpened: the PDF route is the lossy one; simple fixtures will
not exercise its failure modes; and the downstream metric is extractor
truthfulness on relaxometric values, not document normalisation. So how do
we get correctness — or at least loud-failure — on this layer?

The resolution is to notice the question is actually **two questions** that
need different instruments, and to let the metric reframe drive the test
design backwards.

### 2.1 Two questions, kept apart

**Q1 — transform faithfulness.** Does `normalise(docling_dict)` carry
through *everything docling produced*, losing or moving nothing? Pure,
deterministic structural transform. Fully unit-testable. Does **not** need
realistic fixtures.

**Q2 — docling fidelity.** Did docling read the PDF correctly in the first
place — right cell values, right reading order, right heading levels, no
dropped paragraph? The lossy vision-model layer. **Not the normaliser's
job**, cannot be fixed there, dominant error source. Needs real PDFs and
ultimately the gold set.

Conflating them is the trap. A bug in Q1 and a bug in Q2 look totally
different and you catch them with different tooling. Treat the normaliser
test suite as "Q1, exhaustively" and accept that "Q2, cheaply and in
isolation" is not on the menu — its instrument is the gold set, denominated
in the metric you actually care about.

### 2.2 Q1 — adversarial, not realistic, fixtures

The fear ("simple fixtures miss common failure modes") is right but the fix
is **not** "more realistic fixtures" — realistic PDFs test Q2, not Q1. For
Q1 you want **deliberately pathological synthetic `DoclingDocument`-shaped
dicts**, hand-built to break the transform:

- spanned / merged table cells (`row_span` / `col_span` > 1, the
  `*_offset_idx` fields) → assert every logical coordinate `(r, c)` resolves
  to the right cell text after span resolution, against a hand-checked dense
  matrix
- a `body` tree whose `children` are **not** in reading order, or whose
  `parent` / `self_ref` links cross-reference oddly → assert block order is
  correct
- `section_header` items with **skipped levels** (h1 → h3 with no h2), or a
  level that decreases by more than one, or headers appearing after body
  text → assert the heading-stack → `section_path` reconstruction does not
  corrupt or drop
- zero-length / whitespace-only `text` items; an item whose `prov` spans two
  pages; a table that straddles a page break; a `RefItem` pointing at a
  `self_ref` that does not exist; a formula item next to its rendering
  picture item
- duplicate `self_ref`s; an item present in `texts[]` but absent from
  `body` (and vice versa)

Assertions, all route-agnostic:

- **no-text-loss invariant** — every character of every source `text` field
  appears in the assembled `Document`, modulo one declared whitespace rule;
  every cell's text is addressable at its claimed coordinate; every
  charspan maps to a stable offset
- **cattrs round-trip** — `unstructure → structure` is identity on the
  `Block` union
- **section-path well-formedness** — monotone-ish, no orphan ids

Then add **property-based** (Hypothesis): generate random *valid-shaped*
docling dicts, assert no-text-loss + round-trip + "every input text fragment
is reachable." This is what flushes the failure modes you cannot think of by
hand, and it is cheap.

And — crucially — **the no-text-loss invariant is not only a test, it runs
on every extraction.** It is a dict walk; cost is nil. A normaliser bug
that silently drops a sentence in production fails loud the same way the
ingest path does. That is the guarantee you *can* give for Q1.

### 2.3 The sharpening — test backwards from the anchor gate's blind spots

This is the part that connects to "the metric is value truthfulness." The
normaliser's *dangerous* failure is not "loses a paragraph" — the
no-text-loss invariant catches that. It is **moving content in a way that
still passes the verbatim-anchor gate**, because then a *correct* docling
read becomes a *wrong-anchored measurement that the gate greenlights*.
Enumerate those, and they become your highest-priority Q1 tests:

| Normaliser bug | Why the anchor gate misses it | Test |
|---|---|---|
| **table cell transposition / off-by-one in span resolution** | the measurement's anchor `(table_id, r, c)` still resolves to a *real* cell holding a *plausible number* — gate passes, value is from the wrong cell | span resolution diffed against hand-checked dense matrices; this is the #1 case |
| **char-offset drift in prose** | if the value string also appears elsewhere in the block, `value in block.text` is still true | offsets exact against fixtures; *and* make the gate assert `block.text[start:end] == value`, never `value in block.text` |
| **caption ↔ float misattribution** | units / field-strength pulled from the caption get bound to the wrong table; the cell value is still verbatim | caption-association assertions in the adversarial fixtures |
| **reading-order scramble → wrong `section_path`** | value + anchor are fine; only the section label is wrong | lower danger (it is context, feeds `AcquisitionContext` binding, not the value) but still assert it |

So: design the Q1 suite from "what can change a value's meaning without
changing the bytes the gate checks," not from "what does a PDF look like."
That is a small, enumerable list, and it is the list that matters.

### 2.4 Q2 — docling fidelity: instruments, cheapest-and-highest-value first

You cannot unit-test "did docling read this PDF right" cheaply. What you
*can* build is a layered net, and one layer is unusually cheap and
unusually aligned with the metric:

**(a) Cross-format differential test — build first.** You can sideload a
PDF for a paper you *also* have as XML. Assemble ~10–20 such dual-format
papers (pick the table-heavy relaxometry ones). Run *both* routes through
normaliser → Measurement. The XML-route measurement set is the trusted
reference; the PDF route's deviations **are the docling error catalogue,
measured in measurement-space** — "the PDF route recovers 87% of relaxometry
values relative to the XML reference, and of the misses, N are table-cell
misreads, M are dropped, K have wrong field-strength binding." That is
exactly the number we said we care about, it is relative (no
hand-annotation needed beyond picking the papers), and it is cheap. It also
doubles as the empirical input to the `docling-settings-buildout.md` §1.3
`force_backend_text` / TableFormer-V2 bake-off.

**(b) Golden-HTML snapshots on real Wiley PDFs.** Take N real corpus PDFs
including the gnarly two-column-with-floats ones, run docling → normaliser
→ render to readable HTML, freeze one golden per fixture (a human blesses it
once). This does not *prove* correctness — but it makes every regression
loud: a docling version bump, a normaliser change, a settings flip all
surface as a reviewable diff. This is the regression tripwire; (a) is the
quality measurement.

**(c) Gold-set, PDF slice.** The `agentic-buildout-sketch.md` §5.9
50–200-paper gold set, filtered to PDF-route papers, scored on
`(value, unit, tissue, field_strength)` P/R + **anchor rate** + **table-cell
coverage**. The *absolute* truthfulness number for the PDF route (where (a)
gives the *relative*-to-XML one). The expensive instrument — but the
sketch already commits to building the gold set at step 1, so this is just
"remember to slice it by route."

**(d) Runtime, not tests.** `ConversionStatus.PARTIAL_SUCCESS →
DoclingDegradedError` already kills the catastrophic case loud. Add:
monitor **anchor-gate rejection rate per route** — a spike in PDF-route
rejections after a docling bump or a normaliser change is a regression
signal that needs no fixture at all. And emit the per-`Document`
`completeness` summary so a downstream consumer can see "this one came via
the lossy route" without re-deriving it.

### 2.5 Bottom line for Part 2

- **Q1 (transform) we can guarantee** — exhaustive adversarial +
  property-based unit tests, the no-text-loss invariant as a *runtime*
  check, and a Q1 suite designed backwards from "content moved without
  tripping the anchor gate" (cell transposition is the marquee case).
- **Q2 (docling fidelity) we cannot guarantee in isolation, and should not
  try** — its instruments are: cross-format differential test (cheap,
  relative, measurement-space — build first), golden snapshots on real
  PDFs (regression tripwire), gold-set PDF slice (absolute number), and
  anchor-rejection-rate monitoring (free production canary). The
  catastrophic case is already a loud typed error; the layers handle the
  subtle "SUCCESS but quietly wrong table."
- **The reframe is correct and load-bearing:** because the metric is value
  truthfulness, the normaliser does not need to be correct *in a vacuum* —
  it needs to be (i) provably faithful to docling and (ii) lossy *only* in
  ways the anchor gate + plausibility ranges + Auditor can catch. The whole
  test design follows from making (ii) true: anything the normaliser could
  do that defeats those three downstream checks is a P0 test; anything they
  would catch anyway is a P2.

One concrete suggestion: **build the cross-format differential harness (a)
before E0.5b lands**, even with a stub normaliser, so the first thing the
docling → `Document` adapter does when it exists is produce a
measurement-space loss number against the XML reference. That keeps the PDF
route honest from commit one, and it is the test denominated in the thing
we actually care about.

---

## Part 3 — Persistence

Three publishers, three ingestion routes, one normalised `Document` shape —
the on-disk layout has to honour both that convergence and the "one
canonical extraction route per `Document`" + append-only commitments from
CLAUDE.md.

### 3.1 End-to-end flow

```
                                       DOI
                                        │
                                        ▼
                          ┌──────────────────────────┐
                          │ CrossRef metadata        │   polite-pool, mailto=…
                          │ + publisher dispatch     │
                          └──────────────┬───────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │                          │                          │
        wiley DOI                  springer DOI                elsevier DOI
              │                          │                          │
              ▼                          ▼                          ▼
     ┌────────────────┐         ┌────────────────────┐    ┌───────────────────┐
     │ wiley-tdm SDK  │         │ Springer dual-tier │    │ raw httpx + lxml  │
     │ (token + IP)   │         │ OA / TDM key       │    │ view=FULL         │
     └───────┬────────┘         └─────────┬──────────┘    └─────────┬─────────┘
             │ PDF bytes                  │ JATS XML                │ CEP XML
             ▼                            ▼                         ▼
       magic-byte                   magic-byte                magic-byte +
       validate                     validate                  META_ABS check
             │                            │                         │
             │ IngestError                │ IngestError             │ Entitlement-
             │ (loud)                     │ (loud)                  │ DowngradeError
             │                            │                         │ (loud)
             └─────── atomic commit (tmp → os.replace) ─────────────┘
                                         │
                                         ▼
         ╔════════════════════════════════════════════════════════════════╗
         ║ artifacts/{pdf,jats,elsevier}/sha256/<aa>/<sha>.<ext>          ║
         ║ manifests/sha256/<aa>/<sha>.manifest.json                      ║
         ╚════════════════════════════════╤═══════════════════════════════╝
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
              ▼                           ▼                           ▼
       extract/pdf.py            extract/jats.py            extract/elsevier.py
       (docling)                                            (deliberately
              │                           │                  JATS-shaped output)
              ▼                           ▼                           ▼
       docling dict              publisher dict             publisher dict
       (DoclingDocument)         (sections / tables / …)    (same shape — by design)
              │                           │                           │
              └───────────────────────────┼───────────────────────────┘
                                          │
                                          ▼
         ╔════════════════════════════════════════════════════════════════╗
         ║ documents/sha256/<aa>/<sha>/document.json   (raw extractor)    ║
         ║ documents/sha256/<aa>/<sha>/meta.json       (pipeline_view)    ║
         ╚════════════════════════════════╤═══════════════════════════════╝
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  │                                               │
            shape = docling                              shape = publisher dict
                  │                                       (jats + elsevier:
                  │                                        deliberately identical)
                  ▼                                               ▼
        ┌──────────────────────┐                      ┌─────────────────────┐
        │ docling adapter      │                      │ XML adapter         │
        │ (E0.5b)              │                      │ shared by jats +    │
        │                      │                      │ elsevier  (E0.5a)   │
        └──────────┬───────────┘                      └──────────┬──────────┘
                   │ route="docling"                             │ route="jats"
                   │ ref_id often None                           │  or "elsevier"
                   │ refs raw-only                               │ ref_id resolved
                   │ table_source=tableformer                    │ refs structured
                   │ has_equations: yes (formula enrichment)     │ table_source=
                   │                                             │  publisher
                   └───────────────────────┬─────────────────────┘
                                           │
                                           ▼
         ╔════════════════════════════════════════════════════════════════╗
         ║ normalized/sha256/<aa>/<sha>/document.json  (cattrs JSON)      ║
         ║ normalized/sha256/<aa>/<sha>/meta.json      (route, version,   ║
         ║                                              completeness)     ║
         ╚════════════════════════════════╤═══════════════════════════════╝
                                          │
                                          ▼
                          ┌─────────────────────────────┐
                          │ Postgres index (Layer 3)    │
                          │ derived projection,         │
                          │ recomputable from disk      │
                          │ (lands at E5; defer)        │
                          └─────────────────────────────┘
```

A few things the diagram makes visible that aren't obvious from any single
section above:

- **The convergence is at the `normalized/` layer, not earlier.**
  `documents/sha256/<aa>/<sha>/document.json` still holds the route-specific
  raw shape (docling dict on Wiley, publisher dict on Springer, *the same
  publisher dict* on Elsevier — deliberate per `overview-v3.md` §11).
- **The XML adapter is shared** between jats and elsevier (one body of
  code; the only delta is which publisher dict it accepts and whether
  `ref_type` ever populates). The docling adapter is its own thing — by
  necessity, per Part 1.5.
- **Every stage that can fail does so loudly**: `IngestError` subclasses on
  retrieve / validate (CLAUDE.md), `EntitlementDowngradeError` on Elsevier
  `META_ABS` (Elsevier silently downgrades unentitled requests to abstract
  payloads — abstracts are corpus poison), `DoclingDegradedError` on
  `PARTIAL_SUCCESS` (`docling-settings-buildout.md` §1.2). The normaliser
  inherits the discipline: the no-text-loss invariant fires as a runtime
  check (Part 2.2), not just in tests.

### 3.2 Layered, not replacing

Keep the existing raw extractor output (`documents/<sha>/document.json` from
`agentic-buildout-sketch.md` §1) untouched, and add the normalised
representation as a **derived layer** beside it. The reasons aren't
DRY-flavoured — they're operational:

- **Extraction is the expensive step** (docling on a 30-page PDF is minutes;
  XML is seconds). A normaliser bump should re-run
  `normalize(load(documents/<sha>/document.json))` — fast — without
  re-touching docling. Same posture `docling-settings-buildout.md` §0
  takes for docling settings.
- **Cross-format differential test (Part 2.4(a))** wants the raw and the
  normalised side-by-side for diffing — they answer different questions.
- **Provenance honesty.** The normalised view is *derived*; treating it as
  source-of-truth means a normaliser bug silently rewrites history. Two
  layers, the lower one inviolate, makes that impossible.

### 3.3 Sharding and key

Match the ingest one-level sha256 sharding (`<aa>/<sha>` where `<aa>` =
first two hex chars of the artifact sha). Currently `documents/<sha>/…`
per the buildout sketch doesn't shard; that wants tightening before any one
directory holds tens of thousands of entries. **Key off the artifact
sha256**, not DOI — DOI is mutable in pathological cases (preprint →
version-of-record), the artifact hash is stable, and the manifest already
keys this way.

### 3.4 Format

JSON via `cattrs.unstructure(document)`. Three small disciplines:

1. **Discriminator field on the `Block` union** (`type: "text" | "table" |
   "figure" | "equation"`) — `cattrs` un/structure hooks key on it. This is
   the one place markdown round-trip was rejected (`overview.md`) because
   it loses offset precision; JSON keeps offsets exact, which is what the
   anchor gate depends on.
2. **Compact JSON, not pretty.** These files don't go into git;
   readability comes from the `render_html(doc)` path used by the
   golden-snapshot test (Part 2.4(b)), not from on-disk formatting.
3. **One declared whitespace canonicalisation rule** baked into the
   assembly — the same rule the no-text-loss invariant assumes. Recorded in
   `meta.json`. Changing it changes output bytes ⇒ bump the normaliser
   version.

Parquet / msgpack are tempting for size but the on-disk source-of-truth
wants legibility and `jq`-ability; binary formats are an index-layer
concern.

### 3.5 The normalised-layer `meta.json`

Same discipline as `docling-settings-buildout.md` §3 and just as
non-optional. It must record **anything that can change output bytes**:

- `normaliser_version` (semver pinned to attrs models + cattrs hooks)
- `route: Literal["jats", "elsevier", "docling"]` — the discriminator from
  Part 1.5
- `whitespace_rule` (id of the canonicalisation rule)
- `source_extractor_meta_sha` — sha256 of the upstream
  `documents/<sha>/meta.json`, so an extractor `pipeline_view` change (e.g.
  `force_backend_text` flips) correctly invalidates the normalised output
- `completeness` — the summary from Part 1.5 (`has_structured_refs`,
  `has_inline_ref_ids`, `has_equations`,
  `table_source ∈ {publisher, tableformer}`)
- `cattrs_schema_version` if it ever diverges from `normaliser_version`

The rule from the docling settings doc carries word-for-word: if the
recorded view doesn't cover a setting that can change output bytes, the
"is this normalisation stale?" check silently misses the change. Cheap to
maintain (it's a dict literal), mandatory.

### 3.6 Versioning

Single-latest on disk; older versions are reproducible from the preserved
raw extraction, so don't keep them around. If side-by-side ever becomes a
real need (an A/B during a normaliser overhaul), sub-shard by version:
`normalized/sha256/<aa>/<sha>/<normaliser_version>/document.json`. Defer
that — same "pick once, freeze, move on" posture as
`docling-settings-buildout.md` §0; the `meta.json` field makes it possible
to add later without re-architecting.

### 3.7 Atomic commit

`tmp/<…>` + `os.replace` to the canonical path, same non-negotiable
discipline as ingest (`overview-v3.md`, CLAUDE.md). `tmp/` is cleared on
startup. A failed normalisation must leave `normalized/sha256/<aa>/<sha>/`
either fully present at the new version or untouched at the old — never
half-written.

### 3.8 Postgres / index layer

This is `agentic-buildout-sketch.md`'s Layer 3 / §3 / §8 open-decision #6.
Shape:

- `documents` table — one row per normalised `Document`, indexed by
  `(artifact_sha, normaliser_version)`. Carries DOI, route, completeness
  flags, source extractor meta sha. Cheap projection of the on-disk
  `meta.json` plus light document-level summaries.
- `blocks` table (debatable) — only if the measurement layer's queries
  actually need it; otherwise `Block`s stay JSON inside `document.json`
  and are read on demand.
- `measurements` table — separate, downstream, lands at E5.

**The on-disk `normalized/<sha>/document.json` is source-of-truth;
Postgres is a derived projection, recomputable by walking the
`normalized/` tree.** That keeps the append-only commitment intact (the
projection drops and rebuilds; on-disk records are the history). Timing
of when Postgres actually enters is still `agentic-buildout-sketch.md` §8
open-decision #6 — fine to defer to E5.

### 3.9 The one place route-purity bends — gap-fill

CLAUDE.md commits to "one canonical extraction route per `Document`;
auxiliary outputs preserved separately, never silently merged" with the
**gap-fill exception** (`source_kind="<route>_filled"`). The layout above
honours that **only if**:

- `Block.source_kind` is a per-block field (default = the document's
  `route`, exception = `"<route>_filled"`), and
- gap-fill happens as a **separate, named pass** taking two route-pure
  normalised documents and producing a third tagged one written to the
  same `normalized/sha256/<aa>/<sha>/` slot — *not* something the
  route-pure normaliser does inline.

Practical consequence: keep the route-pure normalisers strictly
route-pure. `normalized/<sha>/meta.json`'s `route` field then never has
to lie. Gap-fill is a future, opt-in second pass; until it exists, no
normalised `Document` ever has mixed `source_kind`s.

---

## Open follow-ons surfaced by this discussion

1. Does E0.5a (XML adapters) ship before E0.5b (docling adapter), or
   together? (The sketch §8 leans XML-first; this doc agrees.)
2. Where in the schema does `route` actually live — on every `Block.provenance`,
   or once on `Document` and inferred? (Leaning per-`Block`, because
   gap-filled blocks tagged `source_kind="<route>_filled"` already mix routes
   within a single `Document`.)
3. The `force_backend_text` decision (`docling-settings-buildout.md` §1.3)
   and the TableFormer V1 vs V2 bake-off (§1.2) should be deferred until
   E0.5b + the cross-format differential harness exist, so they are decided
   on a measurement-space loss number rather than on a "char count went up"
   proxy.
4. `tissue-properties` consumer defaults (sketch §8 open-decision #7) —
   `trust_state` and `aggregation_level` — interact with the
   `completeness` summary above; worth pinning together.
5. The existing `documents/<sha>/…` layout from the buildout sketch should
   be re-sharded to `documents/sha256/<aa>/<sha>/…` to match the ingest
   discipline (Part 3.3). When does that migration land — alongside E0.5a,
   or earlier as a stand-alone cleanup?
6. Single-latest vs sub-shard-by-version for `normalized/<sha>/…`
   (Part 3.6). Recommendation: latest-only and rely on reproducibility from
   the preserved raw extraction; revisit only if a real A/B need surfaces.
7. Postgres `blocks` table — populate it (queries get faster, schema
   ossifies) or keep blocks JSON-only inside `document.json` and read on
   demand? Defer until the measurement layer's actual query patterns are
   visible (E5).
