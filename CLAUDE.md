# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

This repository is currently a **design-stage scaffold**. The only Python in tree is `main.py` (a stub) and `pyproject.toml` with no dependencies declared yet. The substance lives in `docs/`:

- `docs/overview.md` — full pipeline design (DOI → resolver → acquisition → extraction → measurement records → agent triad). Read this first to understand what is being built.
- `docs/ingestion-pipeline.md` — detailed design for the **first** stage to implement (resolver + acquisition). Includes the planned module layout under `src/litspectraits/`, dependency list, ranking-policy data model, manual-sideload flow, and the ordered build steps.
- `docs/project-infra-overview.md` — code-style and infra conventions (authoritative; see below).

When adding code, follow `docs/ingestion-pipeline.md` rather than improvising structure. The "Order of Work" section at the bottom of that file is the intended implementation sequence.

## What this project is

**litspectraits** is a pipeline that ingests MRI literature by DOI and produces structured, provenance-tracked records of quantitative MR / electromagnetic property values (T1, T2, T2*, PD, χ, ADC, …) along with the context needed to interpret them (field strength, sequence, tissue, scanner, in vivo / ex vivo / phantom, etc.). Outputs are a queryable database where every record cites back to a specific block (ideally sentence or table cell) in a source paper, and citation chains are followed so duplicates collapse to their primary source. A downstream package called `tissue-properties` is a planned consumer.

## Tooling and conventions (from `docs/project-infra-overview.md`)

- **uv-based project**, Python 3.14 (`.python-version`). Always run Python through the local uv-managed `.venv` (e.g. `uv run python …`, `uv run pytest`, `uv sync` to install).
- **Ruff** for both lint and format. **Pyright** for static type checking. Each commit should be green on `pytest`, `ruff check`, and `pyright` per the build plan in `docs/ingestion-pipeline.md`.
- **Line length 99.**
- **Single quotes preferred** for strings; escape inside nested sequences rather than swapping styles.
- **Numpy-style docstrings.**
- **Type hints almost everywhere.**
- **Do not** use `from __future__ import annotations`.
- **Fail loudly.** No excessive exception catching, no silent fallbacks. The "Failure Semantics" section of `docs/ingestion-pipeline.md` is the canonical example: probe failures get logged as `ProbeOutcome.error` and surface in the audit trail; hash mismatches and malformed sideloads raise; missing required env (e.g. `LITSPECTRAITS_CONTACT_EMAIL`) fails at startup.

## Architectural commitments worth knowing before editing

These decisions are spread across the design docs and won't be obvious from any single file once code lands.

- **`attrs` `@frozen` data models, `cattrs` for (de)serialization.** The `Block` tagged-union in the normalized `Document` schema needs explicit `cattrs` structuring hooks from day one — markdown round-trips are explicitly rejected because they lose offset precision for inline references.
- **One canonical extraction route per Document.** When multiple extractors run on the same artifact (e.g. GROBID + Marker on a PDF), one route produces the canonical `Document` end-to-end; other extractors' output is preserved as `AuxiliaryArtifacts`, never silently merged. The lone exception is gap-filling an empty-but-present block, tagged `source_kind="<route>_filled"`.
- **Append-only data model for measurements.** Agents (Ingestor / Auditor / Corrector) propose new records; they never mutate existing ones. The "current best view" is a derived projection over the event log. Any caching/correction logic must respect this.
- **Three-axis ranking, not a flat priority list.** Acquisition routes are tagged on independent axes (`Version` × `Format` × `Access`) and ranked lexicographically by a configurable `RankingPolicy`. The `local` probe surfaces already-acquired artifacts through the same policy machinery — there is no special "manual wins" rule, the version axis carries it.
- **Storage layers stay separated.** Layer 1 (raw artifacts, content-addressed, two-level sha256 prefix sharding under `<data_dir>/artifacts/<format>/sha256/<aa>/<bb>/<hash>.<ext>`) and Layer 2 (parsed `Document` JSON, versioned by extractor + schema version) are on disk; Layer 3 (Postgres index) holds only indexable fields and is rebuildable from Layer 2. Backups target Layer 2, not Postgres. No vector store in v1.
- **Provenance is non-negotiable.** Every block, every measurement, and every manually sideloaded artifact must carry enough provenance to highlight back to the source. Manual sideloads additionally require a `ManualProvenance` record with operator, license assertion, and source URL — institutional-access PDFs are licensed but not redistributable, and the manifest is the only place that fact lives.
- **Polite-pool HTTP is mandatory.** Every CrossRef and Unpaywall request must carry a `mailto=` parameter and a `User-Agent: litspectraits/<ver> (+mailto:...)` header sourced from `LITSPECTRAITS_CONTACT_EMAIL`. That env var is required at startup.
- **Logging is `structlog` with a Rich/JSON switch** (`LITSPECTRAITS_LOG_FORMAT=rich|json`). DOI is bound once per resolve via `contextvars` so every line in that resolve carries it. Standard `logging` is routed into structlog so `httpx`/`urllib3` join the same stream.

## Common commands

The project does not yet have its dependencies, CLI, or test suite implemented — there are no commands beyond standard `uv` workflow until the first build step lands. Per `docs/ingestion-pipeline.md`, the planned CLI surface (under a `typer` app) is:

```
litspectraits resolve  <doi> [--policy ...] [--json]
litspectraits ingest   <doi> [--policy ...] [--force] [--json]
litspectraits sideload <doi> <path> --version --format --license [--source-url] [--note]
```

When tests exist, run them via `uv run pytest`. When linting/typechecking exist, run `uv run ruff check`, `uv run ruff format`, and `uv run pyright`. There is no `Justfile` yet — `just` is mentioned in the infra doc as a future addition for complex task running.
