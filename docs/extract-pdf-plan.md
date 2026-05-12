# PDF extraction with `docling` — fail-fast linear plan

Detailed plan for Step 10 (`extract/pdf.py`) of `overview-v3.md` §17 /
§21. This document expands the one-paragraph treatment in §11 into an
implementable specification, and adds a corresponding extension to
`doctor` (§12) covering docling model availability.

It is subordinate to `overview-v3.md`; whenever the two disagree, the
v3 design doc wins and this file should be updated to match. All
section references of the form `§N` point into `overview-v3.md` unless
otherwise stated.

## 0. Scope and framing

`extract/pdf.py` handles **PDF only**. Wiley TDM returns PDF; manual
sideload is PDF-only (§9). Elsevier and Springer payloads go through
`lxml`-based extractors (§11) — the publisher has already encoded
section hierarchy, tables, refs, and citation anchors, and we read that
structure directly.

So docling's job is **structural lift**: recovering for PDFs what the
XML extractors get for free. That framing matters — docling is not
here to produce pretty markdown; it is here to expose section paths,
tables (TableFormer is the centre of gravity for our corpus), figure
captions, equations, and crucially **per-block provenance (page +
bbox)** so every measurement can be highlighted back to the source.

Two hard constraints carry over verbatim from `overview.md` /
`CLAUDE.md` and shape every decision below:

1. **Markdown round-trips are rejected.** They lose offset precision
   for inline references, and inline-reference offsets are what
   citation-chain queries downstream depend on.
2. **Append-only data model.** Re-extraction overwrites
   `document.json`, but the *measurements* derived from it later are
   append-only. The extractor is the upstream of that contract — it
   must be deterministic enough that the same PDF reliably yields the
   same structural skeleton across reruns, or the append-only model
   shears against silent extractor drift.

## 1. What docling gives us, mapped to what we need

`DocumentConverter(...).convert(path).document` yields a
`DoclingDocument` whose `body` tree is the input to everything below.
The columns are: the project requirement (from `overview.md` and
§11), the docling node that fulfils it, and notes that matter for
our extractor.

| Need | Docling node | Notes |
|---|---|---|
| Section hierarchy + `section_path` | `SectionHeaderItem` carrying `level` | Reconstruct the heading stack while walking `body` linearly |
| Paragraphs / text blocks | `TextItem` (`label ∈ {text, list_item, caption, ...}`) | One docling `TextItem` ↔ one block in our schema |
| Tables — the load-bearing slot | `TableItem` with `data.grid` (cells, row/col spans) + `captions[]` | TableFormer is the whole reason we pay the GPU bill |
| Figure captions | `PictureItem.captions[]` | We do **not** extract pixel data (`overview.md` non-goal); captions and their bboxes are enough |
| Equations | `TextItem` with `label='formula'`, optional LaTeX export | Important for sequence-parameter formulas; surface as `EquationBlock` later |
| References section | A run of `TextItem` under a `References` heading | Docling does **not** parse `<bibl>` natively; leave that to a later resolver pass |
| **Provenance (non-negotiable)** | `prov: list[ProvenanceItem(page_no, bbox, charspan)]` | This is exactly the page + bbox + offset triple `overview.md` calls for |
| Reading order across columns | Implicit in `body` traversal order | docling resolves multi-column reading order before yielding the tree |

### What docling does not give us

These are the spots where it is tempting to bolt something into
`extract/pdf.py` and where we must not, because doing so closes off
better downstream choices:

- **Inline citation anchors.** Docling does not link `[12]` in a
  paragraph back to `references[11]`. Surface forms (`[12]`,
  `Smith et al., 2019`) are recoverable by regex / LLM-extract in a
  *post-pass*, not in the extractor. Building this into `pdf.py`
  means re-extract-on-reference-parser-bump, which is the wrong
  invalidation boundary.
- **Stable element IDs across reruns.** Docling's `self_ref` strings
  are derived during traversal; they are opaque within one
  extraction, not durable across re-extractions. Our own block IDs
  must be derived from content + position, not copied from docling.
- **Sentence segmentation.** Docling emits paragraph-sized
  `TextItem`s. Sentence-level granularity (which the Ingestor needs
  for citation back to source) is a downstream tokenizer concern. We
  keep paragraph-level blocks canonical and segment on demand.
