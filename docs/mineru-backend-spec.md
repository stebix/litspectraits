# MinerU as a first-class PDF backend — integration spec

How to take the PDF→`Document` path from a single hard-wired parser
(docling) to a selectable backend choice, `PDF → {docling, mineru} →
Document`, with the schema's PDF-route provenance invariants relaxed so a
second backend can land its formula/table output without fighting
rules that were written assuming docling was the only PDF parser.

Subordinate to `overview-v3.md`, `normalized-documents-discussion.md`,
and `CLAUDE.md`; where they disagree, those win and this file is updated.

**Relationship to `pdf-backend-harness-plan.md`.** That doc specs a
*comparison harness* (`compare-backends`) that runs N backends on one PDF
into an isolated `experiments/` tree and **deliberately does not touch
`Route` or the canonical path** (its §5: "leave the `Route` literal
alone… extend it only when a backend is promoted to canonical"). This
spec is that promotion. It assumes the harness exists (or is built
first) to produce the evidence, and then makes MinerU *eligible to be the
canonical PDF backend* — which is the reviewed schema change §5
anticipated. Read the harness plan for the side-by-side metrics; read
this for the canonical-path wiring and the schema relaxation.

Status: **plan only, no code yet.**

---

## 0. The two decisions this spec makes (and why)

The user ask is "MinerU as a first-class drop-in for docling… we may want
to relax the very specific output provenance restrictions — we care more
about correct parsing of formula and tabular values." Two concrete
decisions follow.

**D1 — Backend selection is a swappable single-canonical choice, not a
parallel write.** `documents/` and `normalized/` are keyed on the
*artifact* sha256 only (`store.py:180-201`): one `document.json` per PDF.
CLAUDE.md commits to "one canonical extraction route per Document." So we
do **not** let docling and mineru both write the canonical tree for the
same PDF. Instead: a backend is *selected* (CLI flag + env default),
recorded in the extract `meta.json`, and switching backends is a
deliberate `--reextract`/`--renormalize`. Side-by-side comparison lives
in the harness's `experiments/` tree, never the canonical one. This keeps
the CLAUDE.md commitment intact while making MinerU a true drop-in: it
*can be* the canonical parser, on equal footing with docling.

**D2 — Relax PDF-route provenance from "docling, exact geometry" to "any
PDF backend, geometry graded by fidelity."** Today the schema hard-codes
`'docling'` as the only PDF route and `EquationBlock.latex` as
docling-only (`models.py:66-112`, `:232-277`). The relaxation keeps
provenance *present and honestly labelled* rather than dropping it —
which respects CLAUDE.md's "provenance is non-negotiable" while accepting
that a VLM-predicted bbox is coarser than a text-layer one. We add a
`geometry_fidelity` discriminator instead of silently widening tolerances.

Both decisions are revisitable; see §11.

---

## 1. The dispatch change — format→backend→extractor

Today the PDF leg is 1:1. `extract/_dispatch.py:59-67`:

```python
match record.format:
    case Format.PDF:
        return await extract_pdf(record, store, reextract=reextract, model_cache_dir=model_cache_dir)
    case Format.JATS_XML:
        return await extract_jats(record, store, reextract=reextract)
    case Format.ELSEVIER_XML:
        return await extract_elsevier(record, store, reextract=reextract)
```

`normalize` mirrors this, keyed on `record.format` → adapter
(`normalize_xml_document` / `normalize_docling_document`).

The change: the **PDF leg becomes backend-dispatched**. The other two
legs are untouched.

```python
# extract/_dispatch.py
case Format.PDF:
    return await extract_pdf(
        record, store,
        backend=backend,                 # new: 'docling-standard' (default) | 'docling-vlm' | 'mineru'
        reextract=reextract,
        model_cache_dir=model_cache_dir,
    )
```

`extract` gains a `backend: str = DEFAULT_PDF_BACKEND` parameter
(`DEFAULT_PDF_BACKEND = 'docling-standard'`, overridable via
`LITSPECTRAITS_PDF_BACKEND`). On the XML legs it is ignored (and a
non-default value on an XML artifact is a loud `BackendNotApplicableError`,
not a silent ignore — fail-loud discipline).

**`normalize` cannot key on format alone anymore** — a PDF can now be
docling- or mineru-parsed, and the adapter must match the parser that
produced `document.json`. The seam already exists: the extract step
records its config in `meta.json` (`extract/pdf.py` writes `pipeline_view`
there), and `commit_normalized_document` already reads that meta for
`source_extractor_meta_sha` (`persistence.py:158-286`). So:

- extract `meta.json` gains a top-level **`backend_id`** field
  (`'docling-standard'` / `'docling-vlm'` / `'mineru'`), alongside the
  existing `pipeline_view` (renamed/aliased to `backend_view`).
- `normalize` reads `backend_id` from the upstream meta and dispatches:
  `docling-*` → `normalize_docling_document`, `mineru` →
  `normalize_mineru_document`. XML formats keep dispatching on format.

This is the single most important structural change: **backend identity
flows extract→normalize through `meta.json`, not through a new
`Format`.** MinerU output is still `Format.PDF`; it is a different
*backend*, not a different *format*.

### The backend abstraction

Reuse the harness plan's Protocol + registry (`pdf-backend-harness-plan.md`
§1), but it is now the seam the canonical path uses too, not just the
experiment harness:

