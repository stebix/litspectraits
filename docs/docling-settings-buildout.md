# Docling settings — buildout note

What `PdfPipelineOptions` should be set to for an MRI-literature corpus, why, and
the small amount of machinery we keep around the *possibility* of changing it
later. This is subordinate to `extract-pdf-plan.md` §4 (which holds the live
config and the per-switch rationale for what is already set) and `overview-v3.md`
§11; it informs `agentic-buildout-sketch.md` §1.5 (the PDF route is the
lower-fidelity one — several of these knobs are *why*, and a couple of them
narrow the gap).

## 0. The governing principle: pick once, freeze, move on

Docling is a fixed dependency, not a tuning playground. The corpus is
born-digital publisher PDFs (Wiley TDM + library-proxy sideloads) — a narrow,
well-behaved input distribution. The right move is to spend **one** bounded
effort picking a quasi-optimal configuration against the gold set's PDF-route
fixtures, write it down in `extract-pdf-plan.md` §4, and then stop. Every knob
we leave "flexible" is accidental complexity: a CLI flag to plumb, a `meta.json`
field to record, a stale-detection branch to maintain, a combinatorial test
matrix, an operator decision that didn't need to exist. The PDF route is already
the minority slice (only Wiley TDM returns PDF; Elsevier and Springer return
XML) and the messier one — it does not also deserve a config surface.

So this doc is mostly "here is the target config and the reasoning"; the
"what if we revisit it" part (§4) is deliberately small and gated.

**Status.** §1.2 (Egret-Large layout, `do_formula_enrichment=True`,
`document_timeout=120.0`), the §2 target config, and the §3 `pipeline_view`
widening have **landed** — `litspectraits.extract.pdf._load_docling`, the
`doctor` required-models list, and `extract-pdf-plan.md` §4 / §6 / §8 all
reflect them. Still **open** (fixture-gated, §1.3 + the TableFormer-V2 row of
§1.2): `force_backend_text` stays `False` and TableFormer V1 stands until the
gold-set PDF-route fixtures exist to decide the bake-off. `meta.json`'s
`pipeline` block records `force_backend_text` and `table_structure_kind`
already so a future flip is detectable.

Two facts that make freezing safe:

- **No CLI surface for any of these.** `extract-pdf-plan.md` §4 already commits
  to this. If one genuinely must flex later it becomes a deliberate, reviewed
  code change to the converter builder — never an env var, never a `--flag`.
- **`meta.json` already records the config that produced each document** (the
  `pipeline` block, the extraction-time analogue of the ingest manifest's
  `sdk_version`). A future config bump is detectable as "this extraction is
  stale" without re-running everything. That mechanism is the *entire* reason
  we can afford to change settings later without it being a project — provided
  the recorded view actually covers the knobs we touch (see §3).

## 1. The knob inventory, with verdicts

Pulled against docling 2.93 (`docling_ibm_models` 3.13, TableFormer V1 + Heron
layout the defaults). Grouped by decision, not by where they sit in the option
tree.

### 1.1 Already set, keep as-is (see `extract-pdf-plan.md` §4 for the why)

`do_ocr=False`; `do_table_structure=True`; `TableFormerMode.ACCURATE`;
`do_cell_matching=True`; `generate_picture_images=False`; `images_scale=1.0`;
`accelerator_options.device=AUTO`; `artifacts_path` from
`LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR`. These are the right calls for the input
distribution and nothing here argues with them.

### 1.2 Worth changing now — the short list

The first three rows have **landed** (see Status above); the
`table_structure_options` (V1→V2) row is still open — fixture-gated, same as
§1.3.

| Knob | Default | Target | Why it matters for MR papers |
|---|---|---|---|
| `layout_options.model_spec` | `DOCLING_LAYOUT_HERON` | `DOCLING_LAYOUT_EGRET_LARGE` (or `HERON_101`) | **Highest-leverage quality knob.** Two-column papers with tables and figures interleaved are exactly where region detection earns its keep — clipped tables, captions attached to the wrong float, body text bleeding into a table cell all originate here. A stronger detector is the cheapest way to lift the floor on the PDF route. Cost: bigger weights, slower per-page inference. |
| `table_structure_options` | `TableStructureOptions` (TableFormer **V1**) | evaluate `TableStructureV2Options` (V2); keep V1 if it doesn't win on the relaxometry fixtures | Tables are the corpus's centre of mass and `agentic-buildout-sketch.md` flags docling table output as the lossy path. V2 exists; a head-to-head on the multi-level-header relaxometry tables in the gold set is the deciding test. (`GraniteVisionTableStructureOptions` — a VLM table reader — is a heavier fallback if neither TableFormer mode handles the gnarly headers; default to *not* using it.) |
| `do_formula_enrichment` | `False` | `True` | Closes the `EquationBlock`-empty-on-PDF-route gap from `agentic-buildout-sketch.md` §1.5: MR signal-model and fitting equations become first-class instead of falling out as stray text or picture items. Equations rarely *carry* the measurement value, but a fit equation is interpretive context. Cost: an extra VLM pass (`code_formula_options` / `CodeFormulaVlmOptions`). Acceptable given the PDF slice is small. |
| `document_timeout` | `None` | `120.0` | Not a quality knob — fail-loud hygiene. A timeout produces `ConversionStatus.PARTIAL_SUCCESS`, which `extract/pdf.py`'s `_check_conversion_status` already treats as a hard `DoclingDegradedError`. So this converts "a pathological PDF hangs the worker forever" into the loud, typed failure the project wants everywhere else. |

### 1.3 Worth a test before deciding — one knob

