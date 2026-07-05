# Triage

Forward-looking review queue for design calls made during v3
implementation that the design doc (`overview-v3.md`) did not pin down
explicitly. Each entry has a checkbox so a reviewer can tick when the
call has been confirmed (or replaced).

Format per entry:

- **Title** — one-line summary.
- `[ ]` Reviewed — flip to `[x]` after confirmation.
- **Where:** file paths + symbol; section ref into the design doc.
- **Decision:** what was implemented.
- **Why:** the reasoning at the time of writing.
- **Revisit when:** the concrete trigger that should make us reopen
  this. If none plausible, write `never expected — kept for audit`.

When a call is confirmed and the doc is updated to match, leave the
entry but tick the box; do not delete (the audit trail is the value).
When a call is overturned, append a `**Reversed YYYY-MM-DD:**` line
under the entry rather than rewriting history.

---

## Step 4 — Sniff (committed b533a25, 2026-05-10)

### S4-1 — Strict `%PDF-` at byte 0 (no leading-garbage tolerance)

- [ ] Reviewed
- **Where:** `src/litspectraits/sniff.py` — `classify()`,
  `_PDF_MAGIC`; cross-ref `docs/overview-v3.md` §17.4.
- **Decision:** PDFs must start with `%PDF-` at offset 0. The PDF spec
  allows readers to skip up to 1 KiB of leading garbage; we do not.
- **Why:** the sniffer's whole purpose is rejecting paywall-HTML
  served at a `.pdf` URL. Any tolerance for leading garbage weakens
  that guard. TDM endpoints and library-proxy sideloads in scope
  return clean bytes.
- **Revisit when:** a real-world Wiley TDM or sideloaded PDF fails
  the sniff with `detected='unrecognized'` *and* `head[:32]` shows a
  legitimate PDF magic just past byte 0. At that point, relax the
  rule to "find `%PDF-` in the first 1 KiB" — not earlier.

### S4-2 — XML declaration is required for JATS / Elsevier

- [ ] Reviewed
- **Where:** `src/litspectraits/sniff.py` — `classify()`,
  `_has_xml_declaration()`; design doc §17.4 wording was
  "JATS XML (`<?xml` + JATS root marker)".
- **Decision:** classify returns a JATS / Elsevier `Format` only when
  *both* an XML declaration and a recognized root element are present.
  Decl-less but otherwise-valid XML returns `None`.
- **Why:** the design phrased the rule with `+`; I read that as
  conjunction, not disjunction. The XML decl strengthens the
  discriminator against scraped HTML fragments that happen to start
  with `<article>`.
- **Revisit when:** any publisher's TDM returns XML without a leading
  `<?xml ...?>` declaration. Not seen in the wild for
  Springer-Nature's premium TDM or Elsevier `view=FULL`, but if it
  ever appears the failure mode is `MalformedArtifactError` with
  `detected='unrecognized'` on a real publisher response.

### S4-3 — Local-name matching for namespaced XML roots

- [ ] Reviewed
- **Where:** `src/litspectraits/sniff.py` —
  `_xml_root_local_name()` (`rpartition(':')`); covered by
  `tests/test_sniff.py::test_classify_jats_namespaced_root_uses_local_name`.
- **Decision:** when the first opening tag is namespaced
  (`<jats:article ...>`), we strip the prefix and match on the local
  name. So `jats:article` → `article` → `Format.JATS_XML`.
- **Why:** the design doc didn't speak to namespaced JATS roots
  explicitly. The current Springer-Nature TDM returns
  default-namespaced `<article>`, not prefixed; the prefix path is
  forward-compat insurance. Costs ~1 line of code.
- **Revisit when:** a publisher response uses a namespace prefix that
  collides with another schema we care about (e.g. some other XML
  format also has a local name `article`). Not currently expected in
  the v3 publisher set.

### S4-4 — No structlog emission inside sniff module

- [ ] Reviewed
- **Where:** `src/litspectraits/sniff.py` — `verify()` raises
  without logging; cross-ref `docs/overview-v3.md` §16
  (logger-namespace list omits `litspectraits.sniff`).
- **Decision:** sniff failures raise `MalformedArtifactError` with
  full structured context (`expected`, `detected`, `path`,
  `byte_size`); no log line is emitted from the sniff module itself.
  The caller (retriever / sideload) logs the exception via its own
  DOI-bound `structlog` logger.
- **Why:** §16's namespace list explicitly omits sniff, which I read
  as deliberate. Logging at both call-site and sniff-site would
  produce two lines for one event.
- **Revisit when:** an operator triages a sniff rejection and finds
  the call-site log alone insufficient — typically because the DOI
  contextvar wasn't bound at the relevant call. Would not flip this
  unilaterally; instead fix the contextvar binding upstream.

### S4-5 — `verify()` is path-only (no in-memory `bytes` overload)

- [ ] Reviewed
- **Where:** `src/litspectraits/sniff.py` — `verify(path, *,
  expected, doi)` signature.
- **Decision:** the only public reader entry-point takes a `Path`.
  `classify()` is exposed for callers who already have bytes (and
  for unit tests), but the raise-on-mismatch wrapper is path-only.
- **Why:** every v3 retriever (§7.1–§7.3) writes to disk into
  `tmp_dir` before sniffing — the lib-mediated Wiley download
  produces a file, the Springer SDK's `save_xml(response, path)`
  writes a file, and the Elsevier raw-`httpx` retriever streams to
  `tmp_dir`. No retriever sniffs in-memory.
- **Revisit when:** a future retriever wants to sniff before the
  bytes touch disk (e.g. for very large responses we want to abort
  early). Easy to add as a second overload at that point.

---

## Step 5 — Metadata + dispatch (committed 5c13338, 2026-05-10)

### M5-1 — CrossRef non-404 errors propagate as raw `httpx.HTTPError`

