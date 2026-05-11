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

### Invariants (producer-side contracts)

- **All `datetime` fields are tz-aware UTC.** Producers (`ingest.py`,
  `sideload.py`) construct via `datetime.now(tz=UTC)`. The cattrs
  converter does **not** enforce this — `fromisoformat` will silently
  round-trip a naive value back to a naive value. A dedicated check is a
  future hardening item; for now the contract lives in code review and
  the call-site of every datetime constructor.
- **Field-name shadowing of builtins.** `CrossRefMetadata.type` and
  `RetrievePayload.format` / `AcquisitionRecord.format` mirror the
  upstream schemas (CrossRef payload key; format dimension across §3
  and §11). Ruff's `A` (flake8-builtins) group is intentionally not
  enabled; if it ever is, prefer `# noqa: A003` over renaming.
- **`origin` is an inline `Literal['auto', 'manual']`** on
  `AcquisitionRecord`. Promote to a named alias if more than one or two
  call-sites force a `# type: ignore[arg-type]` against pyright's
  literal narrowing.

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

Three retrievers, each ~60 LOC. Wiley and Springer are sync SDK shims
bridged via `asyncio.to_thread`; Elsevier talks to the HTTP endpoint
directly with our `httpx.AsyncClient` (the Elsevier reference SDK is
archived — see §7.3). Each retriever:

1. Acquires the per-publisher rate-limit token.
2. Checks credential presence; raises `MissingCredentialError` early if
   missing **and** Würzburg IP-fallback is unavailable for that publisher
   (Wiley supports IP-only auth; Springer and Elsevier require the key).
3. Calls the SDK in a worker thread (Wiley, Springer) or `await`s the
   `httpx` call (Elsevier).
4. Translates SDK / HTTP exceptions to our `IngestError` subclasses.
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

- **SDK:** `springernature-api-client` (PyPI, actively maintained;
  upstream `springernature/springernature_api_client`).
  Import `from springernature_api_client import tdm`.
- **Endpoint:** `https://api.springernature.com/...` (lib-managed).
- **Auth:** `api_key` query parameter — `SPRINGER_API_KEY` env. **No
  IP-only fallback** — the key is required.
- **Format:** JATS XML.
- **Rate limit:** per-minute quota (premium tier higher); default 5 req/s
  conservative.
- **SDK call:** the TDM SDK is **query-based**, not DOI-keyed. There is no
  `fetch_by_doi` method. Per-DOI retrieval is a one-record search:
  ```python
  client = tdm.TDMAPI(api_key=settings.springer_api_key)
  response = client.search(q=f'doi:{doi}', p=1, s=1,
                           fetch_all=False, is_premium=True)
  ```
  `is_premium=True` is **mandatory** — the non-premium endpoint returns
  metadata only; only the premium tier returns the JATS XML payload. Assert
  exactly one record came back; zero or multiple hits → `PublisherAPIError`
  (zero = DOI not in the Springer Nature corpus despite a Springer prefix;
  >1 = defensive — should not happen for a `doi:` query).
- **Bytes handling:** route the payload through our `tmp_dir` rather than
  the SDK's default path. The SDK's `save_xml(response, path)` accepts a
  caller-supplied destination — pass `tmp_dir / 'fetch-<rand>.part'`. Hash
  + sniff happen post-write, same pattern as the Wiley shim.
- **Magic-byte sniff:** read first 4 KiB; require `<?xml` declaration plus
  `<article` (or JATS namespace marker) within that prefix. HTML wrappers
  fail this and raise `MalformedArtifactError`.
- **Failure translation:** SDK exceptions for missing key / 401 →
  `MissingCredentialError` / `AuthRejectedError`; 403 (e.g. premium tier
  not on this key) → `AuthRejectedError`; 5xx or malformed →
  `PublisherAPIError`.

### 7.3 Elsevier (`retrievers/elsevier.py`)

- **SDK:** none — direct `httpx`. Elsevier's reference client `elsapy` was
  archived 2025-01-13 (last release v0.5.0, 2019-08-15) and was a thin
  sync `requests` wrapper around a single endpoint. We talk to the API
  directly with our existing `httpx.AsyncClient` — saves a stale dep, fits
  the async stack, no `to_thread` hop, and the entitlement check (below)
  is the same `lxml` parse either way.
- **Endpoint:** `GET https://api.elsevier.com/content/article/doi/{doi}`
  with `view=FULL` query param.
- **Headers:**
  - `X-ELS-APIKey: {ELSEVIER_API_KEY}` — required.
  - `X-ELS-Insttoken: {ELSEVIER_INSTTOKEN}` — optional, needed for
    institutional access beyond the OA tier.
  - `Accept: text/xml` — request the XML envelope; the API also serves
    JSON and we want consistent parsing under `extract/elsevier.py`.
- **IP-scoped entitlement** applies on top of the key: from a
  non-Würzburg IP, the response silently downgrades to `META_ABS`.
- **Format:** Elsevier XML (`<full-text-retrieval-response>`).
- **Rate limit:** per-key, varies by product; default 6 req/s conservative.
- **Call shape:**
  ```python
  url = f'https://api.elsevier.com/content/article/doi/{doi}'
  headers = {'X-ELS-APIKey': settings.elsevier_api_key,
             'Accept': 'text/xml'}
  if settings.elsevier_insttoken:
      headers['X-ELS-Insttoken'] = settings.elsevier_insttoken
  resp = await client.get(url, params={'view': 'FULL'}, headers=headers)
  resp.raise_for_status()
  body = resp.content      # bytes — written to tmp_dir, then sniffed
  ```
