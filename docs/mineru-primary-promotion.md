# Promoting MinerU (vlm-engine) to the primary PDF backend

Turn the PDF→`Document` default from `docling-standard` (text-layer) to
**MinerU's `vlm-engine`**, on the strength of MinerU's dramatically better
formula/table recovery on Wiley PDFs. This is the promotion the
`mineru-backend-spec.md` §11 anticipated — the wiring already exists; this
spec is the *default flip* plus the packaging / `doctor` / output changes
that make MinerU the fundamental backend rather than an opt-in alternative.

Subordinate to `overview-v3.md`, `mineru-backend-spec.md`, and `CLAUDE.md`;
where they disagree, those win and this file is updated.

Status: **in progress** — steps 1–3 landing first (see §8).

---

## 0. The decisions this spec makes

**P1 — The default PDF backend becomes `mineru`, default engine `vlm-engine`.**
Two compile-time constants flip:
`DEFAULT_PDF_BACKEND = 'docling-standard' → 'mineru'` and
`DEFAULT_MINERU_ENGINE = 'pipeline' → 'vlm-engine'`. A bare
`litspectraits extract <doi>` on a PDF now runs MinerU's VLM parse. docling
stays fully wired as the opt-in fallback (`--backend docling-standard`);
nothing is deleted.

**P2 — `vlm-engine`, not `hybrid-engine`.** The user confirmed the full-VLM
engine (matching the reviewed `mrm27665-vlm.md` output). The consequence,
accepted here: `Provenance.geometry_fidelity` becomes `'approximate'`
corpus-wide (VLM-predicted bboxes), i.e. the spec's §11 Q-D gate is now a
live, accepted trade rather than an open question. The alternative
(`hybrid-engine` + `--mineru-effort high`, which keeps `'exact'` text-layer
geometry) is a one-constant switch if the geometry loss ever bites.

**P3 — MinerU is a first-class install + preflight component, not an
appendage.** The `[extract]` extra pulls MinerU; `doctor` leads with it and
treats its absence as a required failure. See §3, §4.

**P4 — JSON is canonical, markdown is auxiliary.** The extractor also
persists MinerU's markdown render + extracted images next to the verbatim
`middle.json`, for human QA. `normalize` continues to read **only**
`document.json` — CLAUDE.md rejects markdown round-trips because they lose
inline-reference offsets. See §5.

---

## 1. Core default flip (§ step 1)

