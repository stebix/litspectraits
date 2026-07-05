# Frontiers backend spec (`frontiersin`, prefix `10.3389`)

Status: **proposed** (research + pre-check done, not yet implemented). Branch: `feat/mineru-backend`.

Adds a fourth publisher route — Frontiers Media S.A. — to the v3 ingest stack.
Frontiers is a fully open-access CC-BY publisher, so this route is unlike the
existing three: **no credential, no entitlement gate, no SDK**. Full text is
delivered as native JATS XML over plain HTTPS. The route reuses the existing
JATS extract → normalize path, with two extractor changes required to handle
Frontiers-specific XML structure (math and structured abstracts).

This spec is the companion to `docs/mineru-backend-spec.md` and follows the
same "order of work, green at each step" convention.

---

## 0. Summary of decisions (resolved with operator)

1. **Direct-only.** Retrieve from `frontiersin.org` `/xml` exclusively. No PMC /
   Europe PMC fallback in the happy path (consistent with `docs/overview-v3.md`
   §"one quasi-linear route per ingest"). PMC remains a possible future
   `--include-oa-fallback`, out of scope here.
2. **Full math handling.** Correct math extraction is paramount, so this route
   fixes the two ways Frontiers loses math today: display equations nested in
   `<p>` and inline `<inline-formula>` MathML. Not deferred.
3. **No-credential OA publishers are modelled explicitly** in config + doctor —
   a new "credential-free / always reachable" status rather than pretending a
   key exists.

## 1. Scope

In scope: a `FrontiersRetriever`, its registration across the two dispatch
tables, format reuse, doctor integration for a credential-free publisher, and
the shared JATS-extractor changes needed for faithful Frontiers math. The route
targets `Format.JATS_XML` and reuses `extract_jats` + `normalize_xml_document(route='jats')`.

Out of scope: PMC/Europe PMC routes; a new `Format`; a new `IngestError`
subclass; inline-math on the PDF routes (the `InlineMath.mathml` field added
here is XML-only; the docling/mineru LaTeX path is unchanged).

## 2. Access route

### 2.1 Endpoints (verified live 2026-07-05)

Given a DOI, retrieval is two requests:

1. **Slug resolution.** `HEAD https://www.frontiersin.org/articles/<doi>/full`
   → `301` with `Location: /journals/<slug>/articles/<doi>/full`. The journal
   slug (`neuroscience`, `physics`, …) is required — the slugless
   `/articles/<doi>/xml` path returns `404`. Parse `<slug>` as the second path
   segment of `Location`.
2. **Full-text fetch.** `GET https://www.frontiersin.org/journals/<slug>/articles/<doi>/xml`
   → `200`, `content-type: text/xml`, native JATS (NLM Journal Publishing DTD
   v2.3, 2007), served from Azure Blob storage.

Verified: `10.3389/fnins.2024.1438260` → slug `neuroscience`, 82 KB XML;
`10.3389/fphy.2023.1230161` → slug `physics`, 84 KB XML.

### 2.2 No bot wall, no auth

Infrastructure is Azure Front Door + Blob storage (**not** Cloudflare). Plain
`httpx` gets `200` with any/empty User-Agent; no JS challenge, no cookie wall,
no API key. `robots.txt` allows `/articles/`, `/journals/`, `/full`, `/pdf`,
`/xml` with no `Crawl-delay`. Frontiers' TDM stance names XML as their
data-mining channel. We stay polite regardless: identifying User-Agent
(`litspectraits/<ver> (+mailto:<contact_email>)`) and a conservative rate cap.

### 2.3 Validation (magic-byte sniff)

The staged body is sniffed before commit via the existing
`sniff.verify(path, expected=Format.JATS_XML, doi=doi)` — the sniffer already
maps root local-name `article` → `Format.JATS_XML`. The Frontiers body opens
with `<?xml …?>` then `<!DOCTYPE article PUBLIC "-//NLM//DTD Journal Publishing
DTD v2.3 …" "journalpublishing.dtd">`. Checklist item (§12): confirm the
sniffer's lxml parse tolerates the external-DTD DOCTYPE **without a network
fetch** (default lxml does not resolve external DTDs — confirmed in the
pre-check, where `extract_jats` parsed the same body cleanly).

## 3. Retriever design — `retrievers/frontiers.py`

A plain class structurally satisfying the `Retriever` protocol (`retrievers/base.py`),
modelled on `ElsevierRetriever` (raw `httpx`, no SDK):