- **Math semantics.** Formula *text* is recovered; the actual numeric
  values inside (e.g. `TR = 2300 ms`) are still text from our point
  of view. Measurement extraction lives in the agent triad, not here.

## 2. Anti-patterns

Each of these is a path where extraction silently goes wrong. They
exist as a checklist because every one is a plausible
"helpful-looking" thing to do that breaks downstream guarantees.

1. **`export_to_markdown()` as the on-disk shape.** Strips `prov`,
   collapses tables to text, drops figure-caption linkage. Forbidden.
2. **Treating `ConversionStatus.PARTIAL_SUCCESS` as success.**
   Partial success commonly means "table X failed structural
   recognition." That has to be a loud `DoclingDegradedError`, not a
   quiet `document.json` with a hole where the measurements live.
3. **Default-on OCR.** Modern Wiley/Elsevier PDFs have a text layer;
   OCR on a 30-page PDF burns minutes for no benefit and introduces
   transcription errors in numeric values. Default `do_ocr=False`.
   An OCR retry is a deliberate later addition gated on
   `ParseDegradedError`, never a silent in-pipeline fallback.
4. **Cross-publisher dict normalisation inside `extract/pdf.py`.**
   §11 explicitly defers the canonical `Document` schema until the
   agent triad work begins. `pdf.py` writes docling's
   `export_to_dict()` verbatim; the normaliser is a separate later
   commit.
5. **Catching docling import / model-download failures.** §14
   pins this: propagate verbatim. The error class is the contract.

## 3. The pipeline — six stages, one job per stage

Mirrors the §0 ingest path. Every failure raises a typed
`ExtractError` subclass before anything is written under
`documents/<sha>/`. Atomic commit via `os.replace` from `<data_dir>/
tmp/`, same pattern as ingest (§3).

```
ExtractRequest(record: AcquisitionRecord, store: ArtifactStore)
 │
 ▼
[1. preflight]      ── assert record.format is Format.PDF
 │                     stat artifact_path → size > 0
 │                       ↘ WrongFormatForExtractorError
 │                       ↘ MissingArtifactError
 │
 ▼
[2. convert]        ── DocumentConverter(pipeline_options).convert(path)
 │                     wrapped in asyncio.to_thread (CPU/GPU-bound)
 │                       ↘ DoclingImportError       (lib not installed; propagate verbatim per §14)
 │                       ↘ DoclingConversionError   (status == FAILURE)
 │                       ↘ DoclingDegradedError     (status == PARTIAL_SUCCESS)
 │
 ▼
[3. structural sanity]
 │                     n_pages ≥ 1
 │                     n_text_blocks ≥ 1
 │                     total_text_chars ≥ FLOOR_CHARS
 │                     warn if zero SectionHeaderItem (letters legitimately have none)
 │                       ↘ EmptyDocumentError        (no text blocks — probably scanned PDF, OCR off)
 │                       ↘ ParseDegradedError        (text under FLOOR_CHARS)
 │
 ▼
[4. serialize]      ── doc.export_to_dict() → tmp_path / 'document.json.part'
 │                     compute stats for meta.json
 │                       ↘ SerializationError (extremely rare; cattrs/json failure)
 │
 ▼
[5. commit]         ── os.replace(tmp_path → documents/<sha>/document.json)
 │                     write meta.json atomically next to it
 │                       ↘ ExtractIntegrityError (existing document.json bytes differ AND --reextract not set)
 │
 ▼
ExtractRecord
```

### Stage 1 — preflight

`record.format` *must* be `Format.PDF`. Anything else means the
dispatcher in `extract/_dispatch.py` is wrong, and we raise
`WrongFormatForExtractorError` rather than try to convert. Equally,
`artifact_path` must exist and be non-empty — the sniff at ingest
time already validated `%PDF-`, so we do not re-sniff here; the
preflight is purely "does the file exist."

### Stage 2 — convert

Lazy import of docling inside the function body. The `[extract]`
extra is optional (`pyproject.toml`); a corpus that is 80%
Elsevier+Springer should never need docling installed. A missing
import raises `DoclingImportError` with the install hint
(`uv sync --extra extract`).