| Knob | Default | Question |
|---|---|---|
| `force_backend_text` | `False` | Setting `True` bypasses the layout model's text detection and uses the PDF's embedded text layer directly. For born-digital publisher PDFs that *raises* text fidelity (no layout-model transcription drift) — and exact text fidelity is load-bearing because the verbatim-anchor gate (`agentic-buildout-sketch.md` §5.3) keys on substrings. The risk: it trusts the PDF's own reading order, which two-column layouts sometimes scramble, and a scrambled paragraph breaks both reading and anchoring. Decide it on fixtures: if reading order survives, `True` is a clean win; if it doesn't, leave it `False`. Pairs with the layout-model choice — a stronger layout model makes `False` better, so test the combination, not the knob in isolation. |

### 1.4 Explicitly *not* now (kept here so the decision is recorded, not re-litigated)

- `do_code_enrichment` — code-aware OCR; no code in MR papers.
- `do_picture_description` / `do_picture_classification` — VLM figure captioning;
  we already get captions from the PDF text, and pixel-reading figures is a v1
  non-goal (`overview.md`).
- `do_chart_extraction=True` — turns bar/line/pie charts into tables. Tempting,
  because some relaxometry comparisons are plotted rather than tabulated — but
  it is the figure-reading non-goal under a different name, auto-enables picture
  classification, and pulls in a VLM. Parked as a possible v2 lever; out of
  scope for v1.
- `PdfBackend` — keep `DOCLING_PARSE` (the default; `PYPDFIUM2` is faster and
  dumber). Set on `PdfFormatOption(backend=...)`, not in `pipeline_options`, so
  it's a separate line if it ever changes.
- `ocr_batch_size` / `layout_batch_size` / the threaded-pipeline batching knobs
  — throughput at batch-ingest scale, not quality; revisit only if and when
  batch ingest is a bottleneck, and even then it's a perf decision with no
  bearing on output bytes.
- `generate_parsed_pages` — keeps intermediate parse structures in memory;
  debugging aid only.

## 2. The target configuration

The `_build_converter()` in `extract-pdf-plan.md` §4 (and the live
`_load_docling()` in `extract/pdf.py`), with the §1.2 changes folded in and the
§1.3 knob pending a fixture test:

```python
pipeline_options = PdfPipelineOptions(
    do_ocr=False,
    do_table_structure=True,
    table_structure_options=TableStructureOptions(   # or TableStructureV2Options(...)
        mode=TableFormerMode.ACCURATE,               #   pending the bake-off in §4
        do_cell_matching=True,
    ),
    do_formula_enrichment=True,                      # new — populates EquationBlock on PDF route
    layout_options=LayoutOptions(
        model_spec=DOCLING_LAYOUT_EGRET_LARGE,       # new — the high-leverage region-detector upgrade
    ),
    document_timeout=120.0,                          # new — PARTIAL_SUCCESS → loud DoclingDegradedError
    generate_picture_images=False,
    images_scale=1.0,
    accelerator_options=AcceleratorOptions(device=AcceleratorDevice.AUTO),
    artifacts_path=model_cache_dir,
)
# force_backend_text: decide on fixtures (§1.3); default False until then.
```

`doctor`'s docling model preflight (`extract-pdf-plan.md` §8) lists the required
model artifacts — note that bumping the layout model to Egret and enabling
formula enrichment **changes that list**: the Egret weights and the
code/formula VLM weights become required downloads. The doctor "required vs
optional models" split has to move in lockstep with §2 here, or `doctor` greenlights
a machine that then fails mid-extract on a missing weight.

## 3. The one coupling that has to hold

`extract/pdf.py` builds a `pipeline_view` dict that lands in `meta.json`. It
**now** records, alongside the original five fields (`do_ocr`,
`do_table_structure`, `table_mode`, `do_cell_matching`, `device`): the layout
model name (`layout_model`), `do_formula_enrichment`, `document_timeout`,
`force_backend_text`, and the table-structure backend kind
(`table_structure_kind` ∈ {`docling_tableformer`, `docling_tableformer_v2`}).
The rule that produced that list still stands for any future knob: if the
recorded view doesn't cover a setting that can change output bytes, the "is
this extraction stale because we changed docling settings?" check silently
misses the change, and the freeze-and-record discipline that makes §0 safe
stops working. This is cheap — it's a dict literal — but it is not optional,
and it is the one place where adding a knob has a mandatory follow-on edit
(`extract-pdf-plan.md` §6 says the same to the implementer).

## 4. If we ever revisit the settings (the small role)

This should be rare — ideally once, at the gold-set bake-off, and then not
again until a docling major version forces a look. The procedure when it does
happen:

1. **It's a code change to the converter builder, reviewed like any other.** Not
   a flag, not an env var. The whole point of §0 is that the config is a fact
   about the pipeline, not an operator dial.
2. **Decide it on the gold set's PDF-route fixtures**, the same ~50–200-paper
   set `agentic-buildout-sketch.md` §5.9 calls for — specifically the relaxometry
   tables and the two-column-with-floats layouts, since that's where the knobs
   bite. Anchor rate and table-cell coverage are the metrics that matter; raw
   "char count went up" is not evidence of anything.
3. **Bump the recorded `pipeline_view` and the doctor required-models list** in
   the same change (§3, §2). A config change that isn't visible in `meta.json`
   is a config change that can't be reasoned about afterward.
4. **Don't re-extract the whole corpus reflexively.** `meta.json` now says which
   documents were produced under the old config; re-extract on demand (the
   `--reextract` path already exists) or in a deliberate batch, not as an
   automatic side effect of the config landing. The append-only data model means
   a stale `document.json` is a known, queryable state, not a corruption.

That's the entire "change management" story, and it's intentionally this short.
The accidental complexity we're refusing to take on — per-knob flags, a settings
file, runtime overrides, an A/B harness baked into the extract path — would each
cost more than it ever returns on an input distribution this narrow.
