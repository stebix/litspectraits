# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Repository state

The ingestion stage is being rebuilt from scratch under the **v3 design**. The old v1/v2 acquisition + resolver packages and their tests have been deleted; the surviving Python is `_logging.py`, `config.py`, `doi.py`, `http.py`, plus a `cli.py` and `tests/conftest.py` that still import the deleted modules and need rewriting before anything runs. The substance lives in `docs/`:

- `docs/overview.md` — whole-project goals (DOI → ingestion → extraction → measurement records → agent triad). Unchanged across design revisions; read this first to understand what is being built end-to-end.
- `docs/overview-v3.md` — **authoritative** design for the ingestion stage. Quasi-linear `DOI → CrossRef metadata → publisher dispatch → credentialed retrieve → magic-byte validate → atomic commit`, with loud failure at every step. Section 17 ("Order of work") is the intended implementation sequence.
- `docs/project-infra-overview.md` — code-style and infra conventions.

When adding code, follow `docs/overview-v3.md`. v1's `ingestion-pipeline.md`, v2's `overview-v2.md`, and the `publisher-routes.md` intel doc are gone — their content is folded into v3.

## What this project is

**litspectraits** ingests MRI literature by DOI and produces structured, provenance-tracked records of quantitative MR / electromagnetic property values (T1, T2, T2*, PD, χ, ADC, …) along with the context needed to interpret them (field strength, sequence, tissue, scanner, in vivo / ex vivo / phantom, etc.). Outputs are a queryable database where every record cites back to a specific block (ideally sentence or table cell) in a source paper, and citation chains are followed so duplicates collapse to their primary source. A downstream package called `tissue-properties` is a planned consumer.

## Tooling and conventions (from `docs/project-infra-overview.md`)

- **uv-based project**, Python 3.14 (`.python-version`). Always run Python through the local uv-managed `.venv` (`uv run python …`, `uv run pytest`, `uv sync` to install).
- **Ruff** for both lint and format. **Pyright** for static type checking. Each commit in v3's order-of-work should be green on `pytest`, `ruff check`, and `pyright`.
- **Line length 99.**
- **Single quotes preferred** for strings; escape inside nested sequences rather than swapping styles.
- **Numpy-style docstrings.**
- **Type hints almost everywhere.**
- **Do not** use `from __future__ import annotations`.
- **Fail loudly.** No excessive exception catching, no silent fallbacks. v3's failure model is the canonical example: every step in the ingest path raises a typed `IngestError` subclass before anything is written to disk; the magic-byte sniffer rejects paywall-HTML-as-PDF; missing required env (`LITSPECTRAITS_CONTACT_EMAIL`) fails at startup.

## Architectural commitments worth knowing before editing

These decisions are spread across the design docs and won't be obvious from any single file once code lands.