```
class FrontiersRetriever:
    publisher: ClassVar[Publisher] = Publisher.FRONTIERSIN
    format:    ClassVar[Format]    = Format.JATS_XML
    rate_per_second: float                    # via _default_rate(publisher, ...)
    async def fetch(self, doi, meta, *, client, tmp_dir, settings) -> RetrievePayload
```

`fetch` flow:

1. Acquire a rate-limit token (`RateLimiter`, as the other retrievers).
2. **Resolve slug** — `client.head(f'{_BASE}/articles/{doi}/full', follow_redirects=False)`;
   read `Location`, take the `<slug>` segment. Missing/`404` → `DOINotFoundError`
   (the DOI is not a Frontiers article). Non-redirect 2xx without a slug →
   `PublisherAPIError` (unexpected shape).
3. **Fetch XML** — `client.get(f'{_BASE}/journals/{slug}/articles/{doi}/xml')` with
   the polite UA. Status mapping: `404` → `DOINotFoundError`; `429` → bounded
   retry then `RateLimitExhaustedError`; `>=500` → `PublisherAPIError`; transport
   error → `PublisherAPIError`.
4. Stage bytes to `tmp_dir` as `frontiers-<token>.xml.part`.
5. `verify(staged, expected=Format.JATS_XML, doi=doi)` — rejects paywall-HTML /
   error bodies (Frontiers should never serve these on `/xml`, but the sniff is
   the invariant).
6. `sha256, byte_size = _hash_and_size(staged)`; return `RetrievePayload(format=Format.JATS_XML, fetched_url=<the /xml URL>, sdk_version='litspectraits-frontiers <ver>')`.

No credential read, no entitlement check (`EntitlementDowngradeError` /
`NotOpenAccessError` are not raised on this route). No new `IngestError`
subclass — the taxonomy is publisher-neutral and `UnsupportedPublisherError`
already covers unknown prefixes.

Rate/politeness: reuse the `_ratelimit` machinery; add a `Publisher.FRONTIERSIN`
entry to `_SPEC_DEFAULTS` (indexed unconditionally by `_default_rate`). Frontiers
is static cached blobs, so a modest default (e.g. the same ballpark as the other
publishers) is fine; expose `LITSPECTRAITS_RATE_LIMIT_FRONTIERS` for parity.

## 4. Format & storage — reuse `JATS_XML`

Frontiers artifacts use the existing `Format.JATS_XML`: stored under
`artifacts/jats/sha256/<aa>/<sha>.xml`, extracted by `extract_jats`, normalized
via `route='jats'`. The manifest `publisher` field (`'frontiersin'`)
disambiguates Frontiers from Springer within the shared `jats/` tree. A new
`Format` is **not** introduced — the pre-check confirmed the JATS path handles
Frontiers structure, so a new format would ripple through ~7 files (`store.py`,
`sniff.py`, `extract/_dispatch.py`, `cli.py` normalize dispatch,
`normalize/models.py`, `xml_adapter.py`, smoke conftest) for no benefit.

## 5. Publisher registration edits (mechanical; several enforced by tests)

1. `manifest.py` — add `FRONTIERSIN = 'frontiersin'` to `Publisher`.
2. `metadata.py` — add `'10.3389': Publisher.FRONTIERSIN` to
   `_PUBLISHER_BY_PREFIX`; add a name token (`'frontiers'`) to
   `_PUBLISHER_NAME_TOKENS` (observability cross-check only).
3. `retrievers/dispatch.py` — add `Publisher.FRONTIERSIN: FrontiersRetriever`
   to `_BUILDERS`. (`tests/retrievers/test_dispatch.py::test_table_covers_every_publisher`
   asserts `set(_BUILDERS) == set(Publisher)` — fails until this lands.)
4. `retrievers/_ratelimit.py` — add `Publisher.FRONTIERSIN` to `_SPEC_DEFAULTS`.
5. Update `tests/test_metadata.py` prefix→publisher parametrization.

## 6. Doctor — credential-free OA publishers

`doctor._has_credential` is a `match publisher` with no default case; every
current publisher gates on a credential. Frontiers has none. Design:

- Add `CredStatus.NO_CREDENTIAL_REQUIRED = 'no_credential_required'`.
- Introduce a credential-free set, e.g. `_CREDENTIAL_FREE: frozenset[Publisher] = frozenset({Publisher.FRONTIERSIN})`.
- `_has_credential` gains `case Publisher.FRONTIERSIN: return True` (it always
  "has" what it needs) — keeps the match exhaustive for pyright.