- [ ] Reviewed
- **Where:** `src/litspectraits/metadata.py` — `fetch_metadata()`;
  cross-ref `docs/overview-v3.md` §5 (error taxonomy) and §17.7
  (orchestrator).
- **Decision:** only HTTP 404 is translated into the v3 taxonomy
  (`DOINotFoundError`). 5xx responses, network failures, and
  malformed JSON propagate as raw `httpx.HTTPError` / `KeyError` /
  `json.JSONDecodeError`.
- **Why:** §5 enumerates publisher-side error classes
  (`PublisherAPIError`, `RateLimitExhaustedError`, …) but does not
  define a CrossRef-specific class, and reusing `PublisherAPIError`
  is a semantic stretch (CrossRef is not the publisher). The clean
  call is "decide later, in the orchestrator (Step 7), whether to
  add a `MetadataAPIError` or fold these into a broader category."
  Doing nothing now is better than introducing an error class we
  may rename in two commits.
- **Revisit when:** Step 7 (`ingest.py`) needs to assign an exit
  code for "CrossRef is down" — at that point introduce
  `MetadataAPIError(IngestError)` and translate inside
  `fetch_metadata`. The 5xx-propagation test in `test_metadata.py`
  is the regression guard; flip its expected exception when this
  is done.

### M5-2 — Publisher cross-check is observability-only, never blocks

- [ ] Reviewed
- **Where:** `src/litspectraits/metadata.py` —
  `warn_on_publisher_mismatch()`, `_PUBLISHER_NAME_TOKENS`;
  cross-ref `docs/overview-v3.md` §6 ("Mismatch is a warning, not
  an error").
- **Decision:** `warn_on_publisher_mismatch` is a separate function
  the orchestrator calls explicitly after dispatch. CrossRef's
  free-text `publisher` string is matched case-insensitively against
  a per-`Publisher` substring table; mismatch logs a warning at
  `litspectraits.metadata` and returns. Routing is unaffected.
- **Why:** §6 is explicit that the prefix table is authoritative
  and CrossRef's `publisher` field varies. Folding the warning into
  `fetch_metadata` would couple two responsibilities; keeping it
  separate lets the orchestrator decide where in the happy path to
  emit it (after `publisher_for_doi`, before `retriever.fetch`).
- **Revisit when:** the observed false-positive rate gets noisy
  (operator triages a warning that turned out to be a known
  acquisition or rebrand) — at that point either expand the token
  set or add a CrossRef-→-publisher alias table. Token coverage
  is currently {wiley, elsevier, springer, nature, biomed central,
  palgrave}; not yet seen: BMC bare-acronym, Hindawi-acquired Wiley
  titles.

### M5-3 — Substring matching for publisher cross-check (not regex / canonical-form)

- [ ] Reviewed
- **Where:** `src/litspectraits/metadata.py` —
  `_publisher_name_matches()`.
- **Decision:** match on lowercased substring presence, not regex
  word boundaries or normalized canonical forms.
- **Why:** publisher strings are short, ASCII, and the tokens are
  unambiguous (`wiley`, `elsevier`); regex word boundaries would
  add complexity for no observed gain. Canonical-form normalization
  (e.g. stripping `Ltd.` / `BV` / `LLC` suffixes) is a slippery
  slope — every new publisher needs a new normalization rule.
- **Revisit when:** a real false positive surfaces — e.g. a journal
  imprint named "Wiley Sons Memorial Press" that isn't actually
  Wiley. Not currently expected.

### M5-4 — DOI URL-encoding via `urllib.parse.quote(doi, safe='/')`

- [ ] Reviewed
- **Where:** `src/litspectraits/metadata.py` — `fetch_metadata()`;
  test `test_fetch_metadata_url_quotes_doi`.
- **Decision:** the prefix-suffix slash is preserved; everything
  else is percent-encoded. DOIs are pre-normalized
  (lowercased, `https://doi.org/` and `doi:` prefixes stripped) by
  `litspectraits.doi.normalize()` before reaching this layer, so
  most inputs need no encoding — but the call is defensive against
  spec-allowed but unusual suffix characters (spaces, parentheses).
- **Why:** the DOI registry spec allows broad punctuation in the
  suffix; CrossRef's URL routing requires those to be percent-
  encoded. Hand-rolling that is error-prone; `urllib.parse.quote`
  with `safe='/'` is the smallest correct expression of the rule.
- **Revisit when:** never expected — kept for audit. If we ever
  see a CrossRef 404 on a DOI we believe is valid, check that the
  URL-encoding hasn't double-escaped a percent in a pre-encoded
  input.

### M5-5 — `_logger` namespace `litspectraits.metadata` matches §16

- [ ] Reviewed
- **Where:** `src/litspectraits/metadata.py` — `_logger` binding.
- **Decision:** `structlog.get_logger('litspectraits.metadata')`.
- **Why:** `docs/overview-v3.md` §16 lists this namespace
  explicitly. No call needed; documenting only because the §16
  list is the place to look if a new module needs a logger.
- **Revisit when:** never expected — kept for audit.

---

## Step 8 — Sideload (committed TBD, 2026-05-11)

### S8-1 — CrossRef metadata is fetched on every sideload

- [ ] Reviewed
- **Where:** `src/litspectraits/sideload.py` — `sideload()`; cross-ref
  `docs/overview-v3.md` §9.
- **Decision:** the sideload happy path calls
  `metadata.fetch_metadata(doi)` exactly like the auto-ingest
  orchestrator. There is no `--no-metadata` flag, and the manifest
  carries the same `CrossRefMetadata` shape regardless of `origin`.
  CrossRef errors (404 → `DOINotFoundError`; 5xx → raw
  `httpx.HTTPError` per M5-1) bubble unmodified.
- **Why:** the §9 wording "Synthesize an `AcquisitionRecord`" doesn't
  say anything about CrossRef, but the `AcquisitionRecord.metadata`
  field is non-optional and `Document` consumers (extraction, the
  future agent triad) need title/authors/year/license uniformly.
  Shipping a `--no-metadata` flag would re-open the abstract-only-
  manifest hole that v3 closed for auto-ingest, just under a different
  name. Network access during sideload is unfortunate, but the
  operator workaround (retry once CrossRef recovers) is small and the
  local PDF stays put.
- **Revisit when:** CrossRef availability becomes a real operational
  blocker for batch sideloads. The remediation is *not* a
  `--no-metadata` flag but a "stage now, enrich later" decoupled
  manifest-completion pass.

### S8-2 — Idempotency is keyed on `(doi, sha256)`, no-op on match

- [ ] Reviewed
- **Where:** `src/litspectraits/sideload.py` — `sideload()` between
  `_stage_pdf()` and `fetch_metadata()`; `docs/overview-v3.md` §9
  ("Idempotent on `(doi, sha256)`").
- **Decision:** after the source is hashed, the sideload looks the DOI
  up via `store.find_by_doi`. If an existing record matches the
  computed sha256, the staged tmp copy is unlinked and the existing
  record is returned untouched — no manifest re-write, no second
  index entry, no CrossRef call.
- **Why:** "idempotent" must mean *observably* idempotent: re-running
  sideload twice should produce one record and one index entry, not
  two. The store's commit is idempotent on sha256 alone, but the index
  is append-only; without the short-circuit, the index would grow on
  every re-sideload. Doing the check between hash and CrossRef lets
  the no-op stay offline.
- **Revisit when:** a use-case appears for "re-sideload to refresh
  manual_provenance fields" (e.g. operator wants to update the license
  assertion). Today that requires deleting the manifest and re-running;
  if it becomes common, add a `--force-update` flag rather than
  silently re-writing.

