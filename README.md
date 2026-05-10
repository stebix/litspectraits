# litspectraits

A pipeline that ingests MRI literature by DOI and produces structured,
provenance-tracked records of quantitative MR / electromagnetic property values
(T1, T2, T2\*, PD, χ, ADC, …) along with the context needed to interpret them
(field strength, sequence, tissue, scanner, in vivo / ex vivo / phantom, …).

Every record cites back to a specific block — ideally a sentence or table cell —
in a source paper, and citation chains are followed so duplicates collapse to
their primary source.

## Status

The ingestion stage is being rebuilt under the **v3 design**
(`docs/overview-v3.md`). The v1 resolver / acquisition / ranking-policy stack
has been deleted; the surviving CLI commands are `ingest`, `doctor`, and
`show`. `extract` and `sideload` from the §10 surface land alongside their
backends and are intentionally absent until then — Typer reports
"unknown command" rather than shipping NotImplementedError stubs.

v3 covers exactly three publishers:

- **Wiley** via the `wiley-tdm` SDK → PDF
- **Springer Nature** via `springernature-api-client` → JATS XML
- **Elsevier** via raw httpx + lxml (`view=FULL`) → Elsevier-flavored XML

Anything else fails with `UnsupportedPublisherError`. Extraction, the
Postgres index, and the agent triad remain on the roadmap — see
`docs/overview.md`.

## Install

```sh
uv sync
```

This installs runtime + dev dependencies into `.venv` (Python 3.14). Two
optional extras pull in the publisher SDKs:

```sh
uv sync --extra wiley --extra springer
# or, once extraction lands:
uv sync --extra all
```

Elsevier has no extra — the v3 retriever talks to ScienceDirect with raw
`httpx` + `lxml` (the reference SDK `elsapy` was archived 2025-01-13).

## Configuration

The CLI calls `dotenv.load_dotenv()` once at startup (in `cli.py`'s root
callback). With no path argument, `python-dotenv` walks up from the
caller's directory (`src/litspectraits/`) toward the filesystem root and
loads the first `.env` it finds. **The expected location is the project
root**: `<repo>/.env`. Variables already present in the process
environment are never overridden.

Add `.env` to your `.gitignore` (or use `.env.local`) before putting
publisher tokens in it — credentials must not be committed.

Only `LITSPECTRAITS_CONTACT_EMAIL` fails at startup. Publisher
credentials are checked lazily by the corresponding retriever and raise
`MissingCredentialError` only when actually needed.

| Variable                                | Purpose                                                                  | Default                            |
| --------------------------------------- | ------------------------------------------------------------------------ | ---------------------------------- |
| `LITSPECTRAITS_CONTACT_EMAIL`           | Polite-pool mailto for CrossRef + sideload operator                      | **required**                       |
| `LITSPECTRAITS_DATA_DIR`                | Root for `artifacts/`, `manifests/`, `index/`, `tmp/`                    | `~/.local/share/litspectraits`     |
| `LITSPECTRAITS_HTTP_TIMEOUT_S`          | Per-request timeout for first-party HTTP (CrossRef, Elsevier, doctor)    | `30`                               |
| `LITSPECTRAITS_LOG_FORMAT`              | `rich` (human) or `json` (aggregation)                                   | `rich`                             |
| `LITSPECTRAITS_RATE_LIMIT_WILEY`        | Wiley retriever ceiling (req/s)                                          | `3`                                |
| `LITSPECTRAITS_RATE_LIMIT_SPRINGER`     | Springer Nature retriever ceiling (req/s)                                | `5`                                |
| `LITSPECTRAITS_RATE_LIMIT_ELSEVIER`     | Elsevier retriever ceiling (req/s)                                       | `6`                                |
| `LITSPECTRAITS_EXPECTED_EGRESS_CIDRS`   | Comma-separated allow-list checked by `doctor`                           | empty (warn-only)                  |
| `WILEY_TDM_TOKEN`                       | Wiley TDM token, forwarded into `wiley-tdm` as `TDM_API_TOKEN`           | unset → IP-based auth              |
| `SPRINGER_API_KEY`                      | Springer Nature TDM API key (no IP fallback)                             | unset → retriever raises           |
| `ELSEVIER_API_KEY`                      | ScienceDirect API key, sent as `X-ELS-APIKey`                            | unset → retriever raises           |
| `ELSEVIER_INSTTOKEN`                    | Elsevier institutional token, sent as `X-ELS-Insttoken`                  | unset → OA-tier titles only        |

A minimal `.env` to get going (CrossRef-only — every actual ingest will
fail at the retriever step until publisher credentials are added):

```dotenv
LITSPECTRAITS_CONTACT_EMAIL=you@example.com
```

## CLI

