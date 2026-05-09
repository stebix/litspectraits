# Document Ingestion Pipeline — Step 1 Design

## Aim

Build the first stage of the literature pipeline: given a DOI, **resolve** the
best available acquisition route and **acquire** the raw artifact into a
content-addressed local store. The output of this stage is a hashed artifact on
disk plus a signed manifest recording where it came from, which the
extraction stage will later consume.

This stage covers the top two boxes of the overall pipeline diagram in
`overview.md`:

```
DOI
 │
 ▼
[Resolver]    ──► structured availability result (route, version, format, access, license)
 │
 ▼
[Acquisition] ──► raw artifact fetched, hashed, persisted to Layer 1
```

Out of scope for this stage:

- Extraction (JATS / PDF / LaTeX → normalized `Document`)
- Reference resolution
- Postgres index (Layer 3) — file store + a small JSONL DOI index is enough
- The agent triad
- Cloudflare R2 mirroring (the local store is the canonical store for now)

## External Services

| Source kind            | Service                                              | Used for                                                          |
| ---------------------- | ---------------------------------------------------- | ----------------------------------------------------------------- |
| Metadata               | CrossRef `/works/{doi}`                              | DOI normalization, publisher `link` array, type, relations        |
| JATS — open access     | PMC ID converter + PMC OA Web Service                | DOI → PMCID → OA package                                          |
| JATS — broader         | Europe PMC `fullTextXML`                             | Coverage where NCBI does not, esp. preprint server records        |
| JATS — preprints       | bioRxiv / medRxiv API                                | Native JATS for preprints                                         |
| LaTeX                  | arXiv API + arXiv source tarball                     | When DOI metadata indicates an arXiv deposit                      |
| PDF — open access      | Unpaywall `/v2/{doi}` (mailto-keyed)                 | Curated index of OA PDF locations, per-location version tags      |
| PDF / JATS — TDM       | CrossRef `link` with `intended-application=text-mining` | Wiley/Elsevier; only usable if a Crossref TDM token is provisioned |
| Local store            | On-disk artifact store                               | Already-acquired artifacts, including manually sideloaded ones    |

The polite pool is mandatory: every CrossRef and Unpaywall request carries a
`mailto=` parameter (and `User-Agent: litspectraits/<ver> (+mailto:...)`),
loaded from `LITSPECTRAITS_CONTACT_EMAIL`.

## Data Model

The core insight is that an acquisition route varies along three independent
axes, which a flat priority list collapses incorrectly. Tag each `Availability`
on all three axes; rank lexicographically by a configurable policy.

### Source axes

```python
class Version(StrEnum):
    PUBLISHED = 'published'
    ACCEPTED_MANUSCRIPT = 'accepted_manuscript'
    PREPRINT = 'preprint'

class Format(StrEnum):
    JATS = 'jats'
    LATEX = 'latex'
    PDF = 'pdf'

class Access(StrEnum):
    OPEN = 'open'
    TDM_TOKEN = 'tdm_token'      # need Crossref Click-Through token
    SUBSCRIPTION = 'subscription'  # need institutional subscription
```

| Axis    | Captures                                                                       |
| ------- | ------------------------------------------------------------------------------ |
| Version | Whose words and numbers — version of record vs author manuscript vs preprint   |
| Format  | Extraction fidelity downstream                                                 |
| Access  | Whether we can actually fetch it from this environment                         |

### Availability and probe outcomes

