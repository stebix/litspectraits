# Manual spot-check — ingest + extract

Operator runbook for exercising the v3 pipeline by hand against a real
publisher and eyeballing what lands on disk. The worked example is a
**full Wiley PDF run** (DOI → CrossRef → Wiley TDM → magic-byte validate
→ atomic commit → docling extract); the Springer/Elsevier variants
follow the same shape with a different DOI and credential.

This is not an automated test — it hits the network and uses your
publisher credentials. It is the thing you do before trusting a batch
run, or after a dependency bump, to confirm the happy path still
produces sane artifacts and *visually* checks the bytes (open the PDF,
eyeball the extracted prose). The gated end-to-end smoke suite (`§22` of
`overview-v3.md`, Step 12 — `uv run pytest -m smoke`) is the automated
counterpart: it covers the ingest half of this runbook with structured
invariant assertions, but does not extract or eyeball anything. Run both
— the suite for a fast regression signal, this runbook when you want to
look at what landed.

Subordinate to `overview-v3.md`; section refs of the form `§N` point
there. Last revised against the codebase state at commit `1704bf2`
(2026-05-11), with the §22 smoke suite (Step 12) landed shortly after:
Steps 0–10 + 12 landed, Step 11 (`ingest --batch`) not started.

## 0. Setup — isolate the run in a scratch data dir

Don't pollute the platformdirs default
(`~/.local/share/litspectraits`). Point `LITSPECTRAITS_DATA_DIR` at a
throwaway directory so cleanup is one `rm -rf`:

```bash
export LITSPECTRAITS_DATA_DIR="$PWD/.scratch-data"
export LITSPECTRAITS_LOG_FORMAT=rich   # human-readable structlog; use json to inspect the raw event stream
```

`LITSPECTRAITS_CONTACT_EMAIL` and `WILEY_TDM_TOKEN` come from `.env`,
which the CLI loads on startup (`dotenv` in `cli.py:_startup`). Confirm
the Wiley SDK extra is installed and you're invoking the console script:

```bash
uv run python -c "import wiley_tdm"   # must not raise — the [wiley] extra
uv run litspectraits --help
```

## 1. Preflight — `doctor`

```bash
uv run litspectraits doctor
```

Inspect:

- **IP / egress table** — Wiley TDM is partly IP-gated on top of the
  token; relevant if you're not on the Würzburg egress allow-list.
- **Wiley credential row** — token present and shaped right.

Plain `doctor` is network-free for the extract section. To also confirm
docling is ready for step 4: `uv run litspectraits doctor
--smoke-extract` (first run downloads layout + TableFormer weights
unless cached; `--download-models` forces it).

## 2. Pick the DOI

Use the one already wired into the codebase:
**`10.1002/advs.202002917`** (Advanced Science, OA — `_smoke_dois.py`).
It is still flagged `# TODO: verify` there, so if it 404s or
`AuthRejectedError`s, suspect the DOI before the code. If you have a
specific paywalled-but-entitled Wiley DOI, use that instead to exercise
the IP-entitlement path on top of the token — expect `AuthRejectedError`
(exit 1) if your egress isn't entitled.

## 3. Ingest

```bash
uv run litspectraits ingest 10.1002/advs.202002917
# or capture the manifest:
uv run litspectraits ingest 10.1002/advs.202002917 --json | tee /tmp/ingest.json
```

In the structlog stream you should see: `metadata fetched` → publisher
dispatch (`wiley`; a `publisher mismatch` warning is observability-only,
routing is unaffected) → the `wiley-tdm` SDK download → magic-byte
verify → `ingest committed`. The final panel / JSON carries `sha256`,
`byte_size`, `format=pdf`, `publisher=wiley`, `artifact_path`.

Negative spot-checks worth running here:

| Command | Expected |
| --- | --- |
| `litspectraits ingest not-a-doi` | exit **2**, Rich `InvalidDOIError` panel |
| `litspectraits ingest 10.1016/j.heliyon.2024.e26000` | routes to the **Elsevier** retriever (proves DOI-prefix dispatch), then `MissingCredentialError` on absent `ELSEVIER_API_KEY` — confirms no Wiley fall-through |
| re-run the same Wiley DOI | refetches by default; identical bytes ⇒ idempotent commit, same sha, **no duplicate index line** |
| re-run with `--cache-hit-ok` | short-circuits without touching CrossRef or Wiley, returns the existing manifest |

## 4. Inspect the artifact on disk

```bash
tree .scratch-data
# artifacts/pdf/sha256/<aa>/<sha>.pdf
# manifests/sha256/<aa>/<sha>.manifest.json
# documents/                       # empty until step 5
# index/by_doi.jsonl
# tmp/                             # empty (cleared on every store init)

SHA=<sha-from-step-3>
file    .scratch-data/artifacts/pdf/sha256/${SHA:0:2}/$SHA.pdf       # → "PDF document, version 1.x"
xxd     .scratch-data/artifacts/pdf/sha256/${SHA:0:2}/$SHA.pdf | head -1   # %PDF-1.
sha256sum .scratch-data/artifacts/pdf/sha256/${SHA:0:2}/$SHA.pdf     # must equal $SHA and the manifest field
cat     .scratch-data/index/by_doi.jsonl                            # one line: doi, sha256, format=pdf, byte_size, retrieved_at
jq .    .scratch-data/manifests/sha256/${SHA:0:2}/$SHA.manifest.json
```