### `ingest` — fetch, validate, and commit a single DOI

Resolves CrossRef metadata, dispatches on DOI prefix, calls the
publisher-specific TDM retriever, validates the response by magic bytes,
and atomically commits the artifact + manifest. Fails loudly with a
typed `IngestError` and a deterministic exit code (see
`docs/overview-v3.md` §14) if any step rejects.

```sh
uv run litspectraits ingest 10.1002/mrm.xxxxx
```

By default `ingest` **refetches** even when a manifest already exists.
Pass `--cache-hit-ok` for the opt-in short-circuit:

```sh
uv run litspectraits ingest 10.1002/mrm.xxxxx --cache-hit-ok
```

JSON output for programmatic consumers (stdout stays clean — diagnostics
and error panels go to stderr):

```sh
uv run litspectraits ingest 10.1002/mrm.xxxxx --json | jq .sha256
```

### `sideload` — register an operator-retrieved PDF

For DOIs where TDM access is unavailable (typical case: a Wiley title
the operator pulls via the library proxy), drop a PDF you fetched
out-of-band into the same content-addressed store. PDF-only.

```sh
uv run litspectraits sideload 10.1002/mrm.xxxxx ~/Downloads/paper.pdf \
    --license 'wiley-tdm-internal-use-only' \
    --source-url 'https://onlinelibrary.wiley.com/doi/pdf/10.1002/mrm.xxxxx' \
    --note 'via uni-wuerzburg library proxy'
```

`--license` is mandatory — it's the legal trail for proxies that
permit the fetch but forbid redistribution. The operator email
(from `LITSPECTRAITS_CONTACT_EMAIL`) and timestamp are written into
the manifest's `manual_provenance` block.

Idempotent on `(doi, sha256)`: re-running with the same bytes
returns the existing record without re-writing the manifest or
duplicating the index entry. Sideload also fetches CrossRef metadata
so the manifest shape stays uniform with auto-ingested records.

### `doctor` — preflight credentials + egress

Run before any batch ingest. Verifies the contact email is set, probes
each configured publisher credential with a minimal authenticated
request, and reports the egress IP against
`LITSPECTRAITS_EXPECTED_EGRESS_CIDRS` if set.

```sh
uv run litspectraits doctor
```

### `show` — look up an existing record

Reads the local DOI index and prints the manifest for a DOI that has
already been ingested. Does not touch the network.

```sh
uv run litspectraits show 10.1002/mrm.xxxxx
uv run litspectraits show 10.1002/mrm.xxxxx --json
```

### Storage layout

```
<data_dir>/artifacts/pdf/sha256/9a/9a3f….pdf
<data_dir>/artifacts/jats/sha256/9a/9a3f….xml
<data_dir>/artifacts/elsevier/sha256/9a/9a3f….xml
<data_dir>/manifests/sha256/9a/9a3f….manifest.json
<data_dir>/index/by_doi.jsonl
```

One-level sharding (first two hex chars of the sha256). The DOI index
carries a `format` column so a single scan answers "what do we have for
this DOI?".

## Library usage

The CLI is a thin shell over `litspectraits.ingest.ingest`:

```python
import asyncio

from litspectraits.config import Settings
from litspectraits.http import http_client
from litspectraits.ingest import ingest
from litspectraits.store import ArtifactStore


async def main() -> None:
    settings = Settings.from_env()
    store = ArtifactStore(settings.data_dir)
    async with http_client(settings) as client:
        record = await ingest(
            '10.1002/mrm.xxxxx',
            settings=settings,
            store=store,
            client=client,
            cache_hit_ok=False,
        )
        print(record.sha256, record.format, record.artifact_path)


asyncio.run(main())
```

Every failure is a typed `IngestError` subclass from
`litspectraits.errors` — `DOINotFoundError`, `UnsupportedPublisherError`,
`MissingCredentialError`, `AuthRejectedError`, `EntitlementDowngradeError`,
`RateLimitExhaustedError`, `PublisherAPIError`, `MalformedArtifactError`,
`IntegrityError`. Subclass identity is the contract; the CLI maps it to
exit codes.

## Development

```sh
uv run ruff check src/ tests/        # lint
uv run ruff format src/ tests/       # format
uv run pyright src/ tests/           # type check
uv run pytest                        # offline (respx-mocked + SDK monkeypatched)
```

## Documentation

- `docs/overview.md` — full pipeline design (ingestion → extraction →
  measurements → agent triad). Unchanged across design revisions.
- `docs/overview-v3.md` — **authoritative** design for the ingestion
  stage. Section 17 is the intended order of work.
- `docs/project-infra-overview.md` — code-style and infra conventions.

## Downstream

A separate package, `tissue-properties`, is the planned consumer of the
records produced by this pipeline.
