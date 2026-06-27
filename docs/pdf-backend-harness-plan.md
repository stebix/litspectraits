# Pluggable PDF-parsing backends — comparison harness plan

A design for switching the PDF→`Document` route between parsing
backends (`docling-standard`, `docling-vlm`, `mineru`, …) and comparing
them on the same artifact. The motivating failure is inline-math
mangling on the docling standard pipeline (`N_t`→`"Nt"`/`"U r"`, `Σ`→
`/u1D6BA`; see the project memories `project_pdf_inline_math_loss` and
`project_math_ocr_research`, confirmed on Wiley DOI `10.1002/mrm.27665`).

Subordinate to `overview-v3.md` and `normalized-documents-discussion.md`;
where they disagree, those win and this file is updated to match. It is
a sibling to `dual-route-comparison-overview.md` — that doc compares
*formats* (PDF vs XML) for one paper; this one compares *parsers* for
one PDF.

Status: **plan only, no code yet.** Lock the abstraction, storage
layout, and metrics here before implementing.

---

## 0. Scope and framing

The dual-route doc pins apart two questions. This harness adds a third:

- **Q1 — transform faithfulness.** Does our normalizer preserve a given
  `DoclingDocument` into a `Document` faithfully? Property of *our code*;
  tested by adversarial synthetic fixtures.
- **Q2 — extraction quality across formats.** Given a paper acquired via
  two publisher routes, do the normalised `Document`s agree? Property of
  *docling vs the publisher XML*; the `diff-routes` harness answers this.
- **Q3 — parser quality on one artifact (this doc).** Holding the paper
  *and* the input format fixed (one PDF), and holding the
  canonical-`Document` target fixed, how do different *parsing backends*
  compare? Property of *the backend*, isolated from both format choice
  and normalizer faithfulness.

Q3 is the only lever for the Wiley case. Wiley is PDF-only
(`project_wiley_cdn_blocking`), so it **cannot participate in Q2** —
there is no XML truth to diff against. The backend harness is therefore
a **side-by-side without a reference**: no backend is "truth", we report
each backend's numbers next to the others and let a human (or, later, a
measurement-space check) judge.

Where a dual-format DOI *does* exist (Springer/Elsevier), the two
harnesses **compose**: the XML route supplies an external reference, so
"which backend best reproduces the XML's inline math / table values" is
answerable by running Q3's backends and grading each against the Q2
reference. That composition is a follow-on, not the first cut.

### What this is not