In the manifest, eyeball: `doi`, `sha256`, `byte_size`,
`format: "pdf"`, `publisher: "wiley"`, the CrossRef metadata subtree,
`retrieved_at`, the recorded `wiley-tdm` SDK version (note: in the
current env `importlib.metadata.version('wiley-tdm')` reports `0.1.0`
even though the package is meant to be 1.0 — flag it if you see it).

Then **open the PDF in a viewer** — confirm it's the real article, not
paywall HTML wearing a `.pdf` suffix. The magic-byte sniffer
(`sniff.verify`, run twice: once inside the retriever as defence in
depth, once at the orchestrator boundary) should already have rejected
that, but this is the "manual inspect" part.

## 5. Extract — run docling on the PDF

```bash
uv run litspectraits extract 10.1002/advs.202002917
# also accepted: a bare sha → litspectraits extract $SHA
# or: litspectraits extract 10.1002/advs.202002917 --json | jq .
```

The heavy step — docling layout + TableFormer. Runs on CUDA when a
usable GPU is present (the `extract` extra pins the `+cu126` torch wheel
so it works on a CUDA-12.4 driver — see `pyproject.toml`'s
`[tool.uv.sources]`); falls back to CPU otherwise, just slower. `doctor`
needs `--download-models` once before the first real extract — the
layout + TableFormer weights are not bundled. Inspect:

```bash
jq '{schema, n_pages, counts}' .scratch-data/documents/$SHA/meta.json
jq '.pipeline'                  .scratch-data/documents/$SHA/meta.json   # resolved device (cpu/cuda/mps), docling version
jq 'keys'                       .scratch-data/documents/$SHA/document.json
jq '.texts[:5]'                 .scratch-data/documents/$SHA/document.json   # first blocks — did the prose come through?
jq '.tables | length'           .scratch-data/documents/$SHA/document.json
```

Then:

- `--reextract` → clean overwrite of both files.
- plain re-`extract` → idempotent no-op when bytes are identical;
  divergence without `--reextract` ⇒ `ExtractIntegrityError`, exit 7.

## 6. `show`

```bash
uv run litspectraits show 10.1002/advs.202002917
uv run litspectraits show 10.1002/advs.202002917 --json | jq .
uv run litspectraits show 10.9999/nope.000 ; echo "exit=$?"   # → exit 1, "not in local store"
```

## 7. Cleanup

```bash
rm -rf .scratch-data
unset LITSPECTRAITS_DATA_DIR LITSPECTRAITS_LOG_FORMAT
```

## Springer / Elsevier variants

Same procedure, different DOI + credential (`_smoke_dois.py`):

- **Springer Nature** — `10.1007/s10334-026-01362-7`; needs **one of**
  two keys (no IP fallback):
  - `SPRINGER_OA_API_KEY` — standard dev-portal key from
    `dev.springernature.com`, free. Routes through the Open Access API
    (`api.springernature.com/openaccess/jats`) and works for OA DOIs
    only. A real Springer DOI that isn't OA fails loudly with
    `NotOpenAccessError` (exit 8).
  - `SPRINGER_TDM_API_KEY` — premium Full-Text / TDM licence from
    `datasolutions.springernature.com`. Routes through the TDM endpoint
    (`spdi.public.springernature.app/xmldata/jats`) and covers the full
    Springer Nature corpus (OA + subscription). Wins over
    `SPRINGER_OA_API_KEY` when both are set.

  Either way the artifact lands under `artifacts/jats/…`, `format=jats`;
  extract via `extract/jats.py`. The smoke DOI above is OA and reachable
  from both tiers.
- **Elsevier** — `10.1016/j.heliyon.2024.e26000`; needs
  `ELSEVIER_API_KEY` (`ELSEVIER_INSTTOKEN` optional, OA-only without
  it). Artifact under `artifacts/elsevier/…`, `format=elsevier`;
  extract via `extract/elsevier.py`. The retriever always sends
  `view=FULL` and raises `EntitlementDowngradeError` on a `META_ABS`
  payload — that error means the title isn't entitled, not a bug.

## Known gotchas

- The `_smoke_dois.py` DOIs are all `# TODO: verify`. A publisher-side
  failure (`DOINotFoundError`, `AuthRejectedError`, `PublisherAPIError`)
  on step 3 ⇒ suspect the DOI first; refresh the constant, don't fall
  back to a different one.
- `wiley-tdm` enforces a 5 s floor between requests internally, so the
  3 req/s rate-limit ceiling is moot for Wiley — a single ingest just
  feels paced.
- `tmp/` is wiped on every `ArtifactStore` construction. A failed
  ingest leaves staged bytes there; the next CLI invocation clears
  them. Nothing is materialized under `artifacts/` / `manifests/` /
  `index/` on failure.