### S8-3 — `sdk_version='manual'` sentinel + `fetched_url=''`

- [ ] Reviewed
- **Where:** `src/litspectraits/sideload.py` — record construction in
  `sideload()`; `docs/overview-v3.md` §4 (`RetrievePayload`).
- **Decision:** on a manual sideload, `AcquisitionRecord.sdk_version`
  is the literal string `'manual'` (not the publisher's SDK version,
  not the empty string) and `AcquisitionRecord.fetched_url` is `''`.
  The operator-supplied URL goes into
  `manual_provenance.source_url`; the canonical-URL field stays empty.
- **Why:** the manifest carries one field per concept. `sdk_version`
  is "what software fetched these bytes"; for manual sideload that's
  unambiguously *not* an SDK, so a sentinel string is the honest
  encoding. `fetched_url` is "URL we ourselves fetched from"; for
  manual sideload that's empty by construction. Promoting either to
  `str | None` is scope creep against the existing model.
- **Revisit when:** a downstream consumer treats `sdk_version` as a
  package-version string and crashes on `'manual'`. The fix at that
  point is making `sdk_version` `str | None` with `None` for manual,
  not introducing a free-text mode.

### S8-4 — Manifest path is sha256-keyed, not (doi, sha256)-keyed

- [ ] Reviewed
- **Where:** `src/litspectraits/store.py` — `manifest_path()`;
  `docs/overview-v3.md` §3.
- **Decision:** noted, not fixed in Step 8. Two distinct DOIs
  sideloading the same PDF bytes will collide on the manifest path —
  the second write overwrites the first, and the first DOI's
  `find_by_doi` then resolves to a manifest carrying the second DOI.
  Sideload makes this slightly more reachable than auto-ingest does,
  but it is a pre-existing v3 modelling decision, not sideload-
  specific (auto-ingest of two DOIs that happened to compress to the
  same TDM bytes would hit the same hole).
- **Why:** §3's "content-addressed, sharded by sha256" maps one sha
  to one file. Promoting the manifest key to `(doi, sha256)` would
  duplicate metadata for legitimately-shared bytes and break the
  "one artifact, one canonical path" rule. The conservative remedy
  is *detecting* the collision (raise `IntegrityError` when the
  existing manifest has a different DOI) rather than silently
  overwriting.
- **Revisit when:** the corpus actually contains two DOIs sharing
  bytes. At that point: add a sha-already-mapped-to-different-DOI
  check inside `ArtifactStore.commit` and surface as a typed error.
  Not worth the code in Step 8.

### S8-5 — Order of operations: sniff → stage+hash → idem → CrossRef

- [ ] Reviewed
- **Where:** `src/litspectraits/sideload.py` — `sideload()` step
  order; `tests/test_sideload.py::test_non_pdf_input_raises_malformed_before_network_call`.
- **Decision:** five stages in fixed order — pre-stage magic-byte
  sniff, stream-copy + hash into tmp, `find_by_doi` idempotency check,
  CrossRef + dispatch, defence-in-depth re-sniff, commit. Non-PDF
  inputs never copy bytes into the store; idempotent re-runs never
  reach the network.
