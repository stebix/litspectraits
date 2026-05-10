# litspectraits v3 — DOI → Publisher Retriever → Artifact

This document supersedes both `ingestion-pipeline.md` (v1) and `overview-v2.md`
(v2). Intel from `publisher-routes.md` is folded in. The whole-project goals
in `overview.md` are unchanged; what changes is the shape of the ingestion
stage, the format set, and the failure model.

## 0. Scope shift (what changes vs v2)

v2 was PDF-only with fall-through across multiple candidate URLs. Two things
broke that frame:

1. **Elsevier and Springer Nature TDM APIs do not return PDF.** Elsevier
   serves Elsevier-flavored XML (`view=FULL`); Springer's premium TDM
   serves JATS XML. Routing those publishers through "PDF or bust" is
   infeasible.
2. **Fall-through across publishers and OA mirrors hides failure modes.**
   Silent fallthrough from "Elsevier returned an abstract" to "Unpaywall
   has a preprint mirror" produces a corpus where half the records are not
   what the manifest claims.

v3 is built around a **quasi-linear happy path with loud failure**:

```
DOI
 │
 ▼
[normalize]
 │
 ▼
[metadata]   ── CrossRef GET → {publisher hint, title, authors, year, license, type}
 │              ↘ DOI 404 ⇒ DOINotFoundError (loud)
 ▼
[dispatch]   ── DOI prefix → Publisher ∈ {WILEY, ELSEVIER, SPRINGER_NATURE}
 │              ↘ unknown prefix ⇒ UnsupportedPublisherError (loud)
 ▼
[retrieve]   ── publisher.fetch(doi) → (bytes, format)
 │              ↘ no token AND no IP ⇒ MissingCredentialError
 │              ↘ rejected           ⇒ AuthRejectedError
 │              ↘ Elsevier META_ABS  ⇒ EntitlementDowngradeError
 │              ↘ 429 after retries  ⇒ RateLimitExhaustedError
 │              ↘ 5xx / malformed    ⇒ PublisherAPIError
 ▼
[validate]   ── magic-byte sniff (PDF / JATS / Elsevier-XML)
 │              ↘ mismatch ⇒ MalformedArtifactError (loud)
 ▼
[commit]     ── hash, atomic move into shard, write manifest, append index
 │              ↘ hash collision ⇒ IntegrityError (loud)
 ▼
AcquisitionRecord
```

**One path, no branches, no fall-through.** Each step has one job; any
failure raises before anything is written to disk. The audit trail lives in
`structlog` (DOI bound via contextvars, every step logged), not in a
structured attempts list. There is no `AcquisitionAttempt`, no
`candidates: tuple[...]`, no `MAX_ACQUIRE_ATTEMPTS`.

Future additions (Unpaywall fallback, PMC, arXiv, accepted-MS preprints)
must be **explicit additions to the happy path** — a new publisher in the
dispatch table, or a deliberate `--include-oa-fallback` flag that opts into
a second pass — never silently. v3 covers exactly
`{wiley, elsevier, springer_nature}`.

## 1. Explicit non-goals

- **No silent fall-through.** If the happy path fails, no artifact is
  materialized.
- **No Unpaywall in the happy path.** May return as an opt-in retriever in a
  later iteration.
- **No multi-version model.** Publisher TDM APIs return the version of
  record. The `Version` enum (published / accepted / preprint) from v1/v2 is
  gone.
- **No abstract-only ingestion.** Elsevier's META_ABS fallback is a fail-loud
  case, not a `--allow-abstracts` opt-in. Abstracts are corpus poison for
  deep value extraction.
- **No JATS/XML sideload.** Manual sideload is PDF-only.
- **No multi-route ranking policy.** No `RankingPolicy`, no `--policy`, no
  axes.
- **No separate `pubfetch` package.** All retrievers live inside
  `litspectraits/retrievers/`. Intel from the `pubfetch` design doc is
  inlined.
- **No Postgres, no R2, no failure memoization, no TOML config.** Same punts
  as v1/v2.

## 2. Module layout