```python
class SourceKind(StrEnum):
    JATS_PMC = 'jats_pmc'
    JATS_EUROPEPMC = 'jats_europepmc'
    JATS_BIORXIV = 'jats_biorxiv'
    JATS_CROSSREF_TDM = 'jats_crossref_tdm'
    LATEX_ARXIV = 'latex_arxiv'
    PDF_UNPAYWALL = 'pdf_unpaywall'
    PDF_CROSSREF_TDM = 'pdf_crossref_tdm'
    MANUAL = 'manual'             # operator-sideloaded
    LOCAL_CACHE = 'local_cache'   # already in the store from a prior run

@frozen
class Availability:
    source_kind: SourceKind     # provenance — which probe surfaced it
    version: Version
    format: Format
    access: Access
    url: str                    # network URL or file:// for local
    media_type: str
    license: str | None
    extra: Mapping[str, str]    # pmcid, arxiv_id, biorxiv_server, oa_location_idx, ...

@frozen
class ProbeOutcome:
    probe: str
    availabilities: tuple[Availability, ...]   # 0..N — probes can yield multiple
    error: str | None                          # only for unexpected failures
    duration_ms: int

@frozen
class ResolveResult:
    doi: str
    chosen: Availability | None                # top-ranked fetchable; None if nothing usable
    candidates: tuple[Availability, ...]       # ordered by active policy, includes auth-blocked
    excluded: tuple[tuple[Availability, str], ...]  # (availability, reason) — debug trail
    probes: tuple[ProbeOutcome, ...]
    policy: 'RankingPolicy'
    resolved_at: datetime
```

`SourceKind` is provenance only; ranking is done on the three semantic axes.
`candidates` keeps everything we found — including auth-blocked entries — so
the CLI and downstream tooling can show "we know JATS exists for this paper,
configure a token to unlock it" without breaking batch runs.

### Acquisition record

```python
class Origin(StrEnum):
    AUTO = 'auto'        # fetched by acquisition.fetch
    MANUAL = 'manual'    # sideloaded by `litspectraits sideload`

@frozen
class ManualProvenance:
    operator: str                # email from LITSPECTRAITS_CONTACT_EMAIL
    retrieved_at: datetime
    source_url: str | None       # the publisher URL the operator hit
    note: str                    # 'via uni-wuerzburg library proxy', etc.
    license_assertion: str       # operator-declared

@frozen
class AcquisitionRecord:
    doi: str
    sha256: str
    artifact_path: str                          # relative to data_dir
    source: Availability                        # the chosen Availability
    resolve_result: ResolveResult | None        # None for manual sideload
    fetched_at: datetime
    fetcher_version: str
    byte_size: int
    origin: Origin
    manual_provenance: ManualProvenance | None  # set iff origin=MANUAL
```

Manual provenance is non-negotiable. Operator-retrieved artifacts typically
arrive through institutional access — licensed for our use but not
redistributable. The manifest is the only place that fact lives.

## Ranking Policy

Lexicographic over the three axes, with a configurable axis order and
per-axis ordering.

```python
class Axis(StrEnum):
    VERSION = 'version'
    FORMAT = 'format'
    ACCESS = 'access'

@frozen
class RankingPolicy:
    axes: tuple[Axis, ...]                      # most-significant first
    version_order: tuple[Version, ...]          # best-to-worst
    format_order: tuple[Format, ...]
    access_order: tuple[Access, ...]
    minimum_access: Access                      # drop tiers worse than this
    excluded_versions: frozenset[Version]       # e.g. forbid preprint entirely

    def permits(self, a: Availability) -> bool: ...
    def sort_key(self, a: Availability) -> tuple[int, ...]: ...
```

Sort key: `tuple(rank_in(getattr(policy, f'{axis}_order'), getattr(a, axis)) for axis in policy.axes)`.
Lower is better. Stable, no magic weights.

### Presets

```python
PUBLISHED_FIRST = RankingPolicy(
    axes=(Axis.VERSION, Axis.FORMAT, Axis.ACCESS),
    version_order=(Version.PUBLISHED, Version.ACCEPTED_MANUSCRIPT, Version.PREPRINT),
    format_order=(Format.JATS, Format.LATEX, Format.PDF),
    access_order=(Access.OPEN, Access.TDM_TOKEN, Access.SUBSCRIPTION),
    minimum_access=Access.TDM_TOKEN,
    excluded_versions=frozenset(),
)

FIDELITY_FIRST = evolve(PUBLISHED_FIRST, axes=(Axis.FORMAT, Axis.VERSION, Axis.ACCESS))
```

`PUBLISHED_FIRST` is the default and produces:

