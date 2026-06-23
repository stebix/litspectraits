# litspectraits — build status and next steps

Last updated: 2026-06-23. Branch: `spike/normalized-document`.

---

## What is done

### Phase 0 — Pre-flight
- Deleted v1/v2 acquisition + resolver packages, superseded docs, and stale tests.
- `CLAUDE.md` rewritten for v3 design.

### Steps 1–9 — Ingest core (fully landed, all green)
- **Bootstrap:** `pyproject.toml` extras (`[wiley]`, `[springer]`, `[extract]`, `[all]`), `config.py`, full `IngestError` taxonomy in `errors.py`.
- **Data model:** `manifest.py` — `AcquisitionRecord`, `ManualProvenance`, `cattrs` converter.
- **Store:** `store.py` — sha256-sharded artifact layout, atomic `os.replace`, DOI index.
- **Sniff:** `sniff.py` — PDF / JATS / Elsevier-XML magic-byte classifier (declaration-optional).
- **Metadata + dispatch:** `metadata.py` — CrossRef polite-pool fetch, DOI-prefix → publisher table.
- **Retrievers:** `retrievers/wiley.py`, `springer.py`, `elsevier.py`, `_ratelimit.py`, `dispatch.py`.
- **Ingest orchestrator:** `ingest.py` — five-step happy path, `--cache-hit-ok` short-circuit.
- **Sideload:** `sideload.py` — PDF-only, `ManualProvenance`, idempotent on `(doi, sha256)`.
- **CLI + doctor:** all commands wired; Rich error panels; class-specific exit codes.
- **End-to-end smoke tests:** three-publisher gated suite (`-m smoke`), verified live.

### Step 10 — Extraction (fully landed, all green)
- **PDF:** `extract/pdf.py` via docling — six-stage pipeline, lazy import, `asyncio.to_thread`.
- **JATS:** `extract/jats.py` — lxml, namespace-agnostic xpath, section/table/reference walk.
- **Elsevier:** `extract/elsevier.py` — CEP markup, CALS table projection, META_ABS guard.
- **Shared helpers:** `extract/_lxml_helpers.py` — xpath walkers, atomic IO, `serialize_document`/`commit_document`.
- **CLI `extract` command**, doctor docling extension, per-format tests (full error matrix).

### E0.5 — Normalization (fully landed, all green)
- **Schema:** `normalize/models.py` — `Document`, `Completeness`, `Block` tagged union (`TextBlock`, `TableBlock`, `FigureBlock`, `EquationBlock`), `InlineRef`, `Reference`.
- **Adapters:** `normalize/xml_adapter.py` (JATS + Elsevier), `normalize/docling_adapter.py` (PDF).
- **Persistence:** `normalize/persistence.py` — sha256-sharded `normalized/` tree, `load_normalized_document`.
- **CLI `normalize` command**, dual-route diff harness, `diff-routes` CLI command.
- **`EquationBlock`** emitted from both JATS and Elsevier extractors.
- **sha256-sharding** applied to both `documents/` and `normalized/` trees.

### Rendering MVP + `list` discovery (landed, all green)
- `src/litspectraits/normalize/render.py` — pure `render_html(doc, context)`; self-contained HTML, no JS, raw math, faithful projection, route/completeness badges, loud-raise on overlapping inline refs.
- `show-document` CLI command (`cli.py`); `render_html` + `RenderContext` exported from `normalize/__init__.py`.
- `tests/normalize/test_render.py` — per-block units, escaping-with-overlap, determinism, completeness badges, and frozen golden HTML per route in `tests/normalize/golden/` (regen via `LITSPECTRAITS_REGEN_GOLDEN=1`).
- `list` discovery command — `cmd_list` over `ArtifactStore.iter_index()` (`DOIIndexEntry` roll-up); table / `--quiet` / `--json` modes; `diff.py` refactored to reuse `iter_index()`.
- `.gitignore` ignores root-level `<sha>.html` so a `show-document` run never pollutes the tree.
- Spec: `docs/rendering-mvp-plan.md`.

---

## Test count

562 passing, 3 deselected (smoke) as of 2026-06-23.

---

## Next steps

### Immediate — reproducibility tripwire on the document *format*

Golden/regression coverage today stops at the HTML render layer; the canonical
`Document` JSON (the "common document format") is not frozen, so an adapter or
schema drift that changes the JSON without changing the rendered HTML would slip
through. The PDF path mocks docling at `_load_docling`, and `Document` JSON is
timestamp-free (the `normalized_at` / `extracted_at` stamps live in the record
*wrappers*), so normalize-from-fixture is fully deterministic — the right seam to
freeze.

- [ ] Add a golden `document.json` per route (jats / elsevier / docling) built from frozen fixture artifacts, with a `LITSPECTRAITS_REGEN_GOLDEN`-style regen path mirroring the HTML golden.
- [ ] Add one end-to-end pipeline test: stored fixture artifact bytes → `extract` → `normalize` → assert `== golden Document`. No network, docling mocked — proves the whole chain reproducible from bytes, not just its pieces.
- [ ] Make the determinism boundary explicit (assertion or doc note): `Document` JSON is timestamp-free; record wrappers carry timestamps; real-docling ML is the only nondeterminism and sits outside the frozen seam.

### Medium-term — measurement extraction

Per `docs/agentic-buildout-sketch.md` and `overview.md` §agent-triad:

- [ ] Define the measurement record schema (value, unit, field strength, sequence, tissue, in-vivo/ex-vivo/phantom, scanner, provenance back-pointer to source block).
- [ ] Implement the **Ingestor agent** — reads a `Document`, proposes measurement records from `TextBlock` / `TableBlock` content, writes to append-only event log.
- [ ] Implement the **Auditor agent** — reviews proposed records for plausibility, flags anomalies.
- [ ] Implement the **Corrector agent** — applies human-reviewed corrections as new events (never mutates existing records).
- [ ] Derived projection layer — "current best view" query over the event log.
- [ ] CLI surface for the agent triad (`litspectraits measure <doi-or-sha>`?).

### Optional — batch ingest (Step 11)

- [ ] `litspectraits ingest --batch <doi-list.txt>` with rate-limit bucket alive across DOIs.
- [ ] Concurrency semaphore cap per publisher, verified against real DOIs.

### Future tiers (rendering)

Per `docs/rendering-mvp-plan.md` §6:

- [ ] **Tier 2** — dual-route side-by-side HTML (calls `render_html` twice via `compare_dual_format_dois`; visual companion to `diff-routes`).
- [ ] **Tier 3** — PDF/image overlay with bbox highlighting ("highlight back to source" viewer; needs page dimensions in the model and coordinate-origin normalisation).