Conversion runs under `asyncio.to_thread` because docling is
CPU/GPU-bound, not I/O-bound. Same threading pattern as the Wiley /
Springer SDK retrievers (§7.1, §7.2).

The `ConversionStatus` returned by docling distinguishes:

- `SUCCESS` → continue.
- `PARTIAL_SUCCESS` → raise `DoclingDegradedError` carrying
  `result.errors`. The reason this is loud rather than warn: a
  partial-success today is almost always a TableFormer failure, and
  tables are precisely where most of the corpus's measurement values
  live. Silently committing a half-extracted document is the kind
  of failure we cannot detect downstream.
- `FAILURE` → raise `DoclingConversionError` carrying
  `result.errors`.

Anything else raised from inside `convert()` — model download
failure, CUDA OOM, malformed PDF — propagates verbatim per §14. We
do not wrap it.

### Stage 3 — structural sanity

The guardrails against silent corpus poisoning. Three hard checks
and one soft warning; thresholds are module-level constants with
inline rationale, not env vars.

```python
FLOOR_CHARS: Final = 500          # MR papers are ≥ 2 pages; 500 chars is ~80 words
MIN_TEXT_BLOCKS: Final = 1        # zero blocks means docling produced nothing usable
MIN_PAGES: Final = 1              # defensive; should be unreachable post-Stage 2
```

Failures:

- 0 text blocks → `EmptyDocumentError`. Almost always a scanned PDF
  served with no text layer, where our default `do_ocr=False` left
  us empty-handed. Operator hint: re-fetch the publisher TDM
  version, or rerun with `--ocr` (a future flag) if the source is
  genuinely image-only.
- `char_count < FLOOR_CHARS` → `ParseDegradedError`. Layout
  recognition probably failed; the PDF parsed but most of the
  content was filtered as `furniture` (headers/footers) or never
  emerged as TextItems.

Zero `SectionHeaderItem`s is a **warning**, not a failure: review
articles and short communications legitimately have flat
structure. Logged at `WARNING` level with the DOI bound.

### Stage 4 — serialize

The on-disk shape is `docling.document.export_to_dict()` **verbatim**.
Do not transform. Do not strip `prov`. Do not flatten. The future
normaliser re-walks this dict to emit the canonical `Document`; that
re-walk is cheap because every node already carries page + bbox.

Stats for `meta.json` are computed during the same walk that builds
the dict (avoid a second traversal):

- `n_pages` — `doc.num_pages()`
- `n_tables` — count of `TableItem`s in `body`
- `n_figures` — count of `PictureItem`s in `body`
- `n_text_blocks` — count of `TextItem`s with non-empty text
- `n_section_headers` — count of `SectionHeaderItem`s
- `char_count` — total characters across all `TextItem`s
- `warnings` — `result.errors` from a `PARTIAL_SUCCESS` are
  unreachable here (we raised in Stage 2); a non-empty list at this
  point would be a bug

### Stage 5 — commit

`os.replace(tmp/document.json.part, documents/<sha>/document.json)`
followed by `os.replace(tmp/meta.json.part, documents/<sha>/
meta.json)`. The directory is created with `parents=True,
exist_ok=True`; the temp filenames carry a random suffix so two
concurrent extractions on the same sha do not collide on tmp paths
even if (somehow) two ingests of the same DOI arrive in parallel.

Re-extract semantics: if `documents/<sha>/document.json` already
exists and the new bytes differ, raise `ExtractIntegrityError` unless
`--reextract` was passed at the CLI level. The flag is the explicit
"yes, I know I am overwriting." This mirrors the §3 / §10 stance
that silent overwrites are never acceptable.

## 4. `PdfPipelineOptions` — concrete configuration

No CLI surface for these in any iteration; tightening the surface
keeps the corpus deterministic. If any of these needs to flex later,
it becomes a deliberate, reviewed change to the converter builder —
never an env var, never a `--flag`. The standing decision is "pick
once against the gold-set PDF-route fixtures, freeze, move on";
`docs/docling-settings-buildout.md` is the change-management story and
holds the per-switch rationale this section summarises. The live
implementation is `litspectraits.extract.pdf._load_docling`.