- `_check_one_publisher`: for a publisher in `_CREDENTIAL_FREE`, skip the
  `NOT_CONFIGURED` early return, run `_smoke_fetch`, and on success report
  `CredStatus.NO_CREDENTIAL_REQUIRED` (detail: "open access — no credential
  required") instead of `OK`. Error taxonomy on failure is unchanged
  (`AUTH_REJECTED` won't occur; `OTHER_FAILURE` catches transport/parse issues).
- `_smoke_dois.py` — add `SMOKE_DOI[Publisher.FRONTIERSIN]` (a stable OA
  Frontiers DOI, e.g. `10.3389/fnins.2024.1438260`). Required: the dict is
  indexed unconditionally, and `tests/test_doctor.py` asserts full coverage.
- Render the new status in the doctor table (it's a benign/OK-class row).

## 7. Config

No credential field is required (Frontiers needs none) — this is the point of
§6. The only config surface is the optional rate-limit knob:

- `Settings` — add `rate_limit_frontiers: float` with a `_DEFAULT_RATE_LIMIT_FRONTIERS`
  const; `from_env` reads `LITSPECTRAITS_RATE_LIMIT_FRONTIERS`.
- Because `Settings` is `@frozen` and every test fixture constructs it with all
  fields explicitly, adding this field touches `tests/conftest.py` (3 fixtures)
  and the inline `Settings(...)` in `tests/retrievers/test_elsevier.py`.

(If we prefer zero config churn, the rate can be a module const in
`_ratelimit._SPEC_DEFAULTS` only, with no env knob. Minor call — default to the
env-knob parity above unless we want to keep the fixture blast radius at zero.)

## 8. Extractor changes — faithful Frontiers math (the substantive work)

Shared code in `extract/jats.py` (+ `extract/_lxml_helpers.py`). These change
behavior for the Springer route too, but are strictly additive; §10 requires
Springer regression tests. Bump `extract.jats.SCHEMA_VERSION` `'3'` → `'4'`.

### 8.1 The problem (verified in pre-check)

Frontiers places **both** `<disp-formula>` (display/block equations) and
`<inline-formula>` (inline math) **inside `<p>`**, interleaved with prose,
`<italic>`, `<sub>`, and `<xref>`. The current walker:

- `_extract_blocks` scans a section's **direct children** only, so nested
  `<disp-formula>` never registers → on a 7-equation article, `n_equations = 0`.
- `_paragraph_to_block` calls `walk_paragraph_with_offsets(p, xref_localnames=('xref',))`,
  which flattens every non-xref descendant via `full_text` — so the display
  equation's MathML glyph text is dumped into the paragraph as newline-separated
  character soup (`…as illustrated below:\n\nC\n\ni\n\nt\n=\n∑…`). Inline
  formulas leak the same way.

Frontiers `<inline-formula>`/`<disp-formula>` carry **only `<mml:math>`** (no
`<tex-math>`, no `<alternatives>`) with an `id` — cleaner than Springer's
`<alternatives>` bloat (see `project_springer_inline_math_bloat`). The `mml:`
prefix is a non-issue: extraction uses `local-name()`, and `serialize_mathml`
preserves namespaces.

### 8.2 New paragraph model

Introduce a generalized paragraph walker (extend `walk_paragraph_with_offsets`
or add a JATS-specific `walk_paragraph_blocks` in `jats.py`) that recognizes
three special local-names with three behaviors, in reading order:

- **`<xref>`** → inline-ref span (unchanged): record `(start, end)` + recurse
  its text so `text[start:end] == full_text(xref)`.
- **`<inline-formula>`** → **inline-math span**: record `(start, end)` +
  verbatim `serialize_mathml(math)`; substitute a **whitespace-collapsed** glyph
  rendering of the MathML text (e.g. `P s i = C i …`) into the paragraph text at
  that position **instead of** recursing the newline-laden MathML. Stops the
  pollution; the canonical form is the stored MathML.
- **`<disp-formula>`** → **block-split boundary**: close the current text run
  into a `paragraph` block, emit an `equation` block dict (`id`, `text` =
  collapsed MathML text, `mathml` = `serialize_mathml(math)`), then open a new
  text run for the remainder. One `<p>` may thus yield `[paragraph, equation,
  paragraph, …]` — the same block sequence disp-formula-as-`<sec>`-child already
  produces, just sourced from within a `<p>`.

Consequences:

- A `<p>` wrapping only a `<disp-formula>` yields just the equation block (empty
  text runs are dropped, as `_extract_blocks` already drops empty paragraphs).
- Paragraph block text is **no longer byte-identical to `full_text(p)`** — by
  design, we replace garbled inline MathML with a clean collapsed surface form.
  The xref offset round-trip invariant still holds **within each emitted text
  segment** (`text[start:end] == surface_form`); tests pin this.
- The extractor's `document.json` paragraph dict gains an `inline_math` list
  parallel to `xrefs`: `[{start, end, mathml}]`. `_disp_formula_to_block` is
  reused for the block equations (already emits `{type:'equation', id, text, mathml}`).

Keep the Elsevier walker on `xref`-only (its `<ce:para>` shape is unaffected):
the new math local-names are JATS-passed parameters, so Elsevier's paragraph
path is untouched.

### 8.3 Structured abstracts (small fix)

Frontiers biomedical abstracts nest paragraphs under sections:
`<abstract><sec><title>Background</title><p>…</p></sec>…`. `_extract_front`
uses `local_findall(abstract_el, 'p')` (**direct children only**), so the
grandchild `<p>` is missed and the abstract comes out empty (verified: Article A
lost its abstract; Article B's flat `<abstract><p>` worked). Fix: use
`all_descendants(abstract_el, 'p')` for abstract paragraphs. Also improves
Springer structured abstracts. One-line change; add a fixture.

## 9. Normalize changes — `normalize/xml_adapter.py`, `normalize/models.py`

Bump `Document.schema_version` `'2'` → `'3'`.

- **`InlineMath` gains MathML.** Currently LaTeX-only (PDF-oriented). Add
  `mathml: str | None = None`, make `latex` optional, and add an
  exactly-one-of invariant in `__attrs_post_init__` (mirroring `EquationBlock`):
  XML routes populate `mathml`, PDF routes populate `latex`. This is the honest,
  no-lossy-conversion choice (`CLAUDE.md`: markdown/lossy round-trips rejected).
- **`_paragraph_to_text_block`** maps the extractor's `inline_math` list →
  `tuple[InlineMath]` with `char_range` + `mathml` (parallel to how `xrefs` map
  to `inline_refs`). `TextBlock.inline_math` already exists.
- **Display equations from `<p>`** flow through the existing
  `_equation_to_block` / `_section_block_to_normalized` path unchanged — they're
  just more `type:'equation'` entries in the section block list now.
- **`Completeness`**: `has_equations` starts reporting `True` for Frontiers
  (display equations now recovered). Optionally add `has_inline_math: bool`
  (recommended, for honest per-document quality gating). If added, bump the
  completeness/meta accordingly and populate in `_derive_completeness`.

## 10. Tests

Unit (hermetic, inline byte-literal fixtures per `tests/extract/test_jats.py`
convention):

- `extract/test_jats.py` — new fixtures:
  - `<p>` containing prose + `<disp-formula>` + more prose → asserts
    `[paragraph, equation, paragraph]` split, `n_equations` incremented,
    paragraph text free of MathML soup.
  - `<p>` containing `<inline-formula>` → asserts one `inline_math` span with
    correct `(start, end)` and verbatim `mathml`, clean surface text, xref
    offsets still round-trip.
  - structured `<abstract><sec><p>` → asserts abstract recovered.
  - **regression:** existing Springer-style `<disp-formula>` as direct `<sec>`
    child still works (the current `_JATS_WITH_EQUATION` fixture must stay green).
- `normalize/test_xml_adapter.py` — `InlineMath` round-trip; exactly-one-of
  invariant (mathml XOR latex); `has_equations` / `has_inline_math` completeness.
- `retrievers/test_frontiers.py` — `respx`-based (mirror `test_elsevier.py`),
  covering: slug-resolution HEAD→`Location` hop; happy-path `/xml` fetch +
  `RetrievePayload`; `404` → `DOINotFoundError`; `429` → retry →
  `RateLimitExhaustedError`; `>=500` → `PublisherAPIError`; HTML body →
  sniff rejects. No credential fixture needed.
- `test_dispatch.py`, `test_metadata.py`, `test_doctor.py` — extend coverage
  assertions for the new `Publisher` member and `SMOKE_DOI` entry.

Smoke (gated, real network, deselected by default):

- `tests/smoke/test_frontiers_e2e.py` — full happy path on the two verified
  DOIs. Assert on `10.3389/fphy.2023.1230161`: `n_equations == 7` and inline
  math recovered (non-empty `inline_math`); on `10.3389/fnins.2024.1438260`:
  structured abstract present. No `requires_frontiers_creds` marker — instead a
  network-availability gate (Frontiers needs no key), or reuse the smoke opt-in
  env used by the other e2e tests.

## 11. Order of work (green at each step: `pytest`, `ruff`, `pyright`)

1. **Publisher enum + prefix + name token** (`manifest.py`, `metadata.py`) +
   `test_metadata.py`. Dispatch/doctor tests now red — expected.
2. **`FrontiersRetriever`** + dispatch `_BUILDERS` + `_ratelimit` default +
   `test_frontiers.py`. Dispatch coverage green.
3. **Doctor credential-free path** (`CredStatus.NO_CREDENTIAL_REQUIRED`,
   `_CREDENTIAL_FREE`, `_has_credential` arm, `SMOKE_DOI` entry, table render) +
   `test_doctor.py`. Doctor coverage green.
4. **Config rate knob** (if adopted) + fixtures.
5. **Extractor: structured-abstract fix** (small, isolated) + fixture.
6. **Extractor: math handling** (paragraph splitter, inline-math spans,
   schema `'3'`→`'4'`) + fixtures + Springer regression.
7. **Normalize: `InlineMath.mathml`, adapter mapping, completeness, schema
   `'2'`→`'3'`** + `test_xml_adapter.py`.
8. **Smoke e2e** on the two real DOIs.
9. Docs: flip this spec to "implemented", update `CLAUDE.md` (the "Three
   publishers, three formats" note becomes four / notes Frontiers as a
   credential-free OA JATS route), `README.md`, `docs/overview-v3.md` publisher
   table.

Steps 5–7 are the math work; they carry the schema bumps and the Springer
regression risk, so land them behind their own reviewed commits.

## 12. Risks & validation checklist

- **External-DTD DOCTYPE in the sniffer.** Confirm `sniff.verify` parses the
  Frontiers body without attempting a network DTD fetch (low risk; `extract_jats`
  already parses it cleanly with default lxml).
- **Named entities across the corpus.** The two sampled articles use numeric
  character references only. Some JATS uses ISO named entities defined in the
  external DTD, which default lxml (no DTD load) would reject with "Entity not
  defined". Validate on a broader Frontiers sample (math-heavy + older articles);
  if it bites, the parser config (not the DTD) is the fix.
- **Paragraph-split offset invariants.** The surface-form substitution for
  inline math changes byte offsets vs `full_text(p)`. Tests must pin that xref
  and inline-math offsets round-trip **within each emitted segment**.
- **Springer regression.** §8 changes shared JATS code; the existing Springer
  extract/normalize tests + a Springer smoke run must stay green.
- **Slug-resolution fragility.** The route depends on the `/full` → `Location`
  redirect. If Frontiers changes the redirect shape, slug resolution breaks
  loudly (`PublisherAPIError`/`DOINotFoundError`), not silently.

## 13. Open sub-decisions (non-blocking)

- **Inline-math surface form**: whitespace-collapsed MathML glyph text
  (recommended — keeps real variable names inline for the anchor gate) vs. a
  fixed sentinel token. MathML is canonical either way.
- **`Completeness.has_inline_math`**: add it (recommended) or let inline math be
  implicit in the blocks.
- **Rate-limit knob**: env-configurable `LITSPECTRAITS_RATE_LIMIT_FRONTIERS`
  (fixture churn) vs. module-const only (zero churn).

## Appendix — pre-check evidence (2026-07-05)

Ran the production `extract_jats` → `normalize_xml_document(route='jats')` on two
real Frontiers artifacts (throwaway tempdirs, nothing committed):

| DOI | journal | structure | extract result |
|---|---|---|---|
| `10.3389/fnins.2024.1438260` | Neuroscience | structured abstract, no math | 39 text blocks, 26 headers, 2 tables, 3 figures, 32 refs; abstract **lost** (§8.3); refs fully structured; xref offsets round-trip |
| `10.3389/fphy.2023.1230161` | Physics | flat abstract, 7 disp + 4 inline formulas | 22 text blocks, 5 headers, 0 tables, 5 figures, 26 refs; **`n_equations = 0`** (§8.1), MathML polluting paragraph text; flat abstract recovered |

References parsed correctly despite Frontiers' old-DTD `<citation>` (not
`element-citation`) via the extractor's `scope = ref` fallback. `normalize`
completed on both (44 / 27 blocks; completeness derived), confirming the
structural reuse is sound — the only gaps are the two math/abstract items this
spec fixes.
