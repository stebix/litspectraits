# litspectraits v2 — DOI → PDF → docling

This document supersedes `ingestion-pipeline.md` for the next iteration. The
overall project goals from `overview.md` are unchanged; what changes is the
shape of the ingestion stage and the extraction tool we commit to.

## 0. Scope shift (what changes vs current docs)

The current `docs/ingestion-pipeline.md` is a resolver-centric design: many
probes, a configurable policy that ranks across `Version × Format × Access`,
two-level sharded storage. That made sense when JATS / LaTeX / PDF were peers.
With the new framing — **PDF only, dispatched by publisher** — the format axis
collapses, the access axis collapses (a publisher retriever either has its
credential or it doesn't), and the probe-explosion stops paying for itself.

The new design has three boxes:

```
DOI
 │
 ▼
[Discover]   ──► ordered list of PdfCandidate (Unpaywall + CrossRef-derived TDM hints)
 │
 ▼
[Retrieve]   ──► dispatch each candidate to a publisher Retriever; fall through on failure
 │
 ▼
[Extract]    ──► docling → structured Document JSON next to the PDF
```

Manual sideload is a side door into [Retrieve] (it injects a record into the
store directly), and a cache-hit check is a pre-flight at the top of
[Retrieve].

## 1. Explicit non-goals (this iteration)

Spelling these out so they don't sneak back in as complexity:

- No JATS, no LaTeX, no arXiv source tarballs. PDF only.
- No 3-axis configurable `RankingPolicy`, no presets, no `--policy` flag. Just
  version order: `published > accepted_manuscript > preprint`. If we want
  preprint-first later, it's a 5-line change.
- No probe-per-source plug-in registry. Discovery is two function calls
  (Unpaywall + CrossRef metadata), not eight async probe classes.
- No `local` probe as a discovery source. The store is checked once at the top
  of ingest as a cache-hit short-circuit, not via the candidate-ordering
  machinery.
- No two-level sharding. One level (`<aa>/<sha256>.pdf`) is enough for
  low-hundreds.
- No Postgres, no R2, no failure memoization, no TOML config. Same punts as
  v1.

## 2. Module layout

```
src/litspectraits/
├── __init__.py
├── _logging.py              # structlog + rich (kept verbatim from current impl)
├── cli.py                   # Typer: ingest / extract / sideload / show
├── config.py                # env-driven Settings (extended for publisher creds)
├── doi.py                   # normalize (kept)
├── http.py                  # async client factory (kept)
│
├── discover.py              # DOI → list[PdfCandidate]; calls Unpaywall + CrossRef
│
├── retrievers/              # publisher-specific PDF fetchers (only package — multiple impls)
│   ├── __init__.py
│   ├── base.py              # Retriever protocol, RetrieveAttempt, AttemptOutcome
│   ├── generic.py           # plain HTTP GET — covers Unpaywall mirrors and preprint servers
│   ├── wiley.py             # CrossRef TDM endpoint + Wiley-TDM-Client-Token
│   ├── elsevier.py          # ScienceDirect API + X-ELS-APIKey (+ X-ELS-Insttoken)
│   ├── springer.py          # Springer Nature TDM + CR-Clickthrough-Client-Token
│   └── dispatch.py          # PdfCandidate → Retriever (DOI-prefix table)
│
├── store.py                 # ArtifactStore (one-level sharding) + DOI index
├── manifest.py              # AcquisitionRecord, ManualProvenance, cattrs hooks
├── sniff.py                 # %PDF- magic-byte check (kept)
│
├── ingest.py                # orchestrator: cache check → discover → retrieve loop → commit
├── sideload.py              # manual artifact registration
└── extract.py               # docling wrapper
```

Compared to the current tree this collapses `resolver/probes/*.py` (8 files)
and `resolver/{policy,resolver,types}.py` into `discover.py` + `retrievers/`,
fuses `acquisition/{store,manifest,sniff,fetch,attempt}.py` into a flatter
set, and adds `extract.py`. **The package count drops from 3 to 1; the file
count drops from ~20 to ~15** while gaining extraction.

## 3. Data model

All `attrs.frozen`. `cattrs` for (de)serialization with explicit hooks for the
tagged-union and `bytes ↔ base64` for sniffed prefixes (preserved from
current impl).

```python
class Version(StrEnum):
    PUBLISHED = 'published'
    ACCEPTED_MANUSCRIPT = 'accepted_manuscript'
    PREPRINT = 'preprint'

class Publisher(StrEnum):
    WILEY = 'wiley'
    ELSEVIER = 'elsevier'
    SPRINGER_NATURE = 'springer_nature'
    GENERIC = 'generic'   # arXiv, bioRxiv, PMC, repositories — no special auth

class DiscoverySource(StrEnum):
    UNPAYWALL = 'unpaywall'
    PUBLISHER_NATIVE = 'publisher_native'  # routed through publisher's preferred channel
                                           # (Wiley TDM lib, Elsevier API, Springer TDM, ...)
    LOCAL_CACHE = 'local_cache'            # only used internally for cache-hit logging

@frozen
class PdfCandidate:
    doi: str
    version: Version
    publisher: Publisher           # drives retriever dispatch
    url: str | None                # None for TDM routes that synthesize from DOI
    discovery: DiscoverySource
    license: str | None
    extra: Mapping[str, str]       # repository, host_type, oa_location_idx, ...
```

`PdfCandidate` replaces the old `Availability`. Three fields drop: `format`
(always PDF), `access` (the retriever knows whether it can authenticate),
`media_type` (always `application/pdf` after sniff). `SourceKind` collapses
into `discovery` + `publisher`.

```python
class AttemptOutcome(StrEnum):
    SUCCESS = 'success'
    HTTP_4XX = 'http_4xx'
    HTTP_5XX = 'http_5xx'
    MAGIC_BYTE_MISMATCH = 'magic_byte_mismatch'
    CONNECTION_DROPPED = 'connection_dropped'
    EMPTY_BODY = 'empty_body'
    MISSING_CREDENTIAL = 'missing_credential'   # NEW: publisher retriever has no token
    AUTH_REJECTED = 'auth_rejected'             # NEW: 401/403 specifically — distinct from HTTP_4XX

@frozen
class RetrieveAttempt:
    candidate: PdfCandidate
    retriever: str                  # 'wiley' / 'elsevier' / 'generic' / ...
    outcome: AttemptOutcome
    http_status: int | None
    fetched_url: str | None         # post-redirect final URL on success
    sniffed_prefix: bytes | None
    duration_ms: int
    attempted_at: datetime
    error: str | None

@frozen
class AcquisitionRecord:
    doi: str
    sha256: str
    artifact_path: str              # relative to data_dir
    candidate: PdfCandidate         # the winning candidate
    fetched_url: str                # post-redirect URL we actually fetched
    fetched_at: datetime
    fetcher_version: str
    byte_size: int
    origin: Literal['auto', 'manual']
    manual_provenance: ManualProvenance | None
    attempts: tuple[RetrieveAttempt, ...]   # winning route last
    discovered_candidates: tuple[PdfCandidate, ...]  # full discovery audit trail
```

`ManualProvenance` is preserved verbatim from the current design — operator
email, retrieved_at, source_url, license_assertion, note. The Wiley CDN
sideload-only memory still applies; the manifest has to capture that legal
trail.

## 4. Discovery (`discover.py`)

Two HTTP calls, max, in `asyncio.gather`:

1. **CrossRef metadata** — `GET https://api.crossref.org/works/{doi}?mailto={email}`.
   Used for: publisher inference (DOI prefix is the primary signal,
   `publisher` field is a sanity log), `link` array for TDM intent
   (Wiley/Elsevier sometimes deposit explicit
   `intended-application=text-mining` URLs).
2. **Unpaywall** — `GET https://api.unpaywall.org/v2/{doi}?email={email}`.
   Yields zero or more `oa_locations` entries, each with a `version` and
   `url_for_pdf`.

DOI-prefix table for publisher dispatch:

```python
_PUBLISHER_BY_PREFIX = {
    '10.1002': Publisher.WILEY,
    '10.1016': Publisher.ELSEVIER,
    '10.1007': Publisher.SPRINGER_NATURE,
    '10.1038': Publisher.SPRINGER_NATURE,   # Nature
    '10.1186': Publisher.SPRINGER_NATURE,   # BioMed Central / SN OA
    '10.1057': Publisher.SPRINGER_NATURE,   # Palgrave
    '10.1056': ...                          # NEJM, etc — extend on demand
}
def publisher_for_doi(doi: str) -> Publisher:
    prefix = doi.split('/', 1)[0]
    return _PUBLISHER_BY_PREFIX.get(prefix, Publisher.GENERIC)
```

The CrossRef metadata's `publisher` string is logged as a cross-check, not
used for dispatch.

Discovery output is **already ordered** so the retrieve loop just walks it:

```
1. published / publisher-native           ← whenever DOI prefix is in the publisher table
2. published / Unpaywall OA mirror
3. accepted_manuscript / Unpaywall OA mirror
4. preprint / Unpaywall OA mirror         ← arXiv / bioRxiv usually
```

Publisher-native candidates are emitted **whenever the DOI prefix maps to a
known publisher**, regardless of whether the corresponding credential is
configured. Reason: from a Würzburg IP without a token, the publisher's lib
can still succeed via IP-based subscription auth (the `wiley-tdm` client
documents this explicitly: "Authentication (API token & IP based auth)" — one
code path, server-side decides). Encoding "do we have campus access" as a
config bit is brittle; letting the retriever try and fall through is
simpler. The retriever returns `MISSING_CREDENTIAL` only when the token is
unset *and* the publisher's library refuses to attempt the request at all.

The full ordered list (including Unpaywall locations that are duplicates /
mirrors) is preserved on `AcquisitionRecord.discovered_candidates` so the
audit trail stays intact even when retrieval needed to fall through several
entries.

### Why one ordered list instead of `RankingPolicy`?

Because for PDF-only discovery, the version axis is the only one that
matters, the access axis is determined by the retriever (not the candidate),
and there is no format axis. The presets `PUBLISHED_FIRST` / `FIDELITY_FIRST`
only ever differed by swapping the format axis — which doesn't exist anymore.
A 5-line `_VERSION_RANK` dict and a stable sort is the whole policy.

If a future need for "prefer preprint" arrives, it's a single env var
(`LITSPECTRAITS_PREFER_PREPRINT=1`) inverting one rank tuple.

## 5. Retriever protocol (`retrievers/base.py`)

```python
class Retriever(Protocol):
    name: str

    async def fetch(
        self,
        candidate: PdfCandidate,
        *,
        client: httpx.AsyncClient,
        tmp_dir: Path,
        settings: Settings,
    ) -> tuple[RetrieveAttempt, _Payload | None]:
        """Attempt the fetch; on SUCCESS return (attempt, payload), else (attempt, None)."""
```

`_Payload` is the same internal handoff type as in current
`acquisition/fetch.py`: sha256, byte_size, tmp_path. The retriever is
responsible for the entire download — request, magic-byte sniff, hash, atomic
temp file. The reason this lives in the retriever and not in a single shared
helper is that **publishers vary in how they answer 200s with non-PDF
bodies**: Wiley redirects to a CAPTCHA HTML page on auth failure (not a
401), Elsevier returns 200 with a JSON error body, Springer sometimes serves
a placeholder image. The shared sniff function (`sniff.py`) catches all of
these once we get bytes; the retriever only needs to decide *what request to
make and through which client*.

There are **two retriever shapes**:

1. **httpx-streaming retrievers** — `generic`, and any publisher we hit with
   our own client. Streams via `client.stream`, magic-byte-sniffs the first
   chunk before opening the temp file, hashes-on-the-fly. The current
   `acquisition/fetch._try_fetch` logic is the canonical implementation;
   factored into a shared `_streaming.py` helper inside `retrievers/`.

2. **library-shim retrievers** — `wiley` is the first example, using the
   official `wiley-tdm` package. The library does its own URL construction,
   header management, IP-and-token auth, and streaming download to a directory
   we configure. Our shim bridges sync→async via `asyncio.to_thread`,
   translates the library's exceptions into our `RetrieveAttempt` outcomes,
   and runs the magic-byte sniff + hash **post-download** (one extra pass
   over the file — negligible for typical PDFs). Defense-in-depth: even when
   the library says "ok", we verify `%PDF-` ourselves before committing.

### Publisher implementations

Each retriever signals **`MISSING_CREDENTIAL`** without making an HTTP call
when its configured token/key is unset, distinct from `AUTH_REJECTED` after a
real 401/403. This gives operators the right error message: "configure
`LITSPECTRAITS_WILEY_TDM_TOKEN`" vs "your token was rejected by Wiley".

| Retriever | Channel | Auth | Failure modes worth flagging |
|-----------|---------|------|------------------------------|
| `generic` | httpx-streaming via `candidate.url` | none | 4xx, 5xx, magic-byte (paywall HTML), connection drop |
| `wiley`   | `wiley-tdm` library shim — lib owns URL construction (`api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}` internally), session, streaming, retries | `TDM_API_TOKEN` env var, forwarded from `LITSPECTRAITS_WILEY_TDM_TOKEN`; lib *also* uses caller IP for subscription auth (Würzburg IP unlocks content the bare token doesn't) | Lib's `AccessDenied` → `AUTH_REJECTED`; lib raising on no-token-and-no-IP → `MISSING_CREDENTIAL`; post-download magic-byte mismatch (defense in depth) → `MAGIC_BYTE_MISMATCH`. Memory note: Wiley CDN blocks bronze-OA — never falls into `generic` retriever for `10.1002/...`, the `wiley` retriever is the only automated path |
| `elsevier` | httpx-streaming, `https://api.elsevier.com/content/article/doi/{doi}?httpAccept=application/pdf` (or `elsapy` library shim if we adopt it later) | `X-ELS-APIKey` (always), `X-ELS-Insttoken` (when set, for institutional access beyond the OA tier) | `MISSING_CREDENTIAL` on no apikey; 401 → `AUTH_REJECTED`; 403 with `quota_exceeded` body → `AUTH_REJECTED` with informative `error` field |
| `springer` | httpx-streaming, `https://link.springer.com/content/pdf/{doi}.pdf` (CR Click-Through path) or Springer's TDM API for OA content | `CR-Clickthrough-Client-Token` (CrossRef token, separate from Wiley's TDM token) | Same shape as the httpx path generally; bronze content sometimes 200s but redirects through `link.springer.com` to a paywall HTML — magic-byte sniff catches it |

Auth tokens are **not unified across publishers**. Wiley issues its own TDM
API token via the WOL TDM resources page (consumed by `wiley-tdm`). Springer
Nature accepts the CrossRef Click-Through token. Elsevier wants its own
ScienceDirect API key. Three separate env vars, three separate retrievers.
That's the honest shape of the publisher landscape — earlier drafts of this
doc consolidated them into one CR token, which doesn't match reality.

### Dispatch (`retrievers/dispatch.py`)

```python
def retriever_for(candidate: PdfCandidate, registry: RetrieverRegistry) -> Retriever:
    # If the candidate came from an OA mirror, use generic regardless of publisher
    if candidate.discovery is DiscoverySource.UNPAYWALL:
        return registry.generic
    return registry.by_publisher[candidate.publisher]
```

The crucial subtlety: an Unpaywall-discovered Wiley DOI gets the **generic**
retriever (it's already an OA copy at PMC or a repository — no TDM dance
needed), but a CrossRef-TDM-discovered Wiley DOI gets the **wiley** retriever
(it must hit Wiley's TDM endpoint with the right header). Discovery and
publisher are independent inputs to dispatch.

## 6. Acquisition orchestration (`ingest.py`)

```python
async def ingest(doi: str, *, settings: Settings, store: ArtifactStore,
                 force: bool = False) -> AcquisitionRecord:
    doi = normalize(doi)
    log = structlog.get_logger('litspectraits.ingest').bind(doi=doi)

    # 1. Cache hit short-circuit — replaces the entire local-probe machinery
    if not force:
        existing = store.find_by_doi(doi)
        if existing:
            log.info('ingest.cache_hit', sha256=existing[0].sha256)
            return existing[0]

    # 2. Discover
    async with http_client(settings) as client:
        candidates = await discover(doi, client=client, settings=settings)
        if not candidates:
            raise NoCandidatesFoundError(doi)
        log.info('ingest.discovered', n_candidates=len(candidates))

        # 3. Retrieve loop with fall-through (cap 5: published_TDM, published_OA,
        #    accepted, preprint, plus one mirror)
        attempts: list[RetrieveAttempt] = []
        for cand in candidates[:MAX_ACQUIRE_ATTEMPTS]:
            ret = retriever_for(cand, REGISTRY)
            attempt, payload = await ret.fetch(
                cand, client=client, tmp_dir=store.tmp_dir, settings=settings,
            )
            attempts.append(attempt)
            if attempt.outcome is AttemptOutcome.SUCCESS and payload:
                return _commit(doi, cand, payload, store,
                               attempts=tuple(attempts), discovered=candidates)
            log.warning('ingest.attempt_failed',
                        retriever=ret.name, outcome=attempt.outcome.value)

    raise AcquisitionExhaustedError(doi, tuple(attempts))
```

This is **everything**. No `ResolveResult`, no `permitted_candidates`
property, no resolve/acquire split. One function, one async context, ~25
lines.

`_commit` is the same atomic-replace-and-write-manifest from current
`acquisition/fetch.py`.

## 7. Storage layout (one-level sharding)

```
<data_dir>/
├── artifacts/pdf/sha256/<aa>/<full-sha256>.pdf
├── manifests/sha256/<aa>/<full-sha256>.manifest.json
├── documents/<full-sha256>/
│   ├── document.json          # docling output, normalized
│   └── meta.json              # docling version, model versions, extracted_at
├── index/by_doi.jsonl         # append-only DOI → sha256 index
└── tmp/                       # download staging, cleared on startup
```

One-level prefix (`<aa>` = first two hex chars of the sha256) gives 256 leaf
directories. With ~hundreds of artifacts, ~1 file per directory on average,
no fanout problem. If we 10x to thousands, still ~12 files per directory —
well under FS-friendly limits.

The `documents/` tree is **not** sharded (low cardinality, indexed by full
sha256 anyway). Re-extraction with a newer docling version creates a new
`meta.json` but overwrites `document.json` — we don't keep historical docling
outputs unless an explicit `--preserve` flag is added later. (Cheap to add;
YAGNI for now.)

`manifest.path()` and `shard_path()` lose the `[2:4]` slice. Migration from
the current two-level layout is trivial: a `litspectraits migrate-store`
one-shot reshards in place. (I'd hold off implementing it unless we have data
already; the new tree starts empty.)

## 8. Extraction with docling (`extract.py`)

```python
import asyncio
from docling.document_converter import DocumentConverter

_CONVERTER: DocumentConverter | None = None  # lazy: docling loads models on first use

def _get_converter() -> DocumentConverter:
    global _CONVERTER
    if _CONVERTER is None:
        _CONVERTER = DocumentConverter()
    return _CONVERTER

async def extract(record: AcquisitionRecord, store: ArtifactStore) -> ExtractRecord:
    pdf_path = store.absolute_path(record)
    log = structlog.get_logger('litspectraits.extract').bind(
        doi=record.doi, sha256=record.sha256,
    )
    log.info('extract.start')
    # docling is sync + CPU/GPU-bound — run in a worker thread
    result = await asyncio.to_thread(_get_converter().convert, str(pdf_path))
    out_dir = store.document_dir(record.sha256)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'document.json').write_text(
        json.dumps(result.document.export_to_dict(), indent=2), encoding='utf-8',
    )
    meta = ExtractRecord(
        sha256=record.sha256, doi=record.doi,
        docling_version=docling.__version__,
        extracted_at=datetime.now(UTC),
        n_pages=len(result.document.pages),
        n_tables=len(result.document.tables),
    )
    (out_dir / 'meta.json').write_text(
        json.dumps(serialize(meta), indent=2), encoding='utf-8',
    )
    log.info('extract.done', n_pages=meta.n_pages, n_tables=meta.n_tables)
    return meta
```

**Open call**: do we keep docling's native dict as the canonical Document, or
do we map it into our own `Document` schema (per `overview.md`)? My
recommendation is to **defer the normalized schema** until the agent triad
work begins — the docling JSON is rich, well-typed, and re-running through a
normalizer later is cheap. Writing an `attrs/cattrs` `Document` schema now
risks committing to a shape before the ingestor agent tells us what it
actually needs.

`docling` itself is the sole new heavy dep. It pulls torch + image models.
That's fine for our setup (you have GPU on the Würzburg server) but doubles
install time; consider an optional `[docling]` extra so CI for resolve /
retrieve tests doesn't need it.

## 9. Sideload (`sideload.py`)

Preserved nearly verbatim from current impl, with the simplifications:
- No `--format` flag (always PDF). Validates `%PDF-` magic bytes.
- No `--version` constraint check against the resolve tree (no resolve tree).
- `--license` and `--note` still required for the legal trail.
- `ManualProvenance` still mandatory and recorded on
  `AcquisitionRecord.manual_provenance`.

The Wiley CDN memory still applies:
`litspectraits sideload <doi> ~/Downloads/wiley-paper.pdf --version published --license 'wiley-tdm-internal-use-only' --note 'via uni-wuerzburg library proxy 2026-05-10'`
is the canonical workflow for Wiley papers when no TDM token is configured.

## 10. CLI

```
litspectraits ingest   <doi> [--force]
litspectraits extract  <doi-or-sha> [--reextract]
litspectraits sideload <doi> <pdf-path> --version --license [--source-url] [--note]
litspectraits show     <doi>                    # show manifest + extraction status
```

The standalone `resolve` command is dropped. It existed mostly to debug the
probe layer; with discovery being two HTTP calls, `ingest --dry-run` is
enough. (Add `--dry-run` to ingest if useful — emits the candidate list and
exits before fetching.)

`ingest` does **not** auto-run extract. Reasons:
- Extract is much slower than ingest (model loading + GPU inference per PDF).
- Re-extracting on docling version bumps without re-fetching is a common
  workflow.
- Easy to chain in scripts:
  `litspectraits ingest <doi> && litspectraits extract <doi>`.

If batching becomes painful, add `--extract` to ingest later.

Rich rendering: same Console + Panel + Table patterns from current impl. The
`attempts` table is still there; gains a `retriever` column.

## 11. Configuration (`config.py`)

| Var | Purpose | Default |
|-----|---------|---------|
| `LITSPECTRAITS_CONTACT_EMAIL` | mailto for polite pool, manifests | **required** — fail loudly |
| `LITSPECTRAITS_DATA_DIR` | store root | `platformdirs.user_data_dir('litspectraits')` |
| `LITSPECTRAITS_LOG_FORMAT` | `rich` / `json` | `rich` |
| `LITSPECTRAITS_HTTP_TIMEOUT_S` | per-request timeout | `30` |
| `LITSPECTRAITS_WILEY_TDM_TOKEN` | Wiley TDM API token (from WOL TDM resources page); forwarded into `TDM_API_TOKEN` for the `wiley-tdm` library | unset → `wiley` retriever still tries (IP-based auth may succeed from Würzburg); reports `MISSING_CREDENTIAL` only when token-less *and* no-IP path |
| `LITSPECTRAITS_CROSSREF_TDM_TOKEN` | CrossRef Click-Through token; used by `springer` retriever (and any other CR-honoring publisher we add) | unset → `springer` retriever falls back to OA-only paths and reports `MISSING_CREDENTIAL` for paywalled content |
| `LITSPECTRAITS_ELSEVIER_API_KEY` | Elsevier ScienceDirect API key | unset → `elsevier` retriever reports `MISSING_CREDENTIAL` |
| `LITSPECTRAITS_ELSEVIER_INSTTOKEN` | Elsevier institutional token (optional) | unset → only OA tier accessible |

Three publisher tokens, not one. Wiley's TDM token is issued separately from
CrossRef Click-Through and is consumed by their `wiley-tdm` library;
forwarding it into `TDM_API_TOKEN` (the lib's expected env var) is done in
the retriever via a scoped env-patch, so the rest of the process never sees
it. Elsevier's API key is unrelated to either.

## 12. Failure semantics

Loud-fail aligns with `project-infra-overview.md`:

- **DOI not in CrossRef** → `DOINotFoundError`, exit 2.
- **No candidates discovered** (Unpaywall has nothing, no creds for publisher
  TDM) → `NoCandidatesFoundError`, exit 1, attempts table is empty.
- **All candidates failed** → `AcquisitionExhaustedError(attempts=...)`, exit
  1, full attempts table rendered.
- **Magic-byte mismatch** → `MAGIC_BYTE_MISMATCH` recorded with first 64
  bytes (helpful for "why is Wiley returning HTML"), fall through.
- **Missing credential** → `MISSING_CREDENTIAL` recorded, fall through. CLI
  summarizes with a one-line "configure `LITSPECTRAITS_*` to unlock Wiley
  TDM" hint.
- **Hash collision against existing artifact** (rare but real on retries) →
  `IntegrityError`, never silently overwrite.
- **Sideload of non-PDF** → `MalformedArtifactError`, refuse.
- **docling import failure / model download failure** → propagate verbatim.
  Don't catch; this stage is a separate command and the user wants the real
  error.

No silent fallbacks anywhere. The fall-through loop is explicit and audited.

## 13. Dependencies

Runtime (current set, mostly preserved):
- `httpx[http2]`, `structlog`, `rich`, `attrs`, `cattrs`, `typer`,
  `platformdirs`, `python-dotenv`
- **add**: `docling` (heavy — PyTorch, image models) under optional `[extract]`
- **add**: `wiley-tdm` (lightweight — pulls `requests>=2.32`) under optional
  `[wiley]`. The Wiley retriever imports lazily inside `fetch()`, so users
  without the extra get a clean `MISSING_CREDENTIAL` rather than an
  `ImportError` at startup
- **drop**: `tenacity` (no per-attempt retry layer — fall-through is the only
  retry, and it's per-candidate not per-request; `wiley-tdm` does its own
  internal retries for transient errors)

Dev:
- `pyright`, `pytest`, `pytest-asyncio`, `respx`, `ruff` — unchanged

Optional installs:
- `pip install litspectraits[extract]` → docling
- `pip install litspectraits[wiley]` → `wiley-tdm`
- `pip install litspectraits[all]` → everything

CI for discover / retrieve tests stays lean by skipping the extras and
asserting `MISSING_CREDENTIAL` outcomes when the libs aren't present.

## 14. Logging

`_logging.py` stays identical. Logger namespaces:
- `litspectraits.ingest` — orchestrator (DOI bound via `contextvars`)
- `litspectraits.discover.unpaywall`, `litspectraits.discover.crossref`
- `litspectraits.retrievers.generic`, `litspectraits.retrievers.wiley`, etc.
- `litspectraits.extract.docling`
- `litspectraits.store`

DOI bound once at the top of `ingest()` / `extract()` / `sideload()` so every
line in that operation carries it — same pattern as current.

## 15. Order of work

Each step ends green on `pytest`, `ruff check`, `pyright`. Same discipline as
current.

1. **Foundations**: prune current files. Keep `_logging.py`, `config.py`,
   `doi.py`, `http.py`, `sniff.py` (renamed to top-level). Add
   `LITSPECTRAITS_ELSEVIER_API_KEY` etc. to `config.py`. One commit.
2. **Data model**: `manifest.py` with `PdfCandidate`, `RetrieveAttempt`,
   `AcquisitionRecord`, cattrs hooks. Tests for round-trip.
3. **Store**: `store.py` with one-level sharding; `find_by_doi`; manifest
   write. Tests for layout + index dedup.
4. **Discovery**: `discover.py` — Unpaywall + CrossRef metadata + DOI-prefix
   publisher table. Respx-mocked tests for OA paper, Wiley DOI without token,
   Wiley DOI with token (synthesizes TDM candidate), preprint-only paper.
5. **Retrievers**: `retrievers/base.py` + `generic.py` first, with shared
   `_stream_to_temp_with_sniff` helper. Tests for 200, 4xx fallthrough-friendly,
   magic-byte mismatch, mid-stream drop.
6. **Publisher retrievers**: `wiley.py` (library shim around `wiley-tdm`,
   imported lazily; `_patched_env` forwards
   `LITSPECTRAITS_WILEY_TDM_TOKEN` → `TDM_API_TOKEN` for the duration of the
   `to_thread` call), `elsevier.py`, `springer.py`. Each gets one test for
   `MISSING_CREDENTIAL`, one for `AUTH_REJECTED`, one for success. Wiley's
   tests monkeypatch `wiley_tdm.TDMClient`; httpx-based ones use `respx`.
7. **Dispatch + ingest**: `retrievers/dispatch.py` + `ingest.py`. Integration
   tests for: cache hit, single-success, fallthrough across versions,
   exhaustion.
8. **Sideload**: `sideload.py` + manual-provenance test (idempotency on
   repeat sideload).
9. **CLI**: `ingest`, `sideload`, `show`. Rich rendering. One golden-output
   test.
10. **Extraction**: `extract.py` + docling. Test with a tiny PDF fixture
    (1-page synthetic). `extract` CLI command.
11. **Migration**: `litspectraits migrate-store` if there's existing data,
    otherwise skip. README quickstart with a worked OA DOI + a worked Wiley
    DOI sideload.

## 16. What this gives up vs current impl

Honest accounting — these are not pretend-savings:

- **Multi-format extensibility**. Adding JATS back means a new candidate
  kind, a new retriever or extraction route, a third axis on `PdfCandidate`.
  Doable but it's a real refactor — perhaps 2 days. v1's design absorbed that
  growth invisibly. v2 punts it.
- **Configurable preference**. The 3-axis policy let you say "I want any
  preprint over a published-PDF when I can get LaTeX or JATS for the
  preprint". v2 can't express that without inverting one rank tuple.
  Acceptable now (we don't have that need); a regression if requirements
  change.
- **Probe-level extensibility**. Adding bioRxiv as a discovery source
  v1-style is "drop a new probe in `probes/`". v2-style it's "add a new
  branch to `discover.py`". Both are small; v1's plugin shape is more
  extensible if we end up with 10+ sources. We won't, in this iteration.

Trade is: **3 fewer files to maintain, ~600 fewer LOC of design surface, no
`RankingPolicy` config to reason about**, in exchange for two known
refactors if scope creeps. That's a clear win for the current target of
low-hundreds of MRI papers.

## 17. Open questions for discussion

These need a call before code starts:

1. ~~**Wiley auth model**~~ → **Resolved.** Wiley issues its own TDM API
   token (separate from CrossRef Click-Through), consumed by the official
   `wiley-tdm` Python package. The package combines token-based and
   IP-based auth in one call, so running from a Würzburg IP unlocks
   subscription content even without — or in addition to — the token.
   `wiley.py` ships as a thin library shim from day one.
2. **Elsevier scope**: do we have a ScienceDirect API key in hand? If not,
   `elsevier.py` is a stub that always returns `MISSING_CREDENTIAL`, which is
   fine but then Elsevier papers are effectively sideload-only too. (Side
   note: Elsevier ships `elsapy` — if we want to mirror the Wiley
   library-shim pattern, that's the natural choice.)
3. **docling output as canonical Document**: defer the normalized `Document`
   schema (recommended) or build it now mapping from docling's dict?
4. **Migration of existing data**: existing test fixtures + `__pycache__`
   suggest the pipeline has been run. Is there real data in any `data_dir`
   we need to preserve (re-shard, port manifests), or is starting clean
   fine?
5. **Cache-hit semantics**: current impl caches per `(doi, source_url)` so a
   force-refresh from a different source still creates a new record. v2
   caches per `doi` (returns the first record found). Simpler, but if you
   re-ingest and a better version becomes available, you'd need `--force`.
   OK, or do you want v1's per-URL cache behavior?