```python
from docling.datamodel.base_models import InputFormat
from docling.datamodel.layout_model_specs import DOCLING_LAYOUT_EGRET_LARGE
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    LayoutOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

DOCUMENT_TIMEOUT_S = 120.0


def _build_converter(model_cache_dir: Path | None = None) -> DocumentConverter:
    """Build the docling converter with our academic-PDF settings.

    Notes
    -----
    Defaults chosen for MRI literature specifically: text-layer PDFs
    from Wiley TDM and library-proxy sideloads, table-heavy content
    where TableFormer accuracy matters more than throughput.
    """
    pipeline_options = PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(  # TableFormer V1
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=True,
        ),
        do_formula_enrichment=True,                      # populates EquationBlock on the PDF route
        layout_options=LayoutOptions(
            model_spec=DOCLING_LAYOUT_EGRET_LARGE,       # region detector over the Heron default
        ),
        document_timeout=DOCUMENT_TIMEOUT_S,             # timeout → PARTIAL_SUCCESS → DoclingDegradedError
        generate_picture_images=False,
        images_scale=1.0,
        accelerator_options=AcceleratorOptions(
            device=AcceleratorDevice.AUTO,
        ),
        artifacts_path=model_cache_dir,                  # LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR; None → docling default
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )
```

`force_backend_text` and the TableFormer V1→V2 question are still
**open** — both are settled on the gold-set relaxometry / two-column
fixtures, which don't exist yet (`docling-settings-buildout.md` §1.2,
§1.3). Until then `force_backend_text` stays `False` and TableFormer
V1 stands; `pipeline_view` (see §6) records both so a future flip is
visible in `meta.json`.

### Why each option is set as it is

- **`do_ocr=False`** — publisher PDFs have text layers; OCR adds
  minutes of latency and numeric transcription errors. See
  anti-pattern §2.3.
- **`do_table_structure=True` + `TableFormerMode.ACCURATE`** (TableFormer
  V1) — measurement tables are the corpus's centre of mass. The
  accuracy mode is materially slower than `FAST` but the right default
  for this domain. The V1-vs-V2 bake-off on the gold-set relaxometry
  tables is still open.
- **`do_cell_matching=True`** — the option that makes per-cell `prov`
  (bbox per cell) work. This is what enables table-cell-level
  highlight-back-to-source end-to-end. Without it, only the
  enclosing `TableItem.prov` is recovered.
- **`do_formula_enrichment=True`** — closes the
  `EquationBlock`-empty-on-PDF-route gap (`agentic-buildout-sketch.md`
  §1.5): MR signal-model and fitting equations become first-class
  instead of falling out as stray `text` / `picture` items. Costs an
  extra VLM pass (the code/formula model — now a *required* doctor
  download, §8); acceptable given the PDF slice is the minority one.
- **`layout_options.model_spec = DOCLING_LAYOUT_EGRET_LARGE`** — the
  highest-leverage quality knob. Two-column papers with tables and
  figures interleaved are exactly where stronger region detection
  earns its keep (clipped tables, captions on the wrong float, body
  text bleeding into a cell all originate in region detection). Egret-
  Large over the docling 2.93 default (Heron). Cost: bigger weights,
  slower per-page inference.
- **`document_timeout=120.0`** — fail-loud hygiene, not a quality knob.
  A timeout yields `ConversionStatus.PARTIAL_SUCCESS`, which
  `_check_conversion_status` already maps to a hard `DoclingDegradedError`
  (§2.2) — so this turns "a pathological PDF hangs the worker forever"
  into the loud, typed failure the project wants everywhere.
- **`generate_picture_images=False`** — figure-pixel extraction is a
  v1 non-goal (`overview.md`). We keep captions and bboxes only;
  cropped image bytes are not stored.
- **`accelerator_options.device=AUTO`** — docling picks
  CUDA > MPS > CPU. `doctor` (§8 below) reports what it picked so
  operators can spot "extraction silently fell back to CPU and is
  now slow."
- **`artifacts_path=model_cache_dir`** — `None` keeps docling's own
  `~/.cache/docling/models` lookup; a path (from
  `LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR`) points it at an out-of-tree
  weights directory, decoupled from `data_dir`.

## 5. Error taxonomy