```
src/litspectraits/
├── __init__.py
├── _logging.py              # structlog + rich (kept verbatim from current impl)
├── cli.py                   # Typer: ingest / extract / sideload / doctor / show
├── config.py                # env-driven Settings (publisher creds + rate limits)
├── doi.py                   # normalize (kept)
├── http.py                  # async client factory (kept)
├── errors.py                # IngestError taxonomy
├── metadata.py              # DOI → CrossRefMetadata; publisher dispatch by prefix
│
├── retrievers/              # publisher-specific TDM fetchers
│   ├── __init__.py
│   ├── base.py              # Retriever Protocol, RetrievePayload (success-only)
│   ├── _ratelimit.py        # token-bucket per publisher
│   ├── wiley.py             # wraps wiley-tdm SDK         → PDF
│   ├── springer.py          # wraps springernature_api_client → JATS XML
│   ├── elsevier.py          # wraps elsapy                → Elsevier XML
│   └── dispatch.py          # Publisher → Retriever
│
├── store.py                 # ArtifactStore: 3 format dirs, one-level sharding
├── manifest.py              # AcquisitionRecord, ManualProvenance, cattrs hooks
├── sniff.py                 # PDF + JATS + Elsevier-XML magic-byte detection
│
├── ingest.py                # the happy path
├── sideload.py              # PDF-only operator-retrieved registration
├── doctor.py                # egress IP + per-publisher creds preflight
│
└── extract/                 # format-dispatched extraction
    ├── __init__.py
    ├── _dispatch.py         # format → extractor
    ├── pdf.py               # docling
    ├── jats.py              # lxml-based JATS XML parser
    └── elsevier.py          # lxml-based Elsevier full-text-retrieval-response parser
```

Compared to v2: drops `acquisition/{attempt,fetch}.py`, drops `resolver/`
entirely, adds `metadata.py` + `errors.py` + `doctor.py`, makes `extract/` a
package with three format-specific impls. File count is similar; per-file
complexity is much lower because there is no fall-through state to manage.

## 3. Filesystem layout

```
<data_dir>/
├── artifacts/
│   ├── pdf/sha256/<aa>/<full-sha256>.pdf          # Wiley TDM + sideload
│   ├── jats/sha256/<aa>/<full-sha256>.xml         # Springer Nature TDM
│   └── elsevier/sha256/<aa>/<full-sha256>.xml     # Elsevier full-text-retrieval
├── manifests/
│   └── sha256/<aa>/<full-sha256>.manifest.json
├── documents/
│   └── <full-sha256>/
│       ├── document.json                          # extractor output
│       └── meta.json                              # extractor name+version, format, extracted_at
├── index/
│   └── by_doi.jsonl                               # {doi, sha256, format, added_at}
└── tmp/                                           # download staging, cleared on startup
```

**One-level sharding** (`<aa>` = first two hex chars of sha256), three
format directories under `artifacts/`. The format directory doubles as the
disambiguator: a JATS XML and an Elsevier XML both get a `.xml` extension
but they live under `jats/` vs `elsevier/`, so the path itself encodes which
extractor to use.

```python
class Format(StrEnum):
    PDF = 'pdf'
    JATS_XML = 'jats_xml'
    ELSEVIER_XML = 'elsevier_xml'

_FORMAT_DIR = {Format.PDF: 'pdf', Format.JATS_XML: 'jats', Format.ELSEVIER_XML: 'elsevier'}
_FORMAT_EXT = {Format.PDF: '.pdf', Format.JATS_XML: '.xml', Format.ELSEVIER_XML: '.xml'}
```

`store.find_by_doi(doi)` reads `index/by_doi.jsonl`, then loads the manifest
at `manifests/sha256/<aa>/<sha>.manifest.json`. The manifest carries the
`format`, which gives us the artifact path. **The index gains a `format`
column** so a single index scan answers "what do we have for this DOI?"
without touching individual manifests.

The `documents/<sha256>/` tree stays format-agnostic — one extractor produces
one `document.json` regardless of input format. The extractor's name +
version go into `meta.json` so we know how to invalidate / re-extract on
schema bumps.

`tmp/` is cleared on startup and again post-commit. Atomic `os.replace` from
tmp to the final shard path is non-negotiable — a half-written `.part` file
must never be visible at the canonical path.

## 4. Data model

All `attrs.frozen`. `cattrs` for (de)serialization. No tagged unions — the
shape is much simpler than v2.

