# litspectraits

A pipeline that ingests MRI literature by DOI and produces structured,
provenance-tracked records of quantitative MR / electromagnetic property values
(T1, T2, T2\*, PD, χ, ADC, …) along with the context needed to interpret them
(field strength, sequence, tissue, scanner, in vivo / ex vivo / phantom, …).

Every record cites back to a specific block — ideally a sentence or table cell —
in a source paper, and citation chains are followed so duplicates collapse to
their primary source.

## Status

Step 1 — **DOI → best-available source resolver + content-addressed
acquisition store** — has landed. The resolver picks across JATS XML
(PMC OA / Europe PMC / bioRxiv), arXiv LaTeX source, OA PDFs (Unpaywall),
and publisher TDM endpoints under a configurable `RankingPolicy`, with
manual operator-sideloads as a first-class path for paywalled artifacts.

Extraction, reference resolution, the Postgres index, and the agent triad
remain on the roadmap — see `docs/overview.md`.

## Install

```sh
uv sync
```

This installs runtime + dev dependencies into `.venv` (Python 3.14).

## Configuration

`.env` (or process environment) is read once at CLI entry. The contact
email is mandatory — it goes into the polite-pool `User-Agent` for
CrossRef and Unpaywall, and is recorded as the operator on manual
sideloads.

| Variable                           | Purpose                                            | Default                            |
| ---------------------------------- | -------------------------------------------------- | ---------------------------------- |
| `LITSPECTRAITS_CONTACT_EMAIL`      | Polite-pool mailto + sideload operator             | **required**                       |
| `LITSPECTRAITS_DATA_DIR`           | Root for `artifacts/`, `manifests/`, `index/`      | `~/.local/share/litspectraits`     |
| `LITSPECTRAITS_CROSSREF_TDM_TOKEN` | Crossref Click-Through token (optional)            | unset → TDM hits stay auth-blocked |
| `LITSPECTRAITS_HTTP_TIMEOUT_S`     | Per-request timeout in seconds                     | `30`                               |
| `LITSPECTRAITS_LOG_FORMAT`         | `rich` (human) or `json` (aggregation)             | `rich`                             |

A minimal `.env` to get going:

```dotenv
LITSPECTRAITS_CONTACT_EMAIL=you@example.com
```

## CLI

### `resolve` — DOI → ranked acquisition candidates

Probes every supported source in parallel and prints what it found,
ranked under the active policy. Does not download anything.

```sh
uv run litspectraits resolve 10.1101/2023.05.01.539123
```

Pipe-friendly JSON for programmatic use:

```sh
uv run litspectraits resolve 10.1101/2023.05.01.539123 --json | jq .chosen
```

Switch policies to invert the format-vs-version trade-off:

```sh
uv run litspectraits resolve <doi> --policy fidelity_first
```

### `ingest` — resolve, then fetch the chosen artifact

Streams the artifact, hashes it on the fly, and stores it
content-addressed under `LITSPECTRAITS_DATA_DIR`. Idempotent: a second
call with the same DOI returns the cached record instead of
re-downloading.

```sh
uv run litspectraits ingest 10.1101/2023.05.01.539123
```

The on-disk layout per artifact:

```
<data_dir>/artifacts/pdf/sha256/9a/3f/9a3f….pdf
<data_dir>/manifests/sha256/9a/3f/9a3f….manifest.json
<data_dir>/index/by_doi.jsonl
```

### `sideload` — register a manually-retrieved artifact

For papers reachable only through institutional access (publisher PDF via
library proxy, etc.), drop a file you fetched out-of-band into the same
content-addressed store. Future `resolve` calls will find it via the
local probe and rank it normally — a manual `(published, pdf)`
automatically beats an Unpaywall `(preprint, pdf)` under the default
policy.

```sh
uv run litspectraits sideload 10.1002/mrm.xxxxx ~/Downloads/paper.pdf \
    --version published \
    --format pdf \
    --license 'wiley-tdm-internal-use-only' \
    --source-url 'https://onlinelibrary.wiley.com/doi/pdf/10.1002/mrm.xxxxx' \
    --note 'via uni-wuerzburg library proxy'
```

The operator email and the timestamp are pulled from the environment and
recorded in the manifest's `manual_provenance` field — that is the
durable legal trail for the artifact.

## Library usage

The CLI is a thin shell over the library entry points:

```python
import asyncio
from litspectraits.acquisition.store import ArtifactStore
from litspectraits.config import Settings
from litspectraits.resolver.policy import PUBLISHED_FIRST
from litspectraits.resolver.resolver import resolve

async def main() -> None:
    settings = Settings.from_env()
    store = ArtifactStore(settings.data_dir)
    result = await resolve(
        '10.1101/2023.05.01.539123',
        settings=settings,
        store=store,
        policy=PUBLISHED_FIRST,
    )
    print(result.chosen)
    for outcome in result.probes:
        print(outcome.probe, len(outcome.availabilities), outcome.error)

asyncio.run(main())
```

`ResolveResult` carries the full audit trail (every probe outcome, every
candidate, exclusion reasons) — useful for batch-pipelining and
debugging.

## Ranking policy

Sources are tagged on three independent axes — `Version`
(published / accepted_manuscript / preprint), `Format` (jats / latex /
pdf), and `Access` (open / tdm_token / subscription) — and ranked
lexicographically by a `RankingPolicy`. Two presets ship:

- **`published_first`** (default): version-of-record beats preprint
  regardless of format. A published PDF outranks a preprint LaTeX.
- **`fidelity_first`**: format wins over version. A preprint JATS
  outranks a published PDF.

When a high-ranked source exists but the environment lacks credentials
to fetch it (e.g. a Wiley TDM JATS detected without a token configured),
the entry stays in `candidates` for the audit trail but is never
selected — the resolver falls through to the next fetchable tier.

## Development

```sh
uv run ruff check src/ tests/        # lint
uv run ruff format src/ tests/       # format
uv run pyright src/ tests/           # type check
uv run pytest                        # 38 tests, fully offline (respx-mocked)
```

## Documentation

- `docs/overview.md` — full pipeline design (resolver → acquisition →
  extraction → measurements → agent triad).
- `docs/ingestion-pipeline.md` — detailed design for step 1 (this
  package).
- `docs/project-infra-overview.md` — code-style and infra conventions.

## Downstream

A separate package, `tissue-properties`, is the planned consumer of the
records produced by this pipeline.
