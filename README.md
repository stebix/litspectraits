# litspectraits

A pipeline that ingests MRI literature by DOI and produces structured,
provenance-tracked records of quantitative MR / electromagnetic property values
(T1, T2, T2\*, PD, χ, ADC, …) along with the context needed to interpret them
(field strength, sequence, tissue, scanner, in vivo / ex vivo / phantom, …).

Every record cites back to a specific block — ideally a sentence or table cell —
in a source paper, and citation chains are followed so duplicates collapse to
their primary source.

## Status

Design-stage scaffold. No functional code yet; the substance lives in `docs/`.

- `docs/overview.md` — full pipeline design (DOI → resolver → acquisition →
  extraction → measurement records → agent triad).
- `docs/ingestion-pipeline.md` — detailed design for the first stage to
  implement (resolver + acquisition), including the planned module layout and
  ordered build steps.
- `docs/project-infra-overview.md` — code-style and infra conventions.

## Tooling

- Python 3.14, managed via [uv](https://docs.astral.sh/uv/).
- [Ruff](https://docs.astral.sh/ruff/) for lint + format.
- [Pyright](https://microsoft.github.io/pyright/) for static type checking.
- [pytest](https://docs.pytest.org/) for tests.

```sh
uv sync                 # install dependencies into .venv
uv run python main.py   # run the stub entry point
```

Linting, type checking, and tests will be wired up as the first build steps
land — see the *Order of Work* section of `docs/ingestion-pipeline.md`.

## Planned CLI

```
litspectraits resolve  <doi> [--policy ...] [--json]
litspectraits ingest   <doi> [--policy ...] [--force] [--json]
litspectraits sideload <doi> <path> --version --format --license [--source-url] [--note]
```

## Downstream

A separate package, `tissue-properties`, is the planned consumer of the
records produced by this pipeline.