```python
class Format(StrEnum):
    PDF = 'pdf'
    JATS_XML = 'jats_xml'
    ELSEVIER_XML = 'elsevier_xml'

class Publisher(StrEnum):
    WILEY = 'wiley'
    ELSEVIER = 'elsevier'
    SPRINGER_NATURE = 'springer_nature'

@frozen
class CrossRefMetadata:
    doi: str
    publisher_str: str               # raw 'Wiley', 'Elsevier BV', etc. — for logging cross-check
    title: str | None
    authors: tuple[str, ...]
    year: int | None
    type: str | None                 # 'journal-article', 'book-chapter', etc.
    license: str | None              # CC-BY-… where stated

@frozen
class RetrievePayload:               # success only — failures raise
    sha256: str
    byte_size: int
    tmp_path: Path
    format: Format
    fetched_url: str                 # post-redirect, for provenance
    sdk_version: str                 # wiley_tdm.__version__, etc.

@frozen
class ManualProvenance:              # operator sideload
    operator: str                    # email from LITSPECTRAITS_CONTACT_EMAIL
    retrieved_at: datetime
    source_url: str | None
    note: str
    license_assertion: str

@frozen
class AcquisitionRecord:
    doi: str
    sha256: str
    artifact_path: str               # relative to data_dir
    format: Format
    publisher: Publisher
    metadata: CrossRefMetadata
    fetched_url: str
    fetched_at: datetime
    fetcher_version: str             # litspectraits __version__
    sdk_version: str                 # publisher SDK version
    byte_size: int
    origin: Literal['auto', 'manual']
    manual_provenance: ManualProvenance | None
```

### What's gone vs v2

`AcquisitionAttempt`, `RetrieveAttempt`, `attempts: tuple[...]`,
`discovered_candidates: tuple[...]`, `ResolveResult`,
`permitted_candidates`, `Version`, `DiscoverySource`, `PdfCandidate`,
`RankingPolicy`. None of these survive into v3.

### What's added

`CrossRefMetadata`, `Publisher`, three-format `Format`. That's it.

## 5. Error taxonomy

```python
class IngestError(RuntimeError):
    """Base for all happy-path failures."""

# Validation / dispatch
class DOINotFoundError(IngestError): ...           # CrossRef 404
class UnsupportedPublisherError(IngestError): ...  # DOI prefix not in our table

# Configuration
class MissingCredentialError(IngestError): ...     # required token unset AND IP not allowlisted

# Publisher-side
class AuthRejectedError(IngestError): ...          # 401/403 from publisher
class EntitlementDowngradeError(IngestError): ...  # Elsevier returned META_ABS instead of FULL
class RateLimitExhaustedError(IngestError): ...    # 429 after retries
class PublisherAPIError(IngestError): ...          # 5xx, malformed response, SDK exception

# Local validation
class MalformedArtifactError(IngestError): ...     # magic-byte mismatch (auto or sideload)
class IntegrityError(IngestError): ...             # hash mismatch on commit
```

Each carries the DOI plus a context dict (publisher, fetched_url where
applicable, raw error string, SDK version). `cli.py` catches `IngestError`
and renders a Rich error panel with the class, the DOI, the context, and a
one-line operator hint (e.g. "configure `WILEY_TDM_TOKEN`" for
`MissingCredentialError`).

### Critical distinctions

- **`MissingCredentialError` vs `AuthRejectedError`.** No-token-and-no-IP is
  "you forgot to configure something"; valid-token-but-rejected is "you
  don't have entitlement for this title." They get different operator
  messages and different exit codes (2 vs 4).
- **`EntitlementDowngradeError` is its own class.** Elsevier silently
  downgrades unentitled requests to a `META_ABS` (abstract-only) view. We
  always pass `view=FULL` and raise this distinct error when the response
  comes back as the abstract envelope. Operators must see exactly that name
  in logs — it's the most common silent-data-quality bug in
  publisher-mining pipelines.

## 6. Metadata + dispatch (`metadata.py`)

```python
async def fetch_metadata(doi: str, *, client: httpx.AsyncClient,
                         settings: Settings) -> CrossRefMetadata:
    """GET https://api.crossref.org/works/{doi}?mailto={email}.
    Raises DOINotFoundError on 404."""
```

CrossRef metadata is fetched once per ingest. Used for:
- **DOI 404 fast-fail** — no point dispatching to a publisher if the DOI
  doesn't exist.
- **Publisher cross-check** — log CrossRef's `publisher` string against our
  prefix-table dispatch. Mismatch is a warning, not an error (CrossRef's
  free-text publisher field varies; the prefix table is authoritative).
- **Manifest enrichment** — title, authors, year, type, license.

### DOI-prefix → Publisher table

```python
_PUBLISHER_BY_PREFIX = {
    # Wiley
    '10.1002': Publisher.WILEY,
    '10.1111': Publisher.WILEY,           # society journals — large slice of MRI lit
    # Elsevier
    '10.1016': Publisher.ELSEVIER,
    '10.1006': Publisher.ELSEVIER,        # Academic Press legacy
    # Springer Nature
    '10.1007': Publisher.SPRINGER_NATURE,
    '10.1038': Publisher.SPRINGER_NATURE, # Nature
    '10.1057': Publisher.SPRINGER_NATURE, # Palgrave
    '10.1186': Publisher.SPRINGER_NATURE, # BioMed Central
}

def publisher_for_doi(doi: str) -> Publisher:
    prefix = doi.split('/', 1)[0]
    if prefix not in _PUBLISHER_BY_PREFIX:
        raise UnsupportedPublisherError(doi=doi, prefix=prefix)
    return _PUBLISHER_BY_PREFIX[prefix]
```