```
1. Published JATS    (PMC OA, EPMC VoR, Crossref TDM XML*)
2. Published LaTeX   (rare — almost never exists)
3. Published PDF     (Unpaywall publishedVersion, Crossref TDM PDF*)
4. Accepted-MS JATS  (PMC OA author manuscripts, EPMC AM)
5. Accepted-MS PDF   (Unpaywall acceptedVersion)
6. Preprint JATS     (bioRxiv / medRxiv)
7. Preprint LaTeX    (arXiv)
8. Preprint PDF      (Unpaywall submittedVersion)
```

Switching the axis tuple to `(FORMAT, VERSION, ACCESS)` flips this to
prefer preprint JATS over published PDF — same data, different policy.

### Auth-blocked handling

When a probe confirms a higher-ranked source exists but the environment lacks
credentials (e.g. Wiley TDM JATS detected, no Crossref token configured):

- The `Availability` is constructed normally and tagged `access=TDM_TOKEN` or
  `SUBSCRIPTION`.
- `policy.permits(a)` returns `False` because the configured `minimum_access`
  excludes that tier when no token is present.
- The entry stays in `candidates` for the audit trail; it is never `chosen`.
- The resolver falls through to the next fetchable tier.
- The CLI prints the entry greyed-out with reason `'auth_required: no_tdm_token'`.

This way the operator sees what they could unlock without batch runs failing.

### Configurability surface

Three layers, increasing power:

1. **CLI flag** — `--policy published-first` (default) | `fidelity-first`.
2. **Library kwarg** — `resolve(doi, policy=my_policy)`.
3. **TOML config** — deferred until someone needs it; YAGNI.

## Resolver

### Probe protocol

```python
class Probe(Protocol):
    name: str

    async def probe(self, ctx: ProbeContext) -> ProbeOutcome:
        ...
```

`ProbeContext` carries the normalized DOI, a shared `httpx.AsyncClient`, the
shared CrossRef metadata (fetched once per resolve), the artifact store
handle (for the local probe), and the active config. Probes are stateless and
idempotent.

### Probe → axis mapping

| Probe              | Yields                | Version determination                                   | Format | Access            |
| ------------------ | --------------------- | ------------------------------------------------------- | ------ | ----------------- |
| `local`            | 0..N                  | Stored on each `AcquisitionRecord`                      | varies | `open` (on disk)  |
| `pmc`              | 0..1                  | JATS `<pub-type>` or PMC OA metadata                    | jats   | `open`            |
| `europepmc`        | 0..1                  | EPMC `pubTypeList` distinguishes VoR vs AM              | jats   | `open`            |
| `biorxiv`          | 0..1                  | Always `preprint`                                       | jats   | `open`            |
| `crossref_tdm` xml | 0..1                  | Always `published`                                      | jats   | `tdm_token`       |
| `arxiv`            | 0..1                  | Always `preprint`                                       | latex  | `open`            |
| `unpaywall`        | **0..N**              | Per-location: `publishedVersion`/`acceptedVersion`/`submittedVersion` | pdf    | `open`            |
| `crossref_tdm` pdf | 0..1                  | Always `published`                                      | pdf    | `tdm_token`       |

Unpaywall yields multiple `Availability` instances — one per `oa_locations`
entry — so the policy can rank a published-version PDF above an
accepted-manuscript PDF from the same probe.

### Flow

```python
async def resolve(doi: str, *, policy: RankingPolicy = PUBLISHED_FIRST) -> ResolveResult:
    doi = normalize(doi)
    ctx = await build_context(doi)               # CrossRef metadata fetched once
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(p.probe(ctx)) for p in enabled_probes(ctx.config)]
    outcomes = tuple(t.result() for t in tasks)
    found = tuple(a for o in outcomes for a in o.availabilities)
    permitted = sorted((a for a in found if policy.permits(a)), key=policy.sort_key)
    excluded = tuple((a, exclusion_reason(a, policy)) for a in found if not policy.permits(a))
    return ResolveResult(
        doi=doi,
        chosen=permitted[0] if permitted else None,
        candidates=tuple(permitted) + tuple(a for a, _ in excluded),
        excluded=excluded,
        probes=outcomes,
        policy=policy,
        resolved_at=datetime.now(UTC),
    )
```

Probes run concurrently with `asyncio.TaskGroup`, capped at 8 in flight via
`asyncio.Semaphore`. Most negatives are fast (single GET + 404), and the audit
trail is more valuable than shaving the last probe off the wall clock.