```python
# extract/backends/_base.py
@runtime_checkable
class PdfBackend(Protocol):
    id: str                                    # 'docling-standard' | 'docling-vlm' | 'mineru'
    config_view: Mapping[str, Any]             # output-affecting knobs → meta.json (was pipeline_view)
    def extract(self, pdf_path: Path) -> Mapping[str, Any]: ...   # PDF → verbatim native dict
    def normalize(self, raw: Mapping[str, Any]) -> Document: ...  # native dict → canonical Document

BACKENDS: dict[str, Callable[[], PdfBackend]]  # id → lazy factory
```

Lazy factories preserve the optional-extra discipline (`extract/pdf.py:421-449`
defers the `docling` import inside `_load_docling`; the registry must not
import `docling` or `mineru` at module load). `docling-standard` is a thin
wrap of the existing `extract_pdf` internals; `mineru` is the new module.

---

## 2. Schema relaxation (D2) — `normalize/models.py`

This is the part the user flagged. Four edits, all in `models.py`, plus a
`schema_version` bump.

### 2.1 `Route` — additive

```python
Route = Literal['jats', 'elsevier', 'docling', 'mineru']   # was: …, 'docling'
_XML_ROUTES: Final = ('jats', 'elsevier')
_PDF_ROUTES: Final = ('docling', 'mineru')
```

Additive is the recommendation: minimal churn, mineru is a peer of
docling, existing on-disk `route='docling'` Documents are untouched, and
`backend_id` in meta still distinguishes `docling-standard` vs
`docling-vlm` (both `route='docling'`). The alternative — collapse to a
single `route='pdf'` and push docling/mineru entirely into `backend_id`
— is cleaner long-term but rewrites a stable literal and forces a corpus
re-extract; defer it (§11, Q-A).

### 2.2 `Provenance` — optional geometry, graded by fidelity

Today (`models.py:66-112`) the docling branch hard-requires `page` and
allows `bbox=None`, with no record of *how good* the geometry is. MinerU's
`pipeline` backend reads the PDF text layer (exact bboxes, points scale);
its `vlm` backend predicts them (approximate). We make that distinction a
first-class, queryable fact:

```python
GeometryFidelity = Literal['exact', 'approximate', 'absent']
# exact       — from the deterministic PDF text-layer geometry
#               (docling text-layer; MinerU pipeline backend)
# approximate — model/VLM-predicted bbox (docling-vlm; MinerU vlm backend)
# absent      — no geometry recovered for this block

@frozen
class Provenance:
    route: Route
    xpath: str | None = None
    page: int | None = None
    bbox: BBox | None = None
    page_char_range: CharRange | None = None
    geometry_fidelity: GeometryFidelity = 'absent'   # new

    def __attrs_post_init__(self) -> None:
        if self.route in _XML_ROUTES:
            xml_violations = [n for n in ('page', 'bbox', 'page_char_range')
                              if getattr(self, n) is not None]
            if xml_violations:
                raise ValueError(f'route={self.route!r} carries XML provenance; '
                                 f'these must be None: {xml_violations}')
            if self.geometry_fidelity != 'absent':
                raise ValueError("XML routes carry no page geometry; "
                                 "geometry_fidelity must be 'absent'")
        elif self.route in _PDF_ROUTES:
            if self.xpath is not None:
                raise ValueError(f"route={self.route!r} carries page provenance; xpath must be None")
            if self.page is None:                          # kept: page is free and always present
                raise ValueError(f"route={self.route!r} requires `page`")
            # the relaxation: bbox is optional, but presence and fidelity must agree
            if self.bbox is None and self.geometry_fidelity != 'absent':
                raise ValueError('no bbox but geometry_fidelity != absent')
            if self.bbox is not None and self.geometry_fidelity == 'absent':
                raise ValueError('bbox present but geometry_fidelity == absent')
```