Extending the table is a one-line PR per added publisher. Anything not in
the table fails loudly with `UnsupportedPublisherError` — no fallback to a
generic / Unpaywall path.

## 7. Retrievers

Three SDK shims, each ~60 LOC, all sync-bridged via `asyncio.to_thread`.
Each one:

1. Acquires the per-publisher rate-limit token.
2. Checks credential presence; raises `MissingCredentialError` early if
   missing **and** Würzburg IP-fallback is unavailable for that publisher
   (Wiley supports IP-only auth; Springer and Elsevier require the key).
3. Calls the SDK in a worker thread.
4. Translates SDK exceptions to our `IngestError` subclasses.
5. Returns `RetrievePayload` on success.

```python
# retrievers/base.py
class Retriever(Protocol):
    publisher: ClassVar[Publisher]
    format: ClassVar[Format]
    rate_per_second: ClassVar[float]

    async def fetch(
        self,
        doi: str,
        meta: CrossRefMetadata,
        *,
        client: httpx.AsyncClient,
        tmp_dir: Path,
        settings: Settings,
    ) -> RetrievePayload:
        """Returns on success; raises IngestError subclass on any failure."""
```

### 7.1 Wiley (`retrievers/wiley.py`)

- **SDK:** `wiley-tdm` (`pip install wiley-tdm`), import `wiley_tdm.TDMClient`.
- **Endpoint:** `https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}` (lib-managed).
- **Auth:** `TDM_API_TOKEN` env var, forwarded from `WILEY_TDM_TOKEN` via a
  scoped `_patched_env` context (the lib reads the env var at construction).
  Lib also honors caller IP for subscription auth — Würzburg-IP requests
  succeed even without a token for entitled titles.
- **Format:** PDF only.
- **Rate limit:** 3 req/s (documented).
- **SDK call:** `tdm = TDMClient(); tdm.download_pdf(doi)` — writes
  `<doi>.pdf` into the configured download directory.
- **Failure translation:**
  - `wiley_tdm.AccessDenied` (or equivalent) → `AuthRejectedError`
  - HTTP 5xx / connection issues → `PublisherAPIError`
  - Lib succeeds but written file fails magic-byte sniff →
    `MalformedArtifactError` (catches paywall HTML written as PDF — defense
    in depth).

Hash and sniff happen **post-download** (the lib writes to disk before we
see bytes). One extra read of the file — negligible for typical PDFs.

### 7.2 Springer Nature (`retrievers/springer.py`)

- **SDK:** `springernature-api-client`,
  import `springernature_api_client.tdm.TDMAPI`.
- **Endpoint:** `https://api.springernature.com/{api}/...` (lib-managed).
- **Auth:** `api_key` query parameter — `SPRINGER_API_KEY` env. **No
  IP-only fallback** — the key is required.
- **Format:** JATS XML.
- **Rate limit:** per-minute quota (premium tier higher); default 5 req/s
  conservative.
- **SDK call:** TDM endpoint per-DOI retrieval (exact method TBD against the
  installed lib; the call shape is `TDMAPI(api_key=...).fetch_by_doi(doi)`
  or similar).
- **Magic-byte sniff:** read first 4 KiB; require `<?xml` declaration plus
  `<article` (or JATS namespace marker) within that prefix. HTML wrappers
  fail this and raise `MalformedArtifactError`.
- **Failure translation:** SDK exceptions for missing key / 401 →
  `MissingCredentialError` / `AuthRejectedError`; 5xx or malformed →
  `PublisherAPIError`.

### 7.3 Elsevier (`retrievers/elsevier.py`)

- **SDK:** `elsapy`, import `elsapy.elsclient.ElsClient` and
  `elsapy.elsdoc.FullDoc`.
- **Endpoint:** `https://api.elsevier.com/content/article/doi/{doi}` (lib-managed).
- **Auth:** `X-ELS-APIKey` header — `ELSEVIER_API_KEY` env.
  **`X-ELS-Insttoken`** header — `ELSEVIER_INSTTOKEN` env, optional, needed
  for institutional access beyond the OA tier. **IP-scoped entitlement**
  applies on top of the key: from a non-Würzburg IP, the response silently
  downgrades to `META_ABS`.