A failed probe (network error, malformed response) produces a `ProbeOutcome`
with `error` populated and `availabilities=()` — it never silently breaks the
resolve.

## Acquisition

```python
async def acquire(result: ResolveResult, *, force: bool = False) -> AcquisitionRecord:
    if result.chosen is None:
        raise NoSourceAvailable(result.doi)

    existing = store.find_by_doi(result.doi)
    if not force and any(rec.source.source_kind == result.chosen.source_kind for rec in existing):
        return existing[0]                      # cache hit; log it

    async with http.stream('GET', result.chosen.url) as response:
        sha256, byte_size, tmp_path = await stream_to_temp_with_hash(response)
    final_path = store.shard_path(result.chosen.format, sha256)
    os.replace(tmp_path, final_path)            # atomic
    record = AcquisitionRecord(
        doi=result.doi, sha256=sha256, artifact_path=str(final_path.relative_to(data_dir)),
        source=result.chosen, resolve_result=result,
        fetched_at=datetime.now(UTC), fetcher_version=__version__,
        byte_size=byte_size, origin=Origin.AUTO, manual_provenance=None,
    )
    store.write_manifest(record)
    return record
```

Streaming download with hash-on-the-fly: never load the full artifact into
memory (arXiv tarballs and some PDFs are large). Temp file under
`<data_dir>/tmp/`, atomic `os.replace` into the sharded path on success, hash
mismatches fail loudly.

## Manual Sideload + Local Probe

The operator path for papers we retrieve manually (publisher PDF via
institutional library proxy, etc.) is a first-class part of the design, not a
side door.

### `litspectraits sideload`

```
litspectraits sideload <doi> <path> \
    --version published          \   # required: published | accepted_manuscript | preprint
    --format pdf                 \   # required: pdf | jats | latex
    --source-url <url>           \   # optional — where it came from
    --license <spdx-or-string>   \   # required for legal trail
    --note 'via uni-wuerzburg library proxy'
```

What it does:

1. Validate file exists. Magic-byte sniff: `%PDF-` for PDF, `<?xml`/`<article`
   for JATS, `\documentclass` for LaTeX. Refuse zero-byte or HTML
   error-page-saved-as-PDF.
2. Stream-hash to sha256, copy into the sharded artifact path atomically.
3. Synthesize an `Availability` with `source_kind=MANUAL`, the user-provided
   axes, `access=OPEN` (we have it locally now), and the user-provided
   `license`.
4. Write `AcquisitionRecord` with `origin=MANUAL`, `resolve_result=None`,
   `manual_provenance` populated.
5. Print a Rich panel matching `ingest`'s output.

Idempotent on `(doi, sha256)` — re-running with the same file is a no-op.

### `local` probe

A probe `resolver/probes/local.py` reads the artifact store on disk and yields
`Availability` instances for every `AcquisitionRecord` already present for the
DOI:

```python
class LocalStoreProbe:
    name = 'local'

    async def probe(self, ctx: ProbeContext) -> ProbeOutcome:
        records = ctx.store.find_by_doi(ctx.doi)
        availabilities = tuple(
            evolve(rec.source, source_kind=SourceKind.LOCAL_CACHE,
                   access=Access.OPEN, url=f'file://{ctx.store.absolute_path(rec)}')
            for rec in records
        )
        return ProbeOutcome(probe=self.name, availabilities=availabilities, error=None,
                            duration_ms=...)
```

The `local` probe runs alongside the network probes and feeds candidates
through the **same `RankingPolicy`** as everything else:

- A sideloaded `(published, pdf)` outranks an Unpaywall `(preprint, pdf)`
  automatically — no special "manual wins" rule, the version axis carries it.
- Re-running `ingest <doi>` after a sideload skips the network entirely: the
  `local` probe finds the artifact, ranking puts it on top, acquisition sees
  `chosen.source_kind ∈ {MANUAL, LOCAL_CACHE}` and short-circuits as a cache
  hit.
- If a published JATS later becomes available (embargo lifts), the next run
  surfaces both — manual published-PDF and auto-fetched published-JATS — and
  ranking promotes the JATS over the PDF (same version tier, better format).

