# Normalized `Document` rendering — MVP plan

A plan for a human-inspection renderer over the normalised `Document`
(`src/litspectraits/normalize/models.py`). Subordinate to
`normalized-documents-discussion.md` (esp. §2.4(b), which already reserves a
`render_html(doc)` function for golden-HTML snapshot tests) and
`dual-route-comparison-overview.md` (the dual-route loader a later tier reuses);
where this disagrees with those, they win and this doc should be updated to
match.

Audience: whoever builds the first cut, and future-self deciding whether a
later tier (dual-route side-by-side, PDF-overlay provenance viewer) is worth
the extra weight.

---

## 0. Decisions already taken

Scoping was settled up front; the rest of the plan follows from these:

- **Target:** static, self-contained HTML, **single route per render**. A pure
  `render_html(doc) -> str` opened via a CLI command. No server, no JS.
- **Faithfulness:** **faithful projection** — render blocks exactly as stored,
  including the XML route's by-kind order (all paragraphs in section order, then
  all tables, then all figures, per `normalize/xml_adapter.py`). The render is
  an honest projection of `document.json`, not an interpretive reconstruction.
- **Math:** **raw source** — MathML (XML route) and LaTeX (docling route) shown
  verbatim as source text. Deterministic, no JS dependency, snapshot-stable.

These three choices are mutually reinforcing: a pure function with no JS and no
reordering is exactly what a byte-stable golden snapshot needs, so the same
function backs both the human-inspection command and the §2.4(b) regression
tripwire.

---

## 1. Why this exists

Two payoffs from one artifact:

1. **Human inspection.** Open an ingested + normalised paper and eyeball *what
   the pipeline actually captured* — to spot-check extraction quality, and (the
   load-bearing reason in this codebase) to verify provenance/anchoring before
   measurements get bound to blocks.
2. **Regression tripwire.** `normalized-documents-discussion.md` §2.4(b) already
   commits to golden-HTML snapshots on real fixtures: a docling version bump, a
   normaliser change, or a settings flip all surface as a reviewable HTML diff.
   `render_html` *is* that renderer.

There is no document renderer today. `litspectraits show` renders the
*manifest*; the `cmd_*` panels render counts/metadata. Nothing renders block
content.

---

## 2. The honesty principle (the thing most likely to be done wrong)

The two adapters produce the same `Document` type but populate it very
differently, and the schema keeps those differences explicit on purpose (a
`None` reads as "this route can't have it," never "bug"). The renderer must
preserve that legibility rather than paper over it:

| Aspect            | XML route (jats / elsevier)           | docling route (PDF)              |
| ----------------- | ------------------------------------- | -------------------------------- |
| Block order       | by kind (paragraphs → tables → figs)  | true reading order               |
| Geometry          | none (`xpath` also `None` today)      | `page` + `bbox` + `page_char_range` |
| Inline ref ids    | resolved (jats) / present (elsevier)  | `None` — markers anchored only   |
| References        | structured (`parsed`)                 | raw text only                    |
| Equations         | MathML                                | LaTeX                            |
| Tables            | publisher markup (`th`/`td` explicit) | TableFormer (header inferred)    |
| Abstract          | from front matter                     | `None` (not extracted)           |

`Completeness` (`has_structured_refs`, `has_inline_ref_ids`, `has_equations`,
`table_source ∈ {publisher, tableformer}`) exists precisely so this can be shown
as badges rather than re-derived. The render surfaces the route + completeness
flags with a legend, so `refs: raw` on a docling render reads as *expected*, not
*broken*.

---

## 3. New module: `src/litspectraits/normalize/render.py`

### 3.1 Entry point

```python
def render_html(doc: Document, *, context: RenderContext | None = None) -> str
```

- **Pure function.** No I/O, no `ArtifactStore`, no `datetime.now()` anywhere in
  the output. Output is a total function of `(doc, context)` so it backs the
  golden snapshot verbatim.
- Emits a **self-contained** HTML document: inline `<style>`, zero external
  assets, zero JS. Opens offline, diffs cleanly.
- `RenderContext` is an optional `@frozen` band carrying only stable identifiers
  (`doi`, `source_artifact_sha`) — both deterministic given the artifact, so
  snapshot-safe. Omit it and you get a doc-only render.

### 3.2 Sub-renderers

- `_render_header(doc, context)` — title, abstract, a **route badge** +
  **completeness badges**, and a short **legend** (a dim badge = "absent by
  route, not a bug").
- `_render_block(block)` — dispatch on `block.type`:
  - **TextBlock** → `<p>` with a `section_path` breadcrumb chip; inline refs
    spliced in by char range — `<a href="#ref-{ref_id}">` when resolved, plain
    `<mark>` highlight when `ref_id is None` (normal on docling).
  - **TableBlock** → real `<table>` with `caption` / `label`; `<th>` / `<td>`
    by `cell.kind`; `rowspan` / `colspan` straight from the unexpanded spans
    (the browser lays them out — the row tuples already hold only the cells
    anchored at that row, so there is no manual grid expansion and no place for
    a transposition bug to creep in at render time).
  - **FigureBlock** → placeholder card with caption / label, explicitly
    labelled "no image data (by design)" — figures never carry pixels
    (`overview.md` non-goal).
  - **EquationBlock** → raw `mathml` or `latex` source in `<pre><code>`, tagged
    with which one is populated.
- `_splice_inline_refs(text, inline_refs)` — **the one tricky bit.**
  HTML-escape each text segment *between* offsets, then wrap each `surface_form`
  span. Working on the raw char offsets *before* escaping keeps the ranges exact
  — the same discipline the verbatim-anchor gate depends on. Overlapping or
  out-of-order ranges should fail loudly rather than silently mis-splice.
- `_render_references(refs)` — ordered list, `id="ref-{id}"` anchors so inline
  links resolve; show `parsed` fields (authors / title / source / year / doi)
  when present, else `raw_text`.
- A **route banner** for the XML route only: "blocks ordered by kind
  (paragraphs → tables → figures), not source reading order," so faithful
  by-kind order doesn't read as a bug. No banner on docling (true reading
  order).