- **Format:** Elsevier XML (`view=FULL`).
- **Rate limit:** per-key, varies by product; default 6 req/s conservative.
- **SDK call:**
  ```python
  client = ElsClient(api_key=settings.elsevier_api_key,
                     inst_token=settings.elsevier_insttoken or None)
  doc = FullDoc(doi=doi)
  doc.read(client, view='FULL')   # always view=FULL
  ```
- **Entitlement check:** after `doc.read()`, inspect the response's root
  element. The full-text response root is `<full-text-retrieval-response>`
  with content under `<originalText>` (or equivalent). The abstract-only
  fallback returns the same envelope but without the full-text payload —
  detectable by absence of the originalText/body subtree. If full-text is
  absent, raise `EntitlementDowngradeError`. **This check happens in the
  retriever**, not in `ingest.py` — it's a publisher-specific concern.
- **Failure translation:** 401 or missing key → `MissingCredentialError`;
  403 → `AuthRejectedError`; META_ABS fallback → `EntitlementDowngradeError`;
  5xx → `PublisherAPIError`.

### 7.4 Dispatch (`retrievers/dispatch.py`)

```python
_RETRIEVERS: dict[Publisher, Retriever] = {
    Publisher.WILEY: WileyRetriever(),
    Publisher.ELSEVIER: ElsevierRetriever(),
    Publisher.SPRINGER_NATURE: SpringerRetriever(),
}

def retriever_for(publisher: Publisher) -> Retriever:
    return _RETRIEVERS[publisher]
```

Trivial — the dispatch table has three entries because that's the whole v3
universe.

## 8. Rate limiting (`retrievers/_ratelimit.py`)

A simple per-publisher token bucket using `asyncio.Lock` plus monotonic
timestamps. ~30 LOC. Defaults from §7:

```python
_DEFAULTS = {
    Publisher.WILEY: 3.0,           # documented limit
    Publisher.SPRINGER_NATURE: 5.0, # conservative until measured
    Publisher.ELSEVIER: 6.0,        # conservative until measured
}
```

Configurable via env:

```
LITSPECTRAITS_RATE_LIMIT_WILEY=3.0
LITSPECTRAITS_RATE_LIMIT_SPRINGER=5.0
LITSPECTRAITS_RATE_LIMIT_ELSEVIER=6.0
```

For single-DOI ingest the bucket is essentially a no-op (one token consumed,
never throttled). It comes alive for batch ingestion (a future
`litspectraits ingest --batch <doi-list.txt>` flag).

429 handling: per-attempt retry inside the retriever using `tenacity` with
exponential backoff and jitter, respecting `Retry-After` when the publisher
sends it. After three consecutive 429s, raise `RateLimitExhaustedError`.
This is the *only* retry layer in v3 — there is no candidate fall-through to
fall back on.

## 9. Sideload (`sideload.py`) — PDF only

```
litspectraits sideload <doi> <pdf-path> --license <spdx-or-string> [--source-url] [--note]
```

Operator-retrieved artifacts (e.g. PDF pulled via Würzburg library proxy
when TDM access is unavailable for a particular Wiley title).

- Magic-byte check: `%PDF-`. Anything else → `MalformedArtifactError`.
- Stream-hash to sha256, copy into `artifacts/pdf/sha256/<aa>/<sha>.pdf`
  atomically.
- Synthesize an `AcquisitionRecord` with `origin='manual'`,
  `format=Format.PDF`, and `manual_provenance` populated.
- Append index line with `format='pdf'`.
- Idempotent on `(doi, sha256)`.

`ManualProvenance` is mandatory — operator email (from
`LITSPECTRAITS_CONTACT_EMAIL`), retrieved_at, source_url (optional but
recommended), license assertion (required for the legal trail), free-text
note.

JATS / Elsevier-XML sideload is **out of scope** for v3. The realistic
operator-retrieval path is "I downloaded a PDF from the publisher via
library proxy"; an operator-retrieved JATS or Elsevier XML is a weird shape
and would need its own design.

## 10. CLI

```
litspectraits ingest   <doi> [--force]
litspectraits extract  <doi-or-sha> [--reextract]
litspectraits sideload <doi> <pdf-path> --license <str> [--source-url <url>] [--note <str>]
litspectraits doctor
litspectraits show     <doi>
```

`ingest` runs the happy path. Cache-hit short-circuit is the only branch.
On any `IngestError` subclass, render a Rich error panel with class, DOI,
context, and operator hint; exit non-zero with class-specific exit codes.

`extract` dispatches by the manifest's `format` field (§11).

`sideload` per §9.

`doctor` per §12.