What relaxed vs what held:

- **Relaxed:** the PDF branch is keyed on `_PDF_ROUTES`, not the literal
  `'docling'` — so `'mineru'` is a valid PDF route. `bbox` is explicitly
  optional on PDF routes (a backend that can't pin geometry for a merged
  or synthesized block emits `bbox=None, geometry_fidelity='absent'`
  instead of failing the whole extract).
- **Held:** `page` stays required on PDF routes — it is always present in
  both backends (docling `page_no`, MinerU `page_idx`) and is the minimum
  needed to highlight back to *a page*. Dropping it would discard
  provenance, which the relaxation does not need. (If even page-level
  provenance must be optional later, that's Q-B in §11.)
- **Added value:** `geometry_fidelity` lets the downstream
  highlight-back-to-source logic (and the harness's bbox-sanity proxy)
  distinguish "trust this bbox" from "this is a VLM guess" *without
  re-deriving it from `backend_id`*.

`docling-standard`'s adapter sets `geometry_fidelity='exact'` (it reads
the text-layer); `docling-vlm` and MinerU-vlm set `'approximate'`. This
is the one behavioural change to the existing docling adapter
(`docling_adapter.py:569-602`): it now stamps `'exact'`.

### 2.3 `EquationBlock` — latex on any PDF route

Today (`models.py:232-277`) latex is gated to `route == 'docling'`. MinerU
emits LaTeX for both inline and display equations (its formula head is
UniMERNet-class). Generalize:

```python
if has_mathml and self.provenance.route not in _XML_ROUTES:
    raise ValueError(f'EquationBlock.mathml only on XML routes; got {self.provenance.route!r}')
if has_latex and self.provenance.route not in _PDF_ROUTES:
    raise ValueError(f'EquationBlock.latex only on PDF routes; got {self.provenance.route!r}')
```

The "exactly one of mathml/latex" invariant is unchanged.

### 2.4 Inline math — the highest-value gap for the formula goal

This is *new representation*, not just a relaxation, and it is where
"correct parsing of the formula values" actually pays off. The documented
core pain (`project_pdf_inline_math_loss`, `project_springer_inline_math_bloat`)
is **inline** math; MinerU recovers inline equations as LaTeX spans inside
text lines (`span.type == 'inline_equation'`). The current `Document` has
nowhere to put them: `TextBlock` is `text: str` + `inline_refs`
(citation markers only). Recommended addition, mirroring `inline_refs`:

```python
@frozen
class InlineMath:
    char_range: CharRange     # offsets into the owning TextBlock.text
    latex: str

@frozen
class TextBlock:
    text: str
    provenance: Provenance
    section_path: tuple[str | None, ...] = ()
    inline_refs: tuple[InlineRef, ...] = ()
    inline_math: tuple[InlineMath, ...] = ()     # new; () on routes that don't recover it
    type: Literal['text'] = 'text'
```

Offsets preserve where the math sat in the prose (markdown round-trips are
rejected exactly because they lose this — CLAUDE.md). XML and docling
adapters emit `()` until they grow inline-math handling; MinerU populates
it. This needs a `cattrs` structure hook only if `InlineMath` has a
non-trivial shape (it doesn't — plain attrs, auto-structured).

### 2.5 `schema_version` + meta

- `Document.schema_version` `'1'`→`'2'` (`models.py:387`); the on-disk
  shape changed (new Provenance field, new TextBlock field, new route).
- `normalize/persistence.py`: bump `NORMALIZER_VERSION` (`:62`) and add
  `backend_id` to `NormalizedMeta` (carried through from the extract meta
  so a normalized Document records which parser produced it).
- The `Block` tagged-union hooks (`hooks.py:44-94`) need **no change** —
  dispatch is per-block on `type`, and no new block *type* is added
  (InlineMath is a field, not a Block).

---

## 3. The MinerU extractor — `extract/backends/mineru.py`

Mirrors `extract_pdf`'s six-stage shape (`extract/pdf.py:136-256`):
preflight → model-cache check → convert → status/structure check →
serialize → atomic commit. Concrete differences:

**Package & API.** PyPI `mineru` (v2.x; formerly `magic_pdf`). The
in-process entrypoint is `mineru.cli.common.do_parse` / `aio_do_parse`.
Critically, **MinerU is file-output-oriented**: `do_parse` writes
`{stem}_middle.json`, `{stem}_content_list.json`, `{stem}_model.json`,
markdown, and visualization PDFs to an output dir — it does not return the
parse in memory. So the extractor:

1. lazy-imports `mineru` inside the function body, guarded →
   `MineruImportError` (mirror `_load_docling`'s try/except at
   `extract/pdf.py:421-449`);
2. runs `do_parse` into a **scratch dir under `store.tmp_dir`** (never the
   canonical tree), via `asyncio.to_thread` like the docling convert
   (`extract/pdf.py:219`);
3. reads back `{stem}_middle.json` (the richest, points-scale, hierarchical
   form — preferred over content_list for the adapter) as the **verbatim
   native dict**, and persists *that* as `document.json` (no
   transformation — same discipline as `_serialize_document`,
   `extract/pdf.py:694-712`);
4. selects backend via MinerU's own `pipeline` vs `vlm` engine; the choice
   is a `config_view` knob (`backend_id='mineru'` with
   `config_view={'engine': 'pipeline'|'vlm', 'formula': True, 'table': True,
   'lang': …, 'mineru_version': …}`).

**Validation reuse.** The structural floor checks
(`extract/pdf.py:646-680`: `EmptyDocumentError` on zero text blocks,
`ParseDegradedError` below `FLOOR_CHARS`) are backend-agnostic — compute
the same counts over MinerU's blocks and reuse the same error leaves.
MinerU has no `ConversionStatus` enum, so the status-check stage
(`extract/pdf.py:545-582`) becomes "did `do_parse` produce a non-empty
`pdf_info`?" → `MineruConversionError` otherwise.

**Commit.** Identical atomic discipline — `atomic_write` (`_io.py:24-58`),
integrity-check-on-rerun, `--reextract` to overwrite
(`extract/pdf.py` commit path). Writes to `store.document_dir(sha)` like
docling; the `meta.json` carries `backend_id='mineru'` + `config_view`.

---

## 4. The MinerU adapter — `normalize/mineru_adapter.py` (the real work)

`normalize_mineru_document(document: dict[str, Any]) -> Document` with
`route='mineru'`. Walk `pdf_info[*].para_blocks` in reading order. The
block-type mapping (MinerU `middle.json` types → `Block`):

| MinerU type | → `Block` | Notes |
|---|---|---|
| `text`, `list`, `index` | `TextBlock` | concat line/span `content`; inline `inline_equation` spans → `InlineMath` |
| `title` | section-path push | mirror docling's `SectionHeaderItem` handling (`docling_adapter.py:185-210`); `text_level` → depth |
| `interline_equation` | `EquationBlock(latex=…, mathml=None)` | display math; `text_format=='latex'` |
| `image` / `chart` | `FigureBlock` | caption from `image_caption`/`chart_caption` |
| `table` | `TableBlock` | parse `table_body` **HTML** → cells (see below) |
| `discarded_blocks` | dropped | headers/footers/page numbers |

**Tables — where "correct tabular values" is realized.** MinerU emits
tables as an **HTML string** (`table_body`), not a cell grid. The adapter
parses that HTML into `TableBlock.cells: tuple[tuple[TableCell, ...], ...]`
+ `n_rows`/`n_cols` using `lxml.html` (already a dependency). Handle
`rowspan`/`colspan` expansion so the grid is rectangular. Consider adding
an optional `TableBlock.source_html: str | None` to retain the raw HTML
for debugging/measurement-space recovery (small additive schema change;
flagged in §11 Q-C).

**Provenance.** Every block carries `Provenance(route='mineru',
page=page_idx, bbox=…, geometry_fidelity=…)`:
- `page` ← the block's `page_idx`.
- `bbox` ← block `bbox`. **Scale matters:** `middle.json` bboxes are in
  **PDF points** (native, what we want — same units as docling's
  `BBox`); `content_list.json` bboxes are **0–1000 normalized**. Prefer
  `middle.json` so no rescaling is needed; if content_list is used,
  rescale by `page_size` and document it.
- `geometry_fidelity` ← `'exact'` for the `pipeline` engine (text-layer),
  `'approximate'` for `vlm`. Read from the extractor's `config_view`,
  threaded into the adapter (the adapter must know which engine ran — pass
  it, or read `_backend` from the middle.json top level).

**Faithfulness discipline (Q1).** Same adversarial-fixture bar as the
docling adapter: synthetic `middle.json` payloads with planted
rowspan/colspan tables, inline+display equations, missing-bbox blocks,
and the `0` vs `1000` scale trap. The adapter must fail loud on a content
block with neither page_idx nor bbox rather than silently emit
`bbox=None` for something that should have had geometry.

---

## 5. Storage (D1) — one canonical, plus the experiments tree

No new canonical layout. `documents/`/`normalized/` stay one-per-artifact
(`store.py:180-201`); the *active* backend is whatever last wrote them,
recorded in `meta.json:backend_id`. Switching backend on an artifact:
`extract <doi> --backend mineru --reextract` then `normalize <doi>
--renormalize`. The integrity guard (`ExtractIntegrityError` /
`NormalizeIntegrityError`) already refuses a silent backend swap unless
the `--re*` flag is passed — that guard now also catches "you switched
backend without meaning to," which is the behaviour we want.

Side-by-side (docling vs mineru on the same PDF, both retained) is the
harness's job: `experiments/pdf-backends/sha256/<aa>/<sha>/<backend-id>/`
(`pdf-backend-harness-plan.md` §3), via a new
`ArtifactStore.backend_experiment_dir`. The harness and the canonical
path share the same `PdfBackend` registry (§1), so there is exactly one
MinerU implementation feeding both.

---

## 6. Errors — `errors.py`

New leaves under `ExtractError` (`errors.py:156-187`), mirroring the
docling import/conversion split (`:193-275`):

- `MineruImportError` — `[mineru]` extra absent → exit 2 (alongside
  `DoclingImportError` in `cli.py:137-175`).
- `MineruConversionError` — `do_parse` produced no `pdf_info` / failed →
  exit 4 (alongside `DoclingConversionError`).
- `BackendNotApplicableError` — a non-default `--backend` on an XML
  artifact, or unknown backend id → exit 2.

Reused as-is: `EmptyDocumentError`, `ParseDegradedError`,
`SerializationError`, `ExtractIntegrityError`,
`WrongFormatForExtractorError`, `MissingArtifactError`. Add the new leaves
to the `_EXTRACT_EXIT_CODES` map (`cli.py:137-175`).

MinerU model-weight absence reuses `MissingModelWeightsError`
(generalize its `extractor=` context to carry `'mineru'`).

---

## 7. `doctor` — `doctor.py`

`_check_docling_models` (`doctor.py:580-634`) probes three docling weight
dirs via `_docling_model_dirs` + `_model_dir_present`
(`extract/pdf.py:290-343`). Add a sibling `_check_mineru_models` that
checks MinerU's model cache (its weights live under MinerU's own cache
dir; the probe is the same "dir exists and is non-empty" invariant). Each
backend's weights are reported:

- present → `ok`;
- absent but `[mineru]` extra installed → `missing` (download hint);
- `[mineru]` extra not installed → `off` (consistent with the existing
  optional-model policy — same treatment docling gets when `[extract]` is
  absent).

`--download-models` (`cli.py:1187-1222`) learns to pull MinerU weights too
(MinerU ships its own `mineru-models-download` helper; wrap it). The
lockstep rule holds: the doctor required/optional list moves with whatever
the *selected* backend actually loads.

---

## 8. CLI — `cli.py`

One new option on the two canonical commands, one new env default:

```
litspectraits extract   <doi-or-sha> [--backend docling-standard|docling-vlm|mineru] [--reextract]
litspectraits normalize <doi-or-sha> [--renormalize]      # backend read from extract meta, not a flag
```

- `extract` gains `--backend` (`typer.Option`, default from
  `LITSPECTRAITS_PDF_BACKEND` else `docling-standard`), mirroring the
  `--reextract` option shape (`cli.py:733-764`). Resolution of DOI/sha is
  unchanged (`_resolve_extract_target`, `cli.py:791-817`).
- `normalize` gets **no** `--backend` flag — it reads `backend_id` from the
  upstream `document_dir/meta.json` and dispatches. This keeps the two
  steps consistent by construction (you cannot normalize with the wrong
  adapter).
- `compare-backends` (the harness command) lands per
  `pdf-backend-harness-plan.md` §6 — separate command, separate
  `experiments/` output, fan-out over backends.

---

## 9. Dependencies — `pyproject.toml`

New optional extra, kept separate from `[extract]`:

```toml
[project.optional-dependencies]
mineru = ['mineru>=2.0']
all = ['litspectraits[wiley,springer,extract,mineru]']
```

**Torch-pin sharing is the gating risk** (harness plan B-5). Both docling
(`docling-ibm-models`) and MinerU pull `torch`/`torchvision`, and this
project pins the CUDA-12.6 wheel index for driver compatibility
(`pyproject.toml` `[tool.uv.sources]`, driver 550.x ceiling). Before
committing the extra, confirm MinerU's torch constraints are satisfiable
under the same `cu126` pin and don't drag a conflicting `torch` range.
Resolve `uv sync --extra mineru` in isolation first; if it forces a torch
bump that breaks docling, the two extras may need separate environments
(documented, not silently co-installed).

---

## 10. Implementation order

Each step ends green on `uv run pytest && uv run ruff check && uv run
pyright`. Assumes `pdf-backend-harness-plan.md` Phase 1 (the
`PdfBackend` Protocol + registry + `docling-standard` wrap) is done; if
not, do that first.

1. **Schema (§2), no backend yet.** Add `'mineru'` to `Route`,
   `geometry_fidelity` to `Provenance`, generalize `EquationBlock`,
   add `InlineMath`/`TextBlock.inline_math`, bump `schema_version`.
   Update the docling adapter to stamp `geometry_fidelity='exact'`. Tests:
   the relaxed invariants accept mineru provenance and reject malformed
   combinations; existing docling/XML Documents still structure. **No
   MinerU dependency touched** — pure schema commit.
2. **`backend_id` plumbing.** Add `backend_id` to extract `meta.json` and
   `NormalizedMeta`; make `normalize` dispatch on it for PDF. Tests with
   a synthetic docling meta carrying `backend_id='docling-standard'`.
3. **`[mineru]` extra + `extract/backends/mineru.py`.** Lazy import,
   `do_parse`-into-tmp, read-back middle.json, verbatim persist, validation
   reuse, new error leaves. Unit tests mock `do_parse` (no weights in CI).
4. **`normalize/mineru_adapter.py`.** The block-type mapping, HTML-table
   parse, inline-math spans, graded provenance. Adversarial fixtures (§4).
5. **`doctor` MinerU probe + `--download-models` wiring (§7).**
6. **CLI `--backend` on `extract`; normalize dispatch (§8).** Golden tests.
7. **Gated smoke on `10.1002/mrm.27665`** (the Wiley inline-math case) —
   docling-standard vs mineru — and record the result in `triage.md` and a
   memory update. This closes the loop the harness plan opened.

---

## 11. Open decisions (for `docs/triage.md`)

- **Q-A. `Route` additive vs collapse.** Recommended: additive
  (`'mineru'` joins the literal; §2.1). The alternative — collapse PDF
  backends to a single `route='pdf'` and carry tool identity only in
  `backend_id` — is conceptually cleaner (provenance *shape* is identical
  across PDF backends) but rewrites a stable literal and forces a corpus
  re-extract. If we are bumping `schema_version` anyway, this is the
  moment to consider it. Defaulting to additive keeps the change
  reversible.
- **Q-B. How far to relax `page`.** This spec keeps `page` required on
  PDF routes (it is free and always present). If a future backend produces
  genuinely page-less blocks, relax it the same way bbox was relaxed
  (optional + `geometry_fidelity='absent'`). Not needed for MinerU.
- **Q-C. Retain raw table HTML / raw display-equation image path?**
  Adding `TableBlock.source_html` (and maybe an `EquationBlock.image_path`)
  retains MinerU's pre-parse payload for measurement-space recovery and
  debugging, at the cost of larger Documents. Lean yes for tables (the
  HTML→cells parse is lossy on merged cells), no for equation images.
- **Q-D. VLM-predicted bbox precision is a promotion gate, not a
  nice-to-have** (harness plan B-2). `geometry_fidelity='approximate'`
  makes the grade *visible*; it does not make it *good enough*. Quantify
  the drift on the gold set before letting a vlm-engine backend become
  canonical for any DOI whose measurements must highlight back to source.
- **Q-E. Hallucination guard** (harness plan B-3). MinerU's formula/table
  heads are autoregressive. The descriptive metrics in the harness flag
  garbage; decide whether the *canonical* MinerU path also needs a
  render-back / repetition-loop guard that fails loud, given that the
  whole point of pulling MinerU in is trusting its formula/table output.
- **Q-F. Inline-math everywhere.** Once `TextBlock.inline_math` exists,
  the XML adapters (which currently drop or bloat inline math —
  `project_springer_inline_math_bloat`) and the docling adapter should
  grow handlers too, so the field isn't mineru-only. Separate follow-on.