- **Why:** sniff-first keeps paywall HTML and error pages out of the
  staging tree (cheap 4 KiB read at the source). Hashing during the
  stream-copy is a single read of the source for the common case.
  Idempotency check before CrossRef means a re-sideload of an already-
  registered artifact stays fully offline (matches §9's "idempotent
  on (doi, sha256)" intent). Defence-in-depth re-sniff at the
  orchestrator boundary mirrors `ingest.py` (S0 invariant: nothing
  reaches `commit` without passing `verify` at the orchestrator).
- **Revisit when:** never expected — the ordering is constrained by
  the failure model.

---

## Step 10d — Elsevier extractor (committed 999e234, 2026-05-11)

### E10d-1 — META_ABS rejection uses `MalformedDocumentError`, not `EmptyDocumentError`

- [ ] Reviewed
- **Where:** `src/litspectraits/extract/elsevier.py` —
  `_assert_has_full_text()`; covered by
  `tests/extract/test_elsevier.py::test_meta_abs_envelope_raises_malformed_document_error`;
  cross-ref `docs/overview-v3.md` §10d.
- **Decision:** an Elsevier envelope without `<originalText>` *and*
  without `<xocs:doc>` raises `MalformedDocumentError` with a
  META_ABS-named hint. The same condition is rejected by the Elsevier
  retriever as `EntitlementDowngradeError` upstream; the extract-side
  taxonomy has no entitlement class, so we map it to the
  structurally-closest member.
- **Why:** the META_ABS shape is structurally valid XML (root tag is
  correct, the envelope parses) — not a parse failure in the lxml
  sense. But the absence of the full-text subtree is a *structural*
  failure for our purposes: the artifact lacks the piece we depend
  on. `EmptyDocumentError`'s hint frames the failure around OCR /
  scanned PDFs / stub envelopes; surfacing META_ABS through that
  class would mislead the operator. `MalformedDocumentError`'s hint
  can explicitly name "META_ABS abstract-only response" and point at
  re-ingesting to validate entitlement.
- **Revisit when:** a third Elsevier failure mode appears where the
  envelope is also "structurally valid but incomplete" (e.g. a future
  preview-only tier). At that point introduce a dedicated
  `EntitlementDowngradeAtExtractError` rather than overloading
  `MalformedDocumentError` further.

### E10d-2 — Elsevier extractor handles CEP markup only

- [ ] Reviewed
- **Where:** `src/litspectraits/extract/elsevier.py` — module
  docstring + `_extract_sections()`; cross-ref `docs/overview-v3.md`
  §11.
- **Decision:** the walker targets Common Element Pool (CEP) markup
  exclusively — `<ce:section>`, `<ce:para>`, `<ce:cross-ref>`,
  `<ce:table>` with CALS rows/entries, `<ce:bib-reference>` with
  `<sb:reference>` substructure. A JATS-via-Elsevier artifact (where
  `<originalText>` wraps a true `<article>` body in the JATS
  namespace) parses fine but yields zero `<section>` descendants and
  surfaces as `EmptyDocumentError`.
- **Why:** §11 says "internal mapper produces a dict shape similar
  to `jats.py`", which constrains the *output* — not that one
  extractor must handle both markup variants. Real ScienceDirect
  responses for the v3 corpus (MRI literature, mostly
  journal-articles) use CEP. Auto-routing JATS-via-Elsevier into
  `extract_jats` would require the dispatcher to peek into the
  artifact body, breaking the format-keyed dispatch invariant.
- **Revisit when:** we observe a JATS-via-Elsevier artifact surface
  as `EmptyDocumentError` in real corpus data. The fix at that
  point is one of: (a) a JATS-namespace branch inside
  `extract_elsevier` that re-routes to a shared `_walk_jats`, or
  (b) a separate extractor that the dispatcher chooses based on
  the namespace *inside* `<originalText>`, not the artifact format
  alone. Not worth designing speculatively.

### E10d-3 — CALS column spans are heuristic (parsed from trailing digits)

- [ ] Reviewed
- **Where:** `src/litspectraits/extract/elsevier.py` —
  `_cals_colspan()`, `_COL_DIGIT_RE`.
- **Decision:** real CEP tables encode column spans via
  `namest="col1" nameend="col3"` on the cell, where `col1` / `col3`
  reference earlier `<colspec colname="col1"/>` elements. We
  approximate by parsing trailing digits from the `namest` /
  `nameend` values — works for the common `col<N>` convention,
  degrades to `colspan=1` for opaque colspec names. Row spans
  (`morerows`) are exact.
- **Why:** properly resolving the colspec map adds a non-trivial
  table-build step (read `<colspec>` children, build a name→index
  map, project each row through it) for a payoff only visible on
  spanning cells in real ScienceDirect tables. The current
  heuristic's worst case is "underreport span", never "lie about
  content" — a downstream normaliser can detect spans that look
  wrong if it cares. Synthetic-fixture tests pass through the
  `colspan=1` default; the heuristic is only exercised on real
  CEP.
- **Revisit when:** the agent triad's table normaliser starts
  producing measurably wrong outputs for spanning cells, or an
  Elsevier sample uses non-`col<N>` colspec names that the
  heuristic misses entirely. Fix is a ~20-line
  `_build_colspec_map(tgroup)` helper at that point.

### E10d-4 — `_commit` / `_atomic_write` / lxml helpers duplicated rather than abstracted

- [ ] Reviewed
- **Where:** `src/litspectraits/extract/elsevier.py` —
  bottom-of-module helpers (mirrored in `src/litspectraits/extract/jats.py`);
  cross-ref `docs/overview-v3.md` §21 step 10d follow-up.
- **Decision:** the per-format commit machinery (`_commit`,
  `_build_extract_record`, `_build_meta`, `_atomic_write`,
  `_file_sha256`) and the lxml xpath helpers (`_local_findall`,
  `_first_child`, `_first_descendant`, `_all_descendants`,
  `_first_child_text`, `_first_descendant_text`, `_full_text`,
  `_ancestor_section_path`) are duplicated verbatim between
  `extract/jats.py` and `extract/elsevier.py`. PDF
  (`extract/pdf.py`) carries its own commit machinery because its
  `meta.json` shape differs (carries the `pipeline` block + `n_pages`).
- **Why:** the JATS module's docstring (landed in 10c) explicitly
  foreshadowed consolidation "after 10d when all three concretes
  exist". Doing the consolidation as part of 10d would have widened
  the commit beyond the spec's scope. Two duplicated ~60-line
  blocks is readable; abstracting prematurely would invert the
  dependency (jats and elsevier both depending on a shim) without
  a third caller forcing the move.
- **Revisit when:** before landing 10f. Cheap follow-up commit;
  not blocking other 10e / 10f work. The consolidation target is
  `extract/_lxml_helpers.py` (xpath/text helpers) and probably
  `extract/_commit.py` (per-format commit machinery parametrized
  by a `_build_meta` callback).

---

## Step 10e — CLI `extract` command (committed TBD, 2026-05-11)

### E10e-1 — `MalformedDocumentError` exit code is 6 (added to §5 matrix)

- [ ] Reviewed
- **Where:** `src/litspectraits/cli.py` — `_EXTRACT_EXIT_CODES`;
  cross-ref `docs/extract-pdf-plan.md` §5 (the canonical exit-code
  table for the extract tree).
- **Decision:** `MalformedDocumentError` maps to exit 6, sharing the
  "malformed output" bucket with `EmptyDocumentError`,
  `ParseDegradedError`, and `SerializationError`. The §5 table
  predates the 10c commit — when §5 was written the docling-only
  extractor taxonomy had no malformed-document class. 10c added it
  for JATS / Elsevier; 10e is the first commit that has to assign an
  exit code.
- **Why:** `MalformedDocumentError` fires when "artifact passed
  magic-byte sniff but failed structural parse" — the artifact
  reached the extractor but the structure we needed wasn't there.
  Same semantic shape as `EmptyDocumentError` (parse-time success,
  structural insufficiency at the next layer). Exit 6 =
  "malformed-output" is the natural bucket.
- **Revisit when:** `docs/extract-pdf-plan.md` §5's table should be
  updated to mention `MalformedDocumentError` explicitly. Single-
  line doc edit; not blocking on it.

### E10e-2 — Shared `_render_error_panel` across both error trees

- [ ] Reviewed
- **Where:** `src/litspectraits/cli.py` — `_render_error_panel(exc:
  IngestError | ExtractError, *, console, hints)`.
- **Decision:** one rendering function, parameterized by a `hints`
  dict, handles both `IngestError` and `ExtractError` panels. The
  two trees are inheritance-disjoint and share the
  `.doi: str` + `.context: dict[str, object]` attribute shape, so
  the rendering logic is identical. Per-tree hints maps
  (`_INGEST_HINTS` / `_EXTRACT_HINTS`) are passed at the call site.
  One `# type: ignore[arg-type]` lives at the dict lookup because
  pyright can't prove cross-tree disjointness statically.
- **Why:** the alternative is two near-identical rendering functions
  (~25 lines each, diverging only on which hints dict they
  reference). Sharing keeps the panel-format contract in one
  place — a future "add a Rich emoji prefix per exit-code bucket"
  change touches one site instead of two. The `# type: ignore` is
  the trade for the cross-tree dict lookup; alternatives (Protocol,
  Union dict type) cost more.
- **Revisit when:** the two trees' panel formats diverge in a
  non-cosmetic way (e.g. extract panels start carrying a "re-run
  with" suggestion that ingest panels don't). At that point split
  the function; until then, the shared one is fine.

### E10e-3 — `MissingArtifactError` exit code is 2 (config bucket), not 1 (store miss)

- [ ] Reviewed
- **Where:** `src/litspectraits/cli.py` —
  `_EXTRACT_EXIT_CODES[MissingArtifactError] = 2`; cross-ref
  `docs/extract-pdf-plan.md` §5.
- **Decision:** the plan-doc §5 table puts `MissingArtifactError`
  in the config bucket (exit 2) alongside `DoclingImportError` and
  `WrongFormatForExtractorError`. The CLI follows that. Operator-
  conceptually it could read as "the store reference is broken"
  (exit 1, what `show`'s missing-DOI case uses), but we follow the
  spec.
- **Why:** §5 is specific. The class fires when the artifact path
  resolves to a non-existent file — distinct from the "record
  absent from the index" case the CLI resolver handles upstream
  (which *does* exit 1 with a `not in local store` message). The
  `MissingArtifactError` case is "we have a manifest pointing at a
  file that's gone", typically because the operator wiped the
  store between ingest and extract; that's a "fix your config /
  re-run ingest" failure, not a "the index says no" failure. Exit
  2 reads correctly under that framing.
- **Revisit when:** never expected — kept for audit. If a
  downstream pipeline starts branching on exit codes and finds the
  2-vs-1 split confusing, the right move is making the CLI
  resolver catch `FileNotFoundError` from `read_manifest` and emit
  exit 1 with a "manifest exists but artifact is gone" hint,
  leaving exit 2 for the truly-config-shaped failures.

## Step 10g — docling settings (Egret layout / formula enrichment / timeout) (committed TBD, 2026-05-12)

Implements `docs/docling-settings-buildout.md` §1.2 / §2 / §3 and the
`extract-pdf-plan.md` §4 / §6 / §8 lockstep edits. Touches
`extract/pdf.py._load_docling` (config + `pipeline_view`) and
`doctor.py` (required-models list + `--download-models`).

### E10g-1 — Egret-Large layout, `do_formula_enrichment=True`, `document_timeout=120.0` set; no CLI surface

- [ ] Reviewed
- **Where:** `src/litspectraits/extract/pdf.py` — `_load_docling`'s
  `PdfPipelineOptions`; constants `DOCUMENT_TIMEOUT_S`,
  `LAYOUT_MODEL_REPO_FOLDER`. Cross-ref `docs/docling-settings-buildout.md`
  §1.2, §2; `docs/extract-pdf-plan.md` §4.
- **Decision:** the three "decided" §1.2 knobs land as a plain code
  change to the converter builder — no env var, no `--flag`. Egret-Large
  over the docling 2.93 default (Heron) for region detection on two-
  column-with-floats layouts; `do_formula_enrichment=True` to populate
  `EquationBlock` on the PDF route; `document_timeout=120.0` to convert a
  pathological hang into the loud `DoclingDegradedError` the
  `PARTIAL_SUCCESS` branch already raises.
- **Why:** the buildout doc's governing principle is "pick once, freeze,
  move on" — these are settled judgement calls, not operator dials, on a
  narrow born-digital-publisher-PDF input distribution. Plumbing a flag
  for each is accidental complexity (`docling-settings-buildout.md` §0).
- **Revisit when:** a docling major version reshuffles the layout-model
  catalogue or the formula-enrichment option, or the gold-set bake-off
  (next item) produces evidence to change one. Then it is again a
  reviewed code change, with the `pipeline_view` and doctor-models list
  bumped in the same commit.

### E10g-2 — `force_backend_text` and TableFormer V1→V2 left at defaults, fixture-gated

- [ ] Reviewed
- **Where:** `src/litspectraits/extract/pdf.py` — `force_backend_text`
  not set (docling default `False`); `TableStructureOptions` (V1), not
  `TableStructureV2Options`. `pipeline_view` records `force_backend_text`
  and `table_structure_kind` so a future flip is visible in `meta.json`.
  Cross-ref `docs/docling-settings-buildout.md` §1.2, §1.3, §4.
- **Decision:** these two are settled on the gold-set PDF-route fixtures
  (two-column-with-floats layouts; multi-level-header relaxometry
  tables), which don't exist yet — so they stay at their conservative
  defaults until the bake-off can run. `force_backend_text` pairs with
  the layout-model choice (test the combination, not the knob alone).
- **Why:** "char count went up" is not evidence; the deciding metrics are
  anchor rate (verbatim-anchor gate keys on substrings) and table-cell
  coverage, and you can't measure those without curated fixtures
  (`agentic-buildout-sketch.md` §5.9).
- **Revisit when:** the ~50–200-paper gold set lands (Phase 2 of this
  work). At that point run `force_backend_text` ∈ {False, True} × the
  layout model, and TableFormer V1 vs V2 on the relaxometry tables; bump
  `table_structure_kind` / `force_backend_text` in `pipeline_view` and
  the doctor required-models list if anything flips.

### E10g-3 — `doctor --download-models` fetches the Egret spec explicitly, not via `download_models(with_layout=True)`

- [ ] Reviewed
- **Where:** `src/litspectraits/doctor.py` — `_maybe_download_models`
  calls `LayoutModel.download_models(..., layout_model_config=DOCLING_LAYOUT_EGRET_LARGE)`
  then `download_models(with_layout=False, with_tableformer=True,
  with_code_formula=True, ...)`; `_docling_model_dirs` returns a 4-tuple
  (layout / TableFormer / code-formula) and reads the layout folder name
  from `extract.pdf.LAYOUT_MODEL_REPO_FOLDER`.
- **Decision:** `download_models(with_layout=True)` pulls docling's
  *default* layout model (Heron), not the Egret-Large spec the extractor
  configures — so doctor would download (and then probe-OK) the wrong
  weights, exactly the "greenlight a machine that fails mid-extract"
  failure the buildout doc warns about. Fetch the configured spec
  directly; pin every OCR / picture-classifier / VLM-figure switch False
  (several default True in docling 2.93).
- **Why:** the required-models list and the converter config have to move
  in lockstep (`docling-settings-buildout.md` §2, §3); routing the layout
  download through the same `LAYOUT_MODEL_REPO_FOLDER` constant the probe
  uses makes "which layout model" single-sourced in `extract/pdf.py`.
- **Revisit when:** docling exposes a `layout_model_config` passthrough on
  `download_models` itself (then the explicit `LayoutModel.download_models`
  call collapses into the bulk call), or the layout-model choice changes
  (update the constant; the probe and download follow).

---

## Step E0.5b — Normalize persistence (committed TBD, 2026-05-14)

Implements `docs/normalized-documents-discussion.md` §3 and the diff-
harness loader prerequisites of `docs/dual-route-comparison-overview.md`
§9. Adds `src/litspectraits/normalize/persistence.py`,
`src/litspectraits/_io.py`, the `NormalizeError` tree, the
`litspectraits normalize` CLI command, and `ArtifactStore.normalized_dir`.

### N0.5b-1 — `normalized/` mirrors un-sharded `documents/`, diverges from discussion-doc §3.3 prescription

- [ ] Reviewed
- **Where:** `src/litspectraits/store.py` — `normalized_dir()` returns
  `_normalized_dir / sha256` with no sharding; layout docstring at
  module top. Cross-ref
  `docs/normalized-documents-discussion.md` §3.3 ("Match the ingest
  one-level sha256 sharding `<aa>/<sha>`") and
  `docs/dual-route-comparison-overview.md` §9.
- **Decision:** the on-disk layout is `normalized/<sha256>/{document.json,
  meta.json}` — un-sharded, one directory per artifact. The discussion
  doc prescribes `normalized/sha256/<aa>/<sha>/...` (one-level sharded)
  *and* notes that `documents/<sha>/...` "wants tightening before any
  one directory holds tens of thousands of entries" — i.e. it
  recommends sharding both layers. We deliberately keep the un-sharded
  shape so the two adjacent layers stay structurally identical.
- **Why:** `store.py`'s own rationale for un-sharded `documents/`
  applies symmetrically — population is bounded by the artifact set
  (one normalisation per artifact, append-only). Sharding `normalized/`
  alone would create cross-layer inconsistency; sharding both would
  fold a migration into a slice that is already wide enough. Current
  corpus size (tens of papers) does not motivate the move. The
  abstraction in `ArtifactStore` (`document_dir` / `normalized_dir`
  methods, callers do not concatenate paths) means a future "shard
  both" migration is one coordinated change.
- **Revisit when:** the artifact population crosses ~5–10k papers,
  *or* the discussion doc's §3.3 lands as enforcement (someone audits
  `documents/` enumeration cost on the production corpus). At that
  point migrate both layers in one commit: update the two
  `*_dir` methods to add the `<aa>/` prefix, rename existing
  directories at startup, and bump
  `Document.schema_version` so the layout change is recorded in
  every fresh manifest.

### N0.5b-2 — `atomic_write` + `file_sha256` lifted to `litspectraits._io`

- [ ] Reviewed
- **Where:** new module `src/litspectraits/_io.py`;
  `src/litspectraits/extract/_lxml_helpers.py` re-imports both names
  for backwards compatibility. Cross-ref the E10d-4 follow-up item
  ("commit machinery duplicated rather than abstracted") above.
- **Decision:** the two file-IO helpers move out of
  `extract/_lxml_helpers.py` into a neutral
  `litspectraits._io` module so the normalize layer can use them
  without depending on extract internals. `_lxml_helpers.py`
  re-imports + re-exports under `__all__` so existing import sites
  continue to work; new code should import from `litspectraits._io`
  directly.
- **Why:** the normalize persistence layer needed the same
  atomic-write discipline (CLAUDE.md non-negotiable). Duplicating
  the helpers into `normalize/persistence.py` would have created two
  implementations of one invariant — a real correctness risk for the
  one piece of plumbing the entire write path depends on.
  `_lxml_helpers.py`'s own docstring already acknowledged the helpers
  were "generic enough that the file-name on the module is a slight
  misnomer", which made this the right moment to lift.
- **Revisit when:** the ingest commit (`store.py`'s `_write_manifest`)
  has its own inline atomic-write implementation. Folding that into
  `litspectraits._io.atomic_write` would unify the third site too.
  Not blocking; the three implementations are byte-identical today.

### N0.5b-3 — `atomic_write` cleans up staging file on `os.replace` failure

- [ ] Reviewed
- **Where:** `src/litspectraits/_io.py` — `atomic_write()` wraps
  `os.replace` in try/except, unlinks the staging `.part` file on
  any `OSError`, then re-raises.
- **Decision:** the previous extract-side implementation left the
  staging `.part` file behind in `<data_dir>/tmp/` when `os.replace`
  failed (disk full, target dir vanished, etc.). The new
  `litspectraits._io.atomic_write` cleans up the staging file before
  the exception propagates. The store's startup `_reset_tmp()` was the
  backstop for these orphans; cleanup is now in-process so callers
  can assert `tmp_dir` is empty after a failure.
- **Why:** persistence-layer test
  `test_failed_normalize_leaves_no_partial_files` exposed the gap.
  The pre-existing behaviour was harmless but eroded the
  "tmp is empty at rest" invariant within a single process — useful
  for tests, and slightly useful for long-running batch sessions where
  the backstop only fires on next process boot.
- **Revisit when:** never expected — kept for audit. The fix benefits
  the extract layer too (jats / elsevier / pdf all route through this
  helper).

### N0.5b-4 — Three error trees keep duplicated `__init__` rather than refactor to shared base

- [ ] Reviewed
- **Where:** `src/litspectraits/errors.py` — `NormalizeError`
  inherits from `RuntimeError` and mirrors `IngestError.__init__` and
  `ExtractError.__init__` verbatim (seven lines, identical body). Module
  docstring updated to acknowledge the three trees.
- **Decision:** the pre-existing comment on the `ExtractError`
  section said *"Revisit if a third tree ever lands"* — adding
  `NormalizeError` is exactly that trigger. We deliberately continued
  the duplication rather than refactoring to a shared private base.
- **Why:** the original rationale ("explicit duplication keeps the
  contracts independent and the class taxonomies grep-able") gets
  *stronger* at three trees, not weaker. A reader chasing an
  `except NormalizeError` at the CLI boundary should see the class
  definition complete and self-contained, not have to chase a base
  class shared with two unrelated stages. The cost is 14 duplicated
  lines across the file; the alternative cost is a coupling between
  three otherwise-independent failure taxonomies.
- **Revisit when:** a fourth error tree lands (unlikely — the
  pipeline stages are bounded), *or* the `__init__` body grows
  non-trivially. At that point the duplication cost crosses the
  coupling cost and a private `_LitspectraitsError` base in
  `errors.py` becomes the right move. Until then, three copies it is.

### N0.5b-5 — Indented + sorted JSON on disk, against discussion-doc §3.4 prescription of compact

- [ ] Reviewed
- **Where:** `src/litspectraits/normalize/persistence.py` —
  `_serialize()` uses `json.dumps(..., indent=2, sort_keys=True,
  ensure_ascii=False)`. Cross-ref
  `docs/normalized-documents-discussion.md` §3.4 point 2
  ("Compact JSON, not pretty").
- **Decision:** match the existing extract-layer output format
  (`extract/_lxml_helpers.py:serialize_document` also uses
  `indent=2, sort_keys=True`) rather than the discussion doc's
  compact-JSON prescription. ``document.json`` and ``meta.json`` are
  pretty-printed and deterministically sorted.
- **Why:** consistency with the upstream extract layer matters more
  than the discussion doc's size argument at corpus sizes we target.
  Indented output supports operator workflows (`jq`, casual `cat`)
  and bit-for-bit reproducibility (sorted keys = stable byte hash
  on identical inputs). The size delta is ~1.5x for typical paper
  outputs (~100–200 KiB), negligible at the ~hundreds-to-thousands
  paper corpus scale.
- **Revisit when:** the corpus crosses ~50k papers *and* on-disk
  total size becomes a real concern, *or* a binary index layer
  (Layer 3 Postgres / parquet) lands and the on-disk JSON's role
  shifts from "source of truth read by tools" to "snapshot
  archive". At that point flip to compact JSON across all four
  layers (extract + normalize × document + meta) in one commit so
  the convention stays uniform.

### N0.5b-6 — `litspectraits normalize` is a separate composable command, not folded into `extract`

- [ ] Reviewed
- **Where:** `src/litspectraits/cli.py` — `cmd_normalize` is its
  own `@app.command(name='normalize')` rather than an extra step
  inside `cmd_extract`. Module-top docstring updated from "Five
  commands" to "Six commands" with the composable-step rationale
  inline.
- **Decision:** the canonical three-step happy path for an artifact
  is `ingest → extract → normalize`. Each step is independently
  re-runnable with its own integrity flag
  (`--reextract` / `--renormalize`) and its own exit-code matrix.
  No `extract --and-normalize` convenience flag.
- **Why:** the diff harness use case
  (`docs/dual-route-comparison-overview.md` §9) requires re-running
  *normalize alone* repeatedly while iterating on adapter code,
  without paying the expensive extract step each iteration.
  Composability also makes the `show` panel's per-stage row
  (`extraction`, `normalization`) accurately reflect which artifacts
  are ready for which downstream consumer. Folding the steps would
  recover one CLI invocation at the cost of these properties.
- **Revisit when:** the gold-set bake-off operationalises a
  ~50–200-paper sweep and the per-paper CLI overhead becomes
  measurable. Even then, prefer a batch-orchestrator script over
  collapsing the per-stage commands.

### N0.5b-7 — `WHITESPACE_RULE = 'passthrough-v1'` (no canonicalisation today)

- [ ] Reviewed
- **Where:** `src/litspectraits/normalize/persistence.py` —
  `WHITESPACE_RULE: Final = 'passthrough-v1'`. Cross-ref
  `docs/normalized-documents-discussion.md` §3.4 point 3
  ("One declared whitespace canonicalisation rule baked into the
  assembly").
- **Decision:** the canonicalisation rule recorded in `meta.json`
  for every committed normalised document is the literal string
  `'passthrough-v1'` — i.e. the adapters take block text verbatim
  from the extractor output, no collapsing, no stripping, no
  normalisation. The rule id exists so a future canonicalisation
  pass can be detected as a rule bump (and forces a
  `NORMALIZER_VERSION` bump in the same commit), rather than
  silently changing output bytes for unchanged inputs.
- **Why:** the discussion doc assumes a canonicalisation rule
  exists; today it does not. Recording the rule's current state
  honestly is better than recording an aspirational rule we do not
  enforce. The `-v1` suffix is forward-compatible: when a real
  rule lands (e.g. `'collapse-runs-v1'`) the recorded id changes
  and the stale-check fires correctly across the corpus.
- **Revisit when:** the verbatim-anchor gate
  (`agentic-buildout-sketch.md` §5.3) starts rejecting records
  whose value strings differ from their `source_block.text` only by
  whitespace. At that point introduce a canonical rule, bump the
  `WHITESPACE_RULE` id, bump `NORMALIZER_VERSION`, and rebuild the
  affected normalisations. Until then, passthrough is the honest
  record.

---

## Step MinerU-primary promotion (steps 4–6, 2026-07-04)

### MP-1 — Gated `vlm-engine` smoke validated end-to-end on the real Wiley PDF

- **Title** — the default path (`extract` → `mineru`/`vlm-engine` → `normalize`)
  validated against the real `10.1002/mrm.27665` middle.json, not just mocked
  `do_parse`.
- `[x]` Reviewed — validated 2026-07-04.
- **Where:** `tests/smoke/test_mineru_vlm_e2e.py`
  (`smoke` + `requires_wiley_creds`); `extract/mineru.py`,
  `normalize/mineru_adapter.py`; spec `docs/mineru-primary-promotion.md` §6.
- **Decision:** shipped the gated smoke test (full ingest → bare extract →
  normalize on a `tmp_path` store). Because the real Wiley PDF was already in
  the local store, the extract→normalize legs were *also* validated offline via
  a fresh real `vlm-engine` re-extract (5m48s, A100) into a throwaway store —
  no live Wiley fetch needed. Findings:
  - real `vlm-engine` stamps `middle.json._backend == 'vlm'` (≠ `'pipeline'`);
    the adapter's `_engine_fidelity` maps it to `'approximate'` correctly, so
    **all 98 normalized blocks are `geometry_fidelity='approximate'`, never
    `'exact'`** — the §6 risk (adapter built on `pipeline` fixtures) did not
    materialize; no adapter fix was needed.
  - `route == 'mineru'`, `schema_version == 2`, title recovered verbatim
    ("Sparsity and locally low rank regularization for MR fingerprinting").
  - aux outputs land on real vlm: `document.md` (56 429 B) + `images/` (30
    figures), and `meta.aux_outputs == {"images_dir": "images", "markdown":
    "document.md"}`. The committed `document.md` is **byte-identical** to the
    human-reviewed `mrm27665-vlm.md` reference.
  - formula/table recovery (the point of the promotion): 12 `EquationBlock`s
    with LaTeX — incl. eq. 5 `\hat{x} = \operatorname{argmin}_{x} \frac{1}{2}
    \|\mathbf{AU}_{r}\mathbf{FCx}...` (the `U_r` term) — and 3 `TableBlock`s
    parsed to real cell grids (NRMSE R=1..4, tube comparisons, literature
    table).
- **Why:** every unit test upstream mocks `do_parse`; this is the only layer
  that exercises the real vlm `middle.json`/`.md`/`images` shape through the
  extractor + adapter, and the byte-identical markdown confirms the parse
  matches the reviewed reference.
- **Revisit when:** MinerU renames the `_backend` tag (the adapter treats
  anything ≠ `'pipeline'` as approximate — a rename to e.g. `'vlm2'` stays
  correct, but a rename *to* `'pipeline'` for a VLM engine would silently
  mis-grade); or the hallucination guard (spec §11 Q-E) lands and the canonical
  path stops trusting the autoregressive formula/table heads verbatim.