`show <doi>` reads the manifest (and meta.json if extracted) and renders:
format, publisher, sha256, byte_size, license, origin, paths, extraction
status.

Rich rendering: `Console + Panel + Table` patterns inherited from current
impl. `Console(stderr=True)` for diagnostics; stdout stays clean for
`--json` outputs (`ingest --json`, `show --json`).

## 11. Extraction (`extract/`)

```python
# extract/_dispatch.py
async def extract(record: AcquisitionRecord, store: ArtifactStore) -> ExtractRecord:
    match record.format:
        case Format.PDF:          return await extract_pdf(record, store)
        case Format.JATS_XML:     return await extract_jats(record, store)
        case Format.ELSEVIER_XML: return await extract_elsevier(record, store)
```

- **`extract/pdf.py`** — `docling`. `asyncio.to_thread` (CPU/GPU-bound).
  Output is `result.document.export_to_dict()` written to
  `documents/<sha>/document.json`.
- **`extract/jats.py`** — `lxml`-based parser. Faster, more deterministic,
  GPU-free. Output is a structured dict with sections, paragraphs, tables,
  references. **Richer than docling-on-PDF** because the publisher's
  structure is already encoded in JATS.
- **`extract/elsevier.py`** — `lxml`-based parser for Elsevier's
  `full-text-retrieval-response` schema. JATS-adjacent but distinct
  namespaces and element names. Internal mapper produces a dict shape
  similar to `jats.py` so downstream callers can mostly ignore the
  publisher.

Each extractor writes:

```
documents/<sha256>/
├── document.json    # extractor output (dict)
└── meta.json        # {extractor: 'docling'|'jats'|'elsevier', version, format, extracted_at, n_pages?, n_tables?}
```

Re-extraction with a newer extractor version overwrites `document.json` and
updates `meta.json`. We don't keep historical document outputs unless an
explicit `--preserve` flag is added later. (Cheap to add; YAGNI for now.)

For now the JATS and Elsevier extractors emit their own dict shapes (no
cross-publisher normalization). Defer the canonical normalized `Document`
schema until the agent triad work begins — the structured dicts are rich
enough that re-running through a normalizer later is cheap.

`docling` is loaded only by `extract/pdf.py` and is gated under the
`[extract]` extra. `lxml` is a base runtime dep (cheap, ~5 MB, no model
downloads). A corpus that's 80% Elsevier+Springer can run extraction without
ever installing docling.

## 12. Doctor (`doctor.py`)

```
litspectraits doctor
```

Operator preflight. Catches "I'm on the wrong VPN" and "my token expired"
before a corpus build silently produces 200 abstract-only manifests.

Steps:

1. **Egress IP** — `GET https://api.ipify.org/?format=json`. Compare against
   `LITSPECTRAITS_EXPECTED_EGRESS_CIDRS` (comma-separated, optional). Warn
   on mismatch; note that mismatch can mean "off-campus / VPN not engaged"
   for Wiley and Elsevier.
2. **Per-publisher creds smoke test** — for each publisher with credential
   set, hit a known-OA test DOI (one per publisher, hard-coded) and assert
   success. Report `MissingCredential`, `AuthRejected`, or success.
3. **Render** Rich table with columns: publisher / credential set / IP
   acceptable / smoke test / hint.

Exit 0 if all green or no creds configured (operator-controlled scope);
exit 1 if any configured creds fail smoke test. **Always run `doctor`
before a batch ingest.**

Doctor never writes to the artifact store. It is a pure read-only diagnostic.

## 13. Configuration (`config.py`)

| Var | Purpose | Default |
|-----|---------|---------|
| `LITSPECTRAITS_CONTACT_EMAIL` | mailto for CrossRef polite pool, manifests | **required** — fail at startup |
| `LITSPECTRAITS_DATA_DIR` | store root | `platformdirs.user_data_dir('litspectraits')` |
| `LITSPECTRAITS_LOG_FORMAT` | `rich` / `json` | `rich` |
| `LITSPECTRAITS_HTTP_TIMEOUT_S` | per-request timeout (CrossRef + doctor IP check) | `30` |
| `WILEY_TDM_TOKEN` | Wiley token (consumed by `wiley-tdm` lib) | unset → `wiley` retriever raises `MissingCredentialError` unless IP-based auth succeeds |
| `SPRINGER_API_KEY` | Springer Nature TDM API key | unset → `springer` retriever raises `MissingCredentialError` |
| `ELSEVIER_API_KEY` | Elsevier ScienceDirect API key | unset → `elsevier` retriever raises `MissingCredentialError` |
| `ELSEVIER_INSTTOKEN` | Elsevier institutional token (optional) | unset → only OA-tier titles accessible |
| `LITSPECTRAITS_RATE_LIMIT_WILEY` | Wiley rate limit override (req/s) | `3.0` |
| `LITSPECTRAITS_RATE_LIMIT_SPRINGER` | Springer rate limit override (req/s) | `5.0` |
| `LITSPECTRAITS_RATE_LIMIT_ELSEVIER` | Elsevier rate limit override (req/s) | `6.0` |
| `LITSPECTRAITS_EXPECTED_EGRESS_CIDRS` | comma-separated CIDRs for `doctor` IP check | empty → doctor warns but doesn't fail |