Lives in `errors.py` next to the `IngestError` tree (§5). Same
conventions: each error carries DOI + context dict; `cli.py extract`
catches `ExtractError` and renders a Rich panel with class, DOI,
context, and operator hint.

```python
class ExtractError(RuntimeError):
    """Base for all extraction failures."""

# Configuration / dispatch
class DoclingImportError(ExtractError): ...        # [extract] extra not installed
class WrongFormatForExtractorError(ExtractError): ...
class MissingArtifactError(ExtractError): ...      # record points at a file that's gone

# Conversion
class DoclingConversionError(ExtractError): ...    # ConversionStatus == FAILURE
class DoclingDegradedError(ExtractError): ...      # ConversionStatus == PARTIAL_SUCCESS

# Post-extraction sanity
class EmptyDocumentError(ExtractError): ...        # zero text blocks
class ParseDegradedError(ExtractError): ...        # char_count < FLOOR_CHARS

# Commit
class SerializationError(ExtractError): ...
class ExtractIntegrityError(ExtractError): ...            # existing document.json differs, no --reextract
```

### Exit codes

Follow §14's grouping (2 = config, 4 = source/conversion, 6 =
malformed-output, 7 = integrity):

| Class | Exit |
|---|---|
| `DoclingImportError` | 2 |
| `WrongFormatForExtractorError` | 2 |
| `MissingArtifactError` | 2 |
| `DoclingConversionError` | 4 |
| `DoclingDegradedError` | 4 |
| `EmptyDocumentError` | 6 |
| `ParseDegradedError` | 6 |
| `SerializationError` | 6 |
| `ExtractIntegrityError` | 7 |

## 6. Output shapes

### `documents/<sha256>/document.json`

`docling_document.export_to_dict()` verbatim. No transformation. We
treat this file as the source of truth for the structural skeleton
of the paper.

### `documents/<sha256>/meta.json`

```json
{
  "extractor": "docling",
  "extractor_version": "2.x.y",
  "schema_version": "docling-document/<docling export schema ver>",
  "format": "pdf",
  "source_sha256": "<artifact sha>",
  "extracted_at": "2026-05-11T12:00:00+00:00",
  "n_pages": 12,
  "n_tables": 4,
  "n_figures": 5,
  "n_text_blocks": 217,
  "n_section_headers": 9,
  "char_count": 38421,
  "pipeline": {
    "do_ocr": false,
    "do_table_structure": true,
    "table_mode": "accurate",
    "table_structure_kind": "docling_tableformer",
    "do_cell_matching": true,
    "do_formula_enrichment": true,
    "layout_model": "docling_layout_egret_large",
    "document_timeout": 120.0,
    "force_backend_text": false,
    "device": "cuda"
  }
}
```

The `pipeline` block is what lets a later commit decide "is the
existing `document.json` stale because we bumped a docling setting?"
without re-running. It is the extraction-time equivalent of the
ingest manifest's `sdk_version`, and it must record *every* knob the
converter sets that can change output bytes — adding a knob to
`_load_docling` without widening this dict is the one place where a
config change becomes invisible to the stale-detection check
(`docling-settings-buildout.md` §3). `table_structure_kind` and
`force_backend_text` are recorded even though their values are fixed
today, precisely so the open V1→V2 / backend-text decisions
(`docling-settings-buildout.md` §1.2, §1.3) show up here when they
land.

`warnings` is intentionally absent: any condition that would
produce one becomes a hard fail per §3, so the field would be
dead weight.

## 7. Module layout

```
src/litspectraits/extract/
├── __init__.py
├── _dispatch.py    # format → extractor (PDF / JATS / Elsevier-XML)
├── pdf.py          # this plan
├── jats.py         # lxml; landed alongside this commit per §17.10
└── elsevier.py     # lxml; landed alongside this commit per §17.10
```

`extract/pdf.py` exports one public function:

```python
async def extract_pdf(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
) -> ExtractRecord:
    """Convert a PDF artifact to a structured document.

    Implements §3's six-stage pipeline. Raises
    :class:`~litspectraits.errors.ExtractError` subclass on any
    failure; commits to ``documents/<sha>/`` only on full success.
    """
```

`ExtractRecord` lives in `manifest.py` alongside `AcquisitionRecord`:
a small `@frozen` struct with `sha`, `extractor`, `extractor_version`,
`extracted_at`, and the `n_*` counters. Returned in-memory; not
persisted separately (the persisted form is `meta.json`).