- `extract/backend_ids.py`: `DEFAULT_PDF_BACKEND = MINERU`. Update the
  `MINERU` / `DEFAULT_PDF_BACKEND` docstrings (MINERU no longer means "the
  pipeline engine"; it defaults to `vlm-engine`).
- `extract/mineru.py`: `DEFAULT_MINERU_ENGINE = 'vlm-engine'`.
  `DEFAULT_MINERU_EFFORT` stays `'medium'` (vlm-engine ignores effort).
  Rewrite the `DEFAULT_MINERU_ENGINE` docstring (was "text-layer, exact
  geometry… preserves prior behaviour").

The `_reject_*` helpers in `extract/_dispatch.py` key on "value ==
DEFAULT means unset", so they stay coherent after the flip without logic
changes:
- bare XML extract: `backend == DEFAULT_PDF_BACKEND (== mineru)` → tolerated,
  routes to the XML leg;
- explicit `--backend docling-standard` on XML → correctly rejected (a PDF
  backend on an XML artifact);
- `--mineru-engine <non-default>` on a non-mineru backend → still rejected.

**Behavioural consequences to own:**
- Default extractions now record `geometry_fidelity='approximate'` (P2).
- The default path now requires the `[mineru]` extra + the `vlm` weight
  family. `MineruImportError` / missing-weights become *default-path*
  (exit-code) failures — intended under the fail-loud model.
- Re-running a bare `extract <doi>` on a DOI previously extracted with
  docling trips `ExtractIntegrityError` (the document bytes differ) — the
  guard doubles as an unintended-backend-swap catch. Switching is the
  deliberate `--reextract` → `normalize --renormalize`.

---

## 2. Dispatch + CLI surface (§ step 3)

No logic change in `extract/_dispatch.py` (only docstrings). CLI text in
`cli.py`:
- `--backend` / `--mineru-engine` / `--mineru-effort` help strings (they
  hardcode "default docling-standard" / "pipeline (default)").
- `cmd_extract` docstring.
- `_EXTRACT_HINTS[MineruImportError]` (`cli.py:266`) and the
  `_NORMALIZE_HINTS[UnknownBackendError]` (`:254`) framing — reword so
  MinerU reads as the primary and docling as the fallback.

The `--json` / panel already surface the resolved `backend_id`; no
structural change, just the default value they report.

---

## 3. Packaging — MinerU in the standard extraction install (§ step 5)

`pyproject.toml`: add `mineru[pipeline,vlm]>=2.0` to the `[extract]` extra
so `uv sync --extra extract` yields a working default path (primary MinerU +
docling fallback). Base `dependencies` stay lean (ingest/doctor-only users
don't pull the VLM stack). `all` is unchanged (already bundles both). The
torch co-resolution under the `cu126` pin is already verified in the
existing `[extract]` / `mineru` comments.

> Open (deferred): the cleaner "reframe" — `[extract]` = mineru only, docling
> moved to its own `[docling]` extra — better encodes P1 but renames a stable
> extra. Not taken now; revisit if the two-backend `[extract]` proves heavy.

---

## 4. `doctor` — MinerU as the backbone (§ step 4)

Invert the current "docling primary, MinerU appended and informational"
posture (`doctor.py:487-499`, `:536-540`, `:566-569`):

- **Order:** report MinerU first, docling second (the alternative).
- **Required flips:** `[mineru]` extra + the **vlm** weight family →
  `is_required=True` (absence flips the exit code). The `pipeline` weight
  row → `is_required=False` (only the non-default `--mineru-engine pipeline`
  needs it). docling extra + its models → `is_required=False` even when
  installed.
- **Short-circuit:** absent `[mineru]` is the required failure; absent
  docling is healthy.
- **`--smoke-extract`** targets the default path — a live MinerU
  `vlm-engine` convert on the packaged synthetic PDF (needs vlm weights;
  stays opt-in / heavy). A docling smoke may remain as a secondary opt-in.

---

## 5. Output layout — JSON + markdown + images (§ step 2)

The extract output directory is sha-addressed (content-addressed on the PDF
bytes); the DOI resolves to it via `index/by_doi.jsonl`. After this change:

```
documents/sha256/<aa>/<sha>/
├── document.json   # verbatim MinerU middle.json — canonical; normalize reads this
├── meta.json       # backend_id='mineru', engine/effort, + aux_outputs pointer
├── document.md      # NEW — auxiliary human-QA markdown (f_dump_md=True)
└── images/          # NEW — figures the markdown references
```

Extractor changes (`extract/mineru.py`):
- `_run_do_parse`: flip `f_dump_md=True`; after reading `*_middle.json`, read
  back the sibling `*.md` and its `images/` dir **before** the scratch tree
  is `rmtree`d. Return a `_ParseOutputs(middle_json, markdown, images)`.
- `_commit`: after the canonical `document.json` + `meta.json` writes, write
  `document.md` and each `images/*` via the same `atomic_write` discipline.
  The no-op path (unchanged `document.json` bytes) leaves aux outputs as-is.
- `_build_meta`: record `aux_outputs = {'markdown': 'document.md',
  'images_dir': 'images'}` (keys omitted when the corresponding output was
  not produced).

Invariants held:
- `normalize` reads only `document.json`; markdown/images are never
  round-tripped into the schema (CLAUDE.md).
- Markdown/images live in the **`documents/`** (extract) tree, not
  `normalized/`. They are a raw-parser artifact, distinct from
  `show-document`'s post-normalize HTML.
- The integrity gate stays keyed on `document.json`; aux outputs are
  best-effort companions written whenever the canonical doc is written.

---

## 6. Errors

No new error leaves. The existing taxonomy already covers the default path:
`MineruImportError` (exit 2), `MineruConversionError` (exit 4),
`EmptyDocumentError` / `ParseDegradedError` / `SerializationError` (exit 6),
`ExtractIntegrityError` (exit 7). Aux-output write failure surfaces as the
underlying `OSError` (loud) — it is not silently swallowed.

---

## 7. Tests

- `tests/extract/test_dispatch.py`: `test_non_default_backend_on_xml_raises`
  passes `backend=DOCLING_STANDARD` now (MINERU == the default == "unset").
  `test_mineru_engine_on_docling_backend_raises` uses `'hybrid-engine'`
  (≠ new `vlm-engine` default) → stays valid.
- `tests/test_cli.py`: flip the three default-assertions
  (`:1039`, `:1057-1063`, `:1088-1096`) to `mineru` / `vlm-engine` /
  `medium`. The invalid-backend test stays green (both ids still listed).
- `tests/extract/test_mineru.py`: assert `document.md` + `images/` land and
  `meta.json` records `aux_outputs`; a positive "bare default →
  backend=mineru, engine=vlm-engine" path.
- `tests/normalize/test_mineru_adapter.py`: a `vlm-engine` `middle.json`
  fixture whose `_backend` tag → `geometry_fidelity='approximate'` as the
  default path (builds on the existing `approximate` case).
- `tests/test_doctor.py`: rewrite required/optional expectations for the
  reframe.

---

## 8. Order of work

Each step ends green on `uv run pytest && uv run ruff check && uv run
pyright`.

1. **Core default flip** (§1) — constants + docstrings; run suite to
   enumerate fallout.
2. **Aux markdown/images output** (§5) — extractor + meta.
3. **Dispatch/CLI text + tests** (§2, §7) — help/hints/docstrings, fix
   dispatch + CLI tests, add positive default tests.
4. **`doctor` reframe** (§4) — order, required flips, smoke retarget +
   `test_doctor.py`.
5. **Packaging + docs + memory** (§3) — `[extract]` extra, README, CLAUDE,
   `mineru-backend-spec.md` cross-ref, memory update.
6. **Gated `vlm-engine` smoke** on `10.1002/mrm.27665` — confirm
   `route='mineru'`, `geometry_fidelity='approximate'`, `document.md` /
   `images/` present, formula/table values match the `-vlm` reference.
   Record in `triage.md` + memory.

---

## 9. Open decisions

- **P2 revisit.** `vlm-engine` (approximate geometry) vs `hybrid-engine` +
  high effort (exact geometry, VLM formula/table). Chosen: vlm-engine.
  Reversible via `DEFAULT_MINERU_ENGINE` (+ `DEFAULT_MINERU_EFFORT`).
- **Hallucination guard (Q-E).** vlm-engine's formula/table heads are
  autoregressive; the canonical path currently trusts them. Whether to add a
  render-back / repetition-loop guard that fails loud is deferred.
- **Packaging reframe** (§3) — `[docling]` as a distinct extra.
- **Image-persist weight** (§5) — ~1–4 MB/doc of figures. Drop to
  markdown-only (skip the `images/` copy) if corpus size demands it.