The publisher tokens use the bare names that match each SDK's documented
env vars (`TDM_API_TOKEN` for Wiley is internal to the lib;
`SPRINGER_API_KEY` and `ELSEVIER_API_KEY` are the conventions used in
`elsapy` and `springernature-api-client` examples). Less translation
surface, fewer foot-guns.

## 14. Failure semantics

Aligned with `project-infra-overview.md`'s "no excessive exception catching
and silent fallback".

- **DOI 404** → `DOINotFoundError`, exit 2.
- **Unknown publisher prefix** → `UnsupportedPublisherError`, exit 2.
- **Missing credential** (token unset and IP-based fallback unavailable) →
  `MissingCredentialError`, exit 2. Doctor would have caught this.
- **Auth rejected** (token rejected, or IP not allowlisted) →
  `AuthRejectedError`, exit 4.
- **Elsevier abstract-only fallback** → `EntitlementDowngradeError`, exit 4.
  No artifact written.
- **Rate limit exhausted** (3 consecutive 429s) → `RateLimitExhaustedError`,
  exit 5.
- **Publisher 5xx / SDK exception** → `PublisherAPIError`, exit 5.
- **Magic-byte mismatch** → `MalformedArtifactError`, exit 6. Catches
  paywall-HTML-as-PDF, error-page-as-XML.
- **Hash collision against existing artifact differing in bytes** →
  `IntegrityError`, exit 7. Never silently overwrite.
- **Sideload of non-PDF** → `MalformedArtifactError`, exit 6.
- **`docling` import or model download failure** → propagate verbatim. Don't
  catch.

No silent fallbacks. The error class is the contract.

## 15. Dependencies

Runtime base:
- `httpx[http2]`, `structlog`, `rich`, `attrs`, `cattrs`, `typer`,
  `platformdirs`, `python-dotenv`
- `tenacity` — restored from v2's drop list; needed for 429 retry inside
  retrievers (no fall-through to fall back on)
- `lxml` — JATS and Elsevier XML parsing in `extract/`

Optional extras:
- `[wiley]` → `wiley-tdm`
- `[springer]` → `springernature-api-client`
- `[elsevier]` → `elsapy`
- `[extract]` → `docling` (PyTorch + image models)
- `[all]` → everything

Each retriever imports its SDK lazily inside `fetch()`. Without the extra,
the retriever raises a clean `MissingCredentialError` (with a hint to
install the extra) rather than `ImportError` at startup.

Dev:
- `pyright`, `pytest`, `pytest-asyncio`, `respx`, `ruff` — unchanged

## 16. Logging

`_logging.py` stays identical to current impl. Logger namespaces:

- `litspectraits.ingest` — orchestrator (DOI bound via `contextvars`)
- `litspectraits.metadata` — CrossRef calls
- `litspectraits.retrievers.wiley`, `.springer`, `.elsevier`
- `litspectraits.extract.pdf`, `.jats`, `.elsevier`
- `litspectraits.store`, `.sideload`, `.doctor`

DOI bound once at the top of `ingest()` / `extract()` / `sideload()` /
`show()` so every line in that operation carries it.

Standard `logging` is routed into structlog so SDK-internal logs (`requests`
in `wiley-tdm`, etc.) join the same stream and can be silenced or elevated
uniformly.

## 17. Order of work

Each step ends green on `pytest`, `ruff check`, `pyright`. Ten commits.

1. **Wipe and bootstrap.** Delete current `acquisition/` + `resolver/`. Keep
   `_logging.py`, `config.py`, `doi.py`, `http.py`. Add publisher creds +
   rate-limit vars to `config.py`. Add `errors.py` with the taxonomy stubs.
   Update `pyproject.toml` deps: drop `tenacity` from base (re-add for
   429 retry), add `lxml`, add `wiley-tdm`/`springernature-api-client`/
   `elsapy` under appropriate extras.