## 8. `doctor` extension — docling model preflight

This is the operator preflight that catches "your extraction will
silently fall back to CPU and take 40 minutes per paper" and
"you've never run extract before so the first invocation will block
on a 1–2 GB model download" *before* a batch ingest does.

### What gets added

A new section in `doctor.py`'s output table, gated on the
`[extract]` extra being installed. If the extra is not installed,
the section is rendered as a single greyed row noting the install
hint, exit code unaffected.

Columns mirror the publisher-credentials section (§12) for visual
consistency:

```
┌────────────────────┬──────────┬──────────┬─────────────────────────────────────┐
│ Component          │ Required │ Status   │ Hint                                │
├────────────────────┼──────────┼──────────┼─────────────────────────────────────┤
│ docling[extract]   │ yes      │ ok       │ docling 2.x.y                       │
│ layout model       │ yes      │ ok       │ egret-large, cached at ~/.cache/...  │
│ TableFormer        │ yes      │ ok       │ accurate mode loaded                │
│ code-formula       │ yes      │ ok       │ formula enrichment loaded           │
│ accelerator        │ auto     │ cuda     │ NVIDIA <gpu-name>                   │
│ OCR engines        │ no       │ off      │ do_ocr=False (default)              │
└────────────────────┴──────────┴──────────┴─────────────────────────────────────┘
```

### Checks performed

1. **Extra installed.** `try: import docling` — on `ImportError`,
   row reads `not installed` with hint
   `uv sync --extra extract`. Subsequent rows are skipped. Exit 0
   if all other doctor checks pass — the extract extra is opt-in.
2. **Model cache present.** Probe the docling model cache directory
   (honoring `LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR`; fall back to
   `~/.cache/docling/models`). Per required model — Egret-Large
   layout, TableFormer, code/formula VLM — check the on-disk
   artifact directory exists and is non-empty. The layout folder name
   is read from `litspectraits.extract.pdf.LAYOUT_MODEL_REPO_FOLDER`
   so the probe and the configured layout model can't drift.
3. **Download-if-missing knob.** `doctor --download-models` fetches
   the three required v3 weights. It does **not** just call
   `download_models(with_layout=True, ...)` — that pulls the docling
   *default* layout model (Heron), not the Egret-Large spec the
   extractor configures — so it calls `LayoutModel.download_models(...,
   layout_model_config=DOCLING_LAYOUT_EGRET_LARGE)` for the layout
   weights, then `download_models(with_layout=False, with_tableformer=True,
   with_code_formula=True, ...)` (every OCR / picture-classifier / VLM-
   figure switch pinned `False` — several default to `True` in docling
   2.93). This is the deliberate first-run path. By default `doctor`
   only *reports* missing models; it does not pull them, because the
   operator should know they are about to start a multi-gigabyte
   download (Egret-Large + a formula VLM is meaningfully more than the
   old Heron + TableFormer).
4. **Accelerator detection.** Read
   `torch.cuda.is_available()` / `torch.backends.mps.is_available()`
   to report what `AcceleratorDevice.AUTO` will resolve to. Report
   GPU name on CUDA via `torch.cuda.get_device_name(0)`.
5. **Smoke convert.** Optional behind `doctor --smoke-extract`: run
   `DocumentConverter(...).convert(tests/fixtures/pdf/synthetic.pdf)`
   on the same tiny fixture used in Step 10 tests. Reports success +
   wall-clock time, or surfaces the verbatim docling error. Off by
   default because we do not want every `doctor` invocation to spin
   up the layout model.

### Required vs optional models

Required (used by our `PdfPipelineOptions` — `docling-settings-buildout.md` §2):

- `docling-layout-egret-large` — the layout / region-detection model
  (`layout_options.model_spec = DOCLING_LAYOUT_EGRET_LARGE`)
- `TableFormer` — `accurate` mode (TableFormer V1; the V1→V2 bake-off
  is still open)
- `code-formula` VLM — required because `do_formula_enrichment=True`

Optional (not used in v3):

- `picture-classifier` / `SmolVLM` for figure description
- `EasyOCR` / `RapidOCR` (or any OCR engine) — only relevant if
  `do_ocr=True`, which we do not set