- **Not a new canonical route.** `docling-standard` remains the one
  canonical PDF backend writing `documents/`/`normalized/`. The harness
  writes elsewhere (§3). Promoting a backend to canonical is a separate,
  deliberate change — never a side effect of running the harness
  (CLAUDE.md: "one canonical extraction route per Document; auxiliary
  outputs preserved separately, never silently merged").
- **Not a measurement-space evaluation.** Like the `diff-routes`
  harness pre-E1, this operates on `Document`-level proxies. The
  measurement-denominated comparison waits on E1.

---

## 1. The backend abstraction

A backend spans both pipeline stages — `extract` (PDF → native dict) and
`normalize` (native dict → canonical `Document`) — but every backend
converges on one type, `Document`. That convergence is the seam the
harness exploits: comparison code only ever sees `Document`s and never
needs to know which parser produced one.

```python
# litspectraits/extract/backends/_base.py
from typing import Any, Protocol, runtime_checkable
from collections.abc import Mapping
from pathlib import Path
from litspectraits.normalize.models import Document

@runtime_checkable
class PdfBackend(Protocol):
    id: str                                              # 'docling-standard' | 'docling-vlm' | 'mineru'
    config_view: Mapping[str, Any]                       # knobs that change output bytes (→ meta)

    def extract(self, pdf_path: Path) -> Mapping[str, Any]:
        """PDF → verbatim backend-native dict (archived, never transformed)."""

    def normalize(self, raw: Mapping[str, Any]) -> Document:
        """backend-native dict → canonical Document."""
```

`config_view` mirrors `extract/pdf.py`'s `pipeline_view` — the recorded
set of knobs that can change output bytes, so a stale experiment is
detectable without re-running (`docling-settings-buildout.md` §3).

A registry keeps the switch a string:

```python
BACKENDS: dict[str, Callable[[], PdfBackend]]   # id → lazy factory
```

Lazy factories preserve the `[extract]`-optional discipline: importing
the registry must not import `docling`/`mineru`. Same lazy-import shape
as `extract/pdf.py:_load_docling` and `docling_adapter.py:_load_docling_sdk`.

---

## 2. The three backends, mapped to effort

| Backend | `extract` produces | `normalize` adapter | New deps | Effort |
|---|---|---|---|---|
| `docling-standard` | `DoclingDocument` dict (StandardPdfPipeline) | `normalize_docling_document` ✅ exists | — | wrap existing |
| `docling-vlm` | `DoclingDocument` dict (**VlmPipeline**) | `normalize_docling_document` ✅ **reused verbatim** | Granite weight (258M) | swap pipeline class |
| `mineru` | MinerU middle-JSON | `normalize_mineru_document` ❌ new | `[mineru]` extra (heavy) | extra + new adapter |

### The load-bearing fact (verified against installed docling 2.93.0)

`docling.pipeline.vlm_pipeline.VlmPipeline` reconstructs a
`DoclingDocument` from the VLM's DocTags output. Granite-Docling is
reachable as `docling.datamodel.vlm_model_specs.GRANITEDOCLING_TRANSFORMERS`
(`repo_id='ibm-granite/granite-docling-258M'`), wired via
`VlmPipelineOptions.vlm_options`. **So `docling-vlm` reuses the entire
existing downstream** — `export_to_dict()` → `normalize_docling_document`
→ the diff harness. The only delta from `docling-standard` is which
pipeline `_load_docling` builds.

Consequence: `docling-standard` vs `docling-vlm` is nearly free and
already produces the inline-math A/B. **MinerU is the only candidate
needing genuinely new code** (its own adapter), which is why it is
Phase 2.

### `force_backend_text` — bounding the OCR-the-numbers risk

`VlmPipelineOptions` exposes `force_backend_text`. A full end-to-end VLM
re-OCRs the page image, which risks altering the exact numeric values
this project exists to extract (T1/T2/…). `force_backend_text=True` tells
docling to take *text* from the deterministic PDF text layer while the
VLM supplies layout + math — i.e. the deterministic-numbers /
math-aware-layout hybrid. Whether Granite's DocTags math survives this
mode is an **open question to settle empirically** (§11), and the harness
is exactly how we settle it: expose it as a `docling-vlm` config knob and
compare both settings as if they were two backends.

---

## 3. Storage — isolate experiments from the canonical tree

Both `documents/` and `normalized/` are keyed on the *artifact* sha256
(`store.py`): exactly one `document.json` per artifact. Two backends on
the same PDF would collide. The harness must **not** touch those trees.

New sibling tree, sharded identically (`sha256/<aa>/<sha>/`), with a
backend-id leaf:

```
experiments/pdf-backends/sha256/<aa>/<sha>/<backend-id>/document.json     # verbatim native dict
experiments/pdf-backends/sha256/<aa>/<sha>/<backend-id>/normalized.json   # canonical Document
experiments/pdf-backends/sha256/<aa>/<sha>/<backend-id>/meta.json         # backend id + config_view + timings
```

`ArtifactStore` gains one method, matching the existing
`document_dir` / `normalized_dir` shape:

```python
def backend_experiment_dir(self, sha256: str, backend_id: str) -> Path:
    shard = sha256[:_SHARD_PREFIX_LEN]
    return self._experiments_dir / 'pdf-backends' / 'sha256' / shard / sha256 / backend_id
```

Commit discipline is the project standard: atomic `os.replace` from
`tmp/` (`_io.atomic_write`), integrity check on re-run, `--rerun` to
overwrite. `docling-standard` writes here too when run *through the
harness* (so the side-by-side has all backends in one place); its
canonical output under `documents/`/`normalized/` is produced by the
existing `extract`/`normalize` commands and left untouched.

**Promotion path (deliberate, separate change).** If a backend wins, it
gets promoted to canonical by (a) extending `Route`/dispatch, (b)
pointing `extract`/`normalize` at it, (c) re-extracting the corpus under
`--reextract`. The harness's job ends at producing the evidence for that
decision.

---

## 4. The comparison harness

`diff.py`'s `compare_documents` is XML-as-truth and directional (it
raises if the docling side isn't `route='docling'`). Backend-vs-backend
has no truth, so this is a **new sibling**, not a generalization of the
existing function (keep the Q2 harness's directional contract intact).

```python
# litspectraits/normalize/backend_diff.py
def compare_backends(docs: Mapping[str, Document]) -> BackendComparison: ...
```

Metrics are tuned to the actual question (inline-math + value safety),
*not* table recall:

1. **Inline-math fidelity proxies** (per backend, over all `TextBlock.text`):
   - count of literal `/uXXXX` glyph escapes (the unmapped-font garbage);
   - count of Mathematical-Alphanumeric-Symbol codepoints (U+1D400–1D7FF)
     and stray combining marks (the silently-wrong-but-rendered class);
   - single-letter-split heuristic count (` [A-Za-z] ` runs — the `U r`
     subscript-became-space pattern);
   - `EquationBlock` count + share of `latex` payloads that parse.
   Lower garbage + more parseable LaTeX = better. None is a correctness
   proof; together they rank backends and catch regressions.
2. **Numeric-value preservation** (the safety check that matters most):
   the multiset of number-like tokens (`\d[\d.,]*` with optional unit
   suffix) per backend. A VLM that drops or mutates `42.3`/`1.5T`
   relative to `docling-standard`'s deterministic text layer is
   *disqualifying* regardless of how clean its math looks. Reported as
   per-pair multiset deltas.
3. **Structural** (reuse `diff.py` helpers verbatim): table count,
   per-table cell-token coverage, section-path overlap, char count.

Reporting reuses the existing scaffolding nearly as-is:
`format_*_report` for the per-artifact side-by-side, and `compare_reports`
for the temporal-regression view ("`docling-vlm` inline-garbage rose
from 3 to 17 after a docling bump"). Persisted reports follow the
`--out`/`--compare-to` pattern already in `diff-routes`.

---

## 5. Schema handling — don't touch `Route` yet

`Route`/`Provenance.route` is `Literal['jats','elsevier','docling']`.
The provenance *shape* is identical for every PDF backend (page + bbox),
so `docling-vlm` keeps `route='docling'`; the *tool* identity lives in a
new `backend_id` field on the experiment `meta.json`, not on
`Provenance`. MinerU has the same page+bbox shape and can do likewise.

Recommendation: **leave the `Route` literal alone for the harness.**
Record `backend_id` in experiment meta only. Extend `Route` (or add a
dedicated `backend`/`extractor_id` to `Document`) **only when a backend
is promoted to canonical** — at which point it is a reviewed schema
change with a `schema_version` bump, not harness plumbing. This keeps
the spike reversible and the canonical schema clean.

(VLM-predicted bboxes are coarser/occasionally wrong vs text-layer
geometry; the harness should surface a bbox-sanity proxy — e.g. fraction
of blocks whose bbox is empty or off-page — since highlight-back-to-source
precision is a promotion gate, not a nice-to-have.)

---

## 6. CLI surface

```
litspectraits compare-backends <doi-or-sha>
    [--backend docling-standard] [--backend docling-vlm] [--backend mineru]
    [--rerun] [--out <report.json>] [--compare-to <prev-report>]
```

- resolves the PDF artifact (DOI→sha via the index, or sha directly);
  refuses non-PDF formats loudly;
- runs each requested backend (default: all installed), each under
  `asyncio.to_thread` like `extract_pdf`;
- writes each backend's verbatim dict + normalized `Document` + meta to
  the `experiments/` tree (§3), idempotent/integrity-checked;
- prints the side-by-side metrics (§4); `--out` persists for
  `--compare-to` regression runs.

Naming mirrors `diff-routes`. This is a separate command, not a flag on
`extract`, because it is an experiment harness with a different output
tree and a fan-out-over-backends shape, not a step in the canonical
ingest path.

---

## 7. Module layout

```
src/litspectraits/extract/backends/
├── __init__.py            # BACKENDS registry (lazy factories), PdfBackend re-export
├── _base.py               # PdfBackend Protocol, shared dataclasses
├── docling_standard.py    # wraps the existing _load_docling (StandardPdfPipeline)
├── docling_vlm.py         # VlmPipeline + Granite spec; reuses normalize_docling_document
└── mineru.py              # Phase 2: MinerU convert + bridge to normalize_mineru_document

src/litspectraits/normalize/
├── backend_diff.py        # compare_backends + BackendComparison + report formatting
└── mineru_adapter.py      # Phase 2: MinerU middle-JSON → Document
```

`extract/pdf.py:_load_docling` is refactored so the
`PdfPipelineOptions`-building half is shared with `docling_standard.py`
(no behaviour change to the canonical path — same settings, same
`pipeline_view`). `docling-vlm` is a parallel builder, not a fork of the
standard one.

---

## 8. `doctor` extension

`doctor` already probes the docling model cache (`extract/pdf.py:
missing_model_dirs`, `_docling_model_dirs`). Extend it so the harness's
weights are first-class:

- `docling-vlm`: Granite-Docling-258M present in the cache;
  `doctor --download-models` learns to pull it (it is a `vlm_model_specs`
  entry, downloadable the same way as the layout/tableformer/code-formula
  weights). Reported `off` (not `missing`) when `[extract]` lacks the VLM
  bits, consistent with the existing optional-model policy.
- `mineru` (Phase 2): `[mineru]` extra installed + its model cache
  populated; same `off`-when-absent treatment.

Lockstep rule from `extract-pdf-plan.md` §8 carries over: the doctor
required/optional list must move with whatever the backends actually
load.

---

## 9. Errors

Reuse the `ExtractError` tree (`errors.py`). `docling-vlm` shares
`DoclingImportError` / `DoclingConversionError` / `DoclingDegradedError`
/ `MissingModelWeightsError` with the standard pipeline. New leaves only
where a failure has no existing home:

- `BackendUnknownError` (config: unknown `--backend` id) → exit 2;
- `MineruImportError` / `MineruConversionError` (Phase 2) →
  mirror the docling import/conversion split.

The harness itself fails loud per backend but does **not** abort the
whole run if one backend dies — a backend that errors is reported as a
failed cell in the side-by-side (the point is comparison; losing one
backend shouldn't hide the others). This is the one sanctioned
deviation from "fail the whole operation", and it is logged loudly.

---

## 10. Implementation order

Each step ends green on `uv run pytest && uv run ruff check && uv run
pyright`.

### Phase 1 — docling-standard vs docling-vlm (the inline-math A/B)
1. `extract/backends/_base.py` — `PdfBackend` Protocol + registry shell;
   refactor `_load_docling` to share the options builder. Tests:
   protocol conformance, registry lookup, unknown-id error.
2. `extract/backends/docling_standard.py` + `docling_vlm.py`. Tests:
   each builds its converter; `docling-vlm` references the Granite spec;
   both call `normalize_docling_document` (mock the converter, assert the
   wiring — no weights in unit tests).
3. `store.backend_experiment_dir` + the `experiments/` commit/load
   (clone `persistence.py`'s atomic-write + integrity discipline).
4. `normalize/backend_diff.py` — `compare_backends` + metrics (§4) +
   report formatting. Tests on synthetic `Document`s with planted
   `/uXXXX`, U+1D6BA, `U r`, and number tokens.
5. CLI `compare-backends`. Golden-output test for the side-by-side.
6. `doctor` Granite probe + download wiring.
7. **Run the A/B on `10.1002/mrm.27665`** (gated smoke / manual) and
   record the result in `triage.md` and an updated memory.

### Phase 2 — MinerU
8. `[mineru]` extra + `extract/backends/mineru.py` + doctor wiring.
9. `normalize/mineru_adapter.py` (`normalize_mineru_document`) — the real
   new work: MinerU middle-JSON → `Document` with page+bbox provenance.
   Adversarial fixtures, same Q1-faithfulness discipline as the docling
   adapter.
10. Slots into the existing registry + `compare-backends` with no Phase-1
    changes.

---

## 11. Tradeoffs and open questions (for `docs/triage.md`)

- **B-1. `force_backend_text` on the VLM pipeline.** Does Granite's
  inline-math survive when text is taken from the PDF layer? If yes, this
  is the best-of-both config (deterministic numbers + math-aware layout)
  and may be the *only* docling-vlm mode worth shipping. Settle on the
  gold-set PDFs.
- **B-2. VLM-predicted bbox precision.** Highlight-back-to-source is a
  hard project requirement. Quantify the bbox drift (VLM loc-tokens vs
  text-layer geometry) before any promotion; it may cap docling-vlm at
  "auxiliary, never canonical."
- **B-3. Hallucination guard.** Any VLM (Granite, MinerU-2.5) is
  autoregressive. Phase 1 measures garbage/value-drift descriptively;
  does the harness also need render-back / repetition-loop detection to
  make hallucination a *loud* failure, or is descriptive enough for an
  experiment tool? (`project_math_ocr_research` argues for guards before
  any production use.)
- **B-4. Determinism across devices.** Same caveat as `extract-pdf-plan.md`
  — VLM output may vary CPU↔GPU. Decide whether experiment integrity
  checks tolerate this (probably `--rerun` is just expected here).
- **B-5. MinerU dependency weight.** `[mineru]` pulls its own model
  stack. Confirm it can share the torch pin and doesn't conflict with
  docling's before committing Phase 2.
- **B-6. Deterministic complement.** Orthogonal to backend choice:
  `unicodeitplus` (Unicode→LaTeX) + PyMuPDF span-geometry sub/superscript
  recovery (`project_math_ocr_research` option 2/3) could post-process
  *any* backend's output. Does that belong in the harness as a fourth
  pseudo-"backend" so it's comparable, or in the normalizer? Likely the
  latter, but the harness is where its value gets measured.
</content>
</invoke>