- **One quasi-linear happy path per ingest.** v3 retrieves exactly one artifact per DOI: CrossRef metadata → DOI-prefix dispatch → publisher-specific TDM retriever → magic-byte validate → atomic commit. No fall-through across publishers, no Unpaywall mirror in the happy path, no candidate-ranking, no `--policy` flag. Each failure mode has its own `IngestError` subclass and exit code; nothing is materialized on failure.
- **Three publishers, three formats.** v3 covers `{wiley, elsevier, springer_nature}` exclusively. Wiley TDM returns PDF; Springer Nature TDM returns JATS XML; Elsevier returns Elsevier-flavored XML (`view=FULL`). Format dispatch on extraction reads the manifest's `format` field. Anything else fails with `UnsupportedPublisherError`.
- **Elsevier `META_ABS` is its own error class.** Elsevier silently downgrades unentitled requests to abstract-only payloads. We always pass `view=FULL` and raise `EntitlementDowngradeError` when the response lacks the full-text subtree — abstracts are corpus poison and must never be ingested.
- **`attrs` `@frozen` data models, `cattrs` for (de)serialization.** The `Block` tagged-union in the future normalized `Document` schema needs explicit `cattrs` structuring hooks from day one — markdown round-trips are explicitly rejected because they lose offset precision for inline references.
- **One canonical extraction route per Document.** When multiple extractors could run on the same artifact, one produces the canonical `Document` end-to-end; auxiliary outputs are preserved separately, never silently merged. The lone exception is gap-filling an empty-but-present block, tagged `source_kind="<route>_filled"`.
- **The default PDF backend is MinerU's `vlm-engine`** (`docs/mineru-primary-promotion.md`), promoted over docling for far better formula/table recovery; `docling-standard` is the opt-in text-layer fallback. Consequence: the default path records `geometry_fidelity='approximate'` corpus-wide (VLM-predicted bboxes) and requires the `[mineru]`/`vlm` weights — both accepted, reversible via `DEFAULT_MINERU_ENGINE`. The MinerU extractor also emits an auxiliary `document.md` + `images/` beside the canonical `document.json`; `normalize` still reads `document.json` only. `doctor` treats MinerU (extra + `vlm` weights) as the required primary and docling as the informational alternative.
- **Append-only data model for measurements.** Agents (Ingestor / Auditor / Corrector) propose new records; they never mutate existing ones. The "current best view" is a derived projection over the event log. Any caching/correction logic must respect this.
- **Storage layout (v3).** Three format directories under `<data_dir>/artifacts/{pdf,jats,elsevier}/sha256/<aa>/<sha>.<ext>` with **one-level sharding** (first two hex chars). Manifests at `manifests/sha256/<aa>/<sha>.manifest.json`. The DOI index at `index/by_doi.jsonl` carries a `format` column so a single scan answers "what do we have for this DOI?". `tmp/` is cleared on startup; `os.replace` from tmp to canonical path is non-negotiable.
- **Provenance is non-negotiable.** Every measurement and every manually sideloaded artifact must carry enough provenance to highlight back to the source. Manual sideloads (PDF-only in v3) require a `ManualProvenance` record with operator email, license assertion, source URL, and free-text note — institutional-access PDFs are licensed but not redistributable, and the manifest is the only place that fact lives.
- **Polite-pool HTTP is mandatory** for CrossRef. Every CrossRef request carries a `mailto=` parameter and a `User-Agent: litspectraits/<ver> (+mailto:...)` header sourced from `LITSPECTRAITS_CONTACT_EMAIL`. Publisher SDKs (`wiley-tdm`, `springernature-api-client`, `elsapy`) handle their own auth; we only forward env-derived tokens.
- **Logging is `structlog` with a Rich/JSON switch** (`LITSPECTRAITS_LOG_FORMAT=rich|json`). DOI is bound once per ingest via `contextvars` so every line in that operation carries it. Standard `logging` is routed into structlog so SDK-internal loggers (`httpx`, `requests`, `urllib3`) join the same stream.
- **Console verbosity defaults to `warning`** (`LITSPECTRAITS_LOG_LEVEL`, or the global `--log-level` / `-v` flags per run) — quiet enough to kill the INFO/DEBUG flood a `mineru` extract produces, loud enough to keep backoff/degraded warnings. Two subtleties: (1) display is gated at the **stdlib handler/root**, not the structlog bound logger, so the processor chain always runs and `structlog.testing.capture_logs` works at any level (`_logging.configure_logging`); (2) `mineru` bypasses stdlib logging via **loguru + tqdm**, so `_logging.tame_third_party_logging` disables tqdm and pins third-party logger levels, and `extract.mineru._bridge_mineru_loguru` forwards loguru into the `mineru` stdlib logger **after** `import mineru` (which reconfigures loguru at import).
- **A live spinner (`progress.py`) feeds coarse stage labels** on the slow commands (`ingest`/`sideload`/`smoke`/`extract`/`normalize`/`doctor`) via `report_stage`, an ambient `contextvars`-bound reporter (no signature threading). It runs only on an interactive TTY, outside `--json`, and only when the level is above INFO (so streaming log lines never fight it) — decoupled from log verbosity by design.

## Common commands

The v3 ingest stack is being rebuilt — `uv run` is the only thing that works today. The current CLI surface (rooted in `docs/overview-v3.md` §10, plus the extract / normalize / render steps that have since landed) is:

```
litspectraits [--log-level critical|error|warning|info|debug] [-v|-vv] <command> ...
litspectraits ingest        <doi> [--cache-hit-ok]
litspectraits extract       <doi-or-sha> [--backend mineru|docling-standard] [--mineru-engine vlm-engine|pipeline|hybrid-engine] [--mineru-effort medium|high] [--reextract]
litspectraits normalize     <doi-or-sha> [--renormalize]
litspectraits show-document <doi-or-sha> [--out <path>] [--open]
litspectraits diff-routes   [--doi <doi>...] [--out <path>] [--compare-to <prev-report>]
litspectraits sideload      <doi> <pdf-path> --license <str> [--source-url <url>] [--note <str>]
litspectraits doctor
litspectraits show          <doi>
litspectraits list          [-q | --json]
```

`doctor` runs an egress-IP + per-publisher credentials preflight and is meant to be run before any batch ingest. `sideload` is **PDF-only** in v3. `ingest` **refetches by default** — `--cache-hit-ok` is the opt-in short-circuit when a manifest already exists for the DOI. The `extract → normalize → diff-routes` chain is deliberately split into composable, independently re-runnable steps; `show-document` renders a normalised `Document` to self-contained HTML for human inspection (faithful, single-route, raw-math — see `docs/rendering-mvp-plan.md`). `list` is the discovery counterpart to `show <doi>`: it enumerates every DOI in the local store (newest first) with title / year / publisher / format(s) / extract+normalize status, so the operator never needs the exact DOI in hand. `--quiet/-q` prints bare DOIs one per line for copy/pipe (e.g. `litspectraits list -q | fzf`); `--json` emits the full per-artifact catalog.

When tests exist, run `uv run pytest`. For lint/typecheck: `uv run ruff check`, `uv run ruff format`, `uv run pyright`. There is no `Justfile` yet — `just` is mentioned in the infra doc as a future addition.