- TableFormer V2, Granite-Vision table reader, chart-extraction — all
  parked; see `docling-settings-buildout.md` §1.2, §1.4

Note this list **must move in lockstep with §4** — bumping the layout
model or toggling formula enrichment changes which weights are
required, and a stale list means `doctor` greenlights a machine that
then fails mid-extract on a missing weight.

Reporting these as `off` rather than `missing` keeps operators from
chasing a phantom error when the v3 default pipeline does not need
them.

### Exit code behaviour

Same policy as the publisher-creds section: exit 0 if all
*configured* checks pass or if the extra is not installed; exit 1 if
the extra is installed *and* a required model is missing *and*
`--download-models` was not requested. A successful
`--download-models` run that ends with all required models present
exits 0.

### Doctor file changes

`doctor.py` gains:

- `check_docling_extra() -> _Row`
- `check_docling_models() -> list[_Row]`
- `check_accelerator() -> _Row`
- `maybe_download_models(force: bool = False) -> None` — invoked
  only when the CLI flag is set
- `maybe_smoke_extract(fixture_path: Path) -> _Row` — invoked only
  when the CLI flag is set

`cli.py doctor` gains:

- `--download-models / --no-download-models` (default off)
- `--smoke-extract / --no-smoke-extract` (default off)

Plain `litspectraits doctor` stays a read-only diagnostic per §12 —
no network for the extract section unless explicitly requested.

## 9. The agentic-workflow shape this enables

The pipeline above stops at `documents/<sha>/document.json` — the
verbatim docling dict. §11 explicitly defers the canonical
`Document` schema until the agent triad work begins; this plan
respects that. But the choices above keep that door open cleanly:

1. **`extract/_normalize.py` (future commit, with the agent triad).**
   Walks the verbatim dict and emits the canonical `Document` from
   `overview.md` — `Block` tagged union of `TextBlock` /
   `TableBlock` / `FigureBlock` / `EquationBlock`, each with
   `section_path` recovered from the heading stack and
   `provenance(kind='docling', page, bbox, char_range)`. Writes
   `documents/<sha>/normalized.json` alongside the verbatim
   `document.json`. The `cattrs` structuring hooks for the tagged
   union land in the same commit (CLAUDE.md flags this as needed
   from day one).
2. **Sentence segmentation** for citation-back-to-source runs at
   agent-consumption time, not extraction time. The agents want
   different granularities (paragraph for scoping context,
   sentence for attributing measurements); paragraph-level blocks
   stay canonical and segmentation is on demand.
3. **Per-cell provenance works end-to-end** because
   `do_cell_matching=True` is on. When the Ingestor proposes a T1
   value from a cell, `source_span = (row, col)` and the highlight
   back to the PDF is one indirection through that cell's bbox.
4. **Reference parsing is a separate post-pass** (`anystyle` /
   `refextract` / LLM-extract) that emits the `Reference[]` array.
   Keeping it out of the extract step means a reference-parser bump
   does not invalidate every PDF extraction.

## 10. Implementation order

This section is the **interior of Step 10** in `overview-v3.md` §21,
which structures the broader extraction work into six commits 10a–10f
covering all three format extractors plus CLI and doctor wiring. The
PDF side touches four of those six commits; this section enumerates
the PDF-side work within each. One commit per overview-v3 substep,
each ending green on `uv run pytest && uv run ruff check && uv run
pyright`.

JATS and Elsevier extractors (overview-v3 §21 10c and 10d) are not
part of this plan; they share `_dispatch.py` and `ExtractRecord` but
emit their own `document.json` dict shapes (no cross-publisher
normalisation yet — deferred per §11).

### Inside 10a — Extract bootstrap

1. **`errors.py` additions.** Add the full `ExtractError` taxonomy
   from §5 — not just PDF-specific classes, so JATS and Elsevier can
   reuse `MissingArtifactError`, `WrongFormatForExtractorError`,
   `ExtractIntegrityError`, etc. when their commits land. No logic —
   context-dict constructors mirroring the `IngestError` shape (DOI +
   context attrs). Tests: each error class instantiates cleanly with
   a DOI; `str(exc)` is useful.