- **Entitlement check:** parse the body root with `lxml`. The full-text
  envelope is `<full-text-retrieval-response>` containing an
  `<originalText>` / `<xocs:doc>` subtree. The abstract-only fallback
  returns the same envelope without that subtree (only `<coredata>`
  metadata + `<dc:description>`). Absence of full-text →
  `EntitlementDowngradeError`. **This check happens in the retriever**,
  not in `ingest.py` — it's a publisher-specific concern.
- **Failure translation:** 401 with key sent → `AuthRejectedError`; 401
  with no key → `MissingCredentialError`; 403 → `AuthRejectedError`;
  META_ABS envelope → `EntitlementDowngradeError`; 5xx →
  `PublisherAPIError`.

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
litspectraits ingest   <doi> [--cache-hit-ok]
litspectraits extract  <doi-or-sha> [--reextract]
litspectraits sideload <doi> <pdf-path> --license <str> [--source-url <url>] [--note <str>]
litspectraits doctor
litspectraits show     <doi>
```

`ingest` runs the happy path. **Default is refetch** — every `ingest <doi>`
invocation goes through the publisher retriever even when a manifest
already exists for the DOI. `--cache-hit-ok` opts into the short-circuit:
if a manifest exists for the DOI, return it without touching the network.
Refetch-by-default keeps the loud-failure model symmetric ("I asked for an
ingest, I expect fresh bytes or a loud error") and avoids silent skips
that look indistinguishable from successful fetches in batch logs;
`--cache-hit-ok` is the opt-in optimization. Cache-hit short-circuit is
the only branch in either mode. On any `IngestError` subclass, render a
Rich error panel with class, DOI, context, and operator hint; exit
non-zero with class-specific exit codes.

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
  `documents/<sha>/document.json`. Detailed plan — six-stage fail-fast
  pipeline, `PdfPipelineOptions` config, error taxonomy, sanity
  thresholds — in `docs/extract-pdf-plan.md`.
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

The PDF-extraction plan adds a fourth check section — docling extra +
model cache + accelerator detection, plus opt-in
`--download-models` / `--smoke-extract` flags — specified in
`docs/extract-pdf-plan.md` §8. Lands alongside Step 10.

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
env vars where applicable: `TDM_API_TOKEN` for Wiley is internal to the
lib (forwarded from `WILEY_TDM_TOKEN` via a scoped env context, §7.1);
`SPRINGER_API_KEY` matches the SDK's example. `ELSEVIER_API_KEY` and
`ELSEVIER_INSTTOKEN` follow the `X-ELS-*` header naming used in
Elsevier's API docs — there is no SDK to match (§7.3). Less translation
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
- `lxml` — JATS + Elsevier XML parsing in `extract/`, plus the Elsevier
  retriever's entitlement check (§7.3)

Optional extras:
- `[wiley]` → `wiley-tdm`
- `[springer]` → `springernature-api-client`
- `[extract]` → `docling` (PyTorch + image models)
- `[all]` → everything

There is no `[elsevier]` extra: the Elsevier retriever uses our base
`httpx` client directly because the upstream `elsapy` SDK is archived
(§7.3). Without the publisher's API key the retriever still raises
`MissingCredentialError` — the gating is on the credential, not on a
missing extra.

The Wiley and Springer retrievers import their SDK lazily inside
`fetch()`. Without the extra installed, those retrievers raise a clean
`MissingCredentialError` (with a hint to install the extra) rather than
`ImportError` at startup.

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
   Update `pyproject.toml` deps: keep `tenacity` (needed for 429 retry),
   add `lxml`, add `wiley-tdm` under `[wiley]` and
   `springernature-api-client` under `[springer]`. **No `[elsevier]`
   extra** — Elsevier uses raw `httpx` (§7.3).
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
10. **Extraction.** Six-commit sequence `10a`–`10f` covering all three
    format extractors plus CLI and doctor wiring: 10a bootstrap
    (errors + `ExtractRecord` + dispatch stubs), 10b PDF, 10c JATS,
    10d Elsevier, 10e CLI `extract` command, 10f doctor docling
    extension. Each ends green on `pytest`, `ruff check`, `pyright`;
    granular checklist in §21 "Step 10 — Extraction." PDF interior
    spec'd in `docs/extract-pdf-plan.md` (six-stage pipeline, error
    taxonomy, `doctor` model-preflight extension); JATS + Elsevier
    governed by §11 alone.

Optional 11: batch ingest CLI flag (`--batch <doi-list.txt>`) plus the
concurrency story when there's data to test against. The rate-limit bucket
makes this safe.

Optional 12: per-publisher end-to-end smoke tests (§22). Gated on
publisher credentials; ephemeral tempdir-backed; assert structural
invariants, not byte equality. Lands once Step 7 (the ingest orchestrator)
is in place.

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

## 20. Resolved questions

All §20 entries from earlier drafts are now decided. Kept here as a short
audit trail; new questions should be raised inline against the relevant
section, not as a new §20 list.

1. *(Resolved 2026-05-10.)* **CrossRef metadata fetch stays in the happy
   path.** ~200 ms extra per ingest is acceptable in exchange for
   DOI-404-fast-fail, publisher cross-check, and richer manifest (§6).
2. *(Resolved 2026-05-10.)* **`ingest <doi>` refetches by default.**
   `--cache-hit-ok` is the opt-in to short-circuit on an existing
   manifest. Rationale and semantics in §10. Refetch-by-default fits the
   loud-failure model — silent cache-hits in batch logs would look
   indistinguishable from successful fetches.
3. *(Resolved 2026-05-10.)* **Elsevier entitlement check** lives in the
   retriever as a direct `lxml` parse of the
   `<full-text-retrieval-response>` envelope — no SDK dependency now that
   we've dropped `elsapy` (§7.3).
4. *(Resolved 2026-05-10.)* **Springer SDK call shape** — query-based,
   not DOI-keyed; per-DOI retrieval uses
   `TDMAPI.search(q=f'doi:{doi}', p=1, s=1, is_premium=True)` (§7.2).

## 21. Implementation checklist

Granular checklist that mirrors §17 with file-level resolution. Each step
should land as one (or a tight few) commits ending green on
`uv run pytest`, `uv run ruff check`, and `uv run pyright`. Items marked
`[x]` are done as of 2026-05-11.

### Phase 0 — Pre-flight (done)

- [x] Delete `src/litspectraits/acquisition/` (v2 fall-through machinery).
- [x] Delete `src/litspectraits/resolver/` (v2 probe set + RankingPolicy).
- [x] Delete `tests/acquisition/` and `tests/resolver/`.
- [x] Delete superseded docs (`ingestion-pipeline.md`, `overview-v2.md`,
      `publisher-routes.md`).
- [x] Update `CLAUDE.md` for the v3 design.
- [x] Save `project_elsapy_archived.md` to memory.

### Step 1 — Bootstrap (§17.1, done)

- [x] Update `pyproject.toml`: add `lxml` to base deps; add extras
      `[wiley]` (`wiley-tdm`), `[springer]` (`springernature-api-client`),
      `[extract]` (`docling`), `[all]`. **No `[elsevier]` extra** (§7.3).
      Pins: `wiley-tdm>=1.0` (only release), `springernature-api-client>=0.0.9`
      (latest; SDK never reached 1.0), `docling>=2.0`, `lxml>=5.3`.
- [x] Rewrite `src/litspectraits/config.py`: drop `crossref_tdm_token`;
      add `wiley_tdm_token`, `springer_api_key`, `elsevier_api_key`,
      `elsevier_insttoken`, three `rate_limit_*` overrides,
      `expected_egress_cidrs`. Float-and-CIDR parsing helpers fail loudly
      on bad input via `MissingConfigError`.
- [x] Create `src/litspectraits/errors.py` with the full `IngestError`
      taxonomy from §5 (DOI + context attrs; no logic yet). Base
      `__init__(message='', *, doi, **context)` synthesizes a
      `ClassName doi=… key=value` message when none is supplied so
      `str(exc)` is useful before the §17.9 Rich panel lands.
- [x] Update `tests/conftest.py` for the new `Settings` shape:
      `settings` fixture (no creds) + `settings_with_creds` (dummy values
      for all four publisher tokens).
- [x] Stub `src/litspectraits/cli.py` to a single empty `app =
      typer.Typer()` so the package imports cleanly until §17.9 lands.
- [x] `uv sync && uv run pytest && uv run ruff check && uv run pyright`
      all green. Each `[wiley]` / `[springer]` / `[extract]` / `[all]`
      extra dry-resolves successfully against PyPI.

### Step 2 — Data model (§17.2, done)

- [x] Create `src/litspectraits/manifest.py`: `Format`, `Publisher`,
      `CrossRefMetadata`, `RetrievePayload`, `AcquisitionRecord`,
      `ManualProvenance`, plus the `cattrs` converter. Only `datetime`
      hooks are registered; `pathlib.Path` already round-trips natively
      in `cattrs >= 24`. **No `bytes` hook** — no field uses `bytes`,
      so the §21 ask was tightened to "only the hooks that records
      actually exercise" (revisit if a field ever goes binary).
- [x] `tests/test_manifest.py`: round-trip every record type through
      `converter.unstructure` / `converter.structure`, including a
      `json.dumps`/`json.loads` round-trip on the unstructured form so
      the on-disk shape is asserted JSON-clean (no custom encoder
      required at the boundary).
- **Producer-side invariants captured in §4** ("Invariants"): tz-aware
  UTC datetimes, builtin-shadowing field names (`type`, `format`),
  and the inline-`Literal` choice for `origin`. Each is mirrored as a
  `Notes` block in the relevant `manifest.py` docstring so the contract
  is visible at the call-site as well as in the design doc.

### Step 3 — Store (§17.3, done)

- [x] Create `src/litspectraits/store.py`: three format dirs
      (`pdf/`, `jats/`, `elsevier/`), one-level sharding, atomic
      `os.replace` from `tmp/`, manifest write, DOI index with `format`
      column.
- [x] Clear `tmp/` on `ArtifactStore.__init__` (per §3).
- [x] `tests/test_store.py`: shard path computation, manifest write +
      read, `find_by_doi` returns latest by sha256, `format` column
      populated in `by_doi.jsonl`.

### Step 4 — Sniff (§17.4, done)

- [x] Create `src/litspectraits/sniff.py`: PDF (`%PDF-`), JATS XML
      (`<?xml` + JATS root marker), Elsevier XML
      (`<full-text-retrieval-response>` root). 4 KiB read window.
- [x] `tests/test_sniff.py`: positive + negative fixtures per format,
      including paywall-HTML-as-PDF and HTML-with-XML-prelude.

### Step 5 — Metadata + dispatch (§17.5, §6, done)

- [x] Create `src/litspectraits/metadata.py`: `fetch_metadata(doi)`
      against CrossRef polite pool; `_PUBLISHER_BY_PREFIX` table;
      `publisher_for_doi(doi)`. Also includes
      `warn_on_publisher_mismatch(metadata, publisher)` — the
      observability-only CrossRef-publisher cross-check called by the
      orchestrator (Step 7).
- [x] `tests/test_metadata.py` with `respx`: OA paper happy path; 404
      → `DOINotFoundError`; unknown prefix → `UnsupportedPublisherError`;
      publisher cross-check warning logged on mismatch.

Open design calls captured in `docs/triage.md` entries M5-1 … M5-5;
notably M5-1 (CrossRef non-404 errors propagate as raw `httpx.HTTPError`
pending the Step 7 orchestrator decision).

### Step 6 — Retrievers (§17.6, §7, done)

- [x] `src/litspectraits/retrievers/base.py`: `Retriever` Protocol +
      success-only `RetrievePayload`.
- [x] `src/litspectraits/retrievers/_ratelimit.py`: per-publisher token
      bucket using `asyncio.Lock` + monotonic timestamps.
- [x] `src/litspectraits/retrievers/wiley.py`: `wiley-tdm` shim,
      scoped `_patched_env` forwarding `WILEY_TDM_TOKEN` →
      `TDM_API_TOKEN`, post-download magic-byte sniff (defense in depth).
- [x] `src/litspectraits/retrievers/springer.py`:
      `TDMAPI.search(q=f'doi:{doi}', p=1, s=1, is_premium=True)` shim,
      single-record assertion, `save_xml(response, tmp_path)` into our
      `tmp_dir`.
- [x] `src/litspectraits/retrievers/elsevier.py`: **raw `httpx`** to
      `/content/article/doi/{doi}?view=FULL`, `lxml` entitlement check
      against `<full-text-retrieval-response>`, distinct exits for
      `EntitlementDowngradeError` vs `MissingCredentialError` vs
      `AuthRejectedError`.
- [x] `src/litspectraits/retrievers/dispatch.py`: `Publisher → Retriever`
      table.
- [x] Per-retriever tests: `MissingCredentialError`, `AuthRejectedError`,
      success path (Wiley + Springer SDKs monkeypatched; Elsevier via
      `respx`).
- [x] `tests/retrievers/test_ratelimit.py`: timing assertion that the
      bucket throttles to its configured rate.

### Step 7 — Ingest orchestrator (§17.7, done)

- [x] Create `src/litspectraits/ingest.py`: the five-step happy path
      from §0; DOI bound via `structlog.contextvars` at the top of
      `ingest()`.
- [x] Cache-hit short-circuit gated on `--cache-hit-ok` (§10); default
      refetches.
- [x] Tests: cache-hit short-circuit fires only when flag set; one
      success integration per publisher; one representative loud-failure
      per `IngestError` subclass.

### Step 8 — Sideload (§17.8, §9, done)

- [x] Create `src/litspectraits/sideload.py`: PDF-only, magic-byte
      check, hash + atomic copy, mandatory `ManualProvenance`.
- [x] Tests: happy path; idempotency on `(doi, sha256)`; non-PDF input
      → `MalformedArtifactError`; missing `LITSPECTRAITS_CONTACT_EMAIL`
      surfaces at startup, not at sideload time (via the
      `test_missing_contact_email_exits_2_with_panel` regression — the
      ``Settings.from_env`` check fires at the same place for sideload).
- [x] CLI `sideload` command wired through with class-specific exit
      codes; `--license` mandatory; `--source-url` and `--note`
      optional. Idempotent on `(doi, sha256)` at the orchestrator
      layer — re-running with the same bytes never re-writes the
      manifest or appends a duplicate index entry.

Design calls made during this step are logged in `docs/triage.md`
entries S8-1 … S8-5 — the substantive ones are S8-1 (CrossRef is
fetched on every sideload; no `--no-metadata` flag), S8-4 (manifest
path is sha256-keyed, not `(doi, sha256)`-keyed — pre-existing v3
quirk surfaced by sideload), and S8-5 (sniff-first / idempotency-
before-CrossRef ordering).

### Step 9 — CLI + doctor (§17.9, §12, done)

- [x] Rewrite `src/litspectraits/cli.py`: `ingest`, `extract`,
      `sideload`, `doctor`, `show` Typer commands; class-specific exit
      codes (§14); Rich error panel on every `IngestError` subclass;
      `--json` outputs for `ingest` and `show`.
- [x] Create `src/litspectraits/doctor.py`: egress-IP check via
      `api.ipify.org`; per-publisher creds smoke test against hard-coded
      OA test DOIs; Rich table render; exit 0 on all-green or no-creds,
      exit 1 on configured-creds-failed.
- [x] Golden-output test for the `doctor` table render.

### Step 10 — Extraction (§17.10, §11)

Six-commit sequence. Each ends green on `uv run pytest`, `uv run ruff
check`, `uv run pyright`. PDF side is spec'd in detail in
`docs/extract-pdf-plan.md` (authoritative for `extract/pdf.py` + the
docling-specific doctor extension; subordinate to this doc). The
plan-doc's §10 ordering is the interior of 10b plus the PDF slices of
10a, 10e, and 10f; cross-referenced inline below.

**10a — Extract bootstrap.** No format implementations; everything
compiles and `_dispatch.py` raises loud `NotImplementedError` on every
leg.

- [x] `src/litspectraits/errors.py`: full `ExtractError` taxonomy per
      `extract-pdf-plan.md` §5 (`DoclingImportError`,
      `WrongFormatForExtractorError`, `MissingArtifactError`,
      `DoclingConversionError`, `DoclingDegradedError`,
      `EmptyDocumentError`, `ParseDegradedError`,
      `SerializationError`, `ExtractIntegrityError`). Context-dict
      constructors mirroring `IngestError`. (`IntegrityError` in the
      plan-doc became `ExtractIntegrityError` to avoid a collision with
      the existing ingest-side `IntegrityError`; both error trees stay
      inheritance-disjoint so CLI panel dispatch pattern-matches each
      pipeline stage independently.)
- [x] `src/litspectraits/manifest.py`: add `Extractor` StrEnum
      (`docling` / `jats` / `elsevier`) and `ExtractRecord` frozen
      struct (`sha256`, `extractor`, `extractor_version`,
      `extracted_at`, `n_text_blocks`, `n_section_headers`, `n_tables`,
      `n_figures`, `char_count`, `n_pages: int | None`). `n_pages` is
      optional because JATS and Elsevier XML have no page concept. The
      existing module-level `converter` round-trips it via the same
      datetime hooks already in place.
- [x] `src/litspectraits/extract/__init__.py` +
      `extract/_dispatch.py`: three-way match on `record.format`; all
      three legs raise `NotImplementedError` with breadcrumbs naming
      the future step that lands each leg.
- [x] Tests: each `ExtractError` and `IngestError` subclass
      instantiates with DOI + context, with cross-tree disjointness
      pinned by a parametrized `isinstance` check (`tests/test_errors.py`,
      52 cases). `ExtractRecord` JSON round-trip — PDF (`n_pages` set)
      and XML (`n_pages=None`) — in `tests/test_manifest.py`. `_dispatch`
      raises `NotImplementedError` for each `Format` with the named
      step in the message, plus an exhaustiveness guard against new
      `Format` values landing without an extractor leg
      (`tests/extract/test_dispatch.py`).

**10b — PDF extractor (done).** Wires PDF leg of `_dispatch.py`; the
biggest single piece. Interior order in `extract-pdf-plan.md` §10.

- [x] `src/litspectraits/extract/pdf.py`: six-stage pipeline
      (`extract-pdf-plan.md` §3) — preflight → convert → structural
      sanity → serialize → commit. Lazy `import docling` behind
      `_load_docling`, `asyncio.to_thread` for the conversion call, the
      `_build_converter()` settings folded inline per
      `extract-pdf-plan.md` §4 (`do_ocr=False`, `TableFormerMode.ACCURATE`,
      `do_cell_matching=True`, `AcceleratorDevice.AUTO`). Module-level
      `FLOOR_CHARS`, `MIN_TEXT_BLOCKS`, `MIN_PAGES` carry the one-line
      "why" comment the plan-doc asks for. PDF leg of `_dispatch.py`
      flipped from `NotImplementedError` to `extract_pdf`. The
      ``meta.json`` ``pipeline.device`` field probes ``torch`` to record
      the *resolved* accelerator (cuda / mps / cpu) rather than the
      configured ``'auto'`` so an operator can spot CPU fallback without
      running doctor.
- [x] `ArtifactStore.document_dir(sha256)` helper added so the
      extractor and any future reader share one source of truth for the
      `documents/<sha256>/` layout. Constructor lazy-creates
      `documents/` alongside `artifacts/` / `manifests/` / `index/` /
      `tmp/`.
- [x] Synthetic 1-page PDF fixture
      (`src/litspectraits/_fixtures/synthetic.pdf`: one heading, one
      paragraph, one 2×2 table; **moved out of `tests/`** in 10f so the
      bytes ship in the wheel and a pip-installed operator can still run
      `doctor --smoke-extract`). Generated once via reportlab; the
      generator script lives in `/tmp` and is not committed (reportlab
      is not part of `[dev]`).
- [x] Tests (`tests/extract/test_pdf.py`, 16 cases): happy path writes
      both files with the expected `meta.json` counts;
      `WrongFormatForExtractorError` on a JATS record;
      `MissingArtifactError` on a record whose artifact_path does not
      exist; `DoclingImportError` via `sys.modules['docling'] = None`
      (the only test that exercises the unmocked import code);
      `DoclingConversionError` on `FAILURE`; `DoclingDegradedError` on
      `PARTIAL_SUCCESS`; `EmptyDocumentError` on zero text blocks;
      `ParseDegradedError` below `FLOOR_CHARS`; flat-structure warning
      via `structlog.testing.capture_logs`; `SerializationError` on both
      `export_to_dict()` raising and an unjsonable payload;
      idempotent re-extract with identical bytes leaves both files
      mtime-stable; `ExtractIntegrityError` on diverging re-extract
      without `--reextract`; `--reextract` overwrites cleanly.
      `tests/extract/test_dispatch.py` updated: PDF leg now pinned to
      route through `extract_pdf` (with positional record/store +
      keyword `reextract`); JATS / Elsevier breadcrumbs still asserted.

**10c — JATS extractor (done).** Wires JATS leg.

- [x] `src/litspectraits/extract/jats.py`: `lxml`-based five-stage
      pipeline (preflight → parse → walk → serialize → commit) emitting a
      schema-versioned dict with `front` (title + abstract), flattened
      `sections` (id / title / level / root-to-leaf `path` / `blocks`
      with inline-citation `xrefs` preserved), `tables` (2-D `cells`
      grid with rowspan / colspan + section_path + caption), `figures`
      (caption + section_path; no pixel data), and `references`
      (raw_text + the easy structured fields: authors, title, source,
      year, DOI). Namespace-agnostic via `local-name()` xpath so both
      bare-DTD and `xmlns="https://jats.nlm.nih.gov/..."` shapes parse
      the same way. No cross-publisher normalization — the agent triad's
      normaliser walks this dict (§11). Cross-extractor `_commit` /
      `_atomic_write` / `_file_sha256` helpers are duplicated rather
      than abstracted; revisit after 10d when all three concretes exist.
- [x] `errors.py`: added `MalformedDocumentError(ExtractError)` —
      "artifact passed sniff but failed structural parse" — used by
      JATS now and reusable by Elsevier in 10d. `tests/test_errors.py`
      parametrize list + the disjointness check pick it up
      automatically (60 cases now, was 52 in 10a).
- [ ] Small JATS fixture trimmed to one section + one table + one ref.
      **Inline literal in `tests/extract/test_jats.py`** rather than a
      tree-side file — same hermetic-byte-literal style as
      `tests/test_sniff.py`; saves a fixture-tree round-trip and keeps
      every test self-contained. Real Springer-Nature samples remain in
      Step 12's gated end-to-end smoke.
- [x] Tests (`tests/extract/test_jats.py`, 12 cases): happy path with
      structural counts + section-path / table-cell / xref / reference
      spot-checks; `WrongFormatForExtractorError` on a PDF record;
      `MissingArtifactError`; `MalformedDocumentError` on syntactically
      broken XML; `MalformedDocumentError` on a non-`<article>` root;
      `EmptyDocumentError` on body-less JATS; namespaced `<jats:article>`
      parses; floating top-level `<p>` outside any `<sec>` surfaces as
      a synthetic section; `SerializationError` via a monkeypatched
      `_walk` that injects an unjsonable value; idempotent re-extract
      with same bytes is no-op; `ExtractIntegrityError` on diverging
      re-extract; `--reextract` overwrites cleanly.
      `tests/extract/test_dispatch.py` now parametrizes the leaf-routes-
      through and missing-artifact pins across both PDF and JATS;
      Elsevier branch breadcrumb still asserted.

**10d — Elsevier extractor (done).** Wires Elsevier leg; `_dispatch.py`
now fully covered.

- [x] `src/litspectraits/extract/elsevier.py`: `lxml`-based five-stage
      pipeline (preflight → parse → META_ABS guard → walk → serialize →
      commit) emitting the same JATS-flavored dict shape as
      `extract/jats.py` so downstream readers stay publisher-agnostic.
      Common Element Pool (CEP) markup is the canonical input: walks
      `<ce:section>` / `<ce:para>` / `<ce:cross-ref>` and projects
      CALS `<row>` / `<entry>` tables (rowspan from `morerows`,
      colspan best-effort from `namest` / `nameend`). References pull
      authors / title / source / year / DOI out of `<sb:reference>` and
      `<ce:source-text>`. **Known limitation**: a JATS-via-Elsevier
      artifact (where `<originalText>` wraps a real `<article>` body)
      surfaces as `EmptyDocumentError` rather than auto-routing through
      `extract_jats`; the right re-route lands when we observe one on
      real corpus data.
- [x] Inline byte-literal fixtures in `tests/extract/test_elsevier.py`
      (same hermetic style as `tests/extract/test_jats.py`).
- [x] Tests (`tests/extract/test_elsevier.py`, 11 cases): preflight
      `WrongFormatForExtractorError` + `MissingArtifactError`; parse
      `MalformedDocumentError` on broken XML and on non-`<full-text-
      retrieval-response>` root; defensive `MalformedDocumentError` on a
      META_ABS envelope (no `<originalText>` and no `<xocs:doc>`) with a
      META_ABS-named hint — same shape that `EntitlementDowngradeError`
      rejects upstream, pinned here as defense-in-depth; walker
      `EmptyDocumentError` on an `<originalText>` body with no
      paragraphs; `SerializationError` via the same monkeypatched
      `_walk` idiom as the JATS tests; happy path with structural
      counts + section path / CALS cell projection / xref `refid → rid`
      remap / reference structured-field spot checks; idempotent
      re-extract; `ExtractIntegrityError` on diverging re-extract;
      `--reextract` overwrites cleanly. `tests/extract/test_dispatch.py`
      now parametrizes leaf-routes-through and missing-artifact pins
      across all three formats; the 10d `NotImplementedError`
      breadcrumb test is gone.
- [x] **Follow-up (landed post-10f)**: consolidated the duplicated
      `_commit` / `_atomic_write` / `_file_sha256` / lxml xpath helpers
      across `extract/jats.py` and `extract/elsevier.py` into a shared
      `extract/_lxml_helpers.py`. The new module owns the
      `LOCAL_NAME_CHILDREN` xpath constant, `DOCUMENT_FILENAME` /
      `META_FILENAME`, the `Counts` dataclass (both XML extractors had
      identical shapes), the eight namespace-agnostic xpath / text
      walkers (`local_findall`, `first_child`, `first_descendant`,
      `all_descendants`, `first_child_text`, `first_descendant_text`,
      `full_text`, `ancestor_section_path`), the atomic file IO
      helpers (`atomic_write`, `file_sha256`), and the
      `serialize_document` + `commit_document` orchestrators
      parameterized on the caller's `Extractor` enum +
      `schema_name` / `schema_version` pair + `structlog` logger. The
      per-extractor `_build_extract_record` / `_build_meta` functions
      were folded into `commit_document` as module-private helpers
      (same shape between the two callers; the only differences were
      the `Extractor` enum value and schema name/version, both
      threaded through). `ancestor_section_path` gained a required
      `section_localname` kwarg — `'sec'` for JATS, `'section'` for
      Elsevier — so each call site reads as spec. Net: ~73 lines
      removed across the two extractors, and every helper now has one
      canonical home. PDF extractor stays untouched; its commit
      function carries an extra `pipeline` block and real `n_pages`
      that don't fit the XML contract.

**10e — CLI `extract` command (done).** Makes the work user-visible
end-to-end; works for every `Format` from the first commit because it
sits on top of the dispatcher landed in 10a.

- [x] `cli.py extract <doi-or-sha> [--reextract] [--json]`: DOI vs sha
      disambiguation by anchored regex (`^[0-9a-f]{64}$` → sha, else
      DOI). Sha path goes through `store.read_manifest`; DOI path
      through `normalize` + `store.find_by_doi`. Sha-not-on-disk and
      DOI-not-in-index both surface as exit 1 with a one-line "not in
      local store" hint on stderr — same shape as `litspectraits show`.
      Invalid DOI exits 2 (mirrors `ingest` / `show`).
- [x] Rich error panel per `ExtractError` subclass via the generalized
      `_render_error_panel(exc, *, console, hints)` — same renderer
      now serves both error trees (they share `.doi` + `.context` and
      are inheritance-disjoint, so one function with a tree-specific
      hints dict handles both). Class-specific exit codes from
      `extract-pdf-plan.md` §5 (2 / 4 / 6 / 7); `MalformedDocumentError`
      (landed in 10c, post-dating §5's original table) added to the
      exit-6 bucket alongside `EmptyDocumentError` / `ParseDegradedError`
      / `SerializationError` since they share the "parsed but
      structural sanity failed" semantics.
- [x] `--json` parity with `ingest --json`: emits the unstructured
      :class:`ExtractRecord` on stdout with `doi` injected at the top
      level (ExtractRecord itself is keyed by sha to align with
      `documents/<sha>/`; injecting the doi keeps `jq` pipelines
      self-contained).
- [x] Tests (`tests/test_cli.py`, 19 new cases): happy path text +
      JSON; `--reextract` flag wiring asserted via a captured-kwargs
      stub; sha-lookup path exercised end-to-end (sha argument →
      `read_manifest` → dispatcher); missing-DOI and missing-sha both
      pin exit 1 with a `not in local store` stderr message; invalid
      DOI exits 2; parametrized exit-code matrix across all ten
      `ExtractError` subclasses (config = 2, conversion = 4,
      malformed-output = 6, integrity = 7); golden-output panel for
      `EmptyDocumentError` pins class-name title + DOI row + context
      key/values + class-level fallback hint; complementary test pins
      that per-call `context['hint']` wins over the class default
      (the contract that lets extractors override hints per call site).

**10f — Doctor docling extension (done).** Lands per
`extract-pdf-plan.md` §8.

- [x] `doctor.py`: `_check_docling_extra`, `_check_docling_models`
      (layout + TableFormer), `_check_accelerator`, `_check_ocr_engines`,
      `_maybe_download_models`, `_maybe_smoke_extract`, all dispatched
      from a new `_check_extract_section` orchestrator. New value
      objects (`ExtractStatus`, `ExtractComponentCheck`,
      `ExtractReport`) follow the IPCheck / CredCheck shape; the
      existing `DoctorReport.ok` property folds `has_required_failure`
      into the exit-code boolean. `_docling_model_dirs` reads the
      cache root from `docling.datamodel.settings` and the per-model
      folder names from `LayoutOptions().model_spec.model_repo_folder`
      and `TableStructureModel._model_repo_folder` rather than
      hardcoding strings — the next docling rename surfaces as an
      AttributeError, not a silent "models all missing." Component /
      detail cells are passed through `rich.markup.escape` so
      `docling[extract]` renders verbatim instead of being parsed as
      a `[extract]…[/extract]` markup tag.
- [x] `cli.py doctor`: `--download-models / --no-download-models`
      (default off), `--smoke-extract / --no-smoke-extract` (default
      off). Plain `litspectraits doctor` stays network-free for the
      extract section — both side-effects (model downloads, live
      docling conversion) require an explicit opt-in.
- [x] Synthetic fixture relocated to
      `src/litspectraits/_fixtures/synthetic.pdf` (see 10b's bullet)
      so `--smoke-extract` works from a pip-installed wheel; loaded
      via `importlib.resources` from the new
      `_packaged_fixture_path()` helper.
- [x] `tests/test_doctor.py` (16 new cases, 31 total): probe-level
      unit tests for every check function (`_check_docling_extra`
      OK / NOT_INSTALLED, `_check_docling_models` MISSING / OK,
      `_check_accelerator` CUDA / CPU, `_check_ocr_engines` OFF);
      `_check_extract_section` exit-code policy pinned in three
      variants (extra-missing → not a required failure;
      models-missing → required failure; models-present → green);
      `--download-models` invokes the downloader once with
      `force=False` then re-probes; `--smoke-extract` stages the
      fixture through ArtifactStore and calls `extract_pdf` (stubbed
      so the test does not depend on docling weights);
      missing-fixture path returns a `MISSING` smoke row without
      flipping the gate. Golden-output test pins the third table
      title, column headers, all six component labels, status labels
      (`ok`, `cuda`, `off`), and hint snippets. The existing IP/cred
      tests default-mute the extract section via an autouse fixture
      keyed off `@pytest.mark.extract_real` so the new tests opt back
      into the real probes; the `extract_real` marker is registered
      in `pyproject.toml`.
- [x] Pre-existing docling-2.93 drift in `extract/pdf.py` (the
      `AcceleratorDevice` / `AcceleratorOptions` private-import
      warnings) fixed in the same commit by sourcing them from
      `docling.datamodel.accelerator_options` rather than the
      `pipeline_options` re-export. Cleaner than carrying the
      diagnostic across the 10f gate.

### Step 11 (optional) — Batch ingest

- [ ] `litspectraits ingest --batch <doi-list.txt>` with the rate-limit
      bucket alive across DOIs.
- [ ] Concurrency story (semaphore cap per publisher) verified against
      real DOIs.

### Step 12 (optional) — End-to-end smoke tests (§22)

Lands after Step 7 at the earliest. Not part of the per-step green-bar
gate; runs on demand via `uv run pytest -m smoke`. Default
`uv run pytest` skips all three when no publisher creds are set.

- [ ] Hardcoded OA DOI per publisher in
      `src/litspectraits/_smoke_dois.py` (or alongside doctor's table).
      Shared by `doctor` (§12) and the smoke tests so the constants
      stay in sync.
- [ ] Register `smoke`, `requires_wiley_creds`,
      `requires_springer_creds`, `requires_elsevier_creds` markers in
      `pyproject.toml` `[tool.pytest.ini_options].markers`.
- [ ] `tests/smoke/conftest.py`: skip-on-missing-env-var logic for each
      `requires_<publisher>_creds` marker; tempdir-backed `Settings`
      fixture wired from real env vars.
- [ ] `tests/smoke/test_wiley_e2e.py`: full happy-path against
      `tmp_path`; invariants per §22 table (PDF magic + size + layout).
- [ ] `tests/smoke/test_springer_e2e.py`: full happy-path against
      `tmp_path`; invariants per §22 table (JATS XML + size + layout).
- [ ] `tests/smoke/test_elsevier_e2e.py`: full happy-path against
      `tmp_path`; invariants per §22 table (Elsevier full-text envelope
      + `<originalText>` subtree + size + layout).
- [ ] Verify `uv run pytest -m smoke` runs all three when all creds
      present; verify default `uv run pytest` skips all three with no
      creds.

## 22. Smoke tests (gated end-to-end)

Three pytest-level end-to-end tests — one per publisher — that exercise
the full v3 happy path (`fetch_metadata` → publisher dispatch →
real-network retrieve → magic-byte sniff → store + manifest) against an
ephemeral tempdir-backed store. These are the highest-confidence
regression signal for "does the pipeline actually wire DOI → bytes for
this publisher today?", and the only test layer that catches
publisher-side surprises (API changes, response-shape drift, expired
tokens, IP de-allowlisting).

### Design constraints

- **Ephemeral.** Each test takes pytest's `tmp_path` as the
  `Settings.data_dir`; nothing touches the operator's real corpus.
  Teardown is pytest's normal tempdir cleanup.
- **OA DOIs only.** One known-OA DOI per publisher, hardcoded next to
  doctor's smoke-test DOI list (§12) so the same constants serve both.
  Open-access avoids two distinct problems: redistribution concerns for
  a paywalled fixture, and IP-scoping false-failures from a
  non-Würzburg test environment.
- **Invariant-style assertions, not byte equality.** Wiley TDM rotates
  PDF metadata (`/CreationDate`, watermark layers) between fetches, so
  `sha256(fetched) == sha256(reference)` flakes by design. Tests assert
  format envelope, plausible byte size, and structural markers instead.
- **Gated on credentials.** Markers `requires_wiley_creds` /
  `requires_springer_creds` / `requires_elsevier_creds` plus a `smoke`
  marker. `tests/smoke/conftest.py` skips a marked test when the
  corresponding env var is unset, so the default `uv run pytest` (no
  creds) skips all three; on-demand runs use `uv run pytest -m smoke`.
- **Not part of the per-step green-bar gate.** Smoke tests run
  pre-release, post-dep-bump, and after Wiley / Springer / Elsevier API
  outages — failures need triage, not a revert.

### Per-publisher invariants

| Publisher | Format | Layout | Byte floor | Structural marker |
|---|---|---|---|---|
| Wiley | `Format.PDF` | `artifacts/pdf/sha256/<aa>/<sha>.pdf` | ≥ 50 KB | `%PDF-` prefix; `%%EOF` in last 1 KB |
| Springer Nature | `Format.JATS_XML` | `artifacts/jats/sha256/<aa>/<sha>.xml` | ≥ 5 KB | `<?xml` declaration; `<article` (or JATS namespace) in first 4 KB |
| Elsevier | `Format.ELSEVIER_XML` | `artifacts/elsevier/sha256/<aa>/<sha>.xml` | ≥ 10 KB | `<full-text-retrieval-response>` root; `<originalText>` subtree present (rules out a META_ABS that slipped past the retriever) |

All three additionally assert: `record.publisher` matches the dispatch
table; the manifest file exists at the manifest path; `by_doi.jsonl`
carries a row with the right `format` column.

### Sketch (Wiley — Springer + Elsevier follow the same shape)

```python
@pytest.mark.smoke
@pytest.mark.requires_wiley_creds
async def test_wiley_e2e(tmp_path: Path) -> None:
    settings = _settings_from_env(data_dir=tmp_path)
    store = ArtifactStore(tmp_path)
    record = await ingest(SMOKE_DOI[Publisher.WILEY],
                          settings=settings, store=store)
    assert record.publisher is Publisher.WILEY
    assert record.format is Format.PDF
    assert record.byte_size >= 50_000
    pdf = (settings.data_dir / record.artifact_path).read_bytes()
    assert pdf.startswith(b'%PDF-')
    assert b'%%EOF' in pdf[-1024:]
    assert record.artifact_path.startswith('artifacts/pdf/sha256/')
```

### Operational notes

- **DOI churn risk.** A DOI that's OA today may not be tomorrow (rare,
  but possible). When a smoke test starts failing on a publisher API
  call rather than an invariant assertion, suspect the DOI before the
  code. Refresh the constant if needed.
- **Overlap with `doctor` (§12) is intentional.** `doctor` is operator-
  facing (interactive, Rich table, exit codes); the smoke tests are
  regression-facing (pytest, structured assertions). Sharing the DOI
  constants keeps them in sync; they never share runtime.
