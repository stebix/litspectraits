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
