# Handoff — MinerU-primary promotion, steps 4–6

Pick-up notes for finishing the promotion of MinerU (`vlm-engine`) to the
primary PDF backend. The design lives in `mineru-primary-promotion.md`; this
doc is the "what's done / what's left / where to touch" companion. Line
numbers are anchors that may drift — trust the function names.

## State of play

**Steps 1–3 are done and green** on `uv run pytest` (673 passed),
`uv run ruff check`, `uv run pyright` (0 errors):

- **Step 1** — defaults flipped: `DEFAULT_PDF_BACKEND = MINERU`
  (`extract/backend_ids.py`), `DEFAULT_MINERU_ENGINE = 'vlm-engine'`
  (`extract/mineru.py`). Docling is the opt-in fallback.
- **Step 2** — auxiliary outputs: the extractor now emits `document.md` +
  `images/` beside the canonical `document.json` (`f_dump_md=True`,
  `_ParseOutputs`, `_read_aux_outputs`, `_write_aux_outputs`,
  `_aux_outputs_descriptor`; `meta.json` gains `aux_outputs`). `normalize`
  still reads `document.json` only.
- **Step 3** — dispatch/CLI text + tests. Notable behavioural decision: the
  MinerU-knob guard `_reject_mineru_knobs_if_inapplicable`
  (`extract/_dispatch.py`) is now **format-aware** — the knobs are applicable
  only on a *PDF artifact parsed by mineru*, so `--mineru-effort high` on an
  XML artifact still fails loud instead of silently no-op'ing now that mineru
  is the default backend.

**Steps 4–6 remain.** They are independent of each other except that step 6
(smoke) is the real end-to-end validation and should run last.

## Decisions already locked (do not re-litigate)

- Engine = `vlm-engine` (full VLM parse), accepting corpus-wide
  `geometry_fidelity='approximate'`. `hybrid-engine`+high (exact geometry) is
  the reversible alternative via `DEFAULT_MINERU_ENGINE`.
- Output = JSON canonical + markdown/images auxiliary; `normalize` never reads
  markdown.
- Install = add `mineru` to the `[extract]` extra (not a base dep; the
  `[docling]`-reframe is deferred — spec §3).
- `doctor --smoke-extract` retargets to the default path (mineru/vlm-engine).

---

## Step 4 — `doctor` reframe (MinerU as the backbone)

**Goal:** `doctor` must treat MinerU as the required primary and docling as
the informational alternative — the inverse of today. All work is in
`doctor.py` + `tests/test_doctor.py`.

### 4.1 `_check_extract_section` (doctor.py:511-580) — order + short-circuit
- **Reorder:** probe MinerU first, docling second. Today docling's
  `extra_row` is built at :533 and MinerU is appended at :570-575.
- **Invert the short-circuit:** today (:536) an absent `[extract]`/docling
  short-circuits docling's model/smoke rows. After the flip, the *primary*
  short-circuit is on the MinerU extra — an absent `[mineru]` is the required
  failure; skip (or mark `OFF`) the MinerU model/smoke rows, and still probe
  docling as the optional alternative.
- `has_required_failure` (:577-579) is already
  `any(row.is_required and status != OK)` — once the `is_required` flags below
  flip, absent `[mineru]` / absent vlm weights correctly flip the exit code.

### 4.2 `_check_mineru_extra` (doctor.py:748-776) — becomes required
- Absent branch: `status=OFF, is_required=False` → **`status=MISSING,
  is_required=True, required='yes'`**; update the detail hint
  (`uv sync --extra extract` now, since mineru rides in `[extract]` — see step
  5). It is now a gap to fix, not a deliberate opt-out.
- OK branch: `is_required=False` → **`True`**, `required='no'` → **`'yes'`**.

### 4.3 `_check_mineru_models` (doctor.py:779-834) — vlm family required
- The **vlm** family backs the default engine → its row must be
  `is_required=True` (`required='yes'`). The **pipeline** family stays
  informational (`is_required=False`) — only `--mineru-engine pipeline` needs
  it. Simplest: derive from the arg, e.g.
  `is_required = (kind == 'vlm')`, or key on the family that
  `DEFAULT_MINERU_ENGINE` actually loads (vlm-engine → `'vlm'`). Apply in all
  three return points (:801, :812, :828) so the OFF / could-not-resolve /
  MISSING rows carry the right flag.
- Update the docstring (:789-791) — the "must not fail a docling-primary
  operator" rationale is now inverted for the vlm row.

### 4.4 `_check_docling_extra` + `_model_row` — docling drops to optional
- `_check_docling_extra` (doctor.py:583-605): OK branch `is_required=True`
  (:602) → **`False`**. docling is no longer required.
- `_check_docling_models` / `_model_row` (doctor.py:665-682): the layout /
  TableFormer / code-formula rows are `is_required=True` (:672, :679) →
  **`False`**. An absent docling cache must not fail a mineru-primary doctor.

### 4.5 `_maybe_smoke_extract` (doctor.py:930-982) — retarget to mineru/vlm
- Swap the local import `from litspectraits.extract.pdf import extract_pdf`
  (:953) for `extract_mineru`, and call it with the default engine/effort
  (`engine=DEFAULT_MINERU_ENGINE, effort=DEFAULT_MINERU_EFFORT`). MinerU
  resolves its own weights (no `model_cache_dir`), so drop that arg.