### 3.3 Provenance display

Honest about absence, per §2:

- **docling** — annotate each block with `p.{page}` and a compact `bbox`
  readout.
- **XML** — annotate as "xml route — no page geometry" (and `xpath` when it
  ever starts being emitted; `None` today).

No PDF/image overlay in this tier — that is Tier 3 (§6).

---

## 4. CLI command

```
litspectraits show-document <doi-or-sha> [--out PATH] [--open]
```

- Reuses `_resolve_extract_target` (already accepts DOI or sha256, with the
  exit-1 "not in local store" / exit-2 "invalid DOI" failure modes).
- Loads via
  `load_normalized_document(source_artifact_sha=record.sha256, store=store)`.
- **Not normalised → exit 1** with the hint to run `litspectraits normalize
  <doi>` first, mirroring the "upstream extraction missing" pattern in
  `cmd_normalize`.
- Writes to `--out` (default: `<sha>.html` in cwd), prints the path to
  **stdout**, diagnostics to **stderr**; `--open` calls `webbrowser.open`.
  Keeps the stdout/stderr split the rest of the CLI maintains.

Open decisions (small, non-blocking):

- **Command name** — `show-document` (keeps the existing `show` = manifest) vs
  folding into `show --document`. Leaning `show-document`.
- **Default output location** — `<sha>.html` in cwd (with `--out` to override)
  vs a temp file printed to stdout. Leaning cwd.

---

## 5. Tests

- `tests/normalize/test_render.py`:
  - **Per-block unit renders** from hand-built `Document`s — assert every
    block's text appears, each `surface_form` is wrapped, table cells land at
    the right row with correct span attrs, equation source is verbatim,
    references are anchored.
  - **Escaping test** — a block whose text contains `<`, `&`, `"` with an
    *overlapping* inline ref → assert escaped output *and* a correctly placed
    highlight.
  - **Determinism test** — `render_html(doc) == render_html(doc)`, and the
    output contains no wall-clock / nondeterministic content.
  - **Completeness badges** reflect the flags for both a publisher-route and a
    docling-route fixture.
- **Golden snapshots** (the §2.4(b) tie-in) — one frozen HTML golden per route
  (jats, elsevier, docling), built from the existing adapter-test fixtures in
  `tests/normalize/test_xml_adapter.py` / `test_docling_adapter.py`. A small
  regeneration path (env flag or script) so an intentional change is a
  one-command refresh + reviewable diff.
- `tests/test_cli.py` — `show-document` against a normalised fixture writes a
  file; not-normalised exits 1.

---

## 6. Scope boundaries

Explicit Tier-1 non-goals, each a clean follow-on on top of the pure
`render_html` core:

- **No PDF/image rendering or bbox overlay** (Tier 3 — the full "highlight back
  to source" viewer; needs the PDF artifact, page dimensions which are *not* in
  the model yet, and care with docling's coordinate origin — the adapter
  forwards `l/t/r/b` verbatim without normalising origin,
  `normalize/docling_adapter.py`).
- **No dual-route side-by-side** (Tier 2 — calls `render_html` twice via the
  existing `compare_dual_format_dois` loader; the visual companion to
  `diff-routes`).
- **No KaTeX/MathJax** (raw source only, per §0).
- **No reading-order reconstruction** (faithful projection, per §0).

---

## 7. Wiring & docs

- Export `render_html` (+ `RenderContext`) from `normalize/__init__.py`.
- Add `show-document` to the command list in `CLAUDE.md`.
- This doc records the faithful-projection / raw-math / no-JS decisions; link it
  from `normalized-documents-discussion.md` §2.4(b) when the renderer lands.

---

## 8. Conventions

Per `project-infra-overview.md` / `CLAUDE.md`: every commit green on `uv run
pytest`, `ruff check`, `ruff format`, `pyright`. Line length 99, single quotes,
numpy-style docstrings, type hints almost everywhere, no `from __future__ import
annotations`, fail loudly (an overlapping inline-ref range or a malformed block
raises, never silently mis-renders).

---

## 9. Build sequence

1. `render.py` — `render_html` + `RenderContext` + sub-renderers (pure).
2. Unit tests, including the escaping and determinism cases.
3. CLI `show-document` + its CLI test.
4. Golden snapshots for the jats / elsevier / docling fixtures.
5. Wiring + doc touch-ups (`normalize/__init__.py`, `CLAUDE.md`, this doc).