2. **Data model.** `manifest.py` with `Format`, `Publisher`,
   `CrossRefMetadata`, `RetrievePayload`, `AcquisitionRecord`,
   `ManualProvenance`, cattrs hooks. Round-trip tests.
3. **Store.** `store.py` with three format dirs, one-level sharding,
   manifest write, DOI index with `format` column. Layout tests.
4. **Sniff.** `sniff.py` with PDF + JATS + Elsevier-XML magic-byte
   detection (read first 4 KiB, distinguish `<?xml` + root element). Tests
   with real fixture bytes.
5. **Metadata.** `metadata.py` — CrossRef fetch + DOI-prefix → Publisher
   table. Respx-mocked tests for OA paper, 404, unknown prefix.
6. **Retrievers.** `retrievers/{base,_ratelimit,wiley,springer,elsevier,
   dispatch}.py`. Each retriever gets one test for `MissingCredentialError`,
   one for `AuthRejectedError`, one for success (monkeypatching the SDKs).
   Rate-limit bucket gets a separate timing test.
7. **Ingest.** `ingest.py` orchestrator. Integration tests for cache hit,
   single-success per publisher, and a representative loud-failure per
   error class.
8. **Sideload.** `sideload.py` with PDF-only validation +
   `ManualProvenance`. Idempotency test.
9. **CLI + doctor.** `cli.py` with `ingest / extract / sideload / doctor /
   show`. `doctor.py` with IP check + per-publisher creds smoke test.
   Golden-output test for the doctor table.
10. **Extraction.** `extract/{_dispatch,pdf,jats,elsevier}.py`. One
    real-fixture test per extractor (a tiny synthetic PDF, a small JATS
    sample, a small Elsevier-XML sample).

Optional 11: batch ingest CLI flag (`--batch <doi-list.txt>`) plus the
concurrency story when there's data to test against. The rate-limit bucket
makes this safe.

## 18. What this drops

Honest accounting. These are not pretend-savings:

- **Multi-attempt machinery**: `attempts`, `MAX_ACQUIRE_ATTEMPTS`,
  `RetrieveAttempt`, fall-through loop. v2 had this for "what if the chosen
  route 404s"; v3 says "then we fail loudly, period." Recoverable later as
  `--include-oa-fallback` if needed.
- **Multi-version model**: `Version` enum + version axis. Publisher TDM = the
  version of record. The accepted-MS / preprint fallback from v1's design is
  gone.
- **Discovery beyond the publisher**: Unpaywall lookup, OA-mirror enumeration,
  `DiscoverySource` enum. Discovery in v3 is "what publisher is this DOI?",
  full stop.
- **Multiple retriever shapes coexisting on one DOI**: there is exactly one
  retriever per publisher; the candidate concept disappears.
- **Resolver/Acquire split**: ingest is one function with five steps. There
  is no `ResolveResult`.
- **`--policy` flag and `RankingPolicy`**: format and version are
  determined, not chosen.

Trade is: substantially less code and less per-file complexity, in exchange
for two known refactors if scope creeps — adding Unpaywall as an opt-in
fallback and adding a fourth+ publisher both stay easy because the core is
small.

## 19. Memory updates planned

To keep across sessions:

- **Update** `project_wiley_cdn_blocking.md`: add `10.1111/` to the prefix
  list; the "Wiley DOIs always through `wiley` retriever, never generic"
  rule still holds.
- **New** `project_elsevier_no_redistribution.md`: per-key personal corpus,
  no internal sharing, affects future R2 mirror plans.
- **New** `project_publisher_ip_scoping.md`: Wiley + Elsevier need
  Würzburg-IP egress; Elsevier silently downgrades to META_ABS when not
  entitled — that's the `EntitlementDowngradeError` case. `litspectraits
  doctor` is the preflight.

## 20. Open questions (small)

1. **CrossRef metadata still worth fetching?** ~200 ms extra per ingest, but
   gives DOI-404-fast-fail, publisher cross-check, and richer manifest.
   **Recommendation: keep it.**
2. **Default cache-hit behavior on `ingest <doi>`** — short-circuit if any
   record exists for that DOI (recommendation), or always re-fetch unless
   `--cache-hit-ok`?
3. **Where does the Elsevier entitlement check live exactly?** Inside the
   `elsapy` response object inspection in `retrievers/elsevier.py`. Need to
   confirm against the lib's actual response shape — there may be an
   `ElsDoc.entitled` attribute or similar that simplifies the check.
4. **Springer SDK call shape** for per-DOI retrieval — exact method on
   `springernature_api_client.tdm.TDMAPI` should be confirmed against the
   installed lib version before §6 of the order of work.