### End-to-end interaction

```
$ litspectraits resolve 10.1002/mrm.xxxxx
Probes:
  pmc           ✗  not_in_oa_subset
  europepmc     ✗  not_found
  biorxiv       —  not_a_preprint
  arxiv         —  not_a_preprint
  unpaywall     ✓  preprint pdf  open
  crossref_tdm  ⚠  published jats  auth_required: no_tdm_token
  local         ✗  no_local_artifact
Chosen: unpaywall (preprint pdf)

$ # operator pulls the publisher PDF via library proxy
$ litspectraits sideload 10.1002/mrm.xxxxx ~/Downloads/paper.pdf \
    --version published --format pdf \
    --source-url https://onlinelibrary.wiley.com/doi/pdf/10.1002/mrm.xxxxx \
    --license 'wiley-tdm-internal-use-only' \
    --note 'via uni-wuerzburg library proxy 2026-05-09'
✓ stored: artifacts/pdf/sha256/9a/3f/9a3f….pdf  (manual)

$ litspectraits resolve 10.1002/mrm.xxxxx
Probes:
  local         ✓  published pdf  open
  unpaywall     ✓  preprint pdf  open
  ...
Chosen: local (published pdf, manual)
```

## Storage Layout

```
<data_dir>/
├── artifacts/
│   ├── jats/sha256/<aa>/<bb>/<aabb…>.xml
│   ├── latex/sha256/<aa>/<bb>/<aabb…>.tar.gz
│   └── pdf/sha256/<aa>/<bb>/<aabb…>.pdf
├── manifests/
│   └── sha256/<aa>/<bb>/<aabb…>.manifest.json
├── index/
│   └── by_doi.jsonl              # append-only — small DOI → sha256 index
└── tmp/                          # download staging, cleared on startup
```

Two-level prefix sharding (matches `overview.md`). The `manifests/` mirror
keeps manifest reads cheap and avoids walking the artifact tree.

`by_doi.jsonl` is an append-log mapping DOIs to acquisition records, so the
`local` probe can answer `find_by_doi(doi)` in O(reads-since-last-compaction)
without scanning the artifact tree. A simple compaction step on startup
deduplicates lines. This is intentionally not a database — Postgres comes in
step 5.

`data_dir` defaults to `~/.local/share/litspectraits` via `platformdirs`,
overridable via `LITSPECTRAITS_DATA_DIR`.

## Logging

`structlog` with two configurations switched by env
(`LITSPECTRAITS_LOG_FORMAT=rich|json`, default `rich`):

- **Rich**: `structlog.dev.ConsoleRenderer(colors=True)` over a
  `rich.console.Console` writing to stderr; exceptions through
  `RichTracebackFormatter`.
- **JSON**: `structlog.processors.JSONRenderer()` for batch / server runs.

Standard processors: `add_log_level`, `TimeStamper(fmt='iso')`,
`contextvars.merge_contextvars`, `StackInfoRenderer`. Loggers are namespaced
(`litspectraits.resolver.pmc`, `litspectraits.acquisition.fetch`, …).

DOI is bound once per resolve via `contextvars` so every line in that resolve
carries it:

```python
log = structlog.get_logger('litspectraits.resolver').bind(doi=doi)
log.info('resolve.start', policy='published_first')
log.debug('probe.skip', probe='biorxiv', reason='not_a_preprint')
log.info('probe.hit', probe='pmc', license='cc-by', pmcid='PMC1234567')
log.info('resolve.done', chosen='jats_pmc', candidates=2, duration_ms=842)
```

Standard library `logging` is routed into structlog so `httpx`/`urllib3` join
the same stream.

## CLI

Three commands, all rendered via Rich:

```
litspectraits resolve  <doi> [--policy ...] [--json]
litspectraits ingest   <doi> [--policy ...] [--force] [--json]
litspectraits sideload <doi> <path> --version --format --license [--source-url] [--note]
```

`resolve` — runs the resolver only. Prints a `rich.table.Table` with one row
per candidate: probe, version, format, access, status (`✓`/`⚠`/`✗`/`—`),
url-or-reason, ms. Closes with a highlighted `chosen` panel. `--json` swaps
the human view for `cattrs.unstructure(result)` printed to stdout for piping.