2. **`extract/__init__.py` + `extract/_dispatch.py`.** Three-way
   match on `record.format`. All three legs raise
   `NotImplementedError` until their commits land.
3. **`ExtractRecord` in `manifest.py`.** Small `@frozen` struct;
   round-trip test in `tests/test_manifest.py`.

### Inside 10b — PDF extractor

4. **`extract/pdf.py`.** The six-stage pipeline from §3. Lazy
   `import docling` inside `extract_pdf()`. `asyncio.to_thread`
   around the conversion call. Module-level constants for
   `FLOOR_CHARS`, `MIN_TEXT_BLOCKS`, `MIN_PAGES` with one-line "why."
   Wire the PDF leg of `_dispatch.py` from `NotImplementedError` to
   `extract_pdf`.
5. **Fixture + tests.** One tiny synthetic PDF in
   `tests/fixtures/pdf/synthetic.pdf` — a single page with one
   heading, one paragraph, one 2×2 table. Tests cover: happy path
   (writes both files, `meta.json` has expected counts);
   `WrongFormatForExtractorError` on a JATS record;
   `EmptyDocumentError` via a PDF stripped of text; re-extract
   overwrite without `--reextract` raises `ExtractIntegrityError`; with
   `--reextract` overwrites cleanly.

### Inside 10e — CLI wiring

6. **CLI `extract` command.** `litspectraits extract <doi-or-sha>
   [--reextract]` looks up the record, dispatches via
   `extract/_dispatch.py`, renders Rich error panels for each
   `ExtractError` subclass with the exit codes from §5. Lands after
   the JATS (10c) and Elsevier (10d) extractors so the command works
   for every `Format` from the first commit — but the PDF-side panel
   and exit-code wiring are this plan's contribution to the commit.

### Inside 10f — Doctor docling extension

7. **`doctor.py` extension (§8 above).** The five new check
   functions plus the two CLI flags (`--download-models`,
   `--smoke-extract`). Golden-output test for the extended doctor
   table with the extract section present.

## 11. Tradeoffs and open questions

### Tradeoffs explicitly accepted

- **First-run model download is a multi-GB implicit dependency.**
  Egret-Large layout + TableFormer + the code/formula VLM — bigger
  than the original Heron + TableFormer set, since §4 traded weight
  size for PDF-route quality. Mitigated by the `doctor --download-models`
  step in §8 so it is an *explicit* operator action, not a surprise
  inside the first `extract` run.
- **Determinism is best-effort.** Docling's neural models are
  deterministic given fixed weights + fixed CPU/GPU device, but
  cross-device reruns (CPU → GPU) may produce different bbox
  numbers in the third decimal. We document this as a known shape
  and accept that re-extraction across heterogeneous machines may
  flag `ExtractIntegrityError` requiring `--reextract`. The append-only
  measurement model downstream is robust to this — measurements
  carry their `extractor_version` and the agents reconcile.
- **TableFormer accurate mode is materially slower than fast
  mode.** Acceptable: tables are where the measurements live.

### Open questions for `docs/triage.md`

- **E10-1.** `FLOOR_CHARS = 500` is a guess from "MR papers are ≥
  2 pages." Revisit once we have ≥ 20 real extractions and can plot
  the distribution.
- **E10-2.** `do_cell_matching=True` cost on large tables — measure
  on a real Wiley fixture. If the latency cost is severe and per-
  cell `prov` is rarely consumed in early agent runs, consider
  flipping to `False` and recovering cell positions geometrically.
- **E10-3.** Whether to surface `result.errors` from a
  `PARTIAL_SUCCESS` into a structured `warnings` field on
  `meta.json` for non-fatal cases (e.g. "page N OCR was attempted
  and skipped"). Right now this field is intentionally absent; the
  trigger to add it is the first real-world `PARTIAL_SUCCESS` we
  decide we want to commit anyway.
- **E10-4.** OCR-fallback as an explicit `--ocr` retry on
  `ParseDegradedError`. Not in scope for the initial commit;
  revisit once we have a real scanned-PDF example.
- **E10-5.** Sentence segmentation strategy (regex / spaCy / a
  dedicated segmenter). Lives in the agent triad work, not here,
  but worth tracking so it does not silently land in `pdf.py`.