- Keep it opt-in and captured (`except ExtractError`) — the vlm smoke is
  heavy (needs vlm weights + likely GPU). Update the docstring and the
  `_COMPONENT_SMOKE` detail wording.
- `--download-models` already pulls both MinerU families
  (`_maybe_download_mineru_models`, :837) — no change.

### 4.6 Comments + tests
- Rewrite the framing comments at doctor.py:487-499, :536-540, :566-569 that
  call MinerU "an alternative… never flips the exit code."
- `tests/test_doctor.py`: flip the required/optional expectations, the
  smoke-target assertion, and any `has_required_failure` cases. The
  `extract_real` marker (pyproject `[tool.pytest.ini_options].markers`) gates
  the live-convert tests.

**Gotcha:** `OFF` vs `MISSING` carry meaning — `OFF` = "deliberately not
configured" (no gap), `MISSING` = "a gap to fix." The vlm/[mineru] rows move
from `OFF` to `MISSING` because their absence is now a real failure.

---

## Step 5 — packaging + docs + memory

### 5.1 `pyproject.toml`
- Add `mineru[pipeline,vlm]>=2.0` to the `[extract]` extra (:27) so
  `uv sync --extra extract` yields a working default path. Keep the standalone
  `mineru` extra (:43) and `all` (:44-46) as-is. Update the `[extract]` /
  `mineru` comments (:23-42) to name MinerU the primary.
- **Verify torch co-resolution after the move:** run `uv sync --extra extract`
  in isolation and confirm no torch bump breaks docling. The pyproject comment
  already claims docling + `[pipeline]` + `[vlm]` share `torch 2.11+cu126`, but
  re-confirm once they're in one extra.
- **CI note:** installing `[extract]` now pulls the MinerU wheel (heavier).
  Check what CI installs — a mineru-in-`[extract]` CI is slower; decide if CI
  should install a lighter set or accept it.

### 5.2 Docs
- `README.md`: env-var default column (:93, `docling-standard` → `mineru`),
  the backend narrative (:176-180), the PDF-backends table (:356-395), and the
  install commands.
- `CLAUDE.md`: the CLI listing (:51, `--backend` default) and the
  "one canonical route" / default-backend note.
- `mineru-backend-spec.md`: cross-reference the promotion; §11 Q-D
  (approximate geometry) / Q-E (hallucination guard) are now live accepted
  gates, not open questions.

### 5.3 Memory (`~/.claude/.../memory/`)
- Update `project_mineru_backend_spike.md` to record: default flip to
  mineru/vlm-engine, aux markdown+images output, the format-aware knob guard,
  and the doctor reframe. Refresh its `MEMORY.md` pointer line. (Update, don't
  duplicate — there's already a spike memory.)

---

## Step 6 — gated `vlm-engine` smoke (the real validation)

Everything above is unit-tested against a **mocked** `do_parse`. This step is
the first time the real `vlm-engine` `middle.json` / `.md` / `images` shape
flows through our extractor + adapter.

- Add a smoke test under `tests/smoke/` marked `smoke` +
  `requires_wiley_creds` (see the pyproject markers). Needs vlm weights + GPU +
  Wiley TDM access.
- Flow: `ingest 10.1002/mrm.27665` → `extract` (bare = mineru/vlm-engine) →
  `normalize`. Assert:
  - `route == 'mineru'`;
  - blocks carry `geometry_fidelity == 'approximate'` (confirms
    `_engine_fidelity` maps the real vlm `_backend` tag correctly — **verify
    what vlm-engine actually stamps in `middle.json._backend`**; the adapter
    treats anything ≠ `'pipeline'` as approximate);
  - `document.md` + `images/` landed and `meta.aux_outputs` names them;
  - spot-check a known table value / inline-formula against the reviewed
    `mrm27665-vlm.md` reference (e.g. the Table 2 bias/SD values, or an
    inline `$U_r$` / display-equation LaTeX).
- Record the result in `triage.md` and the memory update from 5.3.

**Risk:** if the real vlm `middle.json` diverges structurally from the
`pipeline` fixtures the adapter was built on (block types, bbox presence,
`_backend` value), the adapter may need small fixes — budget for that.

---

## How to verify each step

Each step ends green on the full trio:

```
uv run pytest        # full suite; add -m smoke for step 6
uv run ruff check
uv run pyright
```

Run `uv run ruff format <files>` before the check if you hand-wrap long lines.

## Still-open questions (deferred, spec §9)

- **P2** — vlm-engine (approximate geometry) vs hybrid-engine+high (exact).
  Reversible via `DEFAULT_MINERU_ENGINE` / `DEFAULT_MINERU_EFFORT`.
- **Q-E** — hallucination guard on the canonical vlm formula/table output
  (render-back / repetition-loop that fails loud).
- **Packaging reframe** — `[extract]` = mineru only, docling → `[docling]`.
- **Image-persist weight** — ~1–4 MB/doc; drop to markdown-only (skip the
  `images/` copy in `_write_aux_outputs`) if corpus size demands it.