`ingest` — full pipeline (resolve + acquire). The fetch is wrapped in
`rich.progress.Progress` with `DownloadColumn` + `TransferSpeedColumn`,
sharing the same `Console` instance the structlog renderer uses so log lines
and the progress bar coexist. Final summary panel lists sha256, artifact
path, license, origin.

`sideload` — see manual-sideload section above. Final summary panel matches
`ingest`'s output shape, so operator confirmation is uniform.

`Console(stderr=True)` for everything diagnostic; stdout stays clean for
`--json`.

## Config

Single `attrs` settings object loaded from env (no pydantic). `python-dotenv`
loads `.env` once at CLI entry and at pytest session start; library code
remains env-only.

| Var                                | Purpose                                                  | Default                                |
| ---------------------------------- | -------------------------------------------------------- | -------------------------------------- |
| `LITSPECTRAITS_DATA_DIR`           | root for `artifacts/`, `manifests/`, `tmp/`, `index/`    | `platformdirs.user_data_dir(...)`      |
| `LITSPECTRAITS_CONTACT_EMAIL`      | mailto for CrossRef polite pool, Unpaywall, manifests    | **required** — fail loudly on missing  |
| `LITSPECTRAITS_CROSSREF_TDM_TOKEN` | optional Crossref Click-Through token                    | unset → TDM probe records auth-blocked |
| `LITSPECTRAITS_HTTP_TIMEOUT_S`     | per-request timeout                                      | `30`                                   |
| `LITSPECTRAITS_LOG_FORMAT`         | `rich` \| `json`                                         | `rich`                                 |

`LITSPECTRAITS_CONTACT_EMAIL` is required because all polite-pool APIs want
it and unauthenticated traffic gets rate-limited or banned. Failing loudly at
startup is better than silently downgrading.

## Failure Semantics

Aligned with `project-infra-overview.md` "no excessive exception catching and
silent fallback":

- **Unknown DOI / CrossRef 404** → raise `DOINotFound`. Don't keep probing —
  every downstream probe will also fail.
- **All probes returned no `Availability`** → `ResolveResult` with
  `chosen=None`. CLI exits non-zero with the audit table; library callers
  decide.
- **All `Availability`s excluded by policy** (e.g. only TDM-gated hits, no
  token) → `chosen=None`, but `excluded` is populated so the operator sees
  what they could unlock.
- **Single-probe network error** → `ProbeOutcome.error` populated, that probe
  fails-soft, resolver continues. One probe's failure does not dictate
  others.
- **Hash mismatch on download retry** → raise `IntegrityError`; never
  silently overwrite.
- **Existing artifact for same `(doi, sha256)`** → return existing record,
  log `acquire.cache_hit`.
- **`sideload` of a file whose magic bytes don't match `--format`** → refuse,
  raise `MalformedArtifact`.

## Code Layout

```
src/litspectraits/
├── __init__.py
├── _logging.py              # structlog + rich configuration
├── cli.py                   # Typer app: resolve / ingest / sideload
├── config.py                # env-driven Settings (attrs)
├── http.py                  # shared httpx.AsyncClient factory
├── doi.py                   # DOI normalization
├── resolver/
│   ├── __init__.py
│   ├── types.py             # axes, Availability, RankingPolicy, ResolveResult
│   ├── policy.py            # PUBLISHED_FIRST, FIDELITY_FIRST presets
│   ├── resolver.py          # resolve() — orchestrates probes, applies policy
│   └── probes/
│       ├── __init__.py
│       ├── base.py          # Probe protocol, ProbeContext
│       ├── crossref.py      # shared CrossRef metadata client
│       ├── local.py         # local-store probe
│       ├── pmc.py
│       ├── europepmc.py
│       ├── biorxiv.py
│       ├── arxiv.py
│       ├── unpaywall.py
│       └── crossref_tdm.py
└── acquisition/
    ├── __init__.py
    ├── store.py             # content-addressed storage + DOI index
    ├── fetch.py             # streamed download + hash-on-the-fly
    ├── sideload.py          # manual artifact registration
    └── manifest.py          # AcquisitionRecord (de)serialization

tests/
├── conftest.py
├── resolver/
│   ├── test_pmc_probe.py
│   ├── test_europepmc_probe.py
│   ├── test_arxiv_probe.py
│   ├── test_unpaywall_probe.py
│   ├── test_local_probe.py
│   ├── test_policy_published_first.py
│   ├── test_policy_fidelity_first.py
│   └── test_resolver_auth_blocked.py
├── acquisition/
│   ├── test_store_layout.py
│   ├── test_fetch_hashing.py
│   └── test_sideload.py
└── data/cassettes/                  # respx fixtures for offline CI
```

## Dependencies (`pyproject.toml`)

Runtime:

- `httpx[http2]` — single async HTTP client across probes
- `structlog` — structured logging
- `rich` — terminal rendering (`Console`, `Progress`, `RichTracebackFormatter`)
- `attrs` — `@frozen` data models (matches `overview.md`)
- `cattrs` — serialization
- `tenacity` — retry/backoff for HTTP
- `typer` — CLI (Rich-native)
- `platformdirs` — user-cache fallback for `LITSPECTRAITS_DATA_DIR`
- `python-dotenv` — `.env` loading at CLI entry / pytest session

Dev:

- `ruff`, `pyright` (matches `project-infra-overview.md`)
- `pytest`, `pytest-asyncio`
- `respx` — httpx mocking
- `pytest-recording` — for live-mode cassette refresh

## Testing

- `respx` cassettes for each probe, recorded once against a small fixture set:
  one OA PMC paper, one bioRxiv preprint, one arXiv physics paper, one
  paywalled Wiley DOI, one OA paper present on multiple sources.
- `test_policy_*` — fixtures where multiple sources return positives, asserts
  the policy picks the right one. Both presets covered.
- `test_resolver_auth_blocked.py` — Wiley TDM XML detected, no token in env;
  asserts `chosen` is the next fetchable tier and the TDM entry is in
  `excluded`.
- `test_local_probe.py` — pre-populates the store, asserts the probe surfaces
  the artifact and policy promotes it correctly.
- `test_sideload.py` — magic-byte validation, atomic copy, manifest contents,
  idempotency on repeat sideload.
- No live network in CI. A `--live` pytest mark allows ad-hoc runs against
  real services for cassette refresh.

## Order of Work

Each step ends green on `pytest`, `ruff check`, and `pyright`. Nothing
committed that doesn't pass all three.

1. `pyproject.toml` deps + `_logging.py` + `config.py` + `http.py` + `doi.py`
   (foundations; one tight commit).
2. `resolver/types.py` + `resolver/policy.py` + Probe protocol + CrossRef
   metadata client. No probes yet.
3. PMC OA probe end-to-end with respx test (proves the probe pattern).
4. Remaining network probes in priority order: Europe PMC → bioRxiv → arXiv →
   Unpaywall → Crossref TDM.
5. `acquisition/store.py` + `acquisition/fetch.py` + `acquisition/manifest.py`
   + DOI index.
6. `local` probe (depends on the store from step 5).
7. `resolver/resolver.py` orchestrator + policy tests + auth-blocked test.
8. `acquisition/sideload.py` + `litspectraits sideload` CLI command.
9. CLI `resolve` and `ingest` with Rich rendering.
10. README quickstart with one OA DOI worked example.

## Deferred (Explicit Punts)

Listed so future-us doesn't mistake them for missing requirements:

- **Failed-acquisition memoization** — caching "we tried this DOI 7 days ago,
  got a 403, don't try again" is a Postgres / step-5 concern. Adding it to
  the file store now risks a half-baked caching layer.
- **Cloudflare R2 mirroring** — local FS only for step 1, with a clean
  filesystem boundary in `store.py` so an R2 backend can be slotted in later.
- **TOML policy config** — three named CLI presets cover ~all cases.
  YAGNI until someone needs a fully custom policy persistently.
- **Figure-pixel extraction** — out of scope per `overview.md` non-goals.
- **Cross-corpus embeddings / semantic search** — out of scope per
  `overview.md` non-goals.
